#!/usr/bin/env python3
"""Build docs/coins.json (max 12) from the latest --fresh scan for GitHub Pages."""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Repo layout: memescanner/ this file, docs/ at repo root. Also works if docs is a sibling of this file.
CANDIDATE_DOCS = [
    HERE.parent / "docs",
    HERE / "docs",
    Path("/workspace/gh-deploy/docs"),
]


def docs_dir() -> Path:
    for p in CANDIDATE_DOCS:
        if p.parent.exists():
            p.mkdir(parents=True, exist_ok=True)
            return p
    d = HERE.parent / "docs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def links_of(rec: dict) -> dict:
    mint = rec.get("mint") or ""
    links = dict(rec.get("links") or {})
    if mint:
        links.setdefault("fomo", f"https://fomo.family/tokens/solana/{mint}")
        links.setdefault("dexscreener", rec.get("dexscreener_url") or f"https://dexscreener.com/solana/{mint}")
        if mint.endswith("pump") or rec.get("launchpad") == "pumpfun":
            links.setdefault("pumpfun", f"https://pump.fun/coin/{mint}")
    return links


def row(rec: dict, status: str) -> dict:
    links = links_of(rec)
    why = rec.get("why_this_could_fail") or []
    fails = rec.get("failed_criteria") or []
    return {
        "mint": rec.get("mint"),
        "symbol": rec.get("symbol") or rec.get("mint"),
        "name": rec.get("name") or rec.get("symbol"),
        "status": status,  # "passed" or "near"
        "mcap": rec.get("market_cap_usd"),
        "liquidity": rec.get("liquidity_usd"),
        "change_24h_pct": rec.get("price_change_24h_pct"),
        "upside_score": rec.get("upside_score"),
        "age_hours": rec.get("age_hours"),
        "near_graduation": bool(rec.get("near_graduation")),
        "fresh_graduation": bool(rec.get("fresh_graduation")),
        "graduation_progress_pct": rec.get("graduation_progress_pct"),
        "fail_note": why,
        "failed_criteria": [f.get("reason") for f in fails if isinstance(f, dict)],
        "links": {
            "fomo": links.get("fomo"),
            "pumpfun": links.get("pumpfun"),
            "dexscreener": links.get("dexscreener"),
        },
    }


def build(scan: dict, limit: int = 12) -> dict:
    fresh = scan.get("fresh") or {}
    passed = [row(r, "passed") for r in (fresh.get("passed") or [])]
    near = [row(r, "near") for r in (fresh.get("near_passes") or [])]
    coins = (passed + near)[:limit]
    return {
        "scan_time_hst": fresh.get("run_at_hst"),
        "scan_time_utc": fresh.get("run_at_utc"),
        "runtime_sec": fresh.get("runtime_sec"),
        "passed_count": fresh.get("passed_count"),
        "near_pass_count": fresh.get("near_pass_count"),
        "newly_flagged_count": fresh.get("newly_flagged_count"),
        "discovered": fresh.get("discovered"),
        "in_scope": fresh.get("in_scope"),
        "inspected": fresh.get("inspected"),
        "research_only": True,
        "coins": coins,
    }


def main() -> int:
    scan_path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "out" / "latest_scan.json"
    scan = json.loads(scan_path.read_text())
    if not scan.get("fresh"):
        print("publish_docs: no fresh block in", scan_path, file=sys.stderr)
        return 1
    payload = build(scan)
    out = docs_dir() / "coins.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Wrote {out} ({len(payload['coins'])} coins, scan {payload['scan_time_hst']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
