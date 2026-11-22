#!/usr/bin/env python3
"""RADAR: refresh public data (incremental), rank the 20 meme perps, label
regimes, and publish 25+ parameterized setup ideas. Paper research only.

Usage: python radar.py [--no-refresh] [--skip-coingecko]
Output: out/radar_YYYY-MM-DD.json (HST date) and out/radar_latest.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import numpy as np
import pandas as pd

import data
from common import (BTC, OUT, UNIVERSE, atomic_write_json, fail, get_logger, hst_today, now_utc)
from setups import CATALOG, signals

log = get_logger("radar")


def coin_snapshot(coin, btc_close):
    out = {"coin": coin}
    for tf in ("1h", "4h"):
        b = data.bundle(coin, tf, btc_close)
        if b is None:
            return None
        df, f, fund, iv = b
        out[f"regime_{tf}"] = str(f["regime"].iloc[-1])
        out[f"_b_{tf}"] = b
    df, f, fund, iv = out["_b_4h"]
    d1, f1, _, _ = out["_b_1h"]
    c = df["close"]
    last = float(d1["close"].iloc[-1])
    out["last_close"] = last
    out["last_bar_utc"] = (d1.index[-1] + pd.Timedelta(hours=1)).isoformat()
    out["trend"] = round(float((c.iloc[-1] / f["ema50"].iloc[-1] - 1) + (f["ema50"].iloc[-1] / f["ema200"].iloc[-1] - 1)), 4) \
        if np.isfinite(f["ema200"].iloc[-1]) else None
    out["mom_24h_pct"] = round(float((last / d1["close"].iloc[-25] - 1) * 100), 2) if len(d1) > 25 else None
    out["mom_7d_pct"] = round(float((last / d1["close"].iloc[-169] - 1) * 100), 2) if len(d1) > 169 else None
    v24 = float(d1["vol_quote"].iloc[-24:].sum())
    v30 = float(d1["vol_quote"].iloc[-720:].sum()) / 30 if len(d1) >= 720 else None
    out["vol_24h_usd"] = round(v24)
    out["vol_ratio_vs_30d"] = round(v24 / v30, 2) if v30 else None
    if len(fund):
        out["funding_last_pct"] = round(float(fund.iloc[-1]) * 100, 4)
        out["funding_interval_h"] = iv
        last7 = fund[fund.index > fund.index.max() - pd.Timedelta(days=7)]
        out["funding_7d_avg_pct"] = round(float(last7.mean()) * 100, 4)
        out["funding_annualized_pct"] = round(float(last7.mean()) * (24 / iv) * 365 * 100, 1)
        z = (fund.iloc[-1] - fund.mean()) / (fund.std() or np.nan)
        out["funding_z"] = round(float(z), 2) if np.isfinite(z) else None
    oi = data.load_oi(coin)
    if len(oi) > 25:
        out["oi_usd"] = round(float(oi.iloc[-1]))
        out["oi_chg_24h_pct"] = round(float((oi.iloc[-1] / oi.iloc[-25] - 1) * 100), 2)
    atr_pct = float(f["atr"].iloc[-1] / c.iloc[-1] * 100)
    out["atr_4h_pct"] = round(atr_pct, 2)
    r1 = np.log(d1["close"]).diff().iloc[-720:]
    out["realized_vol_30d_ann_pct"] = round(float(r1.std() * np.sqrt(24 * 365) * 100), 1)
    return out


def rank(snaps):
    df = pd.DataFrame([{k: v for k, v in s.items() if not k.startswith("_b_")} for s in snaps]).set_index("coin")
    def pr(col, absval=False):
        x = df[col].astype(float)
        if absval:
            x = x.abs()
        return x.rank(pct=True)
    factors = {
        "trend": pr("trend", True), "momentum": pr("mom_7d_pct", True), "volume": pr("vol_ratio_vs_30d"),
        "funding": pr("funding_z", True) if "funding_z" in df else None,
        "oi": pr("oi_chg_24h_pct", True) if "oi_chg_24h_pct" in df else None,
        "volatility": pr("atr_4h_pct"),
    }
    fr = pd.DataFrame({k: v for k, v in factors.items() if v is not None})
    df["opportunity_score"] = fr.mean(axis=1, skipna=True).round(3)
    df["bias"] = np.where(df["trend"].fillna(0) + df["mom_7d_pct"].fillna(0) / 100 > 0, "long", "short")
    for k in fr.columns:
        df[f"rank_{k}"] = fr[k].rank(ascending=False, method="min").astype("Int64")
    df = df.sort_values("opportunity_score", ascending=False)
    df["rank"] = range(1, len(df) + 1)
    return df


def live_signals(setup, snaps):
    hits = []
    for s in snaps:
        for tf, tfh in (("1h", 1), ("4h", 4)):
            df, f, _, _ = s[f"_b_{tf}"]
            tail = slice(-600, None)
            sig = signals(setup, df.iloc[tail], f.iloc[tail], tfh)
            if len(sig) and sig.iloc[-1] != 0:
                hits.append({"coin": s["coin"], "tf": tf, "dir": "long" if sig.iloc[-1] > 0 else "short",
                             "bar_close_utc": (df.index[-1] + pd.Timedelta(hours=tfh)).isoformat()})
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-refresh", action="store_true")
    ap.add_argument("--skip-coingecko", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    errors = {}
    if not a.no_refresh:
        r = data.refresh_all()
        errors.update({f"refresh_{k}": v for k, v in r["errors"].items()})
        log.info("data refresh took %.1fs", r["seconds"])
    cov = data.read_json(data.COVERAGE, {})
    btc = data.load_candles(BTC, "1h")
    if btc.empty:
        fail(log, "no BTC candles in cache; run without --no-refresh")
    btc_close = btc["close"]
    snaps = []
    for c in UNIVERSE:
        try:
            s = coin_snapshot(c, btc_close)
            if s is None:
                errors[c] = "no candles"
                continue
            snaps.append(s)
        except Exception as e:  # noqa
            errors[c] = f"snapshot failed: {e}"
            log.exception("snapshot %s", c)
    if len(snaps) < 10:
        fail(log, f"only {len(snaps)} coins have data; errors={errors}")
    btc_s = coin_snapshot(BTC, btc_close)
    ranked = rank(snaps)
    cg = {"status": "skipped"} if a.skip_coingecko else data.coingecko_crosscheck()
    ideas = []
    for st in CATALOG:
        need = set(st["regimes"])
        in_regime = [s["coin"] for s in snaps if s["regime_4h"] in need]
        ideas.append({
            "id": st["id"], "family": st["family"], "name": st["name"], "reason": st["reason"],
            "required_regime": st["regimes"], "params": st["params"], "exit": st["exit"],
            "tunable": st["tunable"],
            "coins_in_required_regime_now_4h": in_regime,
            "live_signals_now": live_signals(st, snaps),
        })
    regime_counts = pd.Series([s["regime_4h"] for s in snaps]).value_counts().to_dict()
    btc_trend = "up" if btc_s["trend"] and btc_s["trend"] > 0 else "down"
    summary = [
        f"BTC is {btc_s['regime_4h']} on 4h (trend {btc_trend}, 7d {btc_s['mom_7d_pct']}%, 24h {btc_s['mom_24h_pct']}%).",
        "Meme perps by 4h regime: " + ", ".join(f"{k} {v}" for k, v in regime_counts.items()) +
        f"; median 7d move {np.nanmedian([s['mom_7d_pct'] for s in snaps if s['mom_7d_pct'] is not None]):.1f}%.",
        "Top opportunity scores: " + ", ".join(f"{c} ({r['bias']}, {r['regime_4h']})" for c, r in ranked.head(3).iterrows()) + ".",
    ]
    out = {
        "kind": "radar", "generated_utc": now_utc().isoformat(), "hst_date": hst_today(),
        "paper_only": True,
        "market_summary_3_lines": summary,
        "btc": {k: v for k, v in btc_s.items() if not k.startswith("_b_")},
        "ranking": json.loads(ranked.reset_index().to_json(orient="records")),
        "setup_ideas": ideas,
        "setup_count": len(ideas),
        "news": {"status": "not_scanned", "items": [],
                 "note": "Filled by the scheduled routine from real sources only; nothing is invented here."},
        "coingecko_crosscheck": cg,
        "data_coverage": cov,
        "errors": errors,
        "runtime_sec": round(time.time() - t0, 1),
    }
    p = OUT / f"radar_{hst_today()}.json"
    atomic_write_json(p, out)
    atomic_write_json(OUT / "radar_latest.json", out)
    log.info("radar wrote %s (%d setups, %d coins) in %.1fs", p, len(ideas), len(snaps), out["runtime_sec"])
    print(json.dumps({"ok": True, "file": str(p), "setups": len(ideas), "coins": len(snaps),
                      "errors": errors, "runtime_sec": out["runtime_sec"], "summary": summary}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa
        log.exception("radar failed")
        print(json.dumps({"ok": False, "error": f"radar failed: {e}"}))
        sys.exit(1)
