#!/usr/bin/env python3
"""WEEKLY (Sunday): leader vs runner-ups using Lab backtests plus the last 7
days of paper books, flags fading edges and recommends what to cut.

Usage: python weekly.py [--json]
Writes out/weekly_YYYY-MM-DD.md and .json.
"""
from __future__ import annotations

import argparse
import json
import sys

import pandas as pd

from common import CONFIG, DATA, OUT, ROOT, atomic_write_json, get_logger, hst_today, now_utc, read_json

log = get_logger("weekly")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    lab = read_json(OUT / "lab_latest.json")
    active = read_json(CONFIG / "active_setup.json")
    if not lab or not active:
        print(json.dumps({"ok": False, "error": "need out/lab_latest.json and config/active_setup.json"}))
        sys.exit(2)
    perf = read_json(DATA / "paper_daily.json") or {}
    res = {r["key"]: r for r in lab["results"]}
    rows = []
    books = [("main", active["leader"])] + [(f"shadow:{s['key']}", s) for s in active.get("shadows", [])]
    for bn, s in books:
        r = res.get(s["key"], {})
        days = sorted(perf.get(bn, {}).items())[-7:]
        p7 = float(pd.Series([v for _, v in days]).add(1).prod() - 1) * 100 if days else None
        full_R, rec_R = r.get("full_avg_R"), r.get("recent_90d", {}).get("avg_R")
        fading = bool(full_R is not None and rec_R is not None and (rec_R < 0 or (full_R > 0 and rec_R < full_R / 2)))
        rows.append({"book": bn, "setup": s["key"], "role": "leader" if bn == "main" else "runner-up",
                     "paper_7d_ret_pct": round(p7, 3) if p7 is not None else None, "paper_days": len(days),
                     "backtest_oos_ret_pct_est": r.get("oos", {}).get("ret_pct"), "backtest_oos_dd_pct_est": r.get("oos", {}).get("max_dd_pct"),
                     "backtest_trades": r.get("trades_total"), "score_est": r.get("score"),
                     "full_avg_R": full_R, "recent_90d_avg_R": rec_R, "fading_edge": fading,
                     "passed_all_filters": r.get("passed_all_filters"), "killed": r.get("killed")})
    fading_all = [r["key"] for r in lab["results"] if not r.get("killed") and r.get("recent_90d", {}).get("avg_R") is not None
                  and r["recent_90d"]["avg_R"] < 0]
    cut = [x["setup"] for x in rows if x["role"] == "runner-up" and (x["fading_edge"] or (x["paper_7d_ret_pct"] or 0) < 0)]
    shield = read_json(OUT / "shield_status.json") or {}
    out = {"kind": "weekly", "generated_utc": now_utc().isoformat(), "hst_date": hst_today(), "paper_only": True,
           "label": "Backtest figures are estimates.", "books": rows,
           "surviving_setups_with_negative_recent_90d_R": fading_all,
           "cut_candidates": cut, "swap_check": shield.get("leader_vs_runner_ups", []),
           "killed_count": sum(1 for r in lab["results"] if r.get("killed")), "tests": len(lab["results"])}
    L = [f"# Weekly review {hst_today()} (paper only; backtests are estimates)", ""]
    for x in rows:
        p7s = "n/a (no completed paper days yet)" if x["paper_7d_ret_pct"] is None else f"{x['paper_7d_ret_pct']}% over {x['paper_days']} days"
        L.append(f"- **{x['role']} {x['setup']}**: paper 7d {p7s}; backtest OOS {x['backtest_oos_ret_pct_est']}% "
                 f"(DD {x['backtest_oos_dd_pct_est']}%, {x['backtest_trades']} trades, score {x['score_est']}/5); avg R full {x['full_avg_R']} vs last 90d {x['recent_90d_avg_R']}"
                 + (" **FADING**" if x["fading_edge"] else ""))
    L += ["", f"Surviving setups with negative last-90-day R: {', '.join(fading_all) or 'none'}",
          f"Cut candidates: {', '.join(cut) or 'none'}", f"Killed by Lab this week: {out['killed_count']} of {out['tests']} tests"]
    md = "\n".join(L)
    atomic_write_json(OUT / f"weekly_{hst_today()}.json", out)
    (OUT / f"weekly_{hst_today()}.md").write_text(md)
    print(json.dumps(out, indent=2, default=str) if a.json else md)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa
        log.exception("weekly failed")
        print(json.dumps({"ok": False, "error": f"weekly failed: {e}"}))
        sys.exit(1)
