#!/usr/bin/env python3
"""LAB: backtest every Radar setup on 1h and 4h with fees, slippage and
funding; out-of-sample split, walk-forward, Monte Carlo, double-fee test,
parameter-neighborhood robustness and a lookahead check. Scores 1-5, kills
weak setups, writes strategy cards. ALL NUMBERS ARE BACKTEST ESTIMATES.

Usage: python lab.py [--radar out/radar_latest.json] [--workers 4] [--only ID,ID] [--tf 1h,4h]
Output: out/lab_YYYY-MM-DD.json and out/lab_latest.json
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import sys
import time

import numpy as np
import pandas as pd

import data
from backtest import buy_hold, funding_timeline, monte_carlo, portfolio, run_coin
from common import (BTC, FEE, OUT, RISK_FRAC, SLIP, TF_HOURS, UNIVERSE, atomic_write_json, fail,
                    get_logger, hst_today, now_utc, read_json)
from setups import CATALOG, features, neighborhood, signals

log = get_logger("lab")
OOS_FRAC = 0.30
WF_SEGMENTS = 5
MIN_TRADES = 50
B = {}          # (coin, tf) -> dict(df, f, fund_tl, split_ns, seg_edges)
BTC_REG = None  # Series of bull/bear/sideways by day

WHO_LOSES = {
    "range_breakout": "Traders fading the range edge and stop-runs of tight-range shorts/longs.",
    "donchian_trend": "Counter-trend bottom/top pickers who average into a trend.",
    "trend_pullback": "Late traders who chase the move and are stopped out on the dip.",
    "flush_reversal": "Over-leveraged traders who get force-liquidated at the worst price.",
    "funding_extreme": "The crowded side paying high funding, who exit together when price turns.",
    "oi_divergence": "Late buyers mistaking short covering for real demand (or capitulating longs).",
    "session_open": "Slow/passive liquidity that fills the first strong session move late.",
    "alt_lag_btc": "Holders who react to BTC with a delay and buy/sell after the move.",
    "rsi_meanrev": "Breakout chasers in a market that has no trend.",
    "bb_squeeze": "Range traders short volatility when the squeeze resolves.",
    "ema_cross": "Holders who ignore the regime change until it is obvious.",
    "pump_fade": "Late FOMO buyers (or panic sellers) at the end of a meme move.",
    "contraction_continuation": "Traders who read the pause as a top/bottom.",
    "btc_gated_breakdown": "Dip buyers in a market where BTC risk appetite has dried up.",
}


def load_all(coins, tfs):
    global BTC_REG
    btc = data.load_candles(BTC, "1h")
    btc_close = btc["close"]
    d = btc_close.resample("1D").last().dropna()
    r30 = d / d.shift(30) - 1
    BTC_REG = pd.Series(np.where(r30 > 0.10, "bull", np.where(r30 < -0.10, "bear", "sideways")), index=d.index)
    BTC_REG[r30.isna()] = "unknown"
    cov = {}
    for c in coins:
        for tf in tfs:
            b = data.bundle(c, tf, btc_close)
            if b is None or len(b[0]) < 300:
                cov[f"{c}_{tf}"] = "skipped: too little history"
                continue
            df, f, fund, iv = b
            span = df.index[-1] - df.index[0]
            split = df.index[0] + span * (1 - OOS_FRAC)
            edges = [df.index[0] + span * k / WF_SEGMENTS for k in range(WF_SEGMENTS + 1)]
            B[(c, tf)] = {"df": df, "f": f, "tl": funding_timeline(fund, df.index[0], df.index[-1] + pd.Timedelta(days=1), iv),
                          "split_ns": int(split.value), "edges_ns": [int(e.value) for e in edges],
                          "start": df.index[0], "end": df.index[-1]}
    return cov


def trades_for(setup, tf, sig_cache=None):
    tfh = TF_HOURS[tf]
    rows = []
    for (c, t), b in B.items():
        if t != tf:
            continue
        key = (c, json.dumps(setup["params"], sort_keys=True))
        if sig_cache is not None and key in sig_cache:
            sig = sig_cache[key]
        else:
            sig = signals(setup, b["df"], b["f"], tfh)
            if sig_cache is not None:
                sig_cache[key] = sig
        tr = run_coin(setup, b["df"], b["f"], sig, tfh, b["tl"], c)
        for x in tr:
            x["oos"] = x["entry_ns"] >= b["split_ns"]
            x["seg"] = int(np.searchsorted(b["edges_ns"], x["entry_ns"], "right") - 1)
        rows += tr
    return pd.DataFrame(rows)


def lookahead_check(setup, tf, n_fire=15, n_random=5):
    """Targeted truncation test: for bars where the full-history signal fires
    (plus a few random bars), recompute features+signals on history that ENDS
    at that bar. If any truncated result differs from the full-history result,
    the setup peeks at the future. Entries always use the next bar's open."""
    tfh = TF_HOURS[tf]
    best = None
    for (c, t), b in B.items():
        if t != tf or len(b["df"]) > 20000 // tfh:
            continue
        sig = signals(setup, b["df"], b["f"], tfh)
        cnt = int((sig != 0).sum())
        if best is None or cnt > best[0]:
            best = (cnt, c, sig)
    if best is None:
        return {"ok": None, "note": "no coin small enough for the check"}
    cnt, c, full = best
    b = B[(c, tf)]
    df = b["df"]
    btc_close = data.load_candles(BTC, "1h")["close"]
    fund, oi = data.load_funding(c), data.load_oi(c)
    fire = np.flatnonzero(full.values)
    fire = fire[fire > 300]
    rng = np.random.default_rng(1)
    picks = list(fire[np.linspace(0, len(fire) - 1, min(n_fire, len(fire))).astype(int)]) if len(fire) else []
    picks += list(rng.integers(300, len(df) - 1, n_random))
    mism = 0
    for i in sorted(set(int(x) for x in picks)):
        dfc = df.iloc[: i + 1]
        close_t = dfc.index[-1] + pd.Timedelta(hours=tfh)
        ctx = {"btc_close": btc_close[btc_close.index + pd.Timedelta(hours=1) <= close_t],
               "funding": fund[fund.index <= close_t], "oi": oi[oi.index <= close_t],
               "bar_delta": pd.Timedelta(hours=tfh)}
        sc = signals(setup, dfc, features(dfc, ctx), tfh)
        mism += int((sc.values != full.values[: i + 1]).sum())
    return {"ok": mism == 0, "coin": c, "bars_checked": len(set(picks)), "signal_bars_on_coin": cnt,
            "mismatched_bars": mism,
            "method": "recompute on history ending at signal bars; must match full-history signals"}


