#!/usr/bin/env python3
"""GitHub Actions 15-minute paper cycle: bot then Shield, ntfy halt alerts only 7 AM–10 PM HST.

Paper only. No exchange keys, no wallets, no real orders.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
HST = ZoneInfo("Pacific/Honolulu")
OUT = ROOT / "out"


def awake(now: datetime | None = None) -> bool:
    hour = (now or datetime.now(HST)).hour
    return 7 <= hour < 22


def run_script(name: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(ROOT / name)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    stdout = proc.stdout or ""
    if proc.returncode != 0:
        print(stdout)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(proc.returncode)
    # last JSON object on stdout
    text = stdout.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.rfind("{\n")
        if start == -1:
            start = text.rfind("{")
        if start >= 0:
            try:
                return json.loads(text[start:])
            except json.JSONDecodeError:
                pass
    print(stdout)
    return {}


def ntfy(title: str, body: str) -> None:
    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    if not topic or "/" in topic or topic.startswith("http"):
        print("gha_cycle: NTFY_TOPIC missing or invalid; skip push")
        return
    if not awake():
        print("gha_cycle: silent outside 7 AM–10 PM HST; not sending:", title)
        return
    req = urllib.request.Request(
        "https://ntfy.sh/" + topic,
        data=body.encode("utf-8"),
        method="POST",
        headers={"Title": title, "Tags": "warning", "Content-Type": "text/plain; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(f"gha_cycle: ntfy HTTP {resp.status} for {title}")


def main() -> int:
    print("gha_cycle: running bot.py (paper only)")
    run_script("bot.py")
    print("gha_cycle: running shield.py")
    status = run_script("shield.py")
    OUT.mkdir(exist_ok=True)
    # Halt alerts through ntfy, but only inside the user's 7 AM–10 PM HST window.
    # Difference vs the previous Grok Bot routine: that routine allowed a new halt
    # to notify at any hour. This workflow stays silent overnight, as requested.
    flags = status.get("flags") or []
    new_flags = bool(status.get("new_flags"))
    if new_flags and flags:
        title = "Shield halt" if any(str(f).startswith("HALT") for f in flags) else "Shield flag"
        body = "Quant paper Shield:\n" + "\n".join(str(f) for f in flags) + "\nPaper only — no real orders."
        try:
            ntfy(title, body)
        except Exception as e:
            print(f"gha_cycle: ntfy failed: {e}", file=sys.stderr)
            return 1
    else:
        print("gha_cycle: no new Shield flags")
    print(json.dumps({"ok": True, "halted": status.get("halted"), "new_flags": new_flags,
                      "notify_window": awake(), "flags": flags}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
