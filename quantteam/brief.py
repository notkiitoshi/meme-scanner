#!/usr/bin/env python3
"""BRIEF: the 7:30 AM HST morning brief, printed as markdown (default) or JSON.
Sections: market in 3 lines, top 3 setups with numbers, last 24h paper trades
and P&L, anything Shield blocked/flagged, and one decision for the user.

Usage: python brief.py [--json]
Writes out/brief_YYYY-MM-DD.md and .json too.
"""
from __future__ import annotations

import argparse
import json
import sys

import pandas as pd

from common import CONFIG, LOGS, OUT, ROOT, START_EQUITY, atomic_write_json, get_logger, hst_today, now_utc, read_json

log = get_logger("brief")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    now = pd.Timestamp(now_utc())
    since = now - pd.Timedelta(hours=24)
    radar = read_json(OUT / "radar_latest.json") or {}
    lab = read_json(OUT / "lab_latest.json") or {}
    shield = read_json(OUT / "shield_status.json") or {}
    active = read_json(CONFIG / "active_setup.json") or {}
    state = read_json(ROOT / "state.json") or {}
    missing = [n for n, v in (("radar", radar), ("lab", lab), ("active_setup", active)) if not v]
    res = {r["key"]: r for r in lab.get("results", [])}
    top = []
    for k in lab.get("top3", []):
        r = res.get(k, {})
        top.append({"key": k, "name": next((c["name"] for c in lab.get("strategy_cards", []) if c["key"] == k), k),
                    "score_est": r.get("score"), "oos_ret_pct_est": r.get("oos", {}).get("ret_pct"),
                    "oos_max_dd_pct_est": r.get("oos", {}).get("max_dd_pct"), "oos_trades": r.get("oos", {}).get("trades"),
                    "trades_total": r.get("trades_total"), "buy_hold_oos_pct": r.get("buy_hold_oos_pct"),
                    "walk_forward_ret_pct_est": r.get("walk_forward", {}).get("combined", {}).get("ret_pct"),
                    "passed_all_filters": r.get("passed_all_filters"), "killed": r.get("killed"),
                    "kill_reasons": r.get("kill_reasons")})
    trades = []
    tp = ROOT / "trades.csv"
    if tp.exists():
        t = pd.read_csv(tp)
        t = t[(t["book"] == "main") & (pd.to_datetime(t["exit_time_utc"], utc=True) >= since)]
        trades = t.to_dict("records")
    book = state.get("books", {}).get("main", {})
    eq = book.get("last_equity", START_EQUITY)
    pnl24 = round(sum(x["net_pnl"] for x in trades), 4)
    blocked = []
    dp = LOGS / "decisions.jsonl"
    if dp.exists():
        for line in dp.read_text().splitlines()[-5000:]:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d["action"] in ("blocked_by_shield",) and pd.Timestamp(d["ts_utc"]) >= since:
                blocked.append(f"{d['coin']}: {d['reason']}")
    events = []
    ep = OUT / "shield_events.jsonl"
    if ep.exists():
        for line in ep.read_text().splitlines():
            e = json.loads(line)
            if pd.Timestamp(e["ts_utc"]) >= since:
                events.append(e)
    swap = [c for c in shield.get("leader_vs_runner_ups", []) if c.get("swap_recommended")]
    leader = active.get("leader", {})
    if swap:
        decision = f"Promote runner-up {swap[0]['runner_up']} over {leader.get('key')}? It was stronger on {swap[0]['days_runner_up_stronger']} of the last 7 paper days."
    elif shield.get("halted"):
        decision = f"Shield halted paper entries today ({shield.get('halt', {}).get('reason')}). Keep the halt, or clear it early?"
    elif leader.get("standby"):
        decision = "No setup passed Lab's kill rules, so the paper bot is in standby. Keep it in standby until one passes, or let it paper-trade the best provisional setup anyway?"
    elif not leader.get("lab_passed_all_filters"):
        decision = f"The leader {leader.get('key')} beat buy-and-hold but failed some robustness checks. Keep paper-trading it, or wait for a setup that passes everything?"
    else:
        decision = f"Keep paper-trading {leader.get('key')} as the leader? (No change needed if yes.)"
    out = {
        "kind": "brief", "generated_utc": now.isoformat(), "hst_date": hst_today(), "paper_only": True,
        "market_3_lines": radar.get("market_summary_3_lines", ["Radar output missing."])[:3],
        "top3_setups_ESTIMATES": top,
        "paper_last_24h": {"trades": trades, "net_pnl_usd": pnl24, "equity_usd": round(eq, 2),
                           "start_equity_usd": START_EQUITY, "total_return_pct": round((eq / START_EQUITY - 1) * 100, 2),
                           "open_positions": book.get("positions", {}), "leader": leader.get("key"),
                           "leader_standby": leader.get("standby")},
        "shield": {"halted_now": shield.get("halted"), "halt": shield.get("halt"), "flags": shield.get("flags", []),
                   "events_24h": events, "blocked_signals_24h": blocked},
        "decision": decision, "missing_inputs": missing,
    }
    L = [f"# Quant team brief, {hst_today()} (paper only)", "", "**Market**"] + [f"- {x}" for x in out["market_3_lines"]]
    L += ["", "**Top 3 setups (backtest estimates after fees, funding, slippage)**"]
    for i, s in enumerate(top, 1):
        status = "passed all filters" if s["passed_all_filters"] else ("killed: " + "; ".join(s["kill_reasons"] or []) if s["killed"] else "alive, failed some robustness checks")
        L.append(f"{i}. {s['key']} {s['name']}: OOS {s['oos_ret_pct_est']}% vs buy-and-hold {s['buy_hold_oos_pct']}%, max DD {s['oos_max_dd_pct_est']}%, "
                 f"{s['oos_trades']} OOS / {s['trades_total']} total trades, walk-forward {s['walk_forward_ret_pct_est']}%, score {s['score_est']}/5 ({status})")
    L += ["", "**Paper trades, last 24h**"]
    if trades:
        for x in trades:
            L.append(f"- {x['coin']} {x['dir']} {x['entry']} -> {x['exit']} ({x['exit_reason']}): {x['net_pnl']:+.2f} USD")
    else:
        L.append("- No closed paper trades.")
    L.append(f"- Net 24h {pnl24:+.2f} USD; equity {eq:.2f} USD vs 146.00 start; open: {', '.join(book.get('positions', {})) or 'none'}")
    L += ["", "**Shield**"]
    L += [f"- {f}" for f in out["shield"]["flags"]] or ["- Nothing halted or flagged."]
    if blocked:
        L.append(f"- Blocked {len(blocked)} signal(s): " + "; ".join(blocked[:5]))
    L += ["", f"**Decision for you:** {decision}"]
    md = "\n".join(L)
    atomic_write_json(OUT / f"brief_{hst_today()}.json", out)
    (OUT / f"brief_{hst_today()}.md").write_text(md)
    print(json.dumps(out, indent=2, default=str) if a.json else md)
    if missing:
        log.error("brief missing inputs: %s", missing)
        sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa
        log.exception("brief failed")
        print(json.dumps({"ok": False, "error": f"brief failed: {e}"}))
        sys.exit(1)