def bh_window(tf, oos_only=True):
    vals = []
    for (c, t), b in B.items():
        if t != tf:
            continue
        t0 = pd.Timestamp(b["split_ns"], tz="UTC") if oos_only else b["start"]
        r = buy_hold(b["df"], t0, b["end"] + pd.Timedelta(hours=1))
        if r is not None:
            vals.append(r)
    return round(float(np.mean(vals) * 100), 2) if vals else None


def clean(p):
    p = dict(p)
    p.pop("eq_rets", None)
    return p


def evaluate(task):
    setup, tf = task
    t0 = time.time()
    try:
        sig_cache = {}
        variants = neighborhood(setup)
        vt = {v["variant"]: trades_for(v, tf, sig_cache) for v in variants}
        base = vt["base"]
        res = {"id": setup["id"], "tf": tf, "key": f"{setup['id']}@{tf}", "family": setup["family"]}
        if base.empty:
            res.update({"trades_total": 0, "killed": True, "kill_reasons": ["no trades"], "score": 1,
                        "passed_all_filters": False, "seconds": round(time.time() - t0, 1)})
            return res
        full = portfolio(base)
        is_ = portfolio(base[~base["oos"]])
        oos = portfolio(base[base["oos"]])
        dbl = portfolio(base[base["oos"]], extra_cost=2 * FEE)
        bh_oos = bh_window(tf, True)
        bh_full = bh_window(tf, False)
        # parameter neighborhood
        nb = {}
        for name, t in vt.items():
            if name == "base":
                continue
            po = portfolio(t[t["oos"]]) if len(t) else {"ret_pct": 0, "trades": 0}
            nb[name] = {"oos_ret_pct": po["ret_pct"], "trades": po["trades"]}
        nb_pos = np.mean([v["oos_ret_pct"] > 0 for v in nb.values()]) if nb else 0
        # walk-forward (expanding window, choose best variant on train R, test next segment)
        wf_tests, folds = [], []
        for k in range(1, WF_SEGMENTS):
            best, best_r = None, -1e9
            for name, t in vt.items():
                if t.empty:
                    continue
                tr = t[t["seg"] < k]
                r = tr["R"].sum() if len(tr) else -1e9
                if r > best_r:
                    best, best_r = name, r
            if best is None:
                continue
            test = vt[best][vt[best]["seg"] == k]
            p = portfolio(test)
            folds.append({"fold": k, "chosen_variant": best, "test_trades": p["trades"], "test_ret_pct": p["ret_pct"]})
            wf_tests.append(test)
        wf_all = portfolio(pd.concat(wf_tests)) if wf_tests else portfolio(None)
        wf_pos = sum(1 for f_ in folds if f_["test_ret_pct"] > 0)
        mc = monte_carlo(oos["eq_rets"])
        # BTC regime breakdown (full sample)
        day = pd.to_datetime(base["entry_ns"], utc=True).dt.floor("1D")
        base = base.assign(btc_regime=BTC_REG.reindex(pd.DatetimeIndex(day).as_unit(BTC_REG.index.unit)).fillna("unknown").values)
        reg = {}
        for rg, g in base.groupby("btc_regime"):
            reg[rg] = {"trades": int(len(g)), "avg_R": round(float(g["R"].mean()), 3),
                       "sum_eq_ret_pct_est": round(float(g["eq_ret"].sum() * 100), 2)}
        recent_cut = base["exit_ns"].max() - 90 * 86400 * 10**9
        rec = base[base["entry_ns"] >= recent_cut]
        la = lookahead_check(setup, tf)
        # kills / filters
        kill = []
        if full["trades"] < MIN_TRADES:
            kill.append(f"only {full['trades']} trades (< {MIN_TRADES})")
        if bh_oos is not None and oos["ret_pct"] <= bh_oos:
            kill.append(f"OOS {oos['ret_pct']}% does not beat buy-and-hold {bh_oos}% after costs")
        if la.get("ok") is False:
            kill.append("lookahead check failed")
        filters = {
            "beats_buy_hold_oos": bh_oos is None or oos["ret_pct"] > bh_oos,
            "min_50_trades": full["trades"] >= MIN_TRADES,
            "oos_trades_ge_30": oos["trades"] >= 30,
            "oos_profit_after_costs": oos["ret_pct"] > 0,
            "in_sample_profit_after_costs": is_["ret_pct"] > 0,
            "survives_double_fees": dbl["ret_pct"] > 0,
            "walk_forward_profitable": wf_all["ret_pct"] > 0 and wf_pos >= 2,
            "neighborhood_robust_60pct": nb_pos >= 0.6,
            "monte_carlo_prob_loss_le_20pct": mc.get("bootstrap_prob_loss", 1) <= 0.2,
            "no_lookahead": la.get("ok") is not False,
        }
        pts = 0
        pts += int(oos["ret_pct"] > 0 and (bh_oos is None or oos["ret_pct"] > bh_oos))
        pts += int(oos["max_dd_pct"] > -15)
        pts += int(full["trades"] >= 100 and oos["trades"] >= 30)
        pts += int(oos["longest_losing_streak"] <= 8 and full["longest_losing_streak"] <= 12)
        pts += int(sum(1 for k_, v in reg.items() if k_ != "unknown" and v["avg_R"] > 0) >= 2)
        res.update({
            "trades_total": full["trades"], "full": clean(full), "in_sample": clean(is_), "oos": clean(oos),
            "oos_double_fee": clean(dbl), "buy_hold_oos_pct": bh_oos, "buy_hold_full_pct": bh_full,
            "neighborhood": {"variants": nb, "share_positive_oos": round(float(nb_pos), 2)},
            "walk_forward": {"folds": folds, "combined": clean(wf_all), "folds_positive": wf_pos, "folds_total": len(folds)},
            "monte_carlo": mc, "btc_regime_results": reg,
            "recent_90d": {"trades": int(len(rec)), "avg_R": round(float(rec["R"].mean()), 3) if len(rec) else None},
            "full_avg_R": round(float(base["R"].mean()), 3),
            "lookahead": la, "filters": filters, "killed": bool(kill), "kill_reasons": kill,
            "passed_all_filters": bool(not kill and all(filters.values())),
            "score": max(1, pts), "funding_estimated_share_full": full.get("funding_estimated_share"),
            "seconds": round(time.time() - t0, 1),
        })
        return res
    except Exception as e:  # noqa
        import traceback
        return {"id": setup["id"], "tf": tf, "key": f"{setup['id']}@{tf}", "error": f"{e}",
                "trace": traceback.format_exc()[-1500:], "killed": True, "kill_reasons": ["error"],
                "score": 1, "passed_all_filters": False}


