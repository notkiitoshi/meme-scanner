#!/usr/bin/env python3
"""FORGE: turn Lab's leader into config/active_setup.json (the ONE file that
swaps setups), attach runner-ups as shadow paper books, then run the bot
self-test. Paper only.

Usage: python forge.py                 # leader from out/lab_latest.json
       python forge.py --promote KEY   # make a runner-up (e.g. RB_S@4h) the leader
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

from common import CONFIG, OUT, ROOT, atomic_write_json, fail, get_logger, now_utc, read_json
from setups import CATALOG

log = get_logger("forge")


def spec(key, lab, role):
    sid, tf = key.split("@")
    base = next((s for s in CATALOG if s["id"] == sid), None)
    if base is None:
        raise KeyError(f"setup {sid} not in catalog")
    res = next((r for r in lab["results"] if r["key"] == key), {})
    return {"key": key, "id": sid, "tf": tf, "role": role, "family": base["family"], "name": base["name"],
            "reason": base["reason"], "regimes": base["regimes"], "params": base["params"], "exit": base["exit"],
            "lab_rank": res.get("rank"), "lab_score_estimate": res.get("score"),
            "lab_passed_all_filters": res.get("passed_all_filters"), "lab_killed": res.get("killed"),
            "lab_oos_ret_pct_estimate": res.get("oos", {}).get("ret_pct"),
            "lab_oos_max_dd_pct_estimate": res.get("oos", {}).get("max_dd_pct")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--promote", default="")
    ap.add_argument("--lab", default=str(OUT / "lab_latest.json"))
    ap.add_argument("--reselect", action="store_true", help="take Lab's leader even if the current leader is still alive")
    a = ap.parse_args()
    lab = read_json(__import__("pathlib").Path(a.lab))
    if not lab:
        fail(log, f"no Lab output at {a.lab}; run lab.py first")
    prev = read_json(CONFIG / "active_setup.json") or {}
    survivors = lab.get("survivors_not_killed", [])
    ranked = [r["key"] for r in lab["results"] if "error" not in r]
    if a.promote:
        if a.promote not in ranked:
            fail(log, f"{a.promote} not found in Lab results")
        leader_key, standby = a.promote, False
        why = f"promoted by user/Shield recommendation from {prev.get('leader', {}).get('key')}"
    elif (not a.reselect and prev.get("leader", {}).get("key") in survivors
          and not prev["leader"].get("standby")):
        leader_key, standby = prev["leader"]["key"], False
        why = (f"kept current leader {leader_key}: still alive in tonight's Lab run. Leaders change only via "
               f"Shield's 7-day runner-up rule (forge.py --promote KEY), a kill by Lab, or forge.py --reselect")
    elif lab.get("leader"):
        leader_key, standby = lab["leader"], False
        why = "Lab leader (best non-killed setup)" + (" that passed every filter" if leader_key in lab.get("passed_all_filters", []) else "; it did NOT pass every robustness filter")
    else:
        leader_key, standby = ranked[0], True
        why = "No setup survived Lab's kill rules, so the main paper book is in STANDBY (no new entries). It tracks the top-ranked setup's signals for reference only."
    leader = spec(leader_key, lab, "leader")
    leader["standby"] = standby
    if standby:
        leader["standby_reason"] = why
    shadow_keys = [k for k in (lab.get("runner_ups") or []) if k != leader_key][:2]
    if not shadow_keys:   # nothing alive: shadow the next best ranked setups as provisional paper books
        shadow_keys = [k for k in ranked if k != leader_key][:2]
    shadows = [spec(k, lab, "runner_up_shadow") for k in shadow_keys]
    cfg = {"generated_utc": now_utc().isoformat(), "paper_only": True, "source_lab": a.lab,
           "why": why, "leader": leader, "shadows": shadows,
           "note": "Edit or replace this one file to swap setups. bot.py reads it every run."}
    atomic_write_json(CONFIG / "active_setup.json", cfg)
    log.info("active setup -> %s (standby=%s); shadows=%s", leader_key, standby, shadow_keys)
    st = subprocess.run([sys.executable, str(ROOT / "bot.py"), "--selftest"], capture_output=True, text=True, timeout=300)
    try:
        selft = json.loads(st.stdout)
    except ValueError:
        selft = {"ok": False, "raw": st.stdout[-2000:], "stderr": st.stderr[-2000:]}
    out = {"ok": bool(selft.get("ok")), "leader": leader_key, "standby": standby, "why": why, "shadows": shadow_keys,
           "selftest_ok": selft.get("ok"), "selftest_failures": [t for t in selft.get("selftest", []) if not t["ok"]],
           "config": str(CONFIG / "active_setup.json")}
    atomic_write_json(OUT / "forge_latest.json", out)
    print(json.dumps(out, indent=2))
    if not selft.get("ok"):
        log.error("bot self-test failed: %s", out["selftest_failures"] or selft)
        sys.exit(3)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa
        log.exception("forge failed")
        print(json.dumps({"ok": False, "error": f"forge failed: {e}"}))
        sys.exit(1)
