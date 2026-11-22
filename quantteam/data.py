"""Incremental public-data cache: OKX candles (1H, resampled to 4H), OKX
funding history, OKX open-interest history, CoinGecko cross-check.
No keys, no account endpoints."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

from common import (ns, ALL_COINS, COINGECKO_IDS, DATA, FetchError, atomic_write_json,
                    coingecko_get, get_logger, inst, okx_get, read_json)

log = get_logger("data")
CANDLE_DIR = DATA / "candles"
FUND_DIR = DATA / "funding"
OI_DIR = DATA / "oi"
CG_DIR = DATA / "coingecko"
COVERAGE = DATA / "coverage.json"
MAX_YEARS = 4
COLS = ["ts", "open", "high", "low", "close", "vol_quote"]


# ---------------------------------------------------------------- candles
def _parse_candles(rows):
    out = []
    for r in rows:
        if len(r) >= 9 and r[8] == "0":   # unconfirmed (still forming) bar
            continue
        out.append((int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[7])))
    return out


def load_candles(coin: str, tf: str = "1h") -> pd.DataFrame:
    p = CANDLE_DIR / f"{coin}_1H.csv.gz"
    if not p.exists():
        return pd.DataFrame(columns=COLS[1:])
    df = pd.read_csv(p)
    df = df.drop_duplicates("ts").sort_values("ts")
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop(columns=["ts"])
    if tf == "4h":
        df = resample_4h(df)
    return df


def resample_4h(df1h: pd.DataFrame) -> pd.DataFrame:
    if df1h.empty:
        return df1h
    agg = df1h.resample("4h", label="left", closed="left", origin="epoch").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "vol_quote": "sum",
         })
    cnt = df1h["close"].resample("4h", label="left", closed="left", origin="epoch").count()
    agg = agg[cnt == 4]   # only complete 4H bars
    return agg.dropna()


def refresh_candles(coin: str, cov: dict) -> dict:
    p = CANDLE_DIR / f"{coin}_1H.csv.gz"
    meta = dict(cov.get(coin, {}))
    old = pd.read_csv(p) if p.exists() else pd.DataFrame(columns=COLS)
    have_max = int(old["ts"].max()) if len(old) else None
    have_min = int(old["ts"].min()) if len(old) else None
    target_start = int((time.time() - MAX_YEARS * 365.25 * 86400) * 1000)
    new_rows = []
    # forward fill (newest pages until overlap)
    after = None
    pages = 0
    while True:
        params = {"instId": inst(coin), "bar": "1H", "limit": "100"}
        if after:
            params["after"] = str(after)
        rows = okx_get("/api/v5/market/history-candles", params, bucket="candles")
        pages += 1
        if not rows:
            break
        new_rows += _parse_candles(rows)
        oldest = int(rows[-1][0])
        after = oldest
        if have_max is not None and oldest <= have_max:
            break
        if oldest <= target_start:
            meta["exhausted"] = True
            break
        if have_max is None and pages > 400:
            break
    if have_max is None and new_rows and not meta.get("exhausted"):
        # first run ended because data ran out (empty page) => listing start reached
        meta["exhausted"] = True
    # backward fill if we have a cache but not the full history yet
    if have_min is not None and not meta.get("exhausted"):
        after = have_min
        while True:
            rows = okx_get("/api/v5/market/history-candles",
                           {"instId": inst(coin), "bar": "1H", "limit": "100", "after": str(after)},
                           bucket="candles")
            if not rows:
                meta["exhausted"] = True
                break
            new_rows += _parse_candles(rows)
            after = int(rows[-1][0])
            if after <= target_start:
                meta["exhausted"] = True
                break
    if new_rows:
        nd = pd.DataFrame(new_rows, columns=COLS)
        df = pd.concat([old, nd]).drop_duplicates("ts", keep="last").sort_values("ts")
        df = df[df["ts"] >= target_start]
    else:
        df = old
    if len(df):
        df.to_csv(p, index=False, compression="gzip")
        meta.update({
            "bars_1h": int(len(df)),
            "start_utc": pd.to_datetime(int(df["ts"].min()), unit="ms", utc=True).isoformat(),
            "end_utc": pd.to_datetime(int(df["ts"].max()), unit="ms", utc=True).isoformat(),
            "start_is_listing_or_okx_limit": bool(int(df["ts"].min()) > target_start + 86400000),
            "added_rows": len(new_rows),
        })
        last_age_h = (time.time() * 1000 - int(df["ts"].max())) / 3.6e6
        meta["delisted_or_stale"] = bool(last_age_h > 48)
        gaps = np.diff(df["ts"].values) / 3.6e6
        meta["missing_1h_bars"] = int(np.sum(gaps[gaps > 1] - 1))
    else:
        meta.update({"bars_1h": 0, "error": "no candles returned (not listed / delisted?)"})
    return meta


# ---------------------------------------------------------------- funding
def load_funding(coin: str) -> pd.Series:
    p = FUND_DIR / f"{coin}.csv"
    if not p.exists():
        return pd.Series(dtype=float)
    df = pd.read_csv(p).drop_duplicates("ts").sort_values("ts")
    return pd.Series(df["rate"].values, index=pd.to_datetime(df["ts"], unit="ms", utc=True))


def refresh_funding(coin: str) -> dict:
    p = FUND_DIR / f"{coin}.csv"
    old = pd.read_csv(p) if p.exists() else pd.DataFrame(columns=["ts", "rate"])
    have_max = int(old["ts"].max()) if len(old) else None
    rows, after = [], None
    for _ in range(50):
        params = {"instId": inst(coin), "limit": "400"}
        if after:
            params["after"] = str(after)
        d = okx_get("/api/v5/public/funding-rate-history", params, bucket="public")
        if not d:
            break
        rows += [(int(x["fundingTime"]), float(x.get("realizedRate") or x["fundingRate"])) for x in d]
        after = int(d[-1]["fundingTime"])
        if have_max and after <= have_max:
            break
    df = pd.concat([old, pd.DataFrame(rows, columns=["ts", "rate"])]).drop_duplicates("ts", keep="last").sort_values("ts")
    df.to_csv(p, index=False)
    out = {"funding_rows": int(len(df))}
    if len(df):
        out["funding_start_utc"] = pd.to_datetime(int(df["ts"].min()), unit="ms", utc=True).isoformat()
        iv = np.median(np.diff(df["ts"].values[-30:])) / 3.6e6 if len(df) > 2 else 8
        out["funding_interval_h"] = float(iv)
    return out


# ---------------------------------------------------------------- open interest
def load_oi(coin: str) -> pd.Series:
    p = OI_DIR / f"{coin}.csv"
    if not p.exists():
        return pd.Series(dtype=float)
    df = pd.read_csv(p).drop_duplicates("ts").sort_values("ts")
    return pd.Series(df["oi_usd"].values, index=pd.to_datetime(df["ts"], unit="ms", utc=True))


def refresh_oi(coin: str) -> dict:
    p = OI_DIR / f"{coin}.csv"
    old = pd.read_csv(p) if p.exists() else pd.DataFrame(columns=["ts", "oi_usd"])
    have_max = int(old["ts"].max()) if len(old) else None
    rows, end = [], None
    for _ in range(30):
        params = {"instId": inst(coin), "period": "1H", "limit": "100"}
        if end:
            params["end"] = str(end)
        try:
            d = okx_get("/api/v5/rubik/stat/contracts/open-interest-history", params, bucket="rubik")
        except FetchError as e:
            log.warning("OI %s: %s", coin, e)
            break
        if not d:
            break
        rows += [(int(x[0]), float(x[3])) for x in d]
        end = int(d[-1][0]) - 1
        if have_max and end <= have_max:
            break
    df = pd.concat([old, pd.DataFrame(rows, columns=["ts", "oi_usd"])]).drop_duplicates("ts", keep="last").sort_values("ts")
    df.to_csv(p, index=False)
    out = {"oi_rows": int(len(df))}
    if len(df):
        out["oi_start_utc"] = pd.to_datetime(int(df["ts"].min()), unit="ms", utc=True).isoformat()
    return out


# ---------------------------------------------------------------- CoinGecko cross-check
def coingecko_crosscheck(coins=ALL_COINS, max_age_h: float = 20) -> dict:
    """Compare last 7 days of hourly OKX closes vs CoinGecko 90-day hourly data.
    Cached per coin; refreshed at most every `max_age_h` hours. Degrades gracefully."""
    res = {}
    for c in coins:
        cg_id = COINGECKO_IDS.get(c)
        p = CG_DIR / f"{c}.json"
        cached = read_json(p)
        fresh = cached and (time.time() - cached.get("_fetched", 0)) < max_age_h * 3600
        if not fresh:
            try:
                j = coingecko_get(f"/coins/{cg_id}/market_chart", {"vs_currency": "usd", "days": "90"})
                j["_fetched"] = time.time()
                atomic_write_json(p, j)
                cached = j
            except FetchError as e:
                log.warning("CoinGecko %s: %s", c, e)
                res[c] = {"status": "unavailable", "error": str(e)[:200],
                          "used_cache_from": cached.get("_fetched") if cached else None}
                if not cached:
                    continue
        try:
            px = pd.Series({pd.to_datetime(t, unit="ms", utc=True): v for t, v in cached["prices"]})
            vol = pd.Series({pd.to_datetime(t, unit="ms", utc=True): v for t, v in cached["total_volumes"]})
            okx = load_candles(c, "1h")
            if okx.empty:
                res[c] = {"status": "no_okx_data"}
                continue
            recent = px[px.index > px.index.max() - pd.Timedelta(days=7)]
            # OKX close at bar end ~ the CoinGecko point nearest in time
            okx_close = okx["close"].copy()
            okx_close.index = okx_close.index + pd.Timedelta(hours=1)
            aligned = okx_close.reindex(recent.index, method="nearest", tolerance=pd.Timedelta(minutes=40))
            m = aligned.notna()
            diffs = (recent[m] / aligned[m] - 1).abs() * 100
            okx_vol_24h = float(okx["vol_quote"].iloc[-24:].sum())
            cg_vol_24h = float(vol.iloc[-1]) if len(vol) else None
            res[c] = {
                "status": res.get(c, {}).get("status", "ok"),
                "points_compared": int(m.sum()),
                "median_abs_price_diff_pct": round(float(diffs.median()), 3) if m.sum() else None,
                "max_abs_price_diff_pct": round(float(diffs.max()), 3) if m.sum() else None,
                "okx_perp_vol_24h_usd": round(okx_vol_24h),
                "coingecko_all_venue_vol_24h_usd": round(cg_vol_24h) if cg_vol_24h else None,
                "flag": bool(m.sum() and diffs.median() > 1.0),
            }
        except Exception as e:  # noqa
            res[c] = {"status": "error", "error": str(e)[:200]}
    return res


def refresh_all(coins=ALL_COINS, workers: int = 4) -> dict:
    cov = read_json(COVERAGE, {}) or {}
    t0 = time.time()
    errors = {}

    def job(c):
        m = refresh_candles(c, cov)
        m.update(refresh_funding(c))
        m.update(refresh_oi(c))
        return c, m

    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(job, c): c for c in coins}
        for f in as_completed(futs):
            c = futs[f]
            try:
                c, m = f.result()
                cov[c] = m
                log.info("refreshed %s: %s bars from %s (+%s)", c, m.get("bars_1h"), m.get("start_utc"), m.get("added_rows"))
            except Exception as e:  # noqa
                errors[c] = str(e)[:300]
                log.error("refresh %s failed: %s", c, e)
    cov["_updated_utc"] = pd.Timestamp.utcnow().isoformat()
    atomic_write_json(COVERAGE, cov)
    return {"coverage": cov, "errors": errors, "seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- bundles for research
def bundle(coin: str, tf: str, btc_close: "pd.Series | None" = None):
    """(df, features, funding_series, funding_interval_h) for a coin/timeframe."""
    from setups import features
    tfh = {"1h": 1, "4h": 4}[tf]
    df = load_candles(coin, tf)
    if df.empty:
        return None
    fund = load_funding(coin)
    oi = load_oi(coin)
    ctx = {"btc_close": btc_close, "funding": fund, "oi": oi, "bar_delta": pd.Timedelta(hours=tfh)}
    f = features(df, ctx)
    iv = 8.0
    if len(fund) > 3:
        iv = float(np.median(np.diff(ns(fund.index)[-30:])) / 3.6e12)
    return df, f, fund, iv
