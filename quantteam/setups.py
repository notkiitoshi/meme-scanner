"""Indicators, regime labels and the setup catalog.

Every signal is evaluated on a CLOSED bar using only data up to and including
that bar. Entries happen at the NEXT bar's open (backtest) or at the live
price after the bar closes (paper bot). Lab verifies this with a truncation
(lookahead) test.
"""
from __future__ import annotations

import copy

import numpy as np
import pandas as pd


# ---------------------------------------------------------------- indicators
def ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def atr(df, n=14):
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def adx(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    up = h.diff()
    dn = -l.diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    a = atr(df, n)
    pdi = 100 * pd.Series(pdm, index=df.index).ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / a
    ndi = 100 * pd.Series(ndm, index=df.index).ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / a
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def regime_series(df: pd.DataFrame, win: int = 500) -> pd.Series:
    """Causal regime label per bar: volatile / trending / ranging."""
    a = atr(df) / df["close"]
    q = a.rolling(win, min_periods=100).quantile(0.85)
    ad = adx(df)
    reg = pd.Series("ranging", index=df.index, dtype=object)
    reg[ad > 25] = "trending"
    reg[a > q] = "volatile"
    reg[ad.isna()] = "unknown"
    return reg


def features(df: pd.DataFrame, ctx: dict) -> pd.DataFrame:
    """Precompute indicators used by the catalog (cached per coin/tf)."""
    f = pd.DataFrame(index=df.index)
    c = df["close"]
    f["atr"] = atr(df)
    for n in (20, 50, 200):
        f[f"ema{n}"] = ema(c, n)
    f["rsi"] = rsi(c)
    f["adx"] = adx(df)
    f["vol_avg"] = df["vol_quote"].rolling(48, min_periods=24).mean().shift(1)
    f["regime"] = regime_series(df)
    bb_mid = c.rolling(20).mean()
    bb_sd = c.rolling(20).std()
    f["bb_up"] = bb_mid + 2 * bb_sd
    f["bb_lo"] = bb_mid - 2 * bb_sd
    f["bb_w"] = (4 * bb_sd) / bb_mid
    btc = ctx.get("btc_close")
    if btc is not None:
        f["btc"] = btc.reindex(df.index).ffill()
        f["btc_ema200"] = ema(f["btc"], 200)
    fund = ctx.get("funding")
    if fund is not None and len(fund):
        # funding known once settled: as-of join, last settled rate at/before bar close
        bar_close = df.index + ctx["bar_delta"]
        idx = fund.index.searchsorted(bar_close, side="right") - 1
        vals = np.where(idx >= 0, fund.values[np.clip(idx, 0, None)], np.nan)
        f["funding"] = vals
    else:
        f["funding"] = np.nan
    oi = ctx.get("oi")
    if oi is not None and len(oi):
        bar_close = df.index + ctx["bar_delta"]
        idx = oi.index.searchsorted(bar_close, side="right") - 1
        vals = np.where(idx >= 0, oi.values[np.clip(idx, 0, None)], np.nan)
        # only trust OI values within 2h of the bar close
        ts_ok = np.where(idx >= 0, (bar_close - oi.index[np.clip(idx, 0, None)]) <= pd.Timedelta(hours=2), False)
        f["oi"] = np.where(ts_ok, vals, np.nan)
    else:
        f["oi"] = np.nan
    return f


# ---------------------------------------------------------------- signal families
# Each returns a Series of +1 (long), -1 (short), 0 evaluated at bar close.

def _bars(p, tfh, key):
    """Convert an hours parameter into bars for this timeframe (min 2)."""
    return max(2, int(round(p[key] / tfh)))


def sig_range_breakout(df, f, p, tfh):
    n = _bars(p, tfh, "lookback_h")
    hi = df["high"].rolling(n).max().shift(1)
    lo = df["low"].rolling(n).min().shift(1)
    width = (hi - lo) / df["close"]
    tight = width < p["max_range_pct"] / 100
    vol_ok = df["vol_quote"] > p["vol_mult"] * f["vol_avg"]
    s = pd.Series(0, index=df.index)
    if p["dir"] >= 0:
        s[(df["close"] > hi) & tight & vol_ok] = 1
    if p["dir"] <= 0:
        s[(df["close"] < lo) & tight & vol_ok] = -1
    return s


def sig_donchian_trend(df, f, p, tfh):
    n = _bars(p, tfh, "lookback_h")
    hi = df["high"].rolling(n).max().shift(1)
    lo = df["low"].rolling(n).min().shift(1)
    s = pd.Series(0, index=df.index)
    if p["dir"] > 0:
        s[(df["close"] > hi) & (f["ema50"] > f["ema200"])] = 1
    else:
        s[(df["close"] < lo) & (f["ema50"] < f["ema200"])] = -1
    return s


def sig_trend_pullback(df, f, p, tfh):
    e = f[f"ema{p['ema']}"]
    s = pd.Series(0, index=df.index)
    if p["dir"] > 0:
        up = (f["ema20"] > f["ema50"]) & (f["ema50"] > f["ema200"])
        touch = df["low"] <= e * (1 + p["touch_pct"] / 100)
        s[up & touch & (df["close"] > e) & (df["close"] > df["open"]) & (f["rsi"] > p["rsi_floor"])] = 1
    else:
        dn = (f["ema20"] < f["ema50"]) & (f["ema50"] < f["ema200"])
        touch = df["high"] >= e * (1 - p["touch_pct"] / 100)
        s[dn & touch & (df["close"] < e) & (df["close"] < df["open"]) & (f["rsi"] < 100 - p["rsi_floor"])] = -1
    return s


def sig_flush_reversal(df, f, p, tfh):
    """Liquidation-like flush: huge range bar on huge volume, then fade it."""
    rng = df["high"] - df["low"]
    a = f["atr"].shift(1)
    big = (rng > p["range_atr"] * a) & (df["vol_quote"] > p["vol_mult"] * f["vol_avg"])
    s = pd.Series(0, index=df.index)
    if p["dir"] > 0:
        wick = (df["close"] - df["low"]) / rng.replace(0, np.nan)
        s[big & (df["close"] < df["open"]) & (wick > p["min_wick"])] = 1
    else:
        wick = (df["high"] - df["close"]) / rng.replace(0, np.nan)
        s[big & (df["close"] > df["open"]) & (wick > p["min_wick"])] = -1
    return s


def sig_funding_extreme(df, f, p, tfh):
    fr = f["funding"]
    n = _bars(p, tfh, "mom_h")
    mom = df["close"] / df["close"].shift(n) - 1
    s = pd.Series(0, index=df.index)
    if p["dir"] < 0:
        s[(fr > p["fund_thr"] / 100) & (mom > p["mom_pct"] / 100) & (df["close"] < df["open"])] = -1
    else:
        s[(fr < -p["fund_thr"] / 100) & (mom < -p["mom_pct"] / 100) & (df["close"] > df["open"])] = 1
    return s


def sig_oi_divergence(df, f, p, tfh):
    n = _bars(p, tfh, "win_h")
    pc = df["close"] / df["close"].shift(n) - 1
    oc = f["oi"] / f["oi"].shift(n) - 1
    s = pd.Series(0, index=df.index)
    if p["mode"] == "weak_rally_short":      # price up, OI down => short covering rally
        s[(pc > p["px_pct"] / 100) & (oc < -p["oi_pct"] / 100)] = -1
    elif p["mode"] == "weak_dump_long":      # price down, OI down => long liquidation done
        s[(pc < -p["px_pct"] / 100) & (oc < -p["oi_pct"] / 100) & (df["close"] > df["open"])] = 1
    elif p["mode"] == "oi_breakout_long":    # breakout with fresh OI
        hi = df["high"].rolling(n).max().shift(1)
        s[(df["close"] > hi) & (oc > p["oi_pct"] / 100)] = 1
    return s


def sig_session_open(df, f, p, tfh):
    h = df.index.hour
    target = p["hour_utc"] - (p["hour_utc"] % tfh)
    first = (h == target)
    body = (df["close"] - df["open"]) / f["atr"].shift(1)
    vol_ok = df["vol_quote"] > p["vol_mult"] * f["vol_avg"]
    s = pd.Series(0, index=df.index)
    s[first & vol_ok & (body > p["body_atr"])] = 1 if p["dir"] >= 0 else 0
    if p["dir"] <= 0:
        s[first & vol_ok & (body < -p["body_atr"])] = -1
    return s


def sig_alt_lag_btc(df, f, p, tfh):
    if "btc" not in f:
        return pd.Series(0, index=df.index)
    n = _bars(p, tfh, "win_h")
    br = f["btc"] / f["btc"].shift(n) - 1
    cr = df["close"] / df["close"].shift(n) - 1
    s = pd.Series(0, index=df.index)
    if p["dir"] > 0:
        s[(br > p["btc_pct"] / 100) & (cr < p["lag_pct"] / 100) & (f["btc"] > f["btc_ema200"])] = 1
    else:
        s[(br < -p["btc_pct"] / 100) & (cr > -p["lag_pct"] / 100) & (f["btc"] < f["btc_ema200"])] = -1
    return s


def sig_rsi_meanrev(df, f, p, tfh):
    s = pd.Series(0, index=df.index)
    flat = f["adx"] < p["adx_max"]
    r = f["rsi"]
    if p["dir"] > 0:   # RSI was oversold last bar and this bar closes green
        s[flat & (r.shift(1) < p["rsi_lvl"]) & (df["close"] > df["open"])] = 1
    else:
        s[flat & (r.shift(1) > 100 - p["rsi_lvl"]) & (df["close"] < df["open"])] = -1
    return s


def sig_bb_squeeze(df, f, p, tfh):
    n = _bars(p, tfh, "squeeze_h")
    sq = f["bb_w"].shift(1) <= f["bb_w"].rolling(n).min().shift(1) * (1 + p["tol"])
    s = pd.Series(0, index=df.index)
    if p["dir"] > 0:
        s[sq & (df["close"] > f["bb_up"])] = 1
    else:
        s[sq & (df["close"] < f["bb_lo"])] = -1
    return s


def sig_ema_cross(df, f, p, tfh):
    fast, slow = ema(df["close"], p["fast"]), ema(df["close"], p["slow"])
    up = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    dn = (fast < slow) & (fast.shift(1) >= slow.shift(1))
    s = pd.Series(0, index=df.index)
    if "btc" in f:
        if p["dir"] > 0:
            s[up & (f["btc"] > f["btc_ema200"])] = 1
        else:
            s[dn & (f["btc"] < f["btc_ema200"])] = -1
    return s


def sig_pump_fade(df, f, p, tfh):
    n = _bars(p, tfh, "win_h")
    r = df["close"] / df["close"].shift(n) - 1
    s = pd.Series(0, index=df.index)
    if p["dir"] < 0:
        s[(r > p["move_pct"] / 100) & (df["close"] < df["open"]) & (f["rsi"] > p["rsi_min"])] = -1
    else:
        s[(r < -p["move_pct"] / 100) & (df["close"] > df["open"]) & (f["rsi"] < 100 - p["rsi_min"])] = 1
    return s


def sig_contraction_continuation(df, f, p, tfh):
    n = _bars(p, tfh, "lookback_h")
    a_pct = f["atr"] / df["close"]
    contracted = a_pct < a_pct.rolling(n * 4).median() * p["contract"]
    hi = df["high"].rolling(n).max().shift(1)
    lo = df["low"].rolling(n).min().shift(1)
    s = pd.Series(0, index=df.index)
    if p["dir"] > 0:
        s[contracted.shift(1).fillna(False).astype(bool) & (df["close"] > hi) & (f["ema50"] > f["ema200"])] = 1
    else:
        s[contracted.shift(1).fillna(False).astype(bool) & (df["close"] < lo) & (f["ema50"] < f["ema200"])] = -1
    return s


def sig_btc_gated_breakdown(df, f, p, tfh):
    n = _bars(p, tfh, "lookback_h")
    lo = df["low"].rolling(n).min().shift(1)
    s = pd.Series(0, index=df.index)
    if "btc" in f:
        s[(df["close"] < lo) & (f["btc"] < f["btc_ema200"]) & (df["vol_quote"] > p["vol_mult"] * f["vol_avg"])] = -1
    return s


FAMILIES = {
    "range_breakout": sig_range_breakout,
    "donchian_trend": sig_donchian_trend,
    "trend_pullback": sig_trend_pullback,
    "flush_reversal": sig_flush_reversal,
    "funding_extreme": sig_funding_extreme,
    "oi_divergence": sig_oi_divergence,
    "session_open": sig_session_open,
    "alt_lag_btc": sig_alt_lag_btc,
    "rsi_meanrev": sig_rsi_meanrev,
    "bb_squeeze": sig_bb_squeeze,
    "ema_cross": sig_ema_cross,
    "pump_fade": sig_pump_fade,
    "contraction_continuation": sig_contraction_continuation,
    "btc_gated_breakdown": sig_btc_gated_breakdown,
}


def signals(setup: dict, df: pd.DataFrame, f: pd.DataFrame, tf_hours: int) -> pd.Series:
    s = FAMILIES[setup["family"]](df, f, setup["params"], tf_hours).fillna(0).astype(int)
    regs = setup.get("regimes")
    if regs:
        s[~f["regime"].isin(regs)] = 0
    return s


# ---------------------------------------------------------------- catalog
# exit block: stop = stop_atr * ATR(14) at signal bar; take-profit = tp_r * stop
# distance; time exit after max_hold_h hours. Tunable params get +/-25% in Lab.
def _S(id_, family, name, reason, regimes, params, stop_atr=1.5, tp_r=2.0, max_hold_h=48, tunable=None):
    return {
        "id": id_, "family": family, "name": name, "reason": reason,
        "regimes": regimes, "params": params,
        "exit": {"stop_atr": stop_atr, "tp_r": tp_r, "max_hold_h": max_hold_h},
        "tunable": tunable or [],
    }


CATALOG = [
    _S("RB_L", "range_breakout", "Tight-range breakout long on rising volume",
       "A tight 24h range broken on 1.5x volume often starts a leg as trapped shorts cover.",
       ["ranging", "trending"], {"dir": 1, "lookback_h": 24, "max_range_pct": 8, "vol_mult": 1.5},
       tunable=["lookback_h", "vol_mult"]),
    _S("RB_S", "range_breakout", "Tight-range breakdown short on rising volume",
       "Losing a tight range on heavy volume forces late longs out, extending the drop.",
       ["ranging", "trending"], {"dir": -1, "lookback_h": 24, "max_range_pct": 8, "vol_mult": 1.5},
       tunable=["lookback_h", "vol_mult"]),
    _S("RB_L_WIDE", "range_breakout", "72h range breakout long on 2x volume",
       "A multi-day base broken on double volume shows new buyers overpowering sellers.",
       ["ranging", "trending"], {"dir": 1, "lookback_h": 72, "max_range_pct": 15, "vol_mult": 2.0},
       stop_atr=2.0, tp_r=2.5, max_hold_h=96, tunable=["lookback_h", "vol_mult"]),
    _S("DC_L", "donchian_trend", "55-bar-equivalent (220h) channel breakout long in uptrend",
       "Classic trend following: new multi-week highs in an uptrend tend to persist in memes.",
       ["trending"], {"dir": 1, "lookback_h": 220}, stop_atr=2.5, tp_r=3.0, max_hold_h=240,
       tunable=["lookback_h"]),
    _S("DC_S", "donchian_trend", "220h channel breakdown short in downtrend",
       "Meme downtrends grind as holders capitulate; new multi-week lows tend to continue.",
       ["trending"], {"dir": -1, "lookback_h": 220}, stop_atr=2.5, tp_r=3.0, max_hold_h=240,
       tunable=["lookback_h"]),
    _S("TP_L", "trend_pullback", "Uptrend pullback to EMA50 long",
       "In a stacked uptrend, dips to the 50 EMA are where trend buyers reload.",
       ["trending"], {"dir": 1, "ema": 50, "touch_pct": 0.5, "rsi_floor": 40},
       stop_atr=1.5, tp_r=2.0, max_hold_h=72, tunable=["touch_pct", "rsi_floor"]),
    _S("TP_S", "trend_pullback", "Downtrend rally to EMA50 short",
       "In a stacked downtrend, bounces into the 50 EMA are where trapped longs sell.",
       ["trending"], {"dir": -1, "ema": 50, "touch_pct": 0.5, "rsi_floor": 40},
       stop_atr=1.5, tp_r=2.0, max_hold_h=72, tunable=["touch_pct", "rsi_floor"]),
    _S("TP20_L", "trend_pullback", "Uptrend pullback to EMA20 long (shallow dip)",
       "Strong meme trends rarely reach the 50 EMA; the 20 EMA is the first reload zone.",
       ["trending"], {"dir": 1, "ema": 20, "touch_pct": 0.3, "rsi_floor": 45},
       stop_atr=1.2, tp_r=2.0, max_hold_h=48, tunable=["touch_pct", "rsi_floor"]),
    _S("FL_L", "flush_reversal", "Long after a liquidation-like down flush",
       "Forced long liquidations overshoot fair value; the wick shows buyers absorbing it.",
       ["volatile", "ranging", "trending"], {"dir": 1, "range_atr": 3.0, "vol_mult": 3.0, "min_wick": 0.35},
       stop_atr=1.5, tp_r=1.5, max_hold_h=24, tunable=["range_atr", "vol_mult"]),
    _S("FL_S", "flush_reversal", "Short after a short-squeeze spike up",
       "Short squeezes overshoot; a long upper wick on huge volume shows sellers returning.",
       ["volatile", "ranging", "trending"], {"dir": -1, "range_atr": 3.0, "vol_mult": 3.0, "min_wick": 0.35},
       stop_atr=1.5, tp_r=1.5, max_hold_h=24, tunable=["range_atr", "vol_mult"]),
    _S("FUND_S", "funding_extreme", "Short when funding is extreme positive after a run-up",
       "Very high funding means crowded leveraged longs paying to stay in; they unwind fast.",
       ["trending", "volatile", "ranging"], {"dir": -1, "fund_thr": 0.02, "mom_h": 24, "mom_pct": 5},
       tunable=["fund_thr", "mom_pct"]),
    _S("FUND_L", "funding_extreme", "Long when funding is extreme negative after a dump",
       "Deeply negative funding means crowded shorts; a green bar can start a squeeze.",
       ["trending", "volatile", "ranging"], {"dir": 1, "fund_thr": 0.02, "mom_h": 24, "mom_pct": 5},
       tunable=["fund_thr", "mom_pct"]),
    _S("OI_RALLY_S", "oi_divergence", "Short a rally that comes with falling open interest",
       "Price up while OI falls is short covering, not new buying, so the rally lacks fuel.",
       ["ranging", "trending", "volatile"], {"mode": "weak_rally_short", "win_h": 24, "px_pct": 6, "oi_pct": 5},
       tunable=["px_pct", "oi_pct"]),
    _S("OI_DUMP_L", "oi_divergence", "Long after a dump that flushed open interest",
       "Price down with OI collapsing means longs got liquidated; selling pressure is spent.",
       ["volatile", "ranging"], {"mode": "weak_dump_long", "win_h": 24, "px_pct": 8, "oi_pct": 8},
       tunable=["px_pct", "oi_pct"]),
    _S("OI_BO_L", "oi_divergence", "Breakout long confirmed by rising open interest",
       "A breakout with fresh OI shows new positions entering, not just shorts covering.",
       ["trending", "ranging"], {"mode": "oi_breakout_long", "win_h": 24, "px_pct": 0, "oi_pct": 5},
       tunable=["oi_pct"]),
    _S("SESS_ASIA", "session_open", "Follow a strong Asia-open (00 UTC) bar",
       "Asia open brings a wave of fresh flow; a strong first bar on volume often follows through.",
       ["trending", "volatile", "ranging"], {"dir": 0, "hour_utc": 0, "body_atr": 0.8, "vol_mult": 1.3},
       stop_atr=1.2, tp_r=1.5, max_hold_h=8, tunable=["body_atr", "vol_mult"]),
    _S("SESS_LDN", "session_open", "Follow a strong London-open (08 UTC) bar",
       "London open adds European flow; a decisive first bar tends to set the day's direction.",
       ["trending", "volatile", "ranging"], {"dir": 0, "hour_utc": 8, "body_atr": 0.8, "vol_mult": 1.3},
       stop_atr=1.2, tp_r=1.5, max_hold_h=8, tunable=["body_atr", "vol_mult"]),
    _S("SESS_US", "session_open", "Follow a strong US-open (13-14 UTC) bar",
       "US equity open is the heaviest flow window; a strong first bar often continues.",
       ["trending", "volatile", "ranging"], {"dir": 0, "hour_utc": 13, "body_atr": 0.8, "vol_mult": 1.3},
       stop_atr=1.2, tp_r=1.5, max_hold_h=8, tunable=["body_atr", "vol_mult"]),
    _S("LAG_L", "alt_lag_btc", "Long memes lagging a BTC rally",
       "When BTC rallies in a bull regime, risk appetite spills into memes with a delay.",
       ["ranging", "trending"], {"dir": 1, "win_h": 24, "btc_pct": 3, "lag_pct": 0.5},
       stop_atr=1.5, tp_r=2.0, max_hold_h=48, tunable=["btc_pct", "win_h"]),
    _S("LAG_S", "alt_lag_btc", "Short memes that haven't dropped yet as BTC falls",
       "When BTC falls in a bear regime, memes usually follow down harder with a delay.",
       ["ranging", "trending"], {"dir": -1, "win_h": 24, "btc_pct": 3, "lag_pct": 0.5},
       stop_atr=1.5, tp_r=2.0, max_hold_h=48, tunable=["btc_pct", "win_h"]),
    _S("MR_L", "rsi_meanrev", "Oversold bounce long in a flat market",
       "Without a trend (low ADX), oversold readings tend to snap back to the mean.",
       ["ranging"], {"dir": 1, "rsi_lvl": 30, "adx_max": 25}, stop_atr=1.5, tp_r=1.5, max_hold_h=24,
       tunable=["rsi_lvl", "adx_max"]),
    _S("MR_S", "rsi_meanrev", "Overbought fade short in a flat market",
       "Without a trend, overbought pops in memes get sold back into the range.",
       ["ranging"], {"dir": -1, "rsi_lvl": 30, "adx_max": 25}, stop_atr=1.5, tp_r=1.5, max_hold_h=24,
       tunable=["rsi_lvl", "adx_max"]),
    _S("BBSQ_L", "bb_squeeze", "Bollinger squeeze breakout long",
       "Volatility compresses before expansion; the first close outside the band picks a side.",
       ["ranging", "trending"], {"dir": 1, "squeeze_h": 120, "tol": 0.1}, stop_atr=1.5, tp_r=2.5,
       max_hold_h=72, tunable=["squeeze_h", "tol"]),
    _S("BBSQ_S", "bb_squeeze", "Bollinger squeeze breakdown short",
       "Same volatility-expansion logic, taking the downside break.",
       ["ranging", "trending"], {"dir": -1, "squeeze_h": 120, "tol": 0.1}, stop_atr=1.5, tp_r=2.5,
       max_hold_h=72, tunable=["squeeze_h", "tol"]),
    _S("EMAX_L", "ema_cross", "EMA 20/50 golden cross long, only when BTC is above its 200 EMA",
       "Trend changes in memes work best when the whole market is risk-on.",
       ["trending", "ranging"], {"dir": 1, "fast": 20, "slow": 50}, stop_atr=2.0, tp_r=3.0,
       max_hold_h=168, tunable=["fast", "slow"]),
    _S("EMAX_S", "ema_cross", "EMA 20/50 death cross short, only when BTC is below its 200 EMA",
       "Meme downtrends accelerate when BTC is also in a bear regime.",
       ["trending", "ranging"], {"dir": -1, "fast": 20, "slow": 50}, stop_atr=2.0, tp_r=3.0,
       max_hold_h=168, tunable=["fast", "slow"]),
    _S("PUMP_FADE", "pump_fade", "Fade a +30%/24h pump on the first red bar",
       "Meme pumps are driven by late FOMO buyers who become forced sellers once momentum stalls.",
       ["volatile", "trending"], {"dir": -1, "win_h": 24, "move_pct": 30, "rsi_min": 65},
       stop_atr=2.0, tp_r=2.0, max_hold_h=48, tunable=["move_pct", "rsi_min"]),
    _S("DUMP_BOUNCE", "pump_fade", "Buy the first green bar after a -25%/24h dump",
       "Panic dumps overshoot as stops cascade; the first green bar shows sellers exhausted.",
       ["volatile", "trending"], {"dir": 1, "win_h": 24, "move_pct": 25, "rsi_min": 65},
       stop_atr=2.0, tp_r=1.5, max_hold_h=24, tunable=["move_pct", "rsi_min"]),
    _S("CONT_L", "contraction_continuation", "Uptrend volatility-contraction breakout long",
       "A quiet pause inside an uptrend that breaks higher is the trend resuming.",
       ["trending", "ranging"], {"dir": 1, "lookback_h": 24, "contract": 0.8}, stop_atr=1.5, tp_r=2.5,
       max_hold_h=72, tunable=["lookback_h", "contract"]),
    _S("CONT_S", "contraction_continuation", "Downtrend volatility-contraction breakdown short",
       "A quiet pause inside a downtrend that breaks lower is the trend resuming.",
       ["trending", "ranging"], {"dir": -1, "lookback_h": 24, "contract": 0.8}, stop_atr=1.5, tp_r=2.5,
       max_hold_h=72, tunable=["lookback_h", "contract"]),
    _S("BTCG_S", "btc_gated_breakdown", "48h-low breakdown short only while BTC is below its 200 EMA",
       "In a BTC bear regime, buyers of memes disappear and fresh lows tend to keep going.",
       ["trending", "volatile", "ranging"], {"lookback_h": 48, "vol_mult": 1.2}, stop_atr=2.0, tp_r=2.0,
       max_hold_h=72, tunable=["lookback_h", "vol_mult"]),
]


def neighborhood(setup: dict) -> list[dict]:
    """Base setup plus +/-25% on each tunable parameter, and +/-25% on the stop."""
    out = [copy.deepcopy(setup)]
    for k in setup.get("tunable", []):
        for m in (0.75, 1.25):
            v = copy.deepcopy(setup)
            val = v["params"][k] * m
            if isinstance(setup["params"][k], int) and k not in ("dir",):
                val = int(round(val))
            v["params"][k] = val
            v["variant"] = f"{k}x{m}"
            out.append(v)
    for m in (0.75, 1.25):
        v = copy.deepcopy(setup)
        v["exit"]["stop_atr"] = round(setup["exit"]["stop_atr"] * m, 3)
        v["variant"] = f"stop_atrx{m}"
        out.append(v)
    out[0]["variant"] = "base"
    return out
