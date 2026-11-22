#!/usr/bin/env python3
"""SHIELD: review paper state every run. Halts NEW paper entries for the rest
of the HST day (config/halt.json, read by bot.py) when:
  * main book daily P&L <= -3% of its HST day-start equity, or
  * 3 straight losing trades (the third closing today, HST), or
  * price feed stale: last OKX ticker seen by the bot is > 30 min old.
Also tracks leader vs runner-up shadow books by HST day: a runner-up is
recommended to replace the leader after 7 stronger paper days.
Prints JSON status incl. `new_flags` (true if it halted/flagged something new).

Usage: python shield.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime

import pandas as pd

from common import (CONFIG, DATA, HST, OUT, ROOT, atomic_write_json, get_logger, hst_today, next_hst_midnight,
                    now_utc, read_json)

log = get_logger("shield")
DAILY_LOSS = -0.03
STREAK = 3
STALE_MIN = 30
SWAP_DAYS = 7


def daily_returns(book):
    ds = sorted(book.get("day_start", {}).items())
    out = {}
    for (d0, e0), (d1, e1) in zip(ds, ds[1:]):
        out[d0] = e1 / e0 - 1 if e0 else 0.0
    return out   # completed days only


def main():
    now = pd.Timestamp(now_utc())
    today = hst_today()
    state = read_json(ROOT / "state.json")
    active = read_json(CONFIG / "active_setup.json") or {}
    halt = read_json(CONFIG / "halt.json")
    halt_active = bool(halt and pd.Timestamp(halt["until"]) > now)
    status = {"ts_utc": now.isoformat(), "hst_date": today, "halted": halt_active,
              "halt": halt if halt_active else None, "new_flags": False, "flags": [], "checks": {}}
    if not state or "main" not in state.get("books", {}):
        status["checks"]["state"] = "no paper state yet (bot has not run)"
        atomic_write_json(OUT / "shield_status.json", status)
        print(json.dumps(status, indent=2))
        return
    book = state["books"]["main"]
    day_start = book["day_start"].get(today)
    eq = book.get("last_equity")
    dpnl = (eq / day_start - 1) if day_start else 0.0
    status["checks"]["daily_pnl_pct"] = round(dpnl * 100, 3)
    status["checks"]["equity"] = round(eq, 4) if eq else None
    tp = ROOT / "trades.csv"
    streak = 0
    third_today = False
    if tp.exists():
        tr = pd.read_csv(tp)
        tr = tr[tr["book"] == "main"].sort_values("exit_time_utc")
        for _, r in tr[::-1].iterrows():
            if r["net_pnl"] < 0:
                streak += 1
            else:
                break
        if streak >= STREAK:
            third = tr.iloc[-1]
            third_today = pd.Timestamp(third["exit_time_utc"]).tz_convert(HST).strftime("%Y-%m-%d") == today
    status["checks"]["losing_streak"] = streak
    last_px = state.get("last_price_ts_utc")
    age = (now - pd.Timestamp(last_px)).total_seconds() / 60 if last_px else None
    status["checks"]["price_feed_age_min"] = round(age, 1) if age is not None else None
    reasons = []
    if dpnl <= DAILY_LOSS:
        reasons.append(f"daily P&L {dpnl*100:.2f}% <= -3%")
    if streak >= STREAK and third_today:
        reasons.append(f"{streak} losing trades in a row")
    if age is None or age > STALE_MIN:
        reasons.append(f"price feed stale ({'never' if age is None else f'{age:.0f} min'} > {STALE_MIN} min)")
    if reasons:
        new_reasons = [r for r in reasons if not (halt_active and r.split(" ")[0] in (halt.get("reason") or ""))]
        until = next_hst_midnight()
        if new_reasons or not halt_active:
            h = {"until": until.astimezone(pd.Timestamp.now(tz="UTC").tz).isoformat(), "until_hst": until.isoformat(),
                 "reason": "; ".join(reasons), "set_utc": now.isoformat()}
            atomic_write_json(CONFIG / "halt.json", h)
            status.update({"halted": True, "halt": h, "new_flags": True})
            status["flags"] += [f"HALT: {r}" for r in reasons]
            with open(OUT / "shield_events.jsonl", "a") as f:
                f.write(json.dumps({"ts_utc": now.isoformat(), "event": "halt", "reasons": reasons, "until_hst": until.isoformat()}) + "\n")
            log.warning("halted: %s", reasons)
    # leader vs runner-ups
    perf_path = DATA / "paper_daily.json"
    perf = {bn: daily_returns(b) for bn, b in state["books"].items()}
    atomic_write_json(perf_path, perf)
    lead = perf.get("main", {})
    comps = []
    for s in active.get("shadows", []):
        bn = f"shadow:{s['key']}"
        sh = perf.get(bn, {})
        days = sorted(set(lead) & set(sh))[-SWAP_DAYS:]
        wins = sum(1 for d in days if sh[d] > lead[d])
        cum_l = float(pd.Series([lead[d] for d in days]).add(1).prod() - 1) if days else 0
        cum_s = float(pd.Series([sh[d] for d in days]).add(1).prod() - 1) if days else 0
        rec = len(days) >= SWAP_DAYS and cum_s > cum_l and wins >= 5
        comps.append({"runner_up": s["key"], "days_compared": len(days), "days_runner_up_stronger": wins,
                      "leader_ret_pct": round(cum_l * 100, 3), "runner_up_ret_pct": round(cum_s * 100, 3),
                      "swap_recommended": rec})
        if rec:
            msg = f"SWAP: {s['key']} beat leader {active['leader']['key']} on {wins}/{len(days)} paper days ({cum_s*100:.2f}% vs {cum_l*100:.2f}%). Run: python forge.py --promote {s['key']}"
            prev = read_json(OUT / "shield_status.json") or {}
            if msg not in prev.get("flags", []):
                status["new_flags"] = True
            status["flags"].append(msg)
    status["leader_vs_runner_ups"] = comps
    hour = datetime.now(HST).hour
    awake = 7 <= hour < 22
    new_halt = status["new_flags"] and any(f.startswith("HALT") for f in status["flags"])
    status["notify_now"] = bool(status["new_flags"] and (awake or new_halt))
    status["notify_rule"] = "message the user only 7 AM-10 PM HST, except a new halt which is sent any time"
    status["leader"] = active.get("leader", {}).get("key")
    status["leader_standby"] = active.get("leader", {}).get("standby")
    atomic_write_json(OUT / "shield_status.json", status)
    print(json.dumps(status, indent=2, default=str))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa
        log.exception("shield failed")
        print(json.dumps({"ok": False, "error": f"shield failed: {e}"}))
        sys.exit(1)
