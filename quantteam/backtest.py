"""Backtest engine shared by Lab (and Weekly). Costs: 0.1% fee per side,
0.05% slippage per side, funding paid/received at each funding settlement
(actual OKX history where available, otherwise an ESTIMATE = the coin's median
observed funding rate at its observed interval). Entries at next bar open."""
from __future__ import annotations

import numpy as np
import pandas as pd

from common import ns, FEE, MAX_NOTIONAL_X, MAX_OPEN, RISK_FRAC, SLIP


def funding_timeline(fund: pd.Series, start: pd.Timestamp, end: pd.Timestamp, interval_h: float = 8.0):
    """Return (event_ns, rate, first_actual_ns). Synthetic (estimated) events
    fill the period before OKX's funding history starts."""
    if fund is None or len(fund) == 0:
        med, first = 0.0001, end + pd.Timedelta(days=1)   # OKX baseline 0.01%/8h, ESTIMATE
        actual = pd.Series(dtype=float)
    else:
        med = float(fund.median())
        first = fund.index.min()
        actual = fund
    syn_idx = pd.date_range(start.floor("8h"), first - pd.Timedelta(minutes=1), freq=f"{int(interval_h)}h")
    syn = pd.Series(med, index=syn_idx)
    tl = pd.concat([syn, actual]).sort_index()
    return np.asarray(ns(tl.index)), tl.values.astype(float), int(pd.Timestamp(first).value)


def run_coin(setup: dict, df: pd.DataFrame, f: pd.DataFrame, sig: pd.Series, tf_hours: int,
             fund_tl, coin: str) -> list[dict]:
    ex = setup["exit"]
    hold = max(1, int(round(ex["max_hold_h"] / tf_hours)))
    o, h, l, c = (df[k].values for k in ("open", "high", "low", "close"))
    a = f["atr"].values
    s = sig.values
    ts = ns(df.index)
    bar_ns = tf_hours * 3_600_000_000_000
    ev_ns, ev_rate, first_actual = fund_tl
    cs = np.concatenate([[0.0], np.cumsum(ev_rate)])
    n = len(df)
    trades = []
    free = 0
    for i in np.flatnonzero(s):
        if i < free or i + 1 >= n:
            continue
        d = int(s[i])
        ai = a[i]
        if not np.isfinite(ai) or ai <= 0:
            continue
        e_raw = o[i + 1]
        dist = ex["stop_atr"] * ai
        stop = e_raw - d * dist
        tp = e_raw + d * ex["tp_r"] * dist
        entry = e_raw * (1 + d * SLIP)
        stop_pct = abs(entry - stop) / entry
        if stop_pct < 0.002 or stop_pct > 0.35 or stop <= 0:
            continue
        end = min(i + 1 + hold, n)
        lo, hi = l[i + 1:end], h[i + 1:end]
        if d > 0:
            hs, ht = np.flatnonzero(lo <= stop), np.flatnonzero(hi >= tp)
        else:
            hs, ht = np.flatnonzero(hi >= stop), np.flatnonzero(lo <= tp)
        js = hs[0] if len(hs) else 10**9
        jt = ht[0] if len(ht) else 10**9
        if js <= jt and js < 10**9:          # stop first (same bar => assume stop: conservative)
            j = i + 1 + js
            px = o[j] if (d > 0 and o[j] < stop) or (d < 0 and o[j] > stop) else stop
            why = "stop"
        elif jt < 10**9:
            j = i + 1 + jt
            px = tp
            why = "take_profit"
        else:
            j = end - 1
            px = c[j]
            why = "time" if end - (i + 1) >= hold else "data_end"
        exit_px = px * (1 - d * SLIP)
        t_in = ts[i + 1]
        t_out = ts[j] + bar_ns
        k0, k1 = np.searchsorted(ev_ns, t_in, "right"), np.searchsorted(ev_ns, t_out, "right")
        fsum = cs[k1] - cs[k0]
        gross = d * (exit_px / entry - 1)
        fees = FEE * (1 + exit_px / entry)
        fund_cost = d * fsum
        net = gross - fees - fund_cost
        lev = min(RISK_FRAC / stop_pct, MAX_NOTIONAL_X)
        trades.append({
            "coin": coin, "dir": d, "entry_ns": int(t_in), "exit_ns": int(t_out),
            "entry": float(entry), "exit": float(exit_px), "stop_pct": float(stop_pct),
            "gross": float(gross), "fees": float(fees), "funding": float(fund_cost), "net": float(net),
            "R": float(net / stop_pct), "lev": float(lev), "eq_ret": float(lev * net),
            "why": why, "bars": int(j - i), "funding_est": bool(t_in < first_actual),
        })
        free = j + 1 if why != "data_end" else n
    return trades


