#!/usr/bin/env python3
"""Send ntfy pushes for newly flagged fresh coins. Topic comes from NTFY_TOPIC env (repo secret)."""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    if not topic:
        print("notify_ntfy: NTFY_TOPIC is not set; skipping push")
        return 0
    if "/" in topic or topic.startswith("http"):
        print("notify_ntfy: refusing a topic that looks like a URL or path", file=sys.stderr)
        return 1
    scan_path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "out" / "latest_scan.json"
    scan = json.loads(scan_path.read_text())
    fresh = scan.get("fresh") or {}
    newly = fresh.get("newly_flagged") or scan.get("newly_flagged") or []
    if not newly:
        print("notify_ntfy: no newly flagged coins")
        return 0
    for rec in newly:
        mint = rec.get("mint") or ""
        symbol = rec.get("symbol") or mint[:8]
        fomo = (rec.get("links") or {}).get("fomo") or f"https://fomo.family/tokens/solana/{mint}"
        why = (rec.get("why_this_could_fail") or ["High meme-coin beta"])[0]
        mcap = rec.get("market_cap_usd")
        mcap_s = f"${mcap:,.0f} mcap" if isinstance(mcap, (int, float)) else ""
        title = f"Fresh pass: {symbol}"
        body = f"{symbol} passed the meme scanner checks. {mcap_s}. Open in Fomo: {fomo}\nWHY THIS COULD FAIL: {why}\nResearch only, not financial advice."
        url = "https://ntfy.sh/" + topic
        req = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            method="POST",
            headers={
                "Title": title,
                "Click": fomo,
                "Tags": "chart_with_upwards_trend",
                "Content-Type": "text/plain; charset=utf-8",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                print(f"notify_ntfy: sent {symbol} HTTP {resp.status}")
        except Exception as e:
            print(f"notify_ntfy: failed for {symbol}: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