def card(setup, r, cov):
    ex = setup["exit"]
    o = r.get("oos", {})
    fails = []
    if r.get("funding_estimated_share_full", 0) and r["funding_estimated_share_full"] > 0.5:
        fails.append(f"{int(r['funding_estimated_share_full']*100)}% of backtest trades use ESTIMATED funding (OKX only serves ~3 months of funding history).")
    short_hist = [c for c, m in cov.items() if isinstance(m, dict) and m.get("start_is_listing_or_okx_limit")]
    fails.append(f"{len(short_hist)} of 21 markets have less than 4 years of history; most memes listed in 2023-2025, so results lean on one cycle.")
    fails.append("Survivorship bias: the universe is today's listed memes; coins that died are not in the test.")
    if o.get("top_coin_share", 0) > 0.3:
        fails.append(f"Concentration: {int(o['top_coin_share']*100)}% of OOS trades come from one coin.")
    if (r.get("monte_carlo", {}).get("bootstrap_prob_loss") or 0) >= 0.05:
        fails.append(f"Monte Carlo bootstrap says ~{int(r['monte_carlo']['bootstrap_prob_loss']*100)}% chance the OOS period was a net loss by luck of the draw (estimate).")
    if r.get("oos_double_fee", {}).get("ret_pct", 0) <= 0:
        fails.append("Edge disappears if fees double; it is thin relative to costs.")
    reg = r.get("btc_regime_results", {})
    bad = [k for k, v in reg.items() if k != "unknown" and v["avg_R"] <= 0]
    if bad:
        fails.append("Loses money (avg R <= 0) when BTC is " + "/".join(bad) + ".")
    fails.append("Paper fills assume next-bar open plus 0.05% slippage; real meme order books can be thinner during flushes.")
    rec = r.get("recent_90d", {})
    return {
        "key": r["key"], "setup_id": setup["id"], "timeframe": r["tf"], "name": setup["name"],
        "idea": setup["reason"],
        "why_edge_exists_who_loses": WHO_LOSES.get(setup["family"]),
        "required_regime": setup["regimes"],
        "entry": f"On a closed {r['tf']} bar where the {setup['family']} rule fires ({json.dumps(setup['params'])}) and the coin's regime is one of {setup['regimes']}: enter at next bar open (paper bot: marketable limit at live price +/-0.05%).",
        "exit": f"Take profit at {ex['tp_r']}R, or time exit after {ex['max_hold_h']}h.",
        "stop": f"{ex['stop_atr']} x ATR(14) from entry, attached at entry (same-bar stop+target counts as a stop).",
        "size": f"Risk {RISK_FRAC*100:.0f}% of equity per trade: notional = 1% x equity / stop distance, capped at 3x equity; max 3 open positions.",
        "results_after_costs_ESTIMATE": {
            "oos_return_pct": o.get("ret_pct"), "oos_max_dd_pct": o.get("max_dd_pct"), "oos_trades": o.get("trades"),
            "oos_win_rate": o.get("win_rate"), "oos_avg_R": o.get("avg_R"), "oos_profit_factor": o.get("profit_factor"),
            "buy_and_hold_oos_pct": r.get("buy_hold_oos_pct"),
            "full_return_pct": r.get("full", {}).get("ret_pct"), "full_trades": r.get("trades_total"),
            "oos_double_fee_return_pct": r.get("oos_double_fee", {}).get("ret_pct"),
            "costs": f"fee {FEE*100}%/side, slippage {SLIP*100}%/side, funding at each settlement",
        },
        "walk_forward_ESTIMATE": r.get("walk_forward", {}).get("combined", {}).get("ret_pct"),
        "walk_forward_folds_positive": f"{r.get('walk_forward', {}).get('folds_positive')}/{r.get('walk_forward', {}).get('folds_total')}",
        "worst_month_ESTIMATE": r.get("full", {}).get("worst_month"),
        "longest_losing_streak": r.get("full", {}).get("longest_losing_streak"),
        "score_1_to_5_ESTIMATE": r.get("score"),
        "passed_all_filters": r.get("passed_all_filters"),
        "killed": r.get("killed"), "kill_reasons": r.get("kill_reasons"),
        "condition_that_kills_it": f"Stop trading it if rolling 50-trade avg R falls below 0, or paper drawdown exceeds {min(-10, (o.get('max_dd_pct') or -10)*1.5):.0f}% (1.5x the OOS max drawdown).",
        "first_sign_its_weakening": f"Recent 90-day avg R ({rec.get('avg_R')}) dropping below half the full-sample avg R ({r.get('full_avg_R')}), or 5+ losses in a row.",
        "WHY_THIS_COULD_FAIL": fails,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--radar", default=str(OUT / "radar_latest.json"))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only", default="")
    ap.add_argument("--tf", default="1h,4h")
    a = ap.parse_args()
    t0 = time.time()
    radar = read_json(__import__("pathlib").Path(a.radar))
    if not radar:
        fail(log, f"radar output not found at {a.radar}; run radar.py first")
    cat = {s["id"]: s for s in CATALOG}
    setups = []
    for idea in radar["setup_ideas"]:
        base = cat.get(idea["id"], {})
        s = {**base, **{k: idea[k] for k in ("id", "family", "name", "reason", "params", "exit", "tunable") if k in idea},
             "regimes": idea.get("required_regime", base.get("regimes"))}
        setups.append(s)
    if a.only:
        keep = set(a.only.split(","))
        setups = [s for s in setups if s["id"] in keep]
    tfs = a.tf.split(",")
    cov_note = load_all(UNIVERSE, tfs)
    log.info("loaded %d coin/tf bundles in %.1fs", len(B), time.time() - t0)
    tasks = [(s, tf) for s in setups for tf in tfs]
    ctx = mp.get_context("fork")
    with ctx.Pool(a.workers) as pool:
        results = []
        for r in pool.imap_unordered(evaluate, tasks):
            results.append(r)
            log.info("%s: trades=%s oos=%s%% killed=%s score=%s (%ss)", r["key"], r.get("trades_total"),
                     r.get("oos", {}).get("ret_pct"), r.get("killed"), r.get("score"), r.get("seconds"))
    errs = [r for r in results if "error" in r]
    if len(errs) == len(results):
        fail(log, f"every backtest errored, first: {errs[0].get('trace')}")
    results.sort(key=lambda r: (r.get("passed_all_filters", False), not r.get("killed", True), r.get("score", 0),
                                r.get("oos", {}).get("ret_pct", -1e9)), reverse=True)
    for i, r in enumerate(results, 1):
        r["rank"] = i
    cov = data.read_json(data.COVERAGE, {})
    by_key = {f"{s['id']}": s for s in setups}
    alive = [r for r in results if not r.get("killed")]
    top3 = results[:3]
    leader = alive[0] if alive else None
    runner_ups = [r for r in alive if leader is None or r["key"] != leader["key"]][:3]
    cards = [card(by_key[r["id"]], r, cov) for r in results[:10] if "error" not in r]
    out = {
        "kind": "lab", "generated_utc": now_utc().isoformat(), "hst_date": hst_today(), "paper_only": True,
        "label": "ALL RESULTS ARE BACKTEST ESTIMATES, not predictions or real trading results.",
        "radar_file": a.radar, "setups_tested": len(setups), "timeframes": tfs, "tests_run": len(results),
        "rules": {"fee_per_side": FEE, "slippage_per_side": SLIP, "funding": "actual OKX history where available, else ESTIMATED from the coin's median rate",
                  "oos_split": f"last {int(OOS_FRAC*100)}% of each coin's history", "walk_forward": f"{WF_SEGMENTS} segments, expanding train, best variant by train R",
                  "kill": [f"< {MIN_TRADES} trades", "OOS return after costs <= equal-weight buy-and-hold over the same OOS windows", "lookahead failure"],
                  "risk": "1% of equity per trade, notional capped at 3x equity, max 3 concurrent"},
        "passed_all_filters": [r["key"] for r in results if r.get("passed_all_filters")],
        "survivors_not_killed": [r["key"] for r in alive],
        "leader": leader["key"] if leader else None,
        "top3": [r["key"] for r in top3],
        "runner_ups": [r["key"] for r in runner_ups],
        "strategy_cards": cards,
        "results": results,
        "coverage_notes": cov_note,
        "errors": [{"key": r["key"], "error": r["error"]} for r in errs],
        "runtime_sec": round(time.time() - t0, 1),
    }
    p = OUT / f"lab_{hst_today()}.json"
    atomic_write_json(p, out)
    atomic_write_json(OUT / "lab_latest.json", out)
    print(json.dumps({"ok": True, "file": str(p), "tests": len(results), "passed_all_filters": out["passed_all_filters"],
                      "survivors": len(alive), "leader": out["leader"], "top3": out["top3"], "runner_ups": out["runner_ups"],
                      "errors": len(errs), "runtime_sec": out["runtime_sec"]}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa
        log.exception("lab failed")
        print(json.dumps({"ok": False, "error": f"lab failed: {e}"}))
        sys.exit(1)