def portfolio(trades: pd.DataFrame, extra_cost: float = 0.0) -> dict:
    """Combine trades across coins: max 3 concurrent, 1% risk each, compounding.
    extra_cost is subtracted from each trade's net return (e.g. double fee test)."""
    if trades is None or len(trades) == 0:
        return {"trades": 0, "ret_pct": 0.0, "max_dd_pct": 0.0, "win_rate": None, "avg_R": None,
                "longest_losing_streak": 0, "profit_factor": None, "worst_month": None, "eq_rets": []}
    t = trades.sort_values("entry_ns")
    keep = []
    open_ex = []
    for idx, en, xn in zip(t.index, t["entry_ns"].values, t["exit_ns"].values):
        open_ex = [x for x in open_ex if x > en]
        if len(open_ex) >= MAX_OPEN:
            continue
        open_ex.append(xn)
        keep.append(idx)
    t = t.loc[keep].sort_values("exit_ns")
    net = t["net"].values - extra_cost
    eq_rets = t["lev"].values * net
    eq = np.cumprod(1 + eq_rets)
    peak = np.maximum.accumulate(np.concatenate([[1.0], eq]))[1:]
    dd = (eq / peak - 1).min() if len(eq) else 0
    losses = net <= 0
    streak = best = 0
    for x in losses:
        streak = streak + 1 if x else 0
        best = max(best, streak)
    months = pd.to_datetime(t["exit_ns"].values, utc=True).strftime("%Y-%m")
    mret = pd.Series(eq_rets).groupby(months).apply(lambda r: np.prod(1 + r) - 1)
    gains, loss_sum = eq_rets[eq_rets > 0].sum(), -eq_rets[eq_rets < 0].sum()
    return {
        "trades": int(len(t)), "skipped_for_max_open": int(len(trades) - len(t)),
        "ret_pct": round(float((eq[-1] - 1) * 100), 2) if len(eq) else 0.0,
        "max_dd_pct": round(float(dd * 100), 2),
        "win_rate": round(float((net > 0).mean()), 3),
        "avg_R": round(float((net / t["stop_pct"].values).mean()), 3),
        "longest_losing_streak": int(best),
        "profit_factor": round(float(gains / loss_sum), 2) if loss_sum > 0 else None,
        "worst_month": {"month": mret.idxmin(), "ret_pct": round(float(mret.min() * 100), 2)} if len(mret) else None,
        "best_month": {"month": mret.idxmax(), "ret_pct": round(float(mret.max() * 100), 2)} if len(mret) else None,
        "eq_rets": eq_rets.tolist(),
        "funding_estimated_share": round(float(t["funding_est"].mean()), 3),
        "coins_traded": int(t["coin"].nunique()),
        "top_coin_share": round(float(t["coin"].value_counts(normalize=True).iloc[0]), 3),
    }


def monte_carlo(eq_rets, n: int = 1000, seed: int = 7) -> dict:
    r = np.asarray(eq_rets, float)
    if len(r) < 5:
        return {"runs": 0, "note": "too few trades"}
    rng = np.random.default_rng(seed)
    dds, boots = [], []
    for _ in range(n):
        p = rng.permutation(r)
        eq = np.cumprod(1 + p)
        pk = np.maximum.accumulate(np.concatenate([[1.0], eq]))[1:]
        dds.append((eq / pk - 1).min())
        b = rng.choice(r, size=len(r), replace=True)
        boots.append(np.prod(1 + b) - 1)
    dds, boots = np.array(dds) * 100, np.array(boots) * 100
    return {
        "runs": n,
        "shuffle_max_dd_pct_median": round(float(np.median(dds)), 2),
        "shuffle_max_dd_pct_worst5": round(float(np.percentile(dds, 5)), 2),
        "bootstrap_ret_pct_p5": round(float(np.percentile(boots, 5)), 2),
        "bootstrap_ret_pct_median": round(float(np.median(boots)), 2),
        "bootstrap_prob_loss": round(float((boots <= 0).mean()), 3),
    }


def buy_hold(df: pd.DataFrame, t0, t1) -> float | None:
    w = df[(df.index >= t0) & (df.index < t1)]
    if len(w) < 2:
        return None
    return float(w["close"].iloc[-1] / w["open"].iloc[0] - 1 - 2 * FEE)
