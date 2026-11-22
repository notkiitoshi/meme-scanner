#!/usr/bin/env python3
"""Emit the FRESH_* TypeScript constants for EditCanvas from latest_scan.json."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCAN = ROOT / "out" / "latest_scan.json"
OUT = ROOT / "out" / "fresh_page_block.tsx"
HST_FMT_IN = "%Y-%m-%d %H:%M:%S HST"


def js_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def short_hst(run_at_hst: str | None, run_at_utc: str | None) -> str:
    if run_at_hst:
        try:
            dt = datetime.strptime(run_at_hst.replace(" HST", ""), "%Y-%m-%d %H:%M:%S")
            return dt.strftime("%-I:%M %p HST %b %-d, %Y")
        except ValueError:
            return run_at_hst
    return run_at_utc or "unknown"


def role_for(rec: dict) -> str:
    prog = rec.get("graduation_progress_pct")
    age = rec.get("age_hours")
    age_s = f"{age:.1f}h old" if isinstance(age, (int, float)) else "age unknown"
    if rec.get("near_graduation") and prog is not None:
        return f"Near-grad {prog:.1f}% (pump.fun curve)"
    if rec.get("fresh_graduation"):
        venue = rec.get("venue") or "DEX"
        return f"Fresh {venue} pair, {age_s}"
    if rec.get("fresh_pair"):
        return f"New pair, {age_s}"
    return age_s


def coin_block(rec: dict, indent: str = "  ") -> str:
    links = rec.get("links") or {}
    why = rec.get("why_this_could_fail") or []
    pump = bool(links.get("pumpfun") or str(rec.get("mint") or "").endswith("pump"))
    def num(v):
        return "null" if v is None else (str(v) if isinstance(v, (int, float)) else "null")
    lines = [
        indent + "{",
        f"{indent}  mint: {js_str(rec.get('mint') or '')},",
        f"{indent}  symbol: {js_str(rec.get('symbol') or rec.get('mint') or '')},",
        f"{indent}  name: {js_str(rec.get('name') or rec.get('symbol') or '')},",
        f"{indent}  role: {js_str(role_for(rec))},",
        f"{indent}  marketCap: {num(rec.get('market_cap_usd'))},",
        f"{indent}  liquidity: {num(rec.get('liquidity_usd'))},",
        f"{indent}  change24h: {num(rec.get('price_change_24h_pct'))},",
        f"{indent}  ageHours: {num(round(rec['age_hours'], 2) if isinstance(rec.get('age_hours'), (int, float)) else None)},",
        f"{indent}  upsideScore: {num(rec.get('upside_score'))},",
        f"{indent}  progressPct: {num(rec.get('graduation_progress_pct'))},",
        f"{indent}  why: [",
    ]
    for w in why:
        lines.append(f"{indent}    {js_str(w)},")
    lines.append(f"{indent}  ],")
    if links.get("dexscreener"):
        lines.append(f"{indent}  dexUrl: {js_str(links['dexscreener'])},")
    lines.append(f"{indent}  hasPumpfun: {'true' if pump else 'false'},")
    lines.append(indent + "},")
    return "\n".join(lines)


def main() -> int:
    scan = json.loads(SCAN.read_text())
    fresh = scan.get("fresh") or {}
    if not fresh:
        raise SystemExit("latest_scan.json has no fresh block; run scanner.py --fresh first")
    disco = fresh.get("discovery") or {}
    counts = disco.get("counts") or {}
    hst = short_hst(fresh.get("run_at_hst"), fresh.get("run_at_utc"))
    passed = fresh.get("passed") or []
    near = fresh.get("near_passes") or []
    sol = disco.get("sol_price_usd")
    body = "\n".join([
        f"const FRESH_HST = {js_str(hst)};",
        f"const FRESH_UTC = {js_str(fresh.get('run_at_utc') or '')};",
        "const FRESH_STATS = {",
        f"  runtimeSec: {fresh.get('runtime_sec')},",
        f"  discovered: {fresh.get('discovered')},",
        f"  inScope: {fresh.get('in_scope')},",
        f"  gated: {fresh.get('market_gate_passed')},",
        f"  inspected: {fresh.get('inspected')},",
        f"  passed: {fresh.get('passed_count')},",
        f"  near: {fresh.get('near_pass_count')},",
        f"  newly: {fresh.get('newly_flagged_count')},",
        f"  migrations: {counts.get('migrations')},",
        f"  nearGrad: {counts.get('near_graduation')},",
        f"  sol: {sol if sol is not None else 'null'},",
        "};",
        "const FRESH_PASSED: FreshCoin[] = [" + ("" if not passed else ""),
    ])
    if passed:
        body += "\n" + "\n".join(coin_block(r) for r in passed) + "\n];\n"
    else:
        body += "];\n"
    body += "const FRESH_NEAR: FreshCoin[] = ["
    if near:
        body += "\n" + "\n".join(coin_block(r) for r in near) + "\n];\n"
    else:
        body += "];\n"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(body)
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
