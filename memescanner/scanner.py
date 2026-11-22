#!/usr/bin/env python3
"""Meme Coin Scanner - read-only Solana meme-coin research / instant alerts.

Discovers live candidates from public APIs and flags a coin only when it
passes ALL THREE criteria:

  1. Low or no bundle activity
  2. Low insider concentration
  3. Potential for a 5x to 10x run

Never touches wallets, private keys, exchange keys, or trade execution.
Never fabricates numbers; missing data is recorded and fails closed.

Default notification behavior is INSTANT (not a daily digest): a coin that
newly passes all three filters, or is materially upgraded, is written to
`newly_flagged` with `notify_now: true` so a 15-minute routine can alert
immediately. Cooldown dedup lives in state.json.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import os
import json
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
OUT_DIR = ROOT / "out"
LATEST_JSON = OUT_DIR / "latest_scan.json"
LATEST_MD = OUT_DIR / "latest_report.md"
FLAGGED_CSV = ROOT / "flagged.csv"
HST = ZoneInfo("Pacific/Honolulu")
UTC = timezone.utc

USER_AGENT = "MemeCoinScanner/1.0 (read-only public research; no trading)"
DEX = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
RUGCHECK = "https://api.rugcheck.xyz/v1"
DEFAULT_RPC = "https://api.mainnet-beta.solana.com"

SYSTEM_BURN = "11111111111111111111111111111111"
BURN_OWNERS = {
    SYSTEM_BURN,
    "1nc1nerator11111111111111111111111111111111",
}
LP_ACCOUNT_TYPES = {"AMM", "CLOB", "LOCKER", "PROGRAM", "EXCHANGE", "VAULT"}
LP_NAME_KEYWORDS = (
    "pool", "amm", "vault", " lp", "lp ", "market", "clob", "dlmm", "damm",
    "raydium", "orca", "meteora", "pumpswap", "pump fun", "pump.fun",
    "lifinity", "phoenix", "openbook", "whirlpool", "liquidity", "amm pool",
)
BUNDLE_RISK_NEEDLES = (
    "bundle", "bundled", "sniper", "insider", "copycat",
    "rug", "wash trad", "hidden owner", "airdrop hidden",
)
DANGER_LEVELS = {"danger", "rugged", "critical", "error", "severe"}


def now_utc() -> datetime:
    return datetime.now(UTC)


def fmt_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_hst(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(HST).strftime("%Y-%m-%d %H:%M:%S HST")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open() as f:
        return json.load(f)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str) + "\n")
    tmp.replace(path)


def load_config(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or DEFAULT_CONFIG
    with cfg_path.open() as f:
        cfg = json.load(f)
    cfg["_path"] = str(cfg_path)
    return cfg


def fnum(x: Any, default: float | None = None) -> float | None:
    if x is None or x == "":
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))

class Http:
    def __init__(self, timeout: float = 15.0) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self.timeout = timeout
        self.stats: dict[str, int] = {}
        self.errors: list[str] = []
        self._lock = Lock()

    def _bump(self, key: str) -> None:
        with self._lock:
            self.stats[key] = self.stats.get(key, 0) + 1

    def get(self, url: str, source: str, retries: int = 2, backoff: float | None = None, **kw: Any) -> Any:
        last_err = None
        for attempt in range(retries + 1):
            try:
                r = self.session.get(url, timeout=self.timeout, **kw)
                self._bump(f"{source}_http_{r.status_code}")
                if r.status_code == 429:
                    last_err = f"{source} 429 {url}"
                    self._bump(f"{source}_429")
                    time.sleep(min(30.0, backoff * (2 ** attempt)) if backoff else 0.6 * (attempt + 1))
                    continue
                if r.status_code >= 500:
                    last_err = f"{source} {r.status_code} {url}"
                    time.sleep(0.4 * (attempt + 1))
                    continue
                if r.status_code != 200:
                    last_err = f"{source} {r.status_code} {url}"
                    with self._lock:
                        self.errors.append(last_err)
                    return None
                try:
                    return r.json()
                except ValueError:
                    last_err = f"{source} bad json {url}"
                    return None
            except requests.RequestException as e:
                last_err = f"{source} {type(e).__name__}: {e}"
                time.sleep(0.3 * (attempt + 1))
        if last_err:
            with self._lock:
                self.errors.append(last_err)
                self.stats[f"{source}_fail"] = self.stats.get(f"{source}_fail", 0) + 1
        return None


class Rpc:
    def __init__(self, url: str, pacing_ms: int, timeout: float, http: Http) -> None:
        self.url = url
        self.pacing = pacing_ms / 1000.0
        self.timeout = timeout
        self.http = http
        self._lock = Lock()
        self._last = 0.0
        self.largest_blocked = False
        self.methods_ok: set[str] = set()
        self.methods_fail: set[str] = set()

    def call(self, method: str, params: list[Any]) -> Any:
        with self._lock:
            wait = self.pacing - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            try:
                r = self.http.session.post(
                    self.url,
                    json=payload,
                    timeout=self.timeout,
                    headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
                )
                self._last = time.time()
                data = r.json()
            except (requests.RequestException, ValueError) as e:
                self._last = time.time()
                self.methods_fail.add(method)
                with self.http._lock:
                    self.http.errors.append(f"rpc {method} {type(e).__name__}: {e}")
                    self.http.stats["rpc_fail"] = self.http.stats.get("rpc_fail", 0) + 1
                return None
            err = data.get("error") if isinstance(data, dict) else None
            if err:
                self.methods_fail.add(method)
                code = err.get("code") if isinstance(err, dict) else None
                if code == 429 and method == "getTokenLargestAccounts":
                    self.largest_blocked = True
                with self.http._lock:
                    key = f"rpc_{method}_{code}"
                    self.http.stats[key] = self.http.stats.get(key, 0) + 1
                return None
            self.methods_ok.add(method)
            with self.http._lock:
                key = f"rpc_{method}_ok"
                self.http.stats[key] = self.http.stats.get(key, 0) + 1
            return data.get("result") if isinstance(data, dict) else None

    def token_supply(self, mint: str) -> dict[str, Any] | None:
        result = self.call("getTokenSupply", [mint])
        if not result:
            return None
        val = result.get("value") if isinstance(result, dict) else None
        return val if isinstance(val, dict) else None

    def token_largest(self, mint: str) -> list[dict[str, Any]] | None:
        if self.largest_blocked:
            return None
        result = self.call("getTokenLargestAccounts", [mint])
        if not result:
            return None
        val = result.get("value") if isinstance(result, dict) else None
        return val if isinstance(val, list) else None

    def multiple_accounts(self, addresses: list[str]) -> list[dict[str, Any] | None] | None:
        if not addresses:
            return []
        result = self.call("getMultipleAccounts", [addresses, {"encoding": "jsonParsed"}])
        if not result:
            return None
        val = result.get("value") if isinstance(result, dict) else None
        return val if isinstance(val, list) else None

    def signatures(self, address: str, limit: int = 40) -> list[dict[str, Any]] | None:
        result = self.call("getSignaturesForAddress", [address, {"limit": limit}])
        if result is None:
            return None
        return result if isinstance(result, list) else None

def _mint_from_gt_id(token_id: str | None) -> str | None:
    if not token_id:
        return None
    if token_id.startswith("solana_"):
        return token_id[len("solana_"):]
    return None


def _gt_txn_count(attrs: dict[str, Any]) -> int | None:
    tx = attrs.get("transactions") or {}
    h24 = tx.get("h24") if isinstance(tx, dict) else None
    if not isinstance(h24, dict):
        return None
    try:
        return int(h24.get("buys") or 0) + int(h24.get("sells") or 0)
    except (TypeError, ValueError):
        return None


def _symbol_from_pool_name(name: Any, side: str) -> str | None:
    if not isinstance(name, str) or "/" not in name:
        return None
    parts = [p.strip() for p in name.split("/")]
    if len(parts) < 2:
        return None
    return parts[0] if side == "base" else parts[-1]


def apply_dex_pair_fields(rec: dict[str, Any], p: dict[str, Any], is_primary: bool | None = None) -> None:
    """Merge a DexScreener pair into a candidate. Keeps the highest-liquidity pair as primary."""
    liq = fnum((p.get("liquidity") or {}).get("usd") if isinstance(p.get("liquidity"), dict) else p.get("liquidity"))
    prev_liq = fnum(rec.get("liquidity_usd"), 0.0) or 0.0
    better = is_primary if is_primary is not None else (liq or 0) >= prev_liq
    bt = p.get("baseToken") or {}
    created = p.get("pairCreatedAt")
    info = p.get("info") if isinstance(p.get("info"), dict) else {}
    rec.setdefault("pairs", [])
    pair_addr = p.get("pairAddress")
    if pair_addr and pair_addr not in rec["pairs"]:
        rec["pairs"].append(pair_addr)
    if better:
        rec["symbol"] = bt.get("symbol") or rec.get("symbol")
        rec["name"] = bt.get("name") or rec.get("name")
        rec["price_usd"] = fnum(p.get("priceUsd"))
        rec["market_cap_usd"] = fnum(p.get("marketCap")) if p.get("marketCap") is not None else rec.get("market_cap_usd")
        rec["fdv_usd"] = fnum(p.get("fdv")) if p.get("fdv") is not None else rec.get("fdv_usd")
        rec["liquidity_usd"] = liq if liq is not None else rec.get("liquidity_usd")
        rec["volume_24h_usd"] = fnum((p.get("volume") or {}).get("h24") if isinstance(p.get("volume"), dict) else None)
        rec["price_change_24h_pct"] = fnum((p.get("priceChange") or {}).get("h24") if isinstance(p.get("priceChange"), dict) else None)
        rec["dex_id"] = p.get("dexId")
        rec["pair_address"] = p.get("pairAddress")
        rec["dexscreener_url"] = p.get("url") or rec.get("dexscreener_url")
        rec["pair_created_at_ms"] = created if isinstance(created, (int, float)) else rec.get("pair_created_at_ms")
        tx = p.get("txns") if isinstance(p.get("txns"), dict) else {}
        h24 = tx.get("h24") if isinstance(tx, dict) else {}
        rec["buys_24h"] = fnum((h24 or {}).get("buys"))
        rec["sells_24h"] = fnum((h24 or {}).get("sells"))
        rec["has_socials"] = bool((info or {}).get("socials") or (info or {}).get("websites"))
        rec["socials"] = (info or {}).get("socials") or rec.get("socials")
        rec["websites"] = (info or {}).get("websites") or rec.get("websites")
    if isinstance(created, (int, float)):
        prev = rec.get("oldest_pair_created_at_ms")
        if prev is None or created < prev:
            rec["oldest_pair_created_at_ms"] = created
    rec["pair_count"] = len(rec.get("pairs") or [])


def ingest_dex_pair(p: dict[str, Any], found: dict[str, dict[str, Any]], source: str) -> None:
    bt = p.get("baseToken") or {}
    mint = bt.get("address") or ""
    if not mint:
        return
    rec = found.setdefault(mint, {"mint": mint, "sources": []})
    if source not in rec["sources"]:
        rec["sources"].append(source)
    apply_dex_pair_fields(rec, p)


def discover_candidates(http: Http, cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Pull and deduplicate Solana meme candidates from working public feeds."""
    found: dict[str, dict[str, Any]] = {}
    sources_used: dict[str, int] = {}

    def add(mint: str, source: str, extra: dict[str, Any] | None = None) -> None:
        if not mint or len(mint) < 32:
            return
        rec = found.setdefault(mint, {"mint": mint, "sources": []})
        if source not in rec["sources"]:
            rec["sources"].append(source)
        if extra:
            for k, v in extra.items():
                if v is not None and rec.get(k) is None:
                    rec[k] = v
        sources_used[source] = sources_used.get(source, 0) + 1

    for label, path in (
        ("dex_boosts_latest", f"{DEX}/token-boosts/latest/v1"),
        ("dex_boosts_top", f"{DEX}/token-boosts/top/v1"),
        ("dex_profiles_latest", f"{DEX}/token-profiles/latest/v1"),
    ):
        data = http.get(path, "dexscreener")
        if not isinstance(data, list):
            continue
        for item in data:
            if not isinstance(item, dict):
                continue
            if str(item.get("chainId") or "").lower() != "solana":
                continue
            mint = item.get("tokenAddress") or ""
            links = item.get("links") or []
            add(mint, label, {
                "has_profile": bool(item.get("description") or links or item.get("icon")),
                "social_links": links if isinstance(links, list) else [],
            })

    for q in ("pump", "meme", "bonk"):
        data = http.get(f"{DEX}/latest/dex/search", "dexscreener", params={"q": q})
        pairs = (data or {}).get("pairs") if isinstance(data, dict) else None
        if not isinstance(pairs, list):
            continue
        for p in pairs:
            if not isinstance(p, dict) or str(p.get("chainId") or "").lower() != "solana":
                continue
            ingest_dex_pair(p, found, f"dex_search:{q}")
            sources_used[f"dex_search:{q}"] = sources_used.get(f"dex_search:{q}", 0) + 1

    for label, path, pages in (
        ("gt_trending", f"{GECKO}/networks/solana/trending_pools", 2),
        ("gt_pools", f"{GECKO}/networks/solana/pools", 2),
        ("gt_new_pools", f"{GECKO}/networks/solana/new_pools", 1),
    ):
        for page in range(1, pages + 1):
            url = path + f"?page={page}"
            if label == "gt_trending":
                url += "&duration=24h"
            data = http.get(url, "geckoterminal")
            items = (data or {}).get("data") if isinstance(data, dict) else None
            if not isinstance(items, list):
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                attrs = it.get("attributes") or {}
                rel = it.get("relationships") or {}
                base = _mint_from_gt_id(((rel.get("base_token") or {}).get("data") or {}).get("id"))
                quote = _mint_from_gt_id(((rel.get("quote_token") or {}).get("data") or {}).get("id"))
                extra = {
                    "gt_fdv_usd": fnum(attrs.get("fdv_usd")),
                    "gt_mcap_usd": fnum(attrs.get("market_cap_usd")),
                    "gt_reserve_usd": fnum(attrs.get("reserve_in_usd")),
                    "gt_volume_24h": fnum((attrs.get("volume_usd") or {}).get("h24") if isinstance(attrs.get("volume_usd"), dict) else None),
                    "gt_change_24h": fnum((attrs.get("price_change_percentage") or {}).get("h24") if isinstance(attrs.get("price_change_percentage"), dict) else None),
                    "gt_pool_created_at": attrs.get("pool_created_at"),
                    "gt_pool_name": attrs.get("name"),
                    "gt_txns_24h": _gt_txn_count(attrs),
                }
                if base:
                    add(base, f"{label}:p{page}", extra)
                if quote and not is_excluded_mint(quote, cfg) and not is_excluded_symbol(_symbol_from_pool_name(attrs.get("name"), side="quote"), cfg):
                    add(quote, f"{label}_quote:p{page}", {"gt_pool_created_at": attrs.get("pool_created_at")})
            sources_used[label] = sources_used.get(label, 0) + len(items)

    return list(found.values()), {"per_source": sources_used, "unique_mints": len(found)}


def enrich_with_dexscreener(http: Http, candidates: list[dict[str, Any]], cfg: dict[str, Any],
                            force: bool = False, retries: int = 2, workers: int = 1) -> dict[str, int]:
    """Batch-fetch DexScreener market data (30 mints per call). force=True fetches every mint."""
    need = []
    for c in candidates:
        if force or c.get("liquidity_usd") is None or (c.get("market_cap_usd") is None and c.get("fdv_usd") is None):
            need.append(c["mint"])
    stats = {"batches": 0, "batches_failed": 0}
    if not need:
        return stats
    by_mint = {c["mint"]: c for c in candidates}
    chunks = [need[i:i + 30] for i in range(0, len(need), 30)]

    def fetch(chunk: list[str]) -> Any:
        return http.get(f"{DEX}/tokens/v1/solana/{','.join(chunk)}", "dexscreener",
                        retries=retries, backoff=2.0 if retries > 2 else None)

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(fetch, chunks))
        # one retry pass for failed batches, serial and slower
        for idx, data in enumerate(results):
            if not isinstance(data, list):
                time.sleep(1.0)
                results[idx] = fetch(chunks[idx])
    else:
        results = [fetch(c) for c in chunks]
    for data in results:
        stats["batches"] += 1
        if not isinstance(data, list):
            stats["batches_failed"] += 1
            continue
        for p in data:
            if not isinstance(p, dict) or str(p.get("chainId") or "").lower() != "solana":
                continue
            mint = ((p.get("baseToken") or {}).get("address")) or ""
            rec = by_mint.get(mint)
            if rec is not None:
                apply_dex_pair_fields(rec, p)
    return stats


# ---------------------------------------------------------------------------
# All-graduated mode: enumerate every pump.fun graduate reachable from public,
# keyless sources, drop dead ones, then market-gate and deep-inspect the rest.
# ---------------------------------------------------------------------------


WSOL = "So11111111111111111111111111111111111111112"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
RAYDIUM_V4_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
PUMPFUN_API = "https://frontend-api-v3.pump.fun"
PUMPSWAP_POOL_SIZE = 301
RAYDIUM_V4_POOL_SIZE = 752
CACHE_DIR = OUT_DIR / "cache"
# Fomo launchpad (FomoPad): vanity "...fomo" mints on Meteora Dynamic Bonding Curve, graduating to Meteora DAMM v2.
METEORA_DBC_PROGRAM = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
METEORA_DAMM_V2_PROGRAM = "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"
DBC_POOL_SIZE = 424
DBC_OFF_CONFIG, DBC_OFF_CREATOR, DBC_OFF_BASE_MINT = 72, 104, 136
DBC_OFF_QUOTE_RESERVE, DBC_OFF_IS_MIGRATED, DBC_OFF_MIGRATION_PROGRESS = 240, 305, 308
FOMO_SUFFIX = "fomo"
FOMOPAD_API = "https://fomopad-api-production.up.railway.app"
FOMOPAD_SITE = "https://fomopad.name"
FOMO_APP_TOKEN_URL = "https://fomo.family/tokens/solana/{mint}"
PUMPFUN_COIN_URL = "https://pump.fun/coin/{mint}"
LAUNCHPAD_PUMPFUN, LAUNCHPAD_FOMO, LAUNCHPAD_OTHER = "pumpfun", "fomo", "other"
# Account observed on every pump.fun MigrateV2 / PumpSwap CreatePool transaction
# (not on ordinary PumpSwap swaps). Used to list graduations from the last hour.
PUMP_MIGRATE_ACCOUNT = "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"
PUMP_BC_SEED = b"bonding-curve"
PUMP_INITIAL_VIRTUAL_SOL = 30.0
PUMP_GRADUATION_VIRTUAL_SOL = 85.0  # standard pump.fun curve: 30 virt + 55 real = 85 virt
FRESH_GRADUATION_DEXES = {
    "pumpswap", "raydium", "raydium-clmm", "raydium-launchlab",
    "meteora", "meteora-damm-v2", "meteora-dlmm", "meteora-dbc",
}
DEFAULT_FRESH_SCOPE: dict[str, Any] = {
    "window_sec": 3600,
    "gt_new_pool_pages": 10,  # GeckoTerminal free plan rejects page > 10
    "gt_dex_pool_pages": 5,
    "gt_pumpfun_pool_pages": 10,
    "migrate_account": PUMP_MIGRATE_ACCOUNT,
    "migrate_sig_limit": 300,
    "migrate_tx_workers": 8,
    "near_grad_min_progress_pct": 70.0,
    "graduation_virtual_sol": PUMP_GRADUATION_VIRTUAL_SOL,
    "initial_virtual_sol": PUMP_INITIAL_VIRTUAL_SOL,
    "max_inspect": 24,
    "inspect_retry_rounds": 0,
    "min_pair_age_hours": 0,
    "market_cap_min_usd": 10_000,
    "min_volume_24h_usd": 0,
    "min_vol_mcap_ratio": 0,
    "min_txns_24h": 0,
    "near_grad_min_liquidity_usd": 2000,
}
ALL_GRADUATED_PRESERVE_KEYS = (
    "passed_all_three", "already_alerted", "already_alerted_count", "passed_count",
    "top_potential", "coverage", "discovery_meta", "scanned_discovery_count",
    "prefiltered_in_band_count", "deeply_inspected_count", "scan_mode",
    "run_at_utc", "run_at_hst", "thresholds", "caveats", "rpc_methods_ok",
    "rpc_methods_fail", "dry_run", "version", "notification_mode",
)
B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(B58_ALPHABET)}
_ED_P = 2 ** 255 - 19
_ED_D = (-121665 * pow(121666, _ED_P - 2, _ED_P)) % _ED_P

DEFAULT_GRADUATED_SCOPE: dict[str, Any] = {
    "dead_if_liquidity_below_usd": 5000,
    "dead_if_no_activity_days": 7,
    "require_canonical_pool_pda": True,
    "include_raydium_v4_legacy": True,
    "include_fomo": True,
    "include_legacy_feeds": True,
    "gpa_cache_ttl_hours": 6,
    "gpa_timeout_sec": 180,
    "vault_batch_size": 100,
    "pumpfun_api_page_size": 50,
    "pumpfun_api_max_pages": 400,
    "gt_max_pages": 10,
    "dex_search_queries": ["pumpswap", "pump", "pump.fun", "pumpfun", "raydium pump"],
    "activity_check_max": 3000,
    "activity_cache_ttl_hours": 24,
    "activity_check_workers": 8,
    "inspect_retry_rounds": 3,
    "inspect_retry_sleep_sec": 20,
}


def log(msg: str) -> None:
    """Progress line on stderr (keeps --json stdout clean)."""
    sys.stderr.write(f"[{datetime.now(HST).strftime('%H:%M:%S')} HST] {msg}\n")
    sys.stderr.flush()


def graduated_scope(cfg: dict[str, Any]) -> dict[str, Any]:
    sc = dict(DEFAULT_GRADUATED_SCOPE)
    sc.update(cfg.get("graduated_scope") or {})
    return sc


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = B58_ALPHABET[r] + s
    pad = len(b) - len(b.lstrip(b"\0"))
    return "1" * pad + s


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58_INDEX[c]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\0" * pad + body


def _ed25519_on_curve(b: bytes) -> bool:
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= _ED_P:
        return False
    u = (y * y - 1) % _ED_P
    v = (_ED_D * y * y + 1) % _ED_P
    x2 = u * pow(v, _ED_P - 2, _ED_P) % _ED_P
    if x2 == 0:
        return True
    return pow(x2, (_ED_P - 1) // 2, _ED_P) == 1


def find_program_address(seeds: list[bytes], program_id: str) -> tuple[str, int] | None:
    pid = b58decode(program_id)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + pid + b"ProgramDerivedAddress").digest()
        if not _ed25519_on_curve(h):
            return b58encode(h), bump
    return None


def pump_pool_authority(mint: str) -> str | None:
    res = find_program_address([b"pool-authority", b58decode(mint)], PUMP_PROGRAM)
    return res[0] if res else None


def parse_pumpswap_pool(raw: bytes) -> dict[str, Any] | None:
    """Decode the first 203 bytes of a PumpSwap Pool account."""
    if len(raw) < 203:
        return None
    return {
        "index": int.from_bytes(raw[9:11], "little"),
        "creator": b58encode(raw[11:43]),
        "base_mint": b58encode(raw[43:75]),
        "quote_mint": b58encode(raw[75:107]),
        "quote_vault": b58encode(raw[171:203]),
    }


def is_canonical_graduation_pool(pool: dict[str, Any], require_pda: bool = True) -> bool:
    """A pump.fun graduation pool: index 0, pump-suffix base, WSOL quote, creator = pump pool-authority PDA."""
    base = pool.get("base_mint") or ""
    if not base.endswith("pump") or pool.get("quote_mint") != WSOL or pool.get("index") != 0:
        return False
    if not require_pda:
        return True
    try:
        return pump_pool_authority(base) == pool.get("creator")
    except (KeyError, ValueError):
        return False


def parse_raydium_v4_slice(raw: bytes) -> dict[str, Any] | None:
    """Decode a Raydium AMM v4 slice taken at offset 368 (quoteVault, baseMint)."""
    if len(raw) < 64:
        return None
    return {"quote_vault": b58encode(raw[0:32]), "base_mint": b58encode(raw[32:64])}


def rpc_post(http: Http, url: str, method: str, params: list[Any], timeout: float,
             retries: int = 4, source: str = "rpc_bulk") -> Any:
    """Unpaced RPC call with 429/5xx backoff, for bulk reads (gPA, getMultipleAccounts)."""
    last = None
    for attempt in range(retries + 1):
        try:
            r = http.session.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                  timeout=timeout, headers={"Content-Type": "application/json"})
            http._bump(f"{source}_{method}_http_{r.status_code}")
            if r.status_code == 429 or r.status_code >= 500:
                last = f"{source} {method} HTTP {r.status_code}"
                time.sleep(min(30.0, 1.5 * (2 ** attempt)))
                continue
            data = r.json()
            err = data.get("error") if isinstance(data, dict) else None
            if err:
                code = err.get("code") if isinstance(err, dict) else None
                last = f"{source} {method} rpc error {code}: {str(err)[:160]}"
                if code in (429, -32429, -32005, -32603):
                    time.sleep(min(30.0, 1.5 * (2 ** attempt)))
                    continue
                break
            return data.get("result")
        except (requests.RequestException, ValueError) as e:
            last = f"{source} {method} {type(e).__name__}: {str(e)[:160]}"
            time.sleep(min(30.0, 1.5 * (2 ** attempt)))
    if last:
        with http._lock:
            http.errors.append(last)
            http.stats[f"{source}_fail"] = http.stats.get(f"{source}_fail", 0) + 1
    return None


def _cache_get(name: str, ttl_hours: float) -> Any:
    p = CACHE_DIR / name
    if not p.exists() or ttl_hours <= 0:
        return None
    if time.time() - p.stat().st_mtime > ttl_hours * 3600:
        return None
    try:
        return json.loads(p.read_text())
    except ValueError:
        return None


def _cache_put(name: str, obj: Any) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_DIR / (name + ".tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(CACHE_DIR / name)


def enumerate_pumpswap_graduates(http: Http, cfg: dict[str, Any], sc: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    url = str(cfg.get("rpc_url") or DEFAULT_RPC)
    meta = {"name": "solana_rpc:getProgramAccounts PumpSwap", "url": f"{url} program {PUMPSWAP_PROGRAM}",
            "fully_paginated": False, "status": "error", "pools_scanned": 0, "graduates": 0, "cached": False, "note": None}
    cached = _cache_get("pumpswap_graduates.json", float(sc["gpa_cache_ttl_hours"]))
    if isinstance(cached, dict) and cached.get("graduates"):
        meta.update(cached.get("meta") or {})
        meta["cached"] = True
        return cached["graduates"], meta
    res = rpc_post(http, url, "getProgramAccounts", [PUMPSWAP_PROGRAM, {
        "encoding": "base64", "dataSlice": {"offset": 0, "length": 203},
        "filters": [{"dataSize": PUMPSWAP_POOL_SIZE}, {"memcmp": {"offset": 75, "bytes": WSOL}}],
    }], timeout=float(sc["gpa_timeout_sec"]), retries=3, source="rpc_gpa")
    if not isinstance(res, list):
        meta["note"] = "getProgramAccounts failed or was refused by the public RPC"
        return {}, meta
    out: dict[str, dict[str, Any]] = {}
    pump_suffix = 0
    for acc in res:
        try:
            raw = base64.b64decode(acc["account"]["data"][0])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        pool = parse_pumpswap_pool(raw)
        if not pool:
            continue
        if pool["base_mint"].endswith("pump"):
            pump_suffix += 1
        if not is_canonical_graduation_pool(pool, bool(sc["require_canonical_pool_pda"])):
            continue
        out.setdefault(pool["base_mint"], {"venue": "pumpswap", "pool": acc.get("pubkey"), "quote_vault": pool["quote_vault"]})
    meta.update({"status": "ok", "fully_paginated": True, "pools_scanned": len(res),
                 "pump_suffix_wsol_pools": pump_suffix, "graduates": len(out),
                 "note": "Every WSOL-quoted PumpSwap pool account read on-chain; kept index-0 pools whose creator is the pump.fun pool-authority PDA (canonical graduation pools)."})
    _cache_put("pumpswap_graduates.json", {"graduates": out, "meta": {k: v for k, v in meta.items() if k != "cached"}})
    return out, meta


def enumerate_raydium_legacy_graduates(http: Http, cfg: dict[str, Any], sc: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    url = str(cfg.get("rpc_url") or DEFAULT_RPC)
    meta = {"name": "solana_rpc:getProgramAccounts Raydium AMM v4", "url": f"{url} program {RAYDIUM_V4_PROGRAM}",
            "fully_paginated": False, "status": "error", "pools_scanned": 0, "graduates": 0, "cached": False, "note": None}
    cached = _cache_get("raydium_v4_pump.json", float(sc["gpa_cache_ttl_hours"]))
    if isinstance(cached, dict) and cached.get("graduates"):
        meta.update(cached.get("meta") or {})
        meta["cached"] = True
        return cached["graduates"], meta
    res = rpc_post(http, url, "getProgramAccounts", [RAYDIUM_V4_PROGRAM, {
        "encoding": "base64", "dataSlice": {"offset": 368, "length": 64},
        "filters": [{"dataSize": RAYDIUM_V4_POOL_SIZE}, {"memcmp": {"offset": 432, "bytes": WSOL}}],
    }], timeout=float(sc["gpa_timeout_sec"]), retries=3, source="rpc_gpa")
    if not isinstance(res, list):
        meta["note"] = "getProgramAccounts failed or was refused by the public RPC"
        return {}, meta
    out: dict[str, dict[str, Any]] = {}
    for acc in res:
        try:
            raw = base64.b64decode(acc["account"]["data"][0])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        p = parse_raydium_v4_slice(raw)
        if not p or not p["base_mint"].endswith("pump"):
            continue
        out.setdefault(p["base_mint"], {"venue": "raydium_v4", "pool": acc.get("pubkey"), "quote_vault": p["quote_vault"]})
    meta.update({"status": "ok", "fully_paginated": True, "pools_scanned": len(res), "graduates": len(out),
                 "note": "Pre-PumpSwap graduates (pump.fun migrated to Raydium AMM v4 until Mar 2025). Identified by pump-suffix base mint + WSOL quote; Raydium v4 has no on-chain migrator field, so this is a heuristic."})
    _cache_put("raydium_v4_pump.json", {"graduates": out, "meta": {k: v for k, v in meta.items() if k != "cached"}})
    return out, meta


def enumerate_pumpfun_api(http: Http, sc: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    meta = {"name": "pump.fun frontend API coins?complete=true", "url": f"{PUMPFUN_API}/coins?complete=true",
            "fully_paginated": False, "status": "error", "pages": 0, "graduates": 0, "note": None}
    out: dict[str, dict[str, Any]] = {}
    size = int(sc["pumpfun_api_page_size"])
    for page in range(int(sc["pumpfun_api_max_pages"])):
        url = (f"{PUMPFUN_API}/coins?offset={page * size}&limit={size}&sort=last_trade_timestamp"
               f"&order=DESC&includeNsfw=true&complete=true")
        try:
            r = http.session.get(url, timeout=http.timeout, headers={"Origin": "https://pump.fun", "Referer": "https://pump.fun/"})
        except requests.RequestException as e:
            meta["note"] = f"request failed: {type(e).__name__}"
            break
        http._bump(f"pumpfun_http_{r.status_code}")
        if r.status_code != 200:
            body = r.text[:200].lower()
            why = "Cloudflare bot challenge" if "just a moment" in body or "cloudflare" in body else f"HTTP {r.status_code}"
            meta["note"] = f"blocked from this machine: HTTP {r.status_code} ({why}); not bypassed"
            meta["status"] = "blocked"
            break
        try:
            items = r.json()
        except ValueError:
            meta["note"] = "non-JSON response"
            break
        if isinstance(items, dict):
            items = items.get("coins") or items.get("data") or []
        meta["pages"] = page + 1
        meta["status"] = "ok"
        if not items:
            meta["fully_paginated"] = True
            break
        for it in items:
            mint = (it or {}).get("mint")
            if mint and (it.get("complete") is True or it.get("raydium_pool") or it.get("pump_swap_pool")):
                out.setdefault(mint, {"venue": "pumpfun_api", "pool": it.get("pump_swap_pool") or it.get("raydium_pool"),
                                      "last_trade_ts": it.get("last_trade_timestamp")})
        time.sleep(0.4)
    meta["graduates"] = len(out)
    return out, meta


def _gt_pool_rows(http: Http, path: str, sort: str | None, max_pages: int, label: str) -> tuple[list[dict[str, Any]], int, str | None]:
    rows, pages, note = [], 0, None
    for page in range(1, max_pages + 1):
        url = f"{path}?page={page}" + (f"&sort={sort}" if sort else "")
        data = http.get(url, "geckoterminal", retries=4)
        items = (data or {}).get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            note = f"page {page} unavailable (rate limit or error)"
            break
        pages = page
        if not items:
            break
        for it in items:
            attrs = it.get("attributes") or {}
            rel = it.get("relationships") or {}
            base = _mint_from_gt_id(((rel.get("base_token") or {}).get("data") or {}).get("id"))
            rows.append({"mint": base, "pool": attrs.get("address"), "label": label,
                         "reserve_usd": fnum(attrs.get("reserve_in_usd"))})
        time.sleep(2.1)
    return rows, pages, note


def enumerate_geckoterminal(http: Http, sc: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    out: dict[str, dict[str, Any]] = {}
    metas = []
    maxp = int(sc["gt_max_pages"])
    for dex in ("pumpswap", "raydium"):
        for sort in ("h24_volume_usd_desc", "h24_tx_count_desc"):
            rows, pages, note = _gt_pool_rows(http, f"{GECKO}/networks/solana/dexes/{dex}/pools", sort, maxp, f"gt_{dex}")
            n = 0
            for r in rows:
                m = r["mint"]
                if m and m.endswith("pump"):
                    n += 1
                    out.setdefault(m, {"venue": f"gt_{dex}", "pool": r["pool"]})
            metas.append({"name": f"geckoterminal:dexes/{dex}/pools sort={sort}",
                          "url": f"{GECKO}/networks/solana/dexes/{dex}/pools", "pages": pages,
                          "fully_paginated": False, "status": "ok" if pages else "error", "graduates": n,
                          "note": note or f"free tier hard-caps at page {maxp} (20 pools/page); only volume/tx-count sorts allowed"})
    return out, metas


def enumerate_dexscreener_search(http: Http, sc: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    qs = list(sc.get("dex_search_queries") or [])
    for q in qs:
        data = http.get(f"{DEX}/latest/dex/search", "dexscreener", params={"q": q}, retries=4)
        pairs = (data or {}).get("pairs") if isinstance(data, dict) else None
        for p in pairs or []:
            if str(p.get("chainId") or "").lower() != "solana":
                continue
            m = ((p.get("baseToken") or {}).get("address")) or ""
            if m.endswith("pump") and str(p.get("dexId") or "") in ("pumpswap", "raydium"):
                out.setdefault(m, {"venue": f"dex_{p.get('dexId')}", "pool": p.get("pairAddress")})
    return out, {"name": "dexscreener:latest/dex/search", "url": f"{DEX}/latest/dex/search", "queries": qs,
                 "fully_paginated": False, "status": "ok", "graduates": len(out),
                 "note": "search returns at most ~30 pairs per query (no pagination)"}


def fetch_sol_price(http: Http) -> float | None:
    data = http.get(f"{DEX}/tokens/v1/solana/{WSOL}", "dexscreener", retries=4)
    best, best_liq = None, -1.0
    for p in data or []:
        if not isinstance(p, dict):
            continue
        if ((p.get("baseToken") or {}).get("address")) != WSOL:
            continue
        if ((p.get("quoteToken") or {}).get("symbol") or "").upper() not in ("USDC", "USDT"):
            continue
        liq = fnum((p.get("liquidity") or {}).get("usd"), 0.0) or 0.0
        if liq > best_liq and fnum(p.get("priceUsd")):
            best, best_liq = fnum(p.get("priceUsd")), liq
    return best


def fetch_vault_balances(http: Http, cfg: dict[str, Any], sc: dict[str, Any], vaults: list[str]) -> tuple[dict[str, float], list[str]]:
    """Read WSOL vault balances (SPL token amount, 9 decimals) via getMultipleAccounts with dataSlice."""
    url = str(cfg.get("rpc_url") or DEFAULT_RPC)
    size = int(sc["vault_batch_size"])
    chunks = [vaults[i:i + size] for i in range(0, len(vaults), size)]
    out: dict[str, float] = {}
    failed: list[str] = []

    def one(chunk: list[str]) -> tuple[list[str], Any]:
        return chunk, rpc_post(http, url, "getMultipleAccounts",
                               [chunk, {"encoding": "base64", "dataSlice": {"offset": 64, "length": 8}}],
                               timeout=30, retries=6, source="rpc_vaults")

    with ThreadPoolExecutor(max_workers=3) as ex:
        for chunk, res in ex.map(one, chunks):
            vals = (res or {}).get("value") if isinstance(res, dict) else None
            if not isinstance(vals, list) or len(vals) != len(chunk):
                failed.extend(chunk)
                continue
            for v, acc in zip(chunk, vals):
                if not acc:
                    out[v] = 0.0
                    continue
                try:
                    out[v] = int.from_bytes(base64.b64decode(acc["data"][0]), "little") / 1e9
                except (KeyError, IndexError, TypeError, ValueError):
                    failed.append(v)
    return out, failed


def last_activity_unix(rpc: Rpc, address: str) -> int | None:
    sigs = rpc.signatures(address, limit=1)
    if not sigs:
        return None
    bt = sigs[0].get("blockTime") if isinstance(sigs[0], dict) else None
    return int(bt) if isinstance(bt, (int, float)) else None


def classify_liveness(rec: dict[str, Any], sc: dict[str, Any], now_unix: float) -> str:
    """Return 'alive', 'dead_low_liquidity', 'dead_inactive', or 'needs_activity_check'."""
    cutoff = float(sc["dead_if_liquidity_below_usd"])
    liq = fnum(rec.get("liquidity_usd"))
    if liq is None:
        liq = fnum(rec.get("onchain_liquidity_est_usd"))
    if liq is not None and liq < cutoff:
        return "dead_low_liquidity"
    tx = (fnum(rec.get("buys_24h"), 0) or 0) + (fnum(rec.get("sells_24h"), 0) or 0)
    if tx > 0 or (fnum(rec.get("volume_24h_usd"), 0) or 0) > 0:
        return "alive"
    last = rec.get("last_activity_unix")
    if isinstance(last, (int, float)):
        days = (now_unix - last) / 86400.0
        return "alive" if days <= float(sc["dead_if_no_activity_days"]) else "dead_inactive"
    return "needs_activity_check"


def launchpad_for_mint(mint: str | None, sources: list[str] | None = None) -> str:
    """'fomo' for FomoPad-style vanity mints / fomo sources, else 'pumpfun' (the pump.fun graduate universe)."""
    srcs = set(sources or [])
    if srcs & {"fomo_dbc_onchain", "fomopad_registry"}:
        return LAUNCHPAD_FOMO
    if srcs & {"rpc_pumpswap", "rpc_raydium_v4", "pumpfun_api", "geckoterminal", "dexscreener_search"}:
        return LAUNCHPAD_PUMPFUN
    if mint and mint.endswith(FOMO_SUFFIX):
        return LAUNCHPAD_FOMO
    if mint and mint.endswith("pump"):
        return LAUNCHPAD_PUMPFUN
    return LAUNCHPAD_OTHER


def parse_dbc_pool(raw: bytes) -> dict[str, Any] | None:
    """Meteora DBC VirtualPool: config@72, creator@104, base_mint@136, quote_reserve u64@240, is_migrated u8@305."""
    if len(raw) < DBC_OFF_MIGRATION_PROGRESS + 1:
        return None
    return {
        "config": b58encode(raw[DBC_OFF_CONFIG:DBC_OFF_CONFIG + 32]),
        "creator": b58encode(raw[DBC_OFF_CREATOR:DBC_OFF_CREATOR + 32]),
        "base_mint": b58encode(raw[DBC_OFF_BASE_MINT:DBC_OFF_BASE_MINT + 32]),
        "quote_reserve_raw": int.from_bytes(raw[DBC_OFF_QUOTE_RESERVE:DBC_OFF_QUOTE_RESERVE + 8], "little"),
        "is_migrated": raw[DBC_OFF_IS_MIGRATED] == 1,
        "migration_progress": raw[DBC_OFF_MIGRATION_PROGRESS],
    }


def fetch_fomopad_registry(http: Http) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    meta = {"name": "fomopad_api:/api/tokens (FomoPad verified launch registry)", "url": f"{FOMOPAD_API}/api/tokens",
            "status": "error", "fully_paginated": False, "items": 0, "note": None}
    data = None
    for attempt in range(4):
        try:
            r = http.session.get(f"{FOMOPAD_API}/api/tokens", timeout=30)
            http._bump(f"fomopad_registry_http_{r.status_code}")
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 * (attempt + 1)); continue
            if r.ok:
                data = r.json()
            break
        except (requests.RequestException, ValueError) as e:
            meta["note"] = f"{type(e).__name__}: {str(e)[:120]}"
            time.sleep(2 * (attempt + 1))
    toks = (data or {}).get("tokens") if isinstance(data, dict) else None
    if not isinstance(toks, list):
        meta["note"] = meta["note"] or "FomoPad registry unreachable"
        return [], meta
    scope = str((data or {}).get("scope") or "")
    m = re.search(r"(\d+)", scope)
    page_cap = int(m.group(1)) if m else None
    meta.update({"status": "ok", "items": len(toks), "scope": scope,
                 "fully_paginated": bool(page_cap and len(toks) < page_cap),
                 "by_protocol": {}})
    for t in toks:
        k = str(t.get("protocol") or "?")
        meta["by_protocol"][k] = meta["by_protocol"].get(k, 0) + 1
    meta["note"] = (f"API scope '{scope}'; returned {len(toks)} (< cap, so this is the full registry)"
                    if meta["fully_paginated"] else f"API scope '{scope}'; may be truncated at the cap")
    return toks, meta


_GPA_ITEM_RE = re.compile(rb'"pubkey":"([1-9A-HJ-NP-Za-km-z]{32,44})","account":\{[^{}]*?"data":\["([A-Za-z0-9+/=]*)"')


def gpa_stream_scan(http: Http, url: str, program: str, offset: int, data_size: int, keep, out: dict[str, str],
                    timeout: float, retries: int = 3) -> tuple[int, str | None]:
    """Memory-light getProgramAccounts: stream the (very large) response to disk, regex-scan it via mmap,
    and keep {pubkey: base58(32-byte slice)} where keep(slice_b58) is true. Returns (accounts scanned, error)."""
    import mmap
    import tempfile
    body = {"jsonrpc": "2.0", "id": 1, "method": "getProgramAccounts", "params": [program, {
        "encoding": "base64", "dataSlice": {"offset": offset, "length": 32}, "filters": [{"dataSize": data_size}]}]}
    last = None
    for attempt in range(retries + 1):
        tmp = tempfile.NamedTemporaryFile(prefix="gpa_", suffix=".json", dir=str(CACHE_DIR) if CACHE_DIR.exists() else None, delete=False)
        try:
            with http.session.post(url, json=body, timeout=timeout, stream=True,
                                   headers={"Content-Type": "application/json"}) as r:
                http._bump(f"rpc_gpa_getProgramAccounts_http_{r.status_code}")
                if r.status_code == 429 or r.status_code >= 500:
                    last = f"HTTP {r.status_code}"; time.sleep(min(30.0, 3 * (2 ** attempt))); continue
                for chunk in r.iter_content(1 << 20):
                    tmp.write(chunk)
            tmp.flush(); tmp.close()
            size = os.path.getsize(tmp.name)
            if size < 64:
                last = "empty response"; continue
            n = 0
            with open(tmp.name, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                head = mm[:200]
                if b'"error"' in head and b'"result"' not in head:
                    last = head.decode("utf-8", "replace")[:160]; time.sleep(min(30.0, 3 * (2 ** attempt))); continue
                for m in _GPA_ITEM_RE.finditer(mm):
                    n += 1
                    try:
                        v = b58encode(base64.b64decode(m.group(2)))
                    except ValueError:
                        continue
                    if keep(v):
                        out[m.group(1).decode()] = v
            return n, None
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {str(e)[:120]}"; time.sleep(min(30.0, 3 * (2 ** attempt)))
        finally:
            try:
                tmp.close(); os.unlink(tmp.name)
            except OSError:
                pass
    with http._lock:
        http.errors.append(f"rpc_gpa {program} {last}")
    return 0, last


def enumerate_fomo_graduates(http: Http, cfg: dict[str, Any], sc: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Every Fomo-launchpad coin: on-chain scan of ALL Meteora DBC pools for '...fomo' base mints, plus the
    FomoPad registry (DBC + Stonk.fun rails). Graduated = DBC pool is_migrated, or registry curve.migrated,
    or a live DEX pair exists (DexScreener/GeckoTerminal) for the mint. Returns (graduates, source metas, summary)."""
    url = str(cfg.get("rpc_url") or DEFAULT_RPC)
    metas: list[dict[str, Any]] = []
    summary: dict[str, Any] = {"definition": ("Fomo launchpad = FomoPad (fomopad.name, independent; not an official FOMO Labs product). "
                                              "Coins have vanity mints ending in 'fomo' and launch on Meteora DBC (or Stonk.fun); "
                                              "DBC graduates migrate to Meteora DAMM v2.")}
    gpa_meta = {"name": "solana_rpc:getProgramAccounts Meteora DBC (all pools, '...fomo' base mint)",
                "url": f"{url} program {METEORA_DBC_PROGRAM}", "status": "error", "fully_paginated": False,
                "pools_scanned": 0, "graduates": 0, "cached": False, "note": None}
    cached = _cache_get("dbc_fomo_pools.json", float(sc["gpa_cache_ttl_hours"]))
    fomo_pools: dict[str, str] = {}
    if isinstance(cached, dict) and cached.get("pools") is not None:
        fomo_pools = cached["pools"]; gpa_meta.update(cached.get("meta") or {}); gpa_meta["cached"] = True
    else:
        scanned, err = gpa_stream_scan(http, url, METEORA_DBC_PROGRAM, DBC_OFF_BASE_MINT, DBC_POOL_SIZE,
                                       lambda mint: mint.endswith(FOMO_SUFFIX), fomo_pools, float(sc["gpa_timeout_sec"]))
        if err is None:
            gpa_meta.update({"status": "ok", "fully_paginated": True, "pools_scanned": scanned})
            _cache_put("dbc_fomo_pools.json", {"pools": fomo_pools, "meta": {k: v for k, v in gpa_meta.items() if k != "cached"}})
        else:
            gpa_meta["note"] = f"DBC getProgramAccounts failed or was refused by the public RPC: {err}"
    # read every fomo DBC pool in full for migration state (always fresh, small)
    dbc: dict[str, dict[str, Any]] = {}
    keys = list(fomo_pools)
    for i in range(0, len(keys), 100):
        batch = keys[i:i + 100]
        vals = rpc_post(http, url, "getMultipleAccounts", [batch, {"encoding": "base64"}], timeout=60, retries=5, source="rpc_dbc")
        if not isinstance(vals, dict):
            continue
        for k, a in zip(batch, vals.get("value") or []):
            if not a:
                continue
            try:
                p = parse_dbc_pool(base64.b64decode(a["data"][0]))
            except (KeyError, IndexError, TypeError, ValueError):
                p = None
            if p:
                p["pool"] = k
                dbc[p["base_mint"]] = p
    registry, reg_meta = fetch_fomopad_registry(http)
    reg_by_mint = {t.get("mint"): t for t in registry if t.get("mint")}
    all_mints = sorted(set(dbc) | set(reg_by_mint))
    # DEX pairs for every fomo mint (graduation evidence for Stonk/registry rails, and cross-check for DBC)
    pairs: dict[str, list[dict[str, Any]]] = {}
    for i in range(0, len(all_mints), 30):
        chunk = all_mints[i:i + 30]
        for attempt in range(5):
            try:
                r = http.session.get(f"{DEX}/tokens/v1/solana/{','.join(chunk)}", timeout=20)
                http._bump(f"dexscreener_http_{r.status_code}")
                if r.status_code == 429:
                    time.sleep(2 * (attempt + 1)); continue
                for pr in (r.json() if r.ok else []) or []:
                    b = (pr.get("baseToken") or {}).get("address")
                    if b:
                        pairs.setdefault(b, []).append(pr)
                break
            except (requests.RequestException, ValueError):
                time.sleep(2 * (attempt + 1))
        time.sleep(0.25)
    grads: dict[str, dict[str, Any]] = {}
    for mint in all_mints:
        p = dbc.get(mint); t = reg_by_mint.get(mint) or {}
        dex_pairs = [x for x in pairs.get(mint, []) if str(x.get("dexId") or "") not in ("meteoradbc", "pumpfun")]
        migrated = bool((p and p["is_migrated"]) or ((t.get("curve") or {}).get("migrated")) or t.get("market"))
        if not (migrated or dex_pairs):
            continue
        best = max(dex_pairs, key=lambda x: fnum((x.get("liquidity") or {}).get("usd"), 0) or 0) if dex_pairs else None
        grads[mint] = {
            "venue": ("meteora_damm_v2" if (p and p["is_migrated"]) else (str(best.get("dexId")) if best else "unknown")),
            "pool": (best or {}).get("pairAddress"),
            "dbc_pool": p["pool"] if p else t.get("pool"),
            "fomopad_registered": mint in reg_by_mint,
            "fomo_protocol": t.get("protocol") or ("DBC" if p else None),
            "graduation_evidence": ("dbc_is_migrated" if (p and p["is_migrated"]) else
                                    "registry_migrated" if migrated else "dex_pair_exists"),
        }
    # graduates DexScreener does not index yet: take pool reserve/volume from GeckoTerminal (Meteora DAMM v2 etc.)
    for mint, g in grads.items():
        if g.get("pool"):
            continue
        for attempt in range(4):
            try:
                r = http.session.get(f"{GECKO}/networks/solana/tokens/{mint}/pools", timeout=20)
                http._bump(f"geckoterminal_http_{r.status_code}")
                if r.status_code == 429:
                    time.sleep(3 * (attempt + 1)); continue
                rows = (r.json() or {}).get("data") or [] if r.ok else []
                rows = [x for x in rows if ((x.get("relationships") or {}).get("dex") or {}).get("data", {}).get("id") != "meteora-dbc"]
                if rows:
                    best = max(rows, key=lambda x: fnum((x.get("attributes") or {}).get("reserve_in_usd"), 0) or 0)
                    a = best.get("attributes") or {}
                    g["pool"] = a.get("address")
                    g["gt_reserve_usd"] = fnum(a.get("reserve_in_usd"))
                    g["onchain_liquidity_est_usd"] = g["gt_reserve_usd"]
                    g["gt_volume_24h"] = fnum((a.get("volume_usd") or {}).get("h24"))
                    g["gt_fdv_usd"] = fnum(a.get("fdv_usd"))
                    g["gt_pool_created_at"] = a.get("pool_created_at")
                    g["gt_dex"] = ((best.get("relationships") or {}).get("dex") or {}).get("data", {}).get("id")
                    g["symbol"] = (a.get("name") or "").split(" / ")[0] or None
                break
            except (requests.RequestException, ValueError):
                time.sleep(3 * (attempt + 1))
        time.sleep(0.5)
    gpa_meta["graduates"] = len(grads)
    gpa_meta["fomo_suffix_dbc_pools"] = len(fomo_pools)
    if gpa_meta["status"] == "ok":
        gpa_meta["note"] = (f"Read all {gpa_meta['pools_scanned']:,} Meteora DBC pools on-chain; {len(fomo_pools)} have a '...fomo' base mint; "
                            f"{sum(1 for v in dbc.values() if v['is_migrated'])} have is_migrated=1 (graduated to DAMM v2).")
    metas.append(gpa_meta)
    reg_grads = sum(1 for m in grads if m in reg_by_mint)
    reg_meta["graduates"] = reg_grads
    metas.append(reg_meta)
    on_curve = len(all_mints) - len(grads)
    summary.update({
        "fomo_suffix_dbc_pools": len(fomo_pools),
        "dbc_pools_read": len(dbc),
        "dbc_migrated": sum(1 for v in dbc.values() if v["is_migrated"]),
        "registry_launches": len(registry),
        "registry_by_protocol": reg_meta.get("by_protocol"),
        "registry_dbc_also_seen_onchain": sum(1 for m, t in reg_by_mint.items() if m in dbc),
        "onchain_not_in_registry": sum(1 for m in dbc if m not in reg_by_mint),
        "total_fomo_coins_seen": len(all_mints),
        "still_on_curve": on_curve,
        "graduated": len(grads),
        "graduated_registered_on_fomopad": reg_grads,
        "max_curve_progress_pct_registry": max([fnum((t.get("curve") or {}).get("progressPercent"), 0) or 0 for t in registry] or [0]),
    })
    return grads, metas, summary


def enumerate_graduated(http: Http, rpc: Rpc, cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Enumerate graduates, drop dead ones, and return alive records enriched with DexScreener data."""
    sc = graduated_scope(cfg)
    sources: list[dict[str, Any]] = []
    universe: dict[str, dict[str, Any]] = {}

    def merge(found: dict[str, dict[str, Any]], label: str) -> int:
        added = 0
        for mint, info in found.items():
            if is_excluded_mint(mint, cfg):
                continue
            rec = universe.get(mint)
            if rec is None:
                rec = universe[mint] = {"mint": mint, "sources": [], "graduated": True}
                added += 1
            if label not in rec["sources"]:
                rec["sources"].append(label)
            for k in ("venue", "pool", "quote_vault", "dbc_pool", "fomopad_registered", "fomo_protocol", "graduation_evidence",
                      "gt_reserve_usd", "gt_volume_24h", "gt_fdv_usd", "gt_pool_created_at", "gt_dex", "symbol",
                      "onchain_liquidity_est_usd"):
                if info.get(k) is not None and rec.get(k) is None:
                    rec[k] = info[k]
            rec["launchpad"] = launchpad_for_mint(mint, rec["sources"])
            if info.get("venue") in ("pumpswap", "raydium_v4") and info.get("quote_vault"):
                rec.setdefault("vaults", {})[info["venue"]] = info["quote_vault"]
        return added

    log("enumerating PumpSwap graduation pools (RPC getProgramAccounts)")
    ps, m = enumerate_pumpswap_graduates(http, cfg, sc)
    m["unique_added"] = merge(ps, "rpc_pumpswap"); sources.append(m)
    if sc["include_raydium_v4_legacy"]:
        rv, m = enumerate_raydium_legacy_graduates(http, cfg, sc)
        m["unique_added"] = merge(rv, "rpc_raydium_v4"); sources.append(m)
    log(f"on-chain graduates so far: {len(universe)}; trying pump.fun API")
    pf, m = enumerate_pumpfun_api(http, sc)
    m["unique_added"] = merge(pf, "pumpfun_api"); sources.append(m)
    log(f"pump.fun API: {m.get('status')}; paging GeckoTerminal")
    gt, metas = enumerate_geckoterminal(http, sc)
    added = merge(gt, "geckoterminal")
    for mm in metas:
        sources.append(mm)
    sources[-1]["unique_added_all_gt_queries"] = added
    ds, m = enumerate_dexscreener_search(http, sc)
    m["unique_added"] = merge(ds, "dexscreener_search"); sources.append(m)
    fomo_summary: dict[str, Any] = {}
    if sc.get("include_fomo", True):
        log("enumerating Fomo (FomoPad) coins: Meteora DBC getProgramAccounts + FomoPad registry")
        fg, fmetas, fomo_summary = enumerate_fomo_graduates(http, cfg, sc)
        added = merge(fg, "fomo_dbc_onchain")
        for mm in fmetas:
            mm["launchpad"] = LAUNCHPAD_FOMO
        fmetas[0]["unique_added"] = added
        sources.extend(fmetas)
        log(f"fomo: {fomo_summary.get('total_fomo_coins_seen')} coins seen, {len(fg)} graduated")
    for mm in sources:
        mm.setdefault("launchpad", LAUNCHPAD_PUMPFUN)

    total = len(universe)
    log(f"total graduated enumerated: {total}")
    now_unix = time.time()
    sol_price = fetch_sol_price(http)

    # 1) on-chain liquidity pre-gate: WSOL vault balance * 2 * SOL price
    vault_map: dict[str, list[str]] = {}
    for rec in universe.values():
        for v in (rec.get("vaults") or {}).values():
            vault_map.setdefault(v, []).append(rec["mint"])
    balances, vault_failed = ({}, list(vault_map))
    if sol_price and vault_map:
        balances, vault_failed = fetch_vault_balances(http, cfg, sc, list(vault_map))
        if vault_failed:
            time.sleep(5)
            b2, vault_failed = fetch_vault_balances(http, cfg, sc, vault_failed)
            balances.update(b2)
    for rec in universe.values():
        vs = list((rec.get("vaults") or {}).values())
        known = [balances[v] for v in vs if v in balances]
        if known and sol_price:
            sol = max(known)
            rec["onchain_quote_sol"] = sol
            rec["onchain_liquidity_est_usd"] = round(2 * sol * sol_price, 2)

    dead_liq_onchain = []
    to_market: list[dict[str, Any]] = []
    for rec in universe.values():
        est = rec.get("onchain_liquidity_est_usd")
        if est is not None and est < float(sc["dead_if_liquidity_below_usd"]):
            rec["liveness"] = "dead_low_liquidity"
            dead_liq_onchain.append(rec)
        else:
            to_market.append(rec)

    log(f"vault reads ok={len(balances)} unread={len(vault_failed)}; dead by on-chain liquidity={len(dead_liq_onchain)}; "
        f"DexScreener batch for {len(to_market)}")
    # 2) DexScreener batch market data for every survivor of the on-chain gate
    dex_stats = enrich_with_dexscreener(http, to_market, cfg, force=True, retries=5, workers=4)
    dex_missing = sum(1 for r in to_market if r.get("liquidity_usd") is None and r.get("market_cap_usd") is None)

    # 3) liveness: liquidity cutoff + 7-day activity (pool signatures when 24h shows no trades)
    alive: list[dict[str, Any]] = []
    dead_liq_dex, dead_inactive, unknown_activity = 0, 0, 0
    pending = []
    for rec in to_market:
        st = classify_liveness(rec, sc, now_unix)
        if st == "needs_activity_check":
            pending.append(rec)
        elif st == "alive":
            rec["liveness"] = "alive"; alive.append(rec)
        elif st == "dead_low_liquidity":
            rec["liveness"] = st; dead_liq_dex += 1
        else:
            rec["liveness"] = st; dead_inactive += 1
    log(f"alive so far={len(alive)}; dead liq(dex)={dead_liq_dex}; activity checks needed={len(pending)}")
    checked = 0
    cache_hits = 0
    act_cache = _cache_get("activity.json", 24 * 365) or {}
    ttl = float(sc["activity_cache_ttl_hours"]) * 3600
    to_check: list[tuple[dict[str, Any], str]] = []
    for rec in pending:
        addr = rec.get("pair_address") or rec.get("pool")
        hit = act_cache.get(addr) if addr else None
        if hit and now_unix - float(hit.get("checked_at") or 0) < ttl and hit.get("last") is not None:
            # 24h txns are still zero, so the cached last-activity time is still the latest trade
            rec["last_activity_unix"] = hit["last"]
            cache_hits += 1
        elif addr and len(to_check) < int(sc["activity_check_max"]):
            to_check.append((rec, addr))
    rpc_url = str(cfg.get("rpc_url") or DEFAULT_RPC)

    def _act(item: tuple[dict[str, Any], str]) -> tuple[dict[str, Any], str, int | None]:
        rec_, addr_ = item
        res = rpc_post(http, rpc_url, "getSignaturesForAddress", [addr_, {"limit": 1}],
                       timeout=20, retries=4, source="rpc_activity")
        bt = res[0].get("blockTime") if isinstance(res, list) and res and isinstance(res[0], dict) else None
        if isinstance(res, list) and not res:
            bt = 0  # pool account has no signatures at all
        return rec_, addr_, (int(bt) if isinstance(bt, (int, float)) else None)

    with ThreadPoolExecutor(max_workers=max(1, int(sc["activity_check_workers"]))) as ex:
        for rec_, addr_, bt in ex.map(_act, to_check):
            checked += 1
            rec_["last_activity_unix"] = bt
            if bt is not None:
                act_cache[addr_] = {"last": bt, "checked_at": now_unix}
    for rec in pending:
        st = classify_liveness(rec, sc, now_unix)
        if st == "needs_activity_check":
            rec["liveness"] = "alive_activity_unknown"; unknown_activity += 1; alive.append(rec)
        elif st == "alive":
            rec["liveness"] = "alive"; alive.append(rec)
        elif st == "dead_inactive":
            rec["liveness"] = st; dead_inactive += 1
        else:
            rec["liveness"] = st; dead_liq_dex += 1

    _cache_put("activity.json", act_cache)
    log(f"liveness done: alive={len(alive)} dead_inactive={dead_inactive} unknown={unknown_activity}")
    meta = {
        "graduated_scope": {k: sc[k] for k in ("dead_if_liquidity_below_usd", "dead_if_no_activity_days",
                                                 "require_canonical_pool_pda", "include_raydium_v4_legacy",
                                                 "include_legacy_feeds")},
        "dead_rule": (f"dead = liquidity < ${float(sc['dead_if_liquidity_below_usd']):,.0f} "
                      f"(on-chain WSOL vault x2 x SOL price, then DexScreener) OR no trade/pool tx in last "
                      f"{sc['dead_if_no_activity_days']} days (DexScreener 24h txns, else pool's last signature via RPC)"),
        "sol_price_usd": sol_price,
        "sources": sources,
        "total_graduated_enumerated": total,
        "by_venue": {
            "pumpswap": sum(1 for r in universe.values() if "rpc_pumpswap" in r["sources"]),
            "raydium_v4_legacy": sum(1 for r in universe.values() if "rpc_raydium_v4" in r["sources"]),
            "api_only": sum(1 for r in universe.values() if not (set(r["sources"]) & {"rpc_pumpswap", "rpc_raydium_v4", "fomo_dbc_onchain"})),
            "fomo_meteora": sum(1 for r in universe.values() if "fomo_dbc_onchain" in r["sources"]),
        },
        "vault_reads": {"vaults": len(vault_map), "read_ok": len(balances), "unread": len(vault_failed)},
        "dropped_dead": {
            "total": len(dead_liq_onchain) + dead_liq_dex + dead_inactive,
            "low_liquidity_onchain": len(dead_liq_onchain),
            "low_liquidity_dexscreener": dead_liq_dex,
            "inactive_7d": dead_inactive,
        },
        "alive": len(alive),
        "alive_activity_unknown": unknown_activity,
        "activity_checks_rpc": checked,
        "activity_checks_cached": cache_hits,
        "sent_to_dexscreener_batch": len(to_market),
        "no_dexscreener_pair": dex_missing,
        "dexscreener_batches": dex_stats,
        "_universe_mints": set(universe),
        "fomo": fomo_summary,
        "by_launchpad": launchpad_counts(list(universe.values())),
    }
    return alive, meta


LAUNCHPADS = (LAUNCHPAD_PUMPFUN, LAUNCHPAD_FOMO)


def launchpad_counts(universe: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    out = {lp: {"enumerated": 0, "alive": 0, "dead": 0} for lp in LAUNCHPADS}
    for r in universe:
        lp = r.get("launchpad") or launchpad_for_mint(r.get("mint"), r.get("sources"))
        d = out.setdefault(lp, {"enumerated": 0, "alive": 0, "dead": 0})
        d["enumerated"] += 1
        lv = str(r.get("liveness") or "")
        if lv.startswith("alive"):
            d["alive"] += 1
        elif lv.startswith("dead"):
            d["dead"] += 1
    return out


def coin_links(rec: dict[str, Any]) -> dict[str, str]:
    mint = rec["mint"]
    lp = rec.get("launchpad") or launchpad_for_mint(mint, rec.get("sources"))
    links = {
        "dexscreener": rec.get("dexscreener_url") or f"https://dexscreener.com/solana/{mint}",
        "solscan": f"https://solscan.io/token/{mint}",
        "rugcheck": f"https://rugcheck.xyz/tokens/{mint}",
    }
    if lp == LAUNCHPAD_FOMO:
        links["fomo"] = FOMO_APP_TOKEN_URL.format(mint=mint)
        if rec.get("fomopad_registered"):
            links["fomopad"] = f"{FOMOPAD_SITE}/app#token/{mint}"
    elif lp == LAUNCHPAD_PUMPFUN:
        links["pumpfun"] = f"https://pump.fun/coin/{mint}"
    return links


def build_top_potential(inspected: list[dict[str, Any]], n: int = 15) -> list[dict[str, Any]]:
    """All fully inspected GRADUATED coins (pump.fun + Fomo launchpads) ranked by upside score; top n.
    Legacy trending-feed extras (not graduates of either launchpad) are excluded."""
    rows = [r for r in inspected if r.get("graduated") and not inspection_incomplete(r)
            and (r.get("upside") or {}).get("score") is not None]
    rows.sort(key=lambda r: (-(r.get("upside") or {}).get("score", 0), not r.get("passed_all_three"),
                             -(fnum(r.get("volume_used_usd"), 0) or 0)))
    out = []
    for i, r in enumerate(rows[:n], 1):
        out.append({
            "rank": i,
            "symbol": r.get("symbol"),
            "name": r.get("name"),
            "mint": r.get("mint"),
            "launchpad": r.get("launchpad") or launchpad_for_mint(r.get("mint"), r.get("sources")),
            "upside_score": (r.get("upside") or {}).get("score"),
            "market_cap_usd": r.get("mcap_used_usd"),
            "liquidity_usd": r.get("liquidity_used_usd"),
            "volume_24h_usd": r.get("volume_used_usd"),
            "price_change_24h_pct": r.get("change_24h_used_pct"),
            "age_hours": r.get("age_hours"),
            "passed_all_three": bool(r.get("passed_all_three")),
            "failed_reasons": [f"Criterion {f.get('criterion')} ({f.get('name')}): {f.get('reason')}" for f in (r.get("failed_criteria") or [])],
            "why_this_could_fail": r.get("why_this_could_fail") or [],
            "links": r.get("links") or coin_links(r),
        })
    return out


def inspection_incomplete(rec: dict[str, Any]) -> str | None:
    """Why a deep inspection must be retried (RugCheck/RPC unavailable or exception), else None."""
    for f in rec.get("failed_criteria") or []:
        if f.get("name") == "inspect_error":
            return "exception"
    if rec.get("rugcheck_report_missing"):
        return "rugcheck_unavailable"
    return None


def is_excluded_mint(mint: str, cfg: dict[str, Any]) -> bool:
    excluded = {m.strip() for m in (cfg.get("excluded_mints") or []) if m}
    return mint in excluded


def is_excluded_symbol(symbol: str | None, cfg: dict[str, Any]) -> bool:
    if not symbol:
        return False
    excluded = {s.strip().upper() for s in (cfg.get("excluded_symbols") or []) if s}
    return symbol.strip().upper() in excluded


def effective_mcap(rec: dict[str, Any]) -> float | None:
    if rec.get("market_cap_usd") is not None:
        return fnum(rec.get("market_cap_usd"))
    return fnum(rec.get("fdv_usd") or rec.get("gt_mcap_usd") or rec.get("gt_fdv_usd"))


def pair_age_hours(rec: dict[str, Any], now: datetime | None = None) -> float | None:
    now = now or now_utc()
    ms = rec.get("oldest_pair_created_at_ms") or rec.get("pair_created_at_ms")
    if isinstance(ms, (int, float)) and ms > 0:
        created = datetime.fromtimestamp(ms / 1000.0, tz=UTC)
        return max(0.0, (now - created).total_seconds() / 3600.0)
    iso = rec.get("gt_pool_created_at")
    dt = parse_iso(iso) if isinstance(iso, str) else None
    if dt:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return max(0.0, (now - dt).total_seconds() / 3600.0)
    return None


def prefilter_reason(rec: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    """Return a skip reason, or None if the coin should be deep-inspected.

    Cheap filters run BEFORE RugCheck/RPC so a 15-minute scan stays fast.
    """
    mint = rec.get("mint") or ""
    if is_excluded_mint(mint, cfg):
        return "excluded_mint"
    if is_excluded_symbol(rec.get("symbol"), cfg):
        return "excluded_symbol"
    mcap = effective_mcap(rec)
    if mcap is None:
        return "missing_market_cap"
    if mcap < float(cfg["market_cap_min_usd"]):
        return f"mcap_below_band:${mcap:,.0f}"
    if mcap > float(cfg["market_cap_max_usd"]):
        return f"mcap_above_band:${mcap:,.0f}"
    liq = fnum(rec.get("liquidity_usd"), rec.get("gt_reserve_usd") or rec.get("curve_reserve_usd"))
    if liq is None:
        return "missing_liquidity"
    min_liq = float(cfg["min_liquidity_usd"])
    if rec.get("near_graduation") and not rec.get("graduated"):
        min_liq = min(min_liq, float(cfg.get("near_grad_min_liquidity_usd") or min_liq))
    if liq < min_liq:
        return f"low_liquidity:${liq:,.0f}"
    vol = fnum(rec.get("volume_24h_usd"), rec.get("gt_volume_24h"))
    if vol is None:
        return "missing_volume"
    if vol < float(cfg["min_volume_24h_usd"]):
        return f"low_volume:${vol:,.0f}"
    age = pair_age_hours(rec)
    if age is not None and age < float(cfg["min_pair_age_hours"]):
        return f"pair_too_young:{age:.1f}h"
    chg = fnum(rec.get("price_change_24h_pct"), rec.get("gt_change_24h"))
    if chg is not None and chg > float(cfg["max_price_change_24h_pct"]):
        return f"parabolic_24h:+{chg:.0f}%"
    return None


def prefilter(candidates: list[dict[str, Any]], cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept, skipped = [], []
    for rec in candidates:
        rec["mcap_used_usd"] = effective_mcap(rec)
        rec["liquidity_used_usd"] = fnum(rec.get("liquidity_usd"), rec.get("gt_reserve_usd") if rec.get("gt_reserve_usd") is not None else rec.get("curve_reserve_usd"))
        rec["volume_used_usd"] = fnum(rec.get("volume_24h_usd"), rec.get("gt_volume_24h"))
        rec["change_24h_used_pct"] = fnum(rec.get("price_change_24h_pct"), rec.get("gt_change_24h"))
        rec["age_hours"] = pair_age_hours(rec)
        reason = prefilter_reason(rec, cfg)
        if reason:
            rec["skip_reason"] = reason
            skipped.append(rec)
        else:
            rec["skip_reason"] = None
            kept.append(rec)

    def rank(r: dict[str, Any]) -> tuple:
        mcap = r.get("mcap_used_usd") or 0
        vol = r.get("volume_used_usd") or 0
        liq = r.get("liquidity_used_usd") or 0
        sweet = 1 if 500_000 <= mcap <= 5_000_000 else 0
        return (sweet, vol, liq, len(r.get("sources") or []))

    kept.sort(key=rank, reverse=True)
    raw_cap = cfg.get("max_candidates_per_run")
    if cfg.get("_unlimited_candidates") or raw_cap in (0, None, "all"):
        return kept, skipped
    cap = int(raw_cap)
    deep, overflow = kept[:cap], kept[cap:]
    for r in overflow:
        r["skip_reason"] = "beyond_max_candidates"
    return deep, skipped + overflow

def fetch_rugcheck(http: Http, mint: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    notes = []
    report = http.get(f"{RUGCHECK}/tokens/{mint}/report", "rugcheck", retries=4, backoff=1.5)
    summary = http.get(f"{RUGCHECK}/tokens/{mint}/report/summary", "rugcheck", retries=4, backoff=1.5)
    if report is None:
        notes.append("RugCheck /report unavailable or rate-limited")
    if summary is None:
        notes.append("RugCheck /report/summary unavailable or rate-limited")
    return (report if isinstance(report, dict) else None,
            summary if isinstance(summary, dict) else None,
            notes)


def risk_name_level(risk: dict[str, Any]) -> tuple[str, str]:
    name = str(risk.get("name") or risk.get("description") or "").strip()
    level = str(risk.get("level") or "").strip().lower()
    return name, level


def is_bundle_risk(risk: dict[str, Any]) -> bool:
    name, level = risk_name_level(risk)
    blob = f"{name} {risk.get('description') or ''}".lower()
    if any(n in blob for n in BUNDLE_RISK_NEEDLES):
        return True
    if level in DANGER_LEVELS and "mutable" not in blob:
        return True
    return False


def _report_supply(report: dict[str, Any]) -> float | None:
    token = report.get("token") if isinstance(report.get("token"), dict) else {}
    supply = token.get("supply")
    try:
        return float(supply) if supply is not None else None
    except (TypeError, ValueError):
        return None


def _report_decimals(report: dict[str, Any]) -> int:
    token = report.get("token") if isinstance(report.get("token"), dict) else {}
    try:
        return int(token.get("decimals") or 0)
    except (TypeError, ValueError):
        return 0


def check_bundle(rec: dict[str, Any], report: dict[str, Any] | None, rpc: Rpc | None, cfg: dict[str, Any]) -> dict[str, Any]:
    """Criterion 1 - low or no bundle activity."""
    checks: list[dict[str, Any]] = []
    fail_reasons: list[str] = []
    warnings: list[str] = []

    if report is None:
        checks.append({"name": "rugcheck_report", "result": "unavailable", "detail": "Could not fetch RugCheck report"})
        return {
            "passed": False,
            "reason": "RugCheck report unavailable; cannot verify bundle activity",
            "checks": checks,
            "warnings": ["Bundle verification incomplete without RugCheck"],
            "rpc_launch_check": {"performed": False, "reason": "skipped; rugcheck missing"},
        }

    risks = report.get("risks") if isinstance(report.get("risks"), list) else []
    bundle_hits = [r for r in risks if isinstance(r, dict) and is_bundle_risk(r)]
    mutable = [r for r in risks if isinstance(r, dict) and "mutable" in (r.get("name") or "").lower()]
    checks.append({
        "name": "rugcheck_risks",
        "result": "fail" if bundle_hits else "pass",
        "risk_count": len(risks),
        "bundle_hits": [
            {"name": r.get("name"), "level": r.get("level"), "description": r.get("description"), "value": r.get("value")}
            for r in bundle_hits
        ],
        "mutable_metadata": bool(mutable or ((report.get("tokenMeta") or {}).get("mutable") is True)),
    })
    if bundle_hits:
        names = ", ".join(str(r.get("name") or "risk") for r in bundle_hits)
        fail_reasons.append(f"RugCheck bundle/safety flags: {names}")
    if mutable or ((report.get("tokenMeta") or {}).get("mutable") is True):
        warnings.append("Mutable metadata: the team can still change name/image/links")

    if report.get("rugged") is True:
        checks.append({"name": "rugged_flag", "result": "fail"})
        fail_reasons.append("RugCheck marked this token as rugged")
    else:
        checks.append({"name": "rugged_flag", "result": "pass", "rugged": False})

    supply = _report_supply(report)
    networks = report.get("insiderNetworks") if isinstance(report.get("insiderNetworks"), list) else []
    large = []
    huge = []
    size_cut = int(cfg.get("bundle_large_network_size") or 50)
    hold_cut = float(cfg.get("bundle_large_network_holding_pct") or 3)
    huge_cut = int(cfg.get("bundle_huge_network_size") or 100)
    for n in networks:
        if not isinstance(n, dict):
            continue
        size = int(n.get("size") or n.get("activeAccounts") or 0)
        holding = n.get("currentHolding") or 0
        pct = (100.0 * float(holding) / supply) if supply and holding else 0.0
        if size >= huge_cut and pct >= 0.5:
            huge.append({"id": n.get("id"), "size": size, "holding_pct": round(pct, 3)})
        elif size >= size_cut and pct >= hold_cut:
            large.append({"id": n.get("id"), "size": size, "holding_pct": round(pct, 3)})
    checks.append({
        "name": "insider_network_clusters",
        "result": "fail" if (large or huge) else "pass",
        "network_count": len(networks),
        "graph_insiders_detected": report.get("graphInsidersDetected"),
        "large_clusters": large,
        "huge_clusters": huge,
    })
    if huge:
        fail_reasons.append(
            "Huge coordinated wallet cluster(s): "
            + "; ".join(f"{h['id']} size={h['size']} holding={h['holding_pct']:.2f}%" for h in huge)
        )
    if large:
        fail_reasons.append(
            "Large insider/bundle-like cluster(s): "
            + "; ".join(f"{h['id']} size={h['size']} holding={h['holding_pct']:.2f}%" for h in large)
        )

    launch = {"performed": False, "reason": "not attempted"}
    age = rec.get("age_hours")
    if rpc is not None and age is not None and age <= 72:
        target = rec.get("pair_address") or rec.get("mint")
        sigs = rpc.signatures(target, limit=40)
        if sigs is None:
            launch = {"performed": False, "reason": "RPC getSignaturesForAddress unavailable or rate-limited", "target": target}
            warnings.append("RPC rate-limit or error prevented early-slot bundle verification")
        else:
            slots = [s.get("slot") for s in sigs if isinstance(s, dict) and s.get("slot") is not None]
            times = [s.get("blockTime") for s in sigs if isinstance(s, dict) and s.get("blockTime")]
            launch = {
                "performed": True,
                "target": target,
                "signatures_fetched": len(sigs),
                "unique_slots_in_batch": len(set(slots)),
                "oldest_block_time": min(times) if times else None,
                "newest_block_time": max(times) if times else None,
                "note": "Signature list has no signers; full early-buyer clustering would need getTransaction and was not run",
            }
            checks.append({"name": "rpc_launch_signatures", "result": "info", "detail": launch})
    else:
        reason = "pair older than 72h; skipped expensive launch-signature walk" if (age or 0) > 72 else "no RPC or unknown pair age"
        launch = {"performed": False, "reason": reason}
        if rpc is None:
            warnings.append("No RPC client; early-slot bundle verification not performed")

    passed = not fail_reasons
    return {
        "passed": passed,
        "reason": "; ".join(fail_reasons) if fail_reasons else "No bundled-supply, sniper, copycat, or large coordinated-cluster flags",
        "checks": checks,
        "warnings": warnings,
        "rpc_launch_check": launch,
    }

def detect_uniform_wallet_cluster(wallets: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Flag a split-bundle pattern: many non-LP wallets holding nearly identical shares."""
    min_size = int(cfg.get("uniform_cluster_min_wallets") or 8)
    min_total = float(cfg.get("uniform_cluster_min_total_pct") or 6)
    if len(wallets) < min_size:
        return None
    best: list[dict[str, Any]] = []
    for w in wallets:
        base = float(w.get("pct") or 0)
        if base < 0.3:
            continue
        tol = max(0.15, 0.12 * base)
        group = [x for x in wallets if abs(float(x.get("pct") or 0) - base) <= tol]
        if len(group) > len(best):
            best = group
    if len(best) < min_size:
        return None
    total = sum(float(x.get("pct") or 0) for x in best)
    if total < min_total:
        return None
    return {
        "size": len(best),
        "avg_pct": round(total / len(best), 3),
        "min_pct": round(min(float(x["pct"]) for x in best), 3),
        "max_pct": round(max(float(x["pct"]) for x in best), 3),
        "total_pct": round(total, 3),
    }


def classify_holder(address: str, owner: str | None, report: dict[str, Any]) -> str | None:
    """Return an exclusion tag (lp / burn / known vault) or None if this looks like a wallet."""
    owner = owner or ""
    if owner in BURN_OWNERS or address in BURN_OWNERS:
        return "burn"
    markets = report.get("markets") if isinstance(report.get("markets"), list) else []
    market_pubkeys = set()
    liq_accounts = set()
    for m in markets:
        if not isinstance(m, dict):
            continue
        if m.get("pubkey"):
            market_pubkeys.add(m["pubkey"])
        for k in ("liquidityA", "liquidityB", "mintLP"):
            if m.get(k):
                liq_accounts.add(m[k])
    if owner in market_pubkeys or address in market_pubkeys:
        return "lp_market"
    if address in liq_accounts:
        return "lp_vault"
    known = report.get("knownAccounts") if isinstance(report.get("knownAccounts"), dict) else {}
    for key in (address, owner):
        info = known.get(key) if key else None
        if not isinstance(info, dict):
            continue
        typ = str(info.get("type") or "").upper()
        name = str(info.get("name") or "")
        if typ == "CREATOR":
            continue
        if typ == "BURN" or "burn" in name.lower() or "incinerator" in name.lower():
            return f"known_burn:{name or typ}"
        if typ in LP_ACCOUNT_TYPES:
            return f"known_{typ.lower()}:{name or typ}"
        lname = f" {name.lower()} "
        if any(k in lname or k.strip() in name.lower() for k in LP_NAME_KEYWORDS):
            return f"known_name:{name}"
    return None


def _ui_amount(item: dict[str, Any], decimals: int | None = None) -> float | None:
    # Prefer raw amount / decimals: RugCheck uiAmount is sometimes inconsistent with uiAmountString.
    if item.get("amount") is not None:
        try:
            raw = float(item["amount"])
            dec = decimals if decimals is not None else int(item.get("decimals") or 0)
            return raw / (10 ** dec) if dec else raw
        except (TypeError, ValueError):
            pass
    if item.get("uiAmountString") is not None:
        return fnum(item.get("uiAmountString"))
    return fnum(item.get("uiAmount"))


def _parse_token_account(acc: Any) -> dict[str, Any] | None:
    if not isinstance(acc, dict):
        return None
    data = acc.get("data")
    parsed = (data or {}).get("parsed") if isinstance(data, dict) else None
    info = (parsed or {}).get("info") if isinstance(parsed, dict) else None
    if not isinstance(info, dict):
        return None
    tok = info.get("tokenAmount") if isinstance(info.get("tokenAmount"), dict) else {}
    return {
        "owner": info.get("owner"),
        "mint": info.get("mint"),
        "ui_amount": fnum(tok.get("uiAmountString"), tok.get("uiAmount")),
    }


def collect_live_holders(
    mint: str,
    report: dict[str, Any] | None,
    rpc: Rpc | None,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Prefer live Solana RPC balances; fall back to RugCheck snapshots with a stale warning."""
    out: dict[str, Any] = {
        "source": None,
        "supply_ui": None,
        "supply_raw": None,
        "decimals": None,
        "holders": [],
        "notes": [],
    }
    rug_holders = (report or {}).get("topHolders") if isinstance((report or {}).get("topHolders"), list) else []
    decimals = _report_decimals(report or {})
    supply_raw = _report_supply(report or {})

    if rpc is not None:
        rpc_supply = rpc.token_supply(mint)
        if rpc_supply:
            out["decimals"] = int(rpc_supply.get("decimals") or decimals or 0)
            out["supply_raw"] = fnum(rpc_supply.get("amount"))
            out["supply_ui"] = fnum(rpc_supply.get("uiAmountString"), rpc_supply.get("uiAmount"))
        else:
            out["notes"].append("RPC getTokenSupply unavailable or rate-limited")

    if out["decimals"] is None:
        out["decimals"] = decimals
    if out["supply_raw"] is None:
        out["supply_raw"] = supply_raw
        if supply_raw is not None and out["decimals"] is not None:
            out["supply_ui"] = supply_raw / (10 ** out["decimals"])
            out["notes"].append("Supply taken from RugCheck snapshot (RPC supply missing)")

    largest = None
    if rpc is not None and not (cfg.get("skip_largest_accounts_after_first_429") and rpc.largest_blocked):
        largest = rpc.token_largest(mint)
        if largest:
            out["source"] = "rpc_getTokenLargestAccounts"
            holders = []
            for item in largest:
                addr = item.get("address")
                amt = _ui_amount(item, out["decimals"])
                holders.append({
                    "address": addr,
                    "owner": item.get("owner") or addr,
                    "ui_amount": amt,
                })
            addrs = [h["address"] for h in holders if h.get("address")]
            accs = rpc.multiple_accounts(addrs) if addrs else None
            if accs:
                for h, acc in zip(holders, accs):
                    parsed = _parse_token_account(acc)
                    if parsed:
                        h["owner"] = parsed.get("owner") or h["owner"]
                        if parsed.get("ui_amount") is not None:
                            h["ui_amount"] = parsed["ui_amount"]
            out["holders"] = holders
            return out
        if rpc is not None and rpc.largest_blocked:
            out["notes"].append("RPC getTokenLargestAccounts returned 429 (method rate-limited on this public endpoint)")

    extra_addrs: list[str] = []
    if report:
        for m in (report.get("markets") or []):
            if not isinstance(m, dict):
                continue
            for k in ("liquidityA", "liquidityB"):
                if m.get(k):
                    extra_addrs.append(m[k])
    if rpc is not None and (rug_holders or extra_addrs):
        seed: list[dict[str, Any]] = list(rug_holders)
        seen_addr = {h.get("address") for h in seed if h.get("address")}
        for addr in extra_addrs:
            if addr and addr not in seen_addr:
                seen_addr.add(addr)
                seed.append({"address": addr, "owner": None})
        addrs = [h.get("address") for h in seed if h.get("address")]
        accs = rpc.multiple_accounts(addrs)
        if accs is not None:
            holders = []
            for h, acc in zip(seed, accs):
                parsed = _parse_token_account(acc)
                if parsed:
                    holders.append({
                        "address": h.get("address"),
                        "owner": parsed.get("owner") or h.get("owner"),
                        "ui_amount": parsed.get("ui_amount"),
                    })
                else:
                    out["notes"].append(f"Live RPC returned empty account for {h.get('address')}")
            out["source"] = "rpc_getMultipleAccounts_on_rugcheck_top_accounts"
            out["holders"] = holders
            out["notes"].append(
                "Live balances refreshed via RPC; the top-N set itself comes from RugCheck "
                "(a larger wallet not in that snapshot could be missed)"
            )
            return out
        out["notes"].append("RPC getMultipleAccounts failed; falling back to RugCheck holder snapshot")

    if rug_holders:
        holders = []
        for h in rug_holders:
            holders.append({
                "address": h.get("address"),
                "owner": h.get("owner"),
                "ui_amount": _ui_amount(h, out["decimals"]),
                "rugcheck_pct": fnum(h.get("pct")),
                "insider_flag": bool(h.get("insider")),
            })
        out["source"] = "rugcheck_topHolders_snapshot_may_be_stale"
        out["holders"] = holders
        out["notes"].append("Using RugCheck holder snapshot because live RPC balances were not reachable")
        return out

    out["source"] = "unavailable"
    out["notes"].append("No holder data from RPC or RugCheck")
    return out


def compute_lp_lock(report: dict[str, Any], summary: dict[str, Any] | None) -> dict[str, Any]:
    markets = report.get("markets") if isinstance(report.get("markets"), list) else []
    rows = []
    unlocked_secondary = []
    for m in markets:
        if not isinstance(m, dict):
            continue
        lp = m.get("lp") if isinstance(m.get("lp"), dict) else {}
        usd = (fnum(lp.get("baseUSD"), 0.0) or 0.0) + (fnum(lp.get("quoteUSD"), 0.0) or 0.0)
        pct = fnum(lp.get("lpLockedPct"))
        rows.append({
            "pubkey": m.get("pubkey"),
            "market_type": m.get("marketType"),
            "usd": usd,
            "lp_locked_pct": pct,
        })
    rows.sort(key=lambda r: r["usd"], reverse=True)
    total = sum(r["usd"] for r in rows)
    weighted = None
    if total > 0:
        weighted = sum(r["usd"] * (r["lp_locked_pct"] or 0.0) for r in rows) / total
    summary_pct = fnum((summary or {}).get("lpLockedPct"))
    if weighted is None:
        weighted = summary_pct
    primary = rows[0] if rows else None
    if primary and primary["usd"] > 0:
        for r in rows[1:]:
            if r["usd"] >= max(1000.0, 0.05 * primary["usd"]) and (r["lp_locked_pct"] is None or r["lp_locked_pct"] < 50):
                unlocked_secondary.append({
                    "pubkey": r["pubkey"],
                    "market_type": r["market_type"],
                    "usd": round(r["usd"], 2),
                    "lp_locked_pct": r["lp_locked_pct"],
                })
    return {
        "lp_locked_pct": round(weighted, 3) if weighted is not None else None,
        "lp_locked_pct_rugcheck_summary": summary_pct,
        "primary_lp_locked_pct": (round(primary["lp_locked_pct"], 3) if primary and primary["lp_locked_pct"] is not None else None),
        "primary_pool": (
            {"pubkey": primary["pubkey"], "market_type": primary["market_type"], "usd": round(primary["usd"], 2)}
            if primary else None
        ),
        "unlocked_secondary_pools": unlocked_secondary[:6],
        "market_count": len(rows),
    }


def check_concentration(
    rec: dict[str, Any],
    report: dict[str, Any] | None,
    summary: dict[str, Any] | None,
    live: dict[str, Any],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Criterion 2 - low insider concentration, revoked authorities, locked/burned LP."""
    fail: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, Any] = {
        "holder_source": live.get("source"),
        "holder_notes": live.get("notes") or [],
        "top10_non_lp_pct": None,
        "max_single_non_lp_pct": None,
        "insider_network_pct": None,
        "insider_network_pct_may_overlap": True,
        "creator_pct": None,
        "creator": (report or {}).get("creator"),
        "mint_authority": None,
        "freeze_authority": None,
        "lp_locked_pct": None,
        "primary_lp_locked_pct": None,
        "primary_pool": None,
        "unlocked_secondary_pools": [],
        "excluded_holders": [],
        "wallet_holders": [],
        "total_holders": (report or {}).get("totalHolders"),
    }

    if report is None and live.get("source") in (None, "unavailable"):
        return {
            "passed": False,
            "reason": "No RugCheck report and no live RPC holder data; cannot verify concentration",
            "metrics": metrics,
            "warnings": warnings,
        }

    supply_ui = live.get("supply_ui")
    holders = live.get("holders") or []
    excluded = []
    wallets = []
    if supply_ui and supply_ui > 0 and holders:
        for h in holders:
            amt = fnum(h.get("ui_amount"), 0.0) or 0.0
            pct = 100.0 * amt / supply_ui
            tag = classify_holder(h.get("address") or "", h.get("owner"), report or {})
            row = {
                "address": h.get("address"),
                "owner": h.get("owner"),
                "ui_amount": amt,
                "pct": round(pct, 4),
                "tag": tag,
            }
            if tag:
                excluded.append(row)
            else:
                wallets.append(row)
        wallets.sort(key=lambda r: r["pct"], reverse=True)
        top10 = wallets[:10]
        metrics["wallet_holders"] = wallets[:15]
        metrics["excluded_holders"] = [
            {"owner": e["owner"], "pct": e["pct"], "tag": e["tag"]} for e in excluded[:15]
        ]
        metrics["top10_non_lp_pct"] = round(sum(w["pct"] for w in top10), 3) if top10 else 0.0
        metrics["max_single_non_lp_pct"] = round(top10[0]["pct"], 3) if top10 else 0.0
        if metrics["top10_non_lp_pct"] > float(cfg["max_top10_non_lp_pct"]):
            fail.append(
                f"Top-10 non-LP wallets hold {metrics['top10_non_lp_pct']:.2f}% "
                f"(max {cfg['max_top10_non_lp_pct']}%)"
            )
        if metrics["max_single_non_lp_pct"] > float(cfg["max_single_non_lp_wallet_pct"]):
            fail.append(
                f"Largest non-LP wallet holds {metrics['max_single_non_lp_pct']:.2f}% "
                f"(max {cfg['max_single_non_lp_wallet_pct']}%)"
            )
    else:
        fail.append("Could not compute live (or snapshot) wallet concentration")

    if report is not None:
        supply_raw = live.get("supply_raw") or _report_supply(report)
        nets = report.get("insiderNetworks") if isinstance(report.get("insiderNetworks"), list) else []
        total_hold = 0.0
        for n in nets:
            if isinstance(n, dict) and n.get("currentHolding") is not None:
                try:
                    total_hold += float(n["currentHolding"])
                except (TypeError, ValueError):
                    pass
        if supply_raw and supply_raw > 0:
            insider_pct = 100.0 * total_hold / float(supply_raw)
            metrics["insider_network_pct_raw"] = round(insider_pct, 3)
            metrics["insider_network_pct"] = round(min(insider_pct, 100.0), 3)
            metrics["insider_network_count"] = len(nets)
            metrics["graph_insiders_detected"] = report.get("graphInsidersDetected")
            if insider_pct > float(cfg["max_insider_network_pct"]):
                fail.append(
                    f"Insider networks hold ~{metrics['insider_network_pct']:.2f}% of supply "
                    f"(max {cfg['max_insider_network_pct']}%; sum may overlap)"
                )
        else:
            metrics["insider_network_pct"] = None
            warnings.append("Could not convert insider-network holdings to a % of supply")

        creator_bal = fnum((report or {}).get("creatorBalance"), 0.0) or 0.0
        if supply_ui and creator_bal > 0:
            if creator_bal > (supply_ui * 2) and live.get("decimals") is not None:
                creator_ui = creator_bal / (10 ** int(live["decimals"]))
            else:
                creator_ui = creator_bal
            metrics["creator_pct"] = round(100.0 * creator_ui / supply_ui, 3)
        else:
            metrics["creator_pct"] = 0.0 if supply_ui else None
        if metrics["creator_pct"] is not None and metrics["creator_pct"] > float(cfg["max_creator_pct"]):
            fail.append(f"Creator still holds {metrics['creator_pct']:.2f}% (max {cfg['max_creator_pct']}%)")

        token = report.get("token") if isinstance(report.get("token"), dict) else {}
        mint_auth = report.get("mintAuthority") if "mintAuthority" in report else token.get("mintAuthority")
        freeze_auth = report.get("freezeAuthority") if "freezeAuthority" in report else token.get("freezeAuthority")
        metrics["mint_authority"] = mint_auth
        metrics["freeze_authority"] = freeze_auth
        if cfg.get("require_mint_authority_revoked", True) and mint_auth not in (None, "", SYSTEM_BURN):
            fail.append("Mint authority is still active (token supply can be inflated)")
        if cfg.get("require_freeze_authority_revoked", True) and freeze_auth not in (None, "", SYSTEM_BURN):
            fail.append("Freeze authority is still active (accounts can be frozen)")

        lp = compute_lp_lock(report, summary)
        metrics.update(lp)
        if metrics["lp_locked_pct"] is None:
            fail.append("Could not determine LP locked/burned %")
        elif metrics["lp_locked_pct"] < float(cfg["min_lp_locked_pct"]):
            fail.append(
                f"Weighted LP locked/burned {metrics['lp_locked_pct']:.2f}% "
                f"(min {cfg['min_lp_locked_pct']}%)"
            )
        if metrics["primary_lp_locked_pct"] is None:
            warnings.append("Primary pool LP lock % unavailable")
        elif metrics["primary_lp_locked_pct"] < float(cfg["min_primary_lp_locked_pct"]):
            fail.append(
                f"Primary pool LP locked/burned {metrics['primary_lp_locked_pct']:.2f}% "
                f"(min {cfg['min_primary_lp_locked_pct']}%)"
            )
        if metrics.get("unlocked_secondary_pools"):
            warnings.append(
                f"{len(metrics['unlocked_secondary_pools'])} secondary pool(s) appear unlocked"
            )
    else:
        fail.append("RugCheck report missing; insider networks, creator %, authorities, and LP lock could not be verified")

    if live.get("source") == "rugcheck_topHolders_snapshot_may_be_stale":
        warnings.append("Holder percentages used a RugCheck snapshot because live RPC was unreachable (snapshots can be stale)")

    passed = not fail
    return {
        "passed": passed,
        "reason": "; ".join(fail) if fail else (
            f"Top-10 non-LP {metrics['top10_non_lp_pct']}% / max wallet "
            f"{metrics['max_single_non_lp_pct']}% / insider nets "
            f"{metrics['insider_network_pct']}% / creator {metrics['creator_pct']}% / "
            f"LP locked {metrics['lp_locked_pct']}%"
        ),
        "metrics": metrics,
        "warnings": warnings,
    }

def score_upside(
    mcap: float | None,
    liq: float | None,
    vol: float | None,
    liq_mcap: float | None,
    vol_mcap: float | None,
    buy_ratio: float | None,
    txns: float,
    chg: float | None,
    age: float | None,
    rec: dict[str, Any],
) -> list[dict[str, Any]]:
    """Explicit 0-100 breakdown. Weights: headroom 25, liq 15, volume 20, flow 15, entry 15, bonuses 10."""
    parts: list[dict[str, Any]] = []

    pts = 0.0
    why = "missing market cap"
    if mcap and mcap > 0:
        if 500_000 <= mcap <= 5_000_000:
            pts, why = 25, f"${mcap:,.0f} is in the $500k-$5M sweet spot for 5x-10x headroom"
        elif 200_000 <= mcap < 500_000:
            pts, why = 20, f"${mcap:,.0f} has more headroom but also more early-rug risk"
        elif 5_000_000 < mcap <= 10_000_000:
            pts, why = 16, f"${mcap:,.0f} still has ~5x room to $50M but less torque"
        elif 10_000_000 < mcap <= 15_000_000:
            pts, why = 10, f"${mcap:,.0f} is near the top of the band (10x would be $100M-$150M)"
        else:
            pts, why = 0, f"${mcap:,.0f} is outside the 5x-10x band"
    parts.append({"name": "mcap_headroom", "max": 25, "points": pts, "why": why})

    pts = 0.0
    bits = []
    if liq is not None:
        if liq >= 250_000:
            pts += 8; bits.append(f"liq ${liq:,.0f} is deep")
        elif liq >= 100_000:
            pts += 6; bits.append(f"liq ${liq:,.0f} is adequate")
        elif liq >= 50_000:
            pts += 4; bits.append(f"liq ${liq:,.0f} is modest")
        elif liq >= 30_000:
            pts += 2; bits.append(f"liq ${liq:,.0f} is thin")
        else:
            bits.append(f"liq ${liq:,.0f} is below the floor")
    if liq_mcap is not None:
        if liq_mcap >= 0.12:
            pts += 7; bits.append(f"liq/mcap {liq_mcap:.1%} is healthy")
        elif liq_mcap >= 0.06:
            pts += 5; bits.append(f"liq/mcap {liq_mcap:.1%} is ok")
        elif liq_mcap >= 0.04:
            pts += 3; bits.append(f"liq/mcap {liq_mcap:.1%} is tight")
        else:
            bits.append(f"liq/mcap {liq_mcap:.1%} is weak")
    pts = clamp(pts, 0, 15)
    parts.append({"name": "liquidity_depth", "max": 15, "points": pts, "why": "; ".join(bits) or "missing liquidity"})

    pts = 0.0
    bits = []
    if vol is not None:
        if vol >= 5_000_000:
            pts += 10; bits.append(f"24h vol ${vol:,.0f}")
        elif vol >= 1_000_000:
            pts += 8; bits.append(f"24h vol ${vol:,.0f}")
        elif vol >= 400_000:
            pts += 6; bits.append(f"24h vol ${vol:,.0f}")
        elif vol >= 100_000:
            pts += 3; bits.append(f"24h vol ${vol:,.0f}")
        else:
            bits.append(f"24h vol ${vol:,.0f} is light")
    if vol_mcap is not None:
        if vol_mcap >= 1.0:
            pts += 7; bits.append(f"vol/mcap {vol_mcap:.1f}x is very active")
        elif vol_mcap >= 0.4:
            pts += 5; bits.append(f"vol/mcap {vol_mcap:.1%} is active")
        elif vol_mcap >= 0.15:
            pts += 3; bits.append(f"vol/mcap {vol_mcap:.1%} meets the floor")
        else:
            bits.append(f"vol/mcap {vol_mcap:.1%} is sleepy")
    if txns >= 5000:
        pts += 3; bits.append(f"{txns:.0f} txns")
    elif txns >= 1000:
        pts += 2; bits.append(f"{txns:.0f} txns")
    elif txns >= 200:
        pts += 1; bits.append(f"{txns:.0f} txns")
    pts = clamp(pts, 0, 20)
    parts.append({"name": "volume_velocity", "max": 20, "points": pts, "why": "; ".join(bits) or "missing volume"})

    pts = 0.0
    if buy_ratio is None:
        why, pts = "buy/sell breakdown unavailable", 6
    elif buy_ratio >= 0.55:
        why, pts = f"buy ratio {buy_ratio:.1%} (buyers in control)", 15
    elif buy_ratio >= 0.48:
        why, pts = f"buy ratio {buy_ratio:.1%} (balanced to slightly bullish)", 12
    elif buy_ratio >= 0.40:
        why, pts = f"buy ratio {buy_ratio:.1%} (slightly more sells)", 8
    elif buy_ratio >= 0.35:
        why, pts = f"buy ratio {buy_ratio:.1%} (selling pressure)", 4
    else:
        why, pts = f"buy ratio {buy_ratio:.1%} looks like a dump", 0
    parts.append({"name": "buy_sell_balance", "max": 15, "points": pts, "why": why})

    pts = 0.0
    bits = []
    if chg is None:
        bits.append("24h change missing"); pts += 6
    elif chg > 1500:
        bits.append(f"+{chg:.0f}% in 24h is a blowoff"); pts += 0
    elif chg > 400:
        bits.append(f"+{chg:.0f}% in 24h is already extended"); pts += 3
    elif chg > 150:
        bits.append(f"+{chg:.0f}% in 24h is warm"); pts += 8
    elif chg >= -30:
        bits.append(f"24h change {chg:+.0f}% is a non-parabolic entry"); pts += 12
    else:
        bits.append(f"24h change {chg:+.0f}% is a sharp drawdown (discount, but damaged momentum)"); pts += 7
    if age is None:
        bits.append("pair age unknown")
    elif age < 6:
        bits.append(f"only {age:.1f}h old")
        pts = min(pts, 3)
    elif age < 24:
        bits.append(f"{age:.1f}h old (young)"); pts += 1
    elif age < 24 * 14:
        bits.append(f"{age:.1f}h old (established enough to not be a brand-new launch)"); pts += 3
    else:
        bits.append(f"{age/24:.1f}d old"); pts += 3
    pts = clamp(pts, 0, 15)
    parts.append({"name": "non_parabolic_entry", "max": 15, "points": pts, "why": "; ".join(bits)})

    has = bool(rec.get("has_socials") or rec.get("social_links") or rec.get("has_profile"))
    parts.append({"name": "socials_profile", "max": 5, "points": 5 if has else 0,
                  "why": "DexScreener socials/profile present" if has else "No socials/profile attached"})

    n = int(rec.get("pair_count") or len(rec.get("pairs") or []) or 0)
    if n >= 4:
        pts, why = 5, f"{n} pools (multi-venue traction)"
    elif n >= 2:
        pts, why = 3, f"{n} pools"
    elif n == 1:
        pts, why = 1, "Single pool only"
    else:
        pts, why = 0, "Pool count unknown"
    parts.append({"name": "multi_pool_traction", "max": 5, "points": pts, "why": why})
    return parts


def check_upside(rec: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Criterion 3 - transparent 0-100 rubric for 5x-10x structural potential."""
    mcap = rec.get("mcap_used_usd")
    liq = rec.get("liquidity_used_usd")
    vol = rec.get("volume_used_usd")
    chg = rec.get("change_24h_used_pct")
    age = rec.get("age_hours")
    buys = fnum(rec.get("buys_24h"), 0.0) or 0.0
    sells = fnum(rec.get("sells_24h"), 0.0) or 0.0
    txns = buys + sells
    if rec.get("gt_txns_24h") and txns == 0:
        txns = float(rec["gt_txns_24h"])
    liq_mcap = (liq / mcap) if (liq and mcap) else None
    vol_mcap = (vol / mcap) if (vol and mcap) else None
    buy_ratio = (buys / (buys + sells)) if (buys + sells) > 0 else None

    hard_fail: list[str] = []
    if mcap is None:
        hard_fail.append("Missing market cap")
    elif mcap < float(cfg["market_cap_min_usd"]) or mcap > float(cfg["market_cap_max_usd"]):
        hard_fail.append(f"Market cap ${mcap:,.0f} is outside the ${cfg['market_cap_min_usd']:,.0f}-${cfg['market_cap_max_usd']:,.0f} 5x-10x headroom band")
    if liq is None or liq < float(cfg["min_liquidity_usd"]):
        hard_fail.append(f"Liquidity ${liq if liq is not None else 0:,.0f} below ${cfg['min_liquidity_usd']:,.0f}")
    if liq_mcap is not None and liq_mcap < float(cfg["min_liq_mcap_ratio"]):
        hard_fail.append(f"Liq/mcap {liq_mcap:.2%} below {float(cfg['min_liq_mcap_ratio']):.2%}")
    elif liq_mcap is None and not any("Liquidity" in x or "market cap" in x.lower() for x in hard_fail):
        hard_fail.append("Missing liq/mcap ratio")
    if vol is None or vol < float(cfg["min_volume_24h_usd"]):
        hard_fail.append(f"24h volume ${vol if vol is not None else 0:,.0f} below ${cfg['min_volume_24h_usd']:,.0f}")
    if vol_mcap is not None and vol_mcap < float(cfg["min_vol_mcap_ratio"]):
        hard_fail.append(f"Vol/mcap {vol_mcap:.2%} below {float(cfg['min_vol_mcap_ratio']):.2%}")
    if age is not None and age < float(cfg["min_pair_age_hours"]):
        hard_fail.append(f"Pair age {age:.1f}h below {cfg['min_pair_age_hours']}h floor")
    elif age is None:
        hard_fail.append("Pair age unknown")
    if chg is not None and chg > float(cfg["max_price_change_24h_pct"]):
        hard_fail.append(f"Already up +{chg:.0f}% in 24h (parabolic blowoff cap is +{cfg['max_price_change_24h_pct']:.0f}%)")
    if txns < float(cfg["min_txns_24h"]):
        hard_fail.append(f"Only {txns:.0f} 24h txns (min {cfg['min_txns_24h']})")
    if buy_ratio is not None and buy_ratio < float(cfg["min_buy_ratio"]):
        hard_fail.append(f"Buy ratio {buy_ratio:.2%} looks like dumping (min {float(cfg['min_buy_ratio']):.0%} buys)")

    breakdown = score_upside(mcap, liq, vol, liq_mcap, vol_mcap, buy_ratio, txns, chg, age, rec)
    total = int(round(sum(b["points"] for b in breakdown)))
    min_score = int(cfg["min_upside_score"])
    if total < min_score:
        hard_fail.append(f"Upside score {total}/100 below {min_score}")

    passed = not hard_fail
    return {
        "passed": passed,
        "score": total,
        "min_score": min_score,
        "breakdown": breakdown,
        "reason": "; ".join(hard_fail) if hard_fail else f"Upside score {total}/100 with structural cap headroom",
        "inputs": {
            "market_cap_usd": mcap,
            "liquidity_usd": liq,
            "volume_24h_usd": vol,
            "liq_mcap": round(liq_mcap, 4) if liq_mcap is not None else None,
            "vol_mcap": round(vol_mcap, 4) if vol_mcap is not None else None,
            "buy_ratio": round(buy_ratio, 4) if buy_ratio is not None else None,
            "txns_24h": txns,
            "price_change_24h_pct": chg,
            "age_hours": round(age, 2) if age is not None else None,
            "has_socials": bool(rec.get("has_socials") or rec.get("social_links") or rec.get("has_profile")),
            "pair_count": rec.get("pair_count") or len(rec.get("pairs") or []),
        },
    }


def why_this_could_fail(rec: dict[str, Any], bundle: dict, conc: dict, upside: dict) -> list[str]:
    bullets: list[str] = []
    metrics = conc.get("metrics") or {}
    inputs = upside.get("inputs") or {}

    for w in (bundle.get("warnings") or []) + (conc.get("warnings") or []):
        if w not in bullets:
            bullets.append(w)

    unlocked = metrics.get("unlocked_secondary_pools") or []
    if unlocked:
        u = unlocked[0]
        bullets.append(
            f"Unlocked secondary {u.get('market_type') or 'pool'} "
            f"{(u.get('pubkey') or '')[:8]}... holds ~${u.get('usd') or 0:,.0f} and can be yanked"
        )
    if metrics.get("lp_locked_pct") is not None and metrics["lp_locked_pct"] < 70:
        bullets.append(f"Only {metrics['lp_locked_pct']:.1f}% of LP is locked/burned on a liquidity-weighted basis")
    if (metrics.get("insider_network_pct") or 0) >= 8:
        bullets.append(
            f"Insider networks still cluster ~{metrics['insider_network_pct']:.1f}% of supply "
            f"(upper bound; wallets may overlap)"
        )
    if (metrics.get("max_single_non_lp_pct") or 0) >= 4:
        bullets.append(f"A single non-LP wallet holds {metrics['max_single_non_lp_pct']:.1f}% and can dump")
    if (metrics.get("top10_non_lp_pct") or 0) >= 18:
        bullets.append(f"Top-10 non-LP wallets control {metrics['top10_non_lp_pct']:.1f}% of supply")

    chg = inputs.get("price_change_24h_pct")
    if chg is not None and chg <= -40:
        bullets.append(f"Already down {chg:.0f}% in 24h - momentum is damaged and another leg down is common")
    if chg is not None and chg >= 200:
        bullets.append(f"Already up {chg:.0f}% in 24h - late entry risk even if under the blowoff cap")

    liq = inputs.get("liquidity_usd")
    if liq is not None and liq < 80_000:
        bullets.append(f"Liquidity is only ${liq:,.0f}; a modest sell can move the price sharply")
    age = inputs.get("age_hours")
    if age is not None and age < 24:
        bullets.append(f"Primary trading history is only {age:.1f} hours old")
    elif age is not None and age < 72:
        bullets.append(f"Pair is only {age/24:.1f} days old - typical meme death-rate is still high")

    launch = bundle.get("rpc_launch_check") or {}
    if not launch.get("performed"):
        bullets.append(
            f"Early-slot bundle verification was incomplete ({launch.get('reason') or 'not performed'})"
        )

    graph = metrics.get("graph_insiders_detected")
    if isinstance(graph, int) and graph >= 80:
        bullets.append(f"RugCheck graph still flags {graph} related insider accounts")

    beta = "High meme-coin beta: even a clean setup can go to zero on a sector fade or a dev dump"
    bullets.append(beta)

    seen, out = set(), []
    for b in bullets:
        if b not in seen:
            seen.add(b)
            out.append(b)
    # Always keep the meme-beta caveat even if we have to drop a mid-priority bullet.
    if beta in out:
        kept = [b for b in out if b != beta][:7]
        kept.append(beta)
        return kept
    return out[:8]


def inspect_one(rec: dict[str, Any], cfg: dict[str, Any], http: Http, rpc: Rpc | None) -> dict[str, Any]:
    report, summary, rug_notes = fetch_rugcheck(http, rec["mint"])
    rec["rugcheck_notes"] = rug_notes
    rec["rugcheck_report_missing"] = report is None
    if report:
        rec["symbol"] = rec.get("symbol") or ((report.get("tokenMeta") or {}).get("symbol")) or ((report.get("fileMeta") or {}).get("symbol"))
        rec["name"] = rec.get("name") or ((report.get("tokenMeta") or {}).get("name")) or ((report.get("fileMeta") or {}).get("name"))
        rec["pair_count"] = max(int(rec.get("pair_count") or 0), len(report.get("markets") or []))

    live = collect_live_holders(rec["mint"], report, rpc, cfg)
    bundle = check_bundle(rec, report, rpc, cfg)
    conc = check_concentration(rec, report, summary, live, cfg)
    split = detect_uniform_wallet_cluster(((conc.get("metrics") or {}).get("wallet_holders") or []), cfg)
    if split:
        bundle["passed"] = False
        bundle["reason"] = (
            f"Uniform split-wallet cluster: {split['size']} non-LP wallets each hold "
            f"~{split['avg_pct']:.2f}% (range {split['min_pct']:.2f}-{split['max_pct']:.2f}%, "
            f"together {split['total_pct']:.2f}%) - looks like a bundled launch split"
        )
        bundle.setdefault("checks", []).append({"name": "uniform_wallet_cluster", "result": "fail", "cluster": split})
    upside = check_upside(rec, cfg)

    passed = bool(bundle["passed"] and conc["passed"] and upside["passed"])
    failed = []
    if not bundle["passed"]:
        failed.append({"criterion": 1, "name": "bundle", "reason": bundle["reason"]})
    if not conc["passed"]:
        failed.append({"criterion": 2, "name": "insider_concentration", "reason": conc["reason"]})
    if not upside["passed"]:
        failed.append({"criterion": 3, "name": "upside_5x_10x", "reason": upside["reason"]})

    rec.update({
        "bundle": bundle,
        "concentration": conc,
        "upside": upside,
        "passed_all_three": passed,
        "failed_criteria": failed,
        "why_this_could_fail": why_this_could_fail(rec, bundle, conc, upside),
        "links": coin_links(rec),
    })
    return rec

def load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    st = load_json(path, None)
    if not isinstance(st, dict):
        st = {}
    st.setdefault("run_count", 0)
    st.setdefault("last_run_utc", None)
    st.setdefault("last_run_hst", None)
    st.setdefault("notification_mode", "instant")
    st.setdefault("alerted", {})
    return st


def is_material_upgrade(prev: dict[str, Any], rec: dict[str, Any], cfg: dict[str, Any]) -> tuple[bool, str | None]:
    score = (rec.get("upside") or {}).get("score")
    insider = ((rec.get("concentration") or {}).get("metrics") or {}).get("insider_network_pct")
    top10 = ((rec.get("concentration") or {}).get("metrics") or {}).get("top10_non_lp_pct")
    lp = ((rec.get("concentration") or {}).get("metrics") or {}).get("lp_locked_pct")
    reasons = []
    if score is not None and prev.get("last_upside_score") is not None:
        if score - float(prev["last_upside_score"]) >= float(cfg["material_upgrade_score_delta"]):
            reasons.append(f"upside score {prev['last_upside_score']} -> {score}")
    if insider is not None and prev.get("last_insider_pct") is not None:
        if float(prev["last_insider_pct"]) - float(insider) >= float(cfg["material_upgrade_concentration_delta_pct"]):
            reasons.append(f"insider % {prev['last_insider_pct']} -> {insider}")
    if top10 is not None and prev.get("last_top10_non_lp_pct") is not None:
        if float(prev["last_top10_non_lp_pct"]) - float(top10) >= float(cfg["material_upgrade_concentration_delta_pct"]):
            reasons.append(f"top-10 % {prev['last_top10_non_lp_pct']} -> {top10}")
    if lp is not None and prev.get("last_lp_locked_pct") is not None:
        if float(lp) - float(prev["last_lp_locked_pct"]) >= float(cfg["material_upgrade_lp_lock_delta_pct"]):
            reasons.append(f"LP locked {prev['last_lp_locked_pct']} -> {lp}")
    if reasons:
        return True, "material_upgrade: " + "; ".join(reasons)
    return False, None


def classify_alerts(
    passed: list[dict[str, Any]],
    state: dict[str, Any],
    cfg: dict[str, Any],
    now: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split passers into newly_flagged (notify now) vs already_alerted (cooldown)."""
    cooldown = timedelta(hours=float(cfg.get("alert_cooldown_hours") or 24))
    alerted = state.get("alerted") or {}
    newly, already = [], []
    for rec in passed:
        mint = rec["mint"]
        prev = alerted.get(mint)
        if not prev:
            rec["notify_reason"] = "new"
            newly.append(rec)
            continue
        last = parse_iso(prev.get("last_alerted_at_utc"))
        expired = last is None or (now - last) >= cooldown
        upgraded, why = is_material_upgrade(prev, rec, cfg)
        if expired:
            rec["notify_reason"] = "cooldown_expired"
            newly.append(rec)
        elif upgraded:
            rec["notify_reason"] = why
            newly.append(rec)
        else:
            rec["notify_reason"] = "cooldown"
            rec["last_alerted_at_utc"] = prev.get("last_alerted_at_utc")
            already.append(rec)
    return newly, already


def snapshot_alert(rec: dict[str, Any], now: datetime, prev: dict[str, Any] | None) -> dict[str, Any]:
    m = (rec.get("concentration") or {}).get("metrics") or {}
    return {
        "symbol": rec.get("symbol"),
        "name": rec.get("name"),
        "first_alerted_at_utc": (prev or {}).get("first_alerted_at_utc") or fmt_iso(now),
        "last_alerted_at_utc": fmt_iso(now),
        "last_upside_score": (rec.get("upside") or {}).get("score"),
        "last_insider_pct": m.get("insider_network_pct"),
        "last_top10_non_lp_pct": m.get("top10_non_lp_pct"),
        "last_lp_locked_pct": m.get("lp_locked_pct"),
        "last_notify_reason": rec.get("notify_reason") or "new",
        "launchpad": rec.get("launchpad") or launchpad_for_mint(rec.get("mint"), rec.get("sources")),
    }


def append_flagged_csv(path: Path, rows: list[dict[str, Any]], now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    fields = [
        "timestamp_utc", "timestamp_hst", "mint", "symbol", "name",
        "market_cap_usd", "liquidity_usd", "volume_24h_usd", "upside_score",
        "top10_non_lp_pct", "max_wallet_pct", "insider_pct", "creator_pct",
        "lp_locked_pct", "notify_reason", "dexscreener_url", "solscan_url",
    ]
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if write_header:
            w.writeheader()
        for rec in rows:
            m = (rec.get("concentration") or {}).get("metrics") or {}
            inp = (rec.get("upside") or {}).get("inputs") or {}
            links = rec.get("links") or {}
            w.writerow({
                "timestamp_utc": fmt_iso(now),
                "timestamp_hst": fmt_hst(now),
                "mint": rec.get("mint"),
                "symbol": rec.get("symbol"),
                "name": rec.get("name"),
                "market_cap_usd": inp.get("market_cap_usd") or rec.get("mcap_used_usd"),
                "liquidity_usd": inp.get("liquidity_usd") or rec.get("liquidity_used_usd"),
                "volume_24h_usd": inp.get("volume_24h_usd") or rec.get("volume_used_usd"),
                "upside_score": (rec.get("upside") or {}).get("score"),
                "top10_non_lp_pct": m.get("top10_non_lp_pct"),
                "max_wallet_pct": m.get("max_single_non_lp_pct"),
                "insider_pct": m.get("insider_network_pct"),
                "creator_pct": m.get("creator_pct"),
                "lp_locked_pct": m.get("lp_locked_pct"),
                "notify_reason": rec.get("notify_reason"),
                "dexscreener_url": links.get("dexscreener"),
                "solscan_url": links.get("solscan"),
            })


def compact_candidate(rec: dict[str, Any]) -> dict[str, Any]:
    m = (rec.get("concentration") or {}).get("metrics") or {}
    return {
        "mint": rec.get("mint"),
        "symbol": rec.get("symbol"),
        "name": rec.get("name"),
        "market_cap_usd": rec.get("mcap_used_usd"),
        "liquidity_usd": rec.get("liquidity_used_usd"),
        "volume_24h_usd": rec.get("volume_used_usd"),
        "price_change_24h_pct": rec.get("change_24h_used_pct"),
        "age_hours": rec.get("age_hours"),
        "passed_all_three": rec.get("passed_all_three"),
        "notify_reason": rec.get("notify_reason"),
        "upside_score": (rec.get("upside") or {}).get("score"),
        "upside": rec.get("upside"),
        "bundle": rec.get("bundle"),
        "concentration": rec.get("concentration"),
        "failed_criteria": rec.get("failed_criteria") or [],
        "why_this_could_fail": rec.get("why_this_could_fail") or [],
        "links": rec.get("links") or {},
        "sources": rec.get("sources") or [],
        "holder_source": m.get("holder_source"),
        "top10_non_lp_pct": m.get("top10_non_lp_pct"),
        "max_single_non_lp_pct": m.get("max_single_non_lp_pct"),
        "insider_network_pct": m.get("insider_network_pct"),
        "creator_pct": m.get("creator_pct"),
        "lp_locked_pct": m.get("lp_locked_pct"),
        "primary_lp_locked_pct": m.get("primary_lp_locked_pct"),
        "graduated": bool(rec.get("graduated")),
        "venue": rec.get("venue"),
        "liveness": rec.get("liveness"),
        "launchpad": rec.get("launchpad") or launchpad_for_mint(rec.get("mint"), rec.get("sources")),
        "fomopad_registered": rec.get("fomopad_registered"),
    }


def write_report_md(path: Path, scan: dict[str, Any]) -> None:
    newly = scan.get("newly_flagged") or []
    already = scan.get("already_alerted") or []
    passed = scan.get("passed_all_three") or []
    misses = scan.get("near_misses") or []
    lines = [
        f"# Meme Coin Scanner - {scan.get('run_at_hst') or scan.get('run_at_utc')}",
        "",
        "Read-only research / instant alerts. Fake signals are never invented. "
        "**Default notification is instant** (not a daily digest): `notify_now` is true "
        "when at least one coin is in `newly_flagged`.",
        "",
        "## Run summary",
        "",
        f"- notify_now: **{str(scan.get('notify_now')).lower()}**",
        f"- Newly flagged (instant alert): **{scan.get('newly_flagged_count', len(newly))}**",
        f"- Passed all 3 (including already-alerted): **{scan.get('passed_count', len(passed))}**",
        f"- Discovered: {scan.get('scanned_discovery_count')} unique mints",
        f"- Deeply inspected: {scan.get('deeply_inspected_count')}",
        f"- Runtime: {scan.get('runtime_sec')}s",
        f"- Data sources: {', '.join((scan.get('data_sources') or {}).keys()) or 'n/a'}",
        "",
    ]
    fresh = scan.get("fresh") or {}
    if fresh:
        lines += [
            "## Fresh pairs & graduating",
            "",
            f"- Fresh scan at {fresh.get('run_at_hst')} ({fresh.get('run_at_utc')}) in {fresh.get('runtime_sec')}s",
            f"- Window: last {fresh.get('window_sec')}s",
            f"- Discovered: {fresh.get('discovered')} | in scope: {fresh.get('in_scope')} | gated: {fresh.get('market_gate_passed')} | inspected: {fresh.get('inspected')}",
            f"- Passed all 3: **{fresh.get('passed_count')}** | near passes: {fresh.get('near_pass_count')} | newly flagged: {fresh.get('newly_flagged_count')}",
            "",
        ]
        for rec in (fresh.get("passed") or [])[:12]:
            links = rec.get("links") or {}
            lines.append(f"- PASS {rec.get('symbol') or rec.get('mint')} `{rec.get('mint')}` mcap={rec.get('market_cap_usd')} liq={rec.get('liquidity_usd')} [Fomo]({links.get('fomo')}) [pump.fun]({links.get('pumpfun')}) [DexScreener]({links.get('dexscreener')})")
            for w in (rec.get("why_this_could_fail") or [])[:3]:
                lines.append(f"  - WHY THIS COULD FAIL: {w}")
        for rec in (fresh.get("near_passes") or [])[:8]:
            links = rec.get("links") or {}
            lines.append(f"- NEAR {rec.get('symbol') or rec.get('mint')} `{rec.get('mint')}` score={(rec.get('upside') or {}).get('score') or rec.get('upside_score')} [Fomo]({links.get('fomo')})")
            for w in (rec.get("why_this_could_fail") or [])[:2]:
                lines.append(f"  - WHY THIS COULD FAIL: {w}")
        lines.append("")
    cov = scan.get("coverage") or {}
    if cov:
        lines += ["## Coverage", ""]
        lines.append(f"- Mode: {cov.get('mode')} | scan at {cov.get('scan_timestamp_hst')} ({cov.get('scan_timestamp_utc')})")
        if cov.get("mode") == "all_graduated":
            dd = cov.get("dropped_dead") or {}
            lines += [
                f"- Graduated tokens enumerated: **{cov.get('total_graduated_enumerated')}**",
                f"- Alive: **{cov.get('alive')}** | dropped dead: **{dd.get('total')}** "
                f"(low liquidity on-chain {dd.get('low_liquidity_onchain')}, low liquidity DexScreener {dd.get('low_liquidity_dexscreener')}, inactive 7d {dd.get('inactive_7d')})",
                f"- Dead rule: {cov.get('dead_rule')}",
            ]
            bl = cov.get("by_launchpad") or {}
            if bl:
                lines += ["", "| Launchpad | Enumerated | Alive | Dead | Gated | Inspected | Passed all 3 |", "|---|---|---|---|---|---|---|"]
                for lp, d in bl.items():
                    lines.append(f"| {lp} | {d.get('enumerated')} | {d.get('alive')} | {d.get('dead')} | {d.get('gated')} | {d.get('inspected')} | {d.get('passed')} |")
                for lp, note in (cov.get("launchpad_notes") or {}).items():
                    lines.append(f"- {lp}: {note}")
                lines.append("")
        lines += [
            f"- Passed market gate: **{cov.get('market_gate_passed')}** | fully inspected: **{cov.get('fully_inspected')}** "
            f"| inspection errors left uninspected: **{cov.get('uninspected_after_retries')}** (initial {cov.get('inspection_errors_initial')}, retry rounds {cov.get('retry_rounds_used')})",
            "",
            "| Source | Status | Fully paginated | Tokens | Note |",
            "|---|---|---|---|---|",
        ]
        for src in cov.get("sources") or []:
            lines.append(f"| {src.get('name')} | {src.get('status')} | {'yes' if src.get('fully_paginated') else 'no'} | {src.get('graduates', src.get('items', ''))} | {src.get('note') or ''} |")
        lines.append("")
    if scan.get("caveats"):
        lines += ["## Caveats", ""]
        for c in scan["caveats"]:
            lines.append(f"- {c}")
        lines.append("")

    def coin_block(rec: dict[str, Any], heading: str) -> list[str]:
        links = rec.get("links") or {}
        mcap = rec.get("market_cap_usd")
        liq = rec.get("liquidity_usd")
        vol = rec.get("volume_24h_usd")
        chg = rec.get("price_change_24h_pct")
        out = [
            f"### {heading}",
            "",
            f"- Mint: `{rec.get('mint')}`",
            f"- Market cap: ${mcap:,.0f}" if isinstance(mcap, (int, float)) else f"- Market cap: {mcap}",
            f"- Liquidity: ${liq:,.0f}" if isinstance(liq, (int, float)) else f"- Liquidity: {liq}",
            f"- 24h volume: ${vol:,.0f}" if isinstance(vol, (int, float)) else f"- 24h volume: {vol}",
            f"- 24h change: {chg:+.1f}%" if isinstance(chg, (int, float)) else f"- 24h change: {chg}",
            f"- Age: {rec.get('age_hours'):.1f}h" if isinstance(rec.get("age_hours"), (int, float)) else f"- Age: {rec.get('age_hours')}",
            f"- Upside score: {rec.get('upside_score')}/100",
            f"- Top-10 non-LP: {rec.get('top10_non_lp_pct')}% | max wallet: {rec.get('max_single_non_lp_pct')}% | insider nets: {rec.get('insider_network_pct')}% | creator: {rec.get('creator_pct')}% | LP locked: {rec.get('lp_locked_pct')}%",
            f"- Holder source: {rec.get('holder_source')}",
            f"- Notify reason: {rec.get('notify_reason')}",
            f"- DexScreener: {links.get('dexscreener')}",
            f"- Solscan: {links.get('solscan')}",
            f"- RugCheck: {links.get('rugcheck')}",
            "",
            "WHY THIS COULD FAIL:",
        ]
        for b in rec.get("why_this_could_fail") or []:
            out.append(f"- {b}")
        failed = rec.get("failed_criteria") or []
        if failed:
            out.append("")
            out.append("Failed criteria:")
            for f_ in failed:
                out.append(f"- Criterion {f_.get('criterion')} ({f_.get('name')}): {f_.get('reason')}")
        out.append("")
        return out

    tp = scan.get("top_potential") or []
    lines += ["## Highest potential (all fully inspected coins, both launchpads, by upside score)", ""]
    if not tp:
        lines += ["None this run.", ""]
    else:
        lines += ["| # | Symbol | Launchpad | Upside | Market cap | Liquidity | Passed all 3 | Why not |", "|---|---|---|---|---|---|---|---|"]
        for t in tp:
            mc, lq = t.get("market_cap_usd"), t.get("liquidity_usd")
            lines.append(f"| {t['rank']} | {t.get('symbol')} | {t.get('launchpad')} | {t.get('upside_score')} | "
                         f"{'$' + format(mc, ',.0f') if isinstance(mc, (int, float)) else mc} | "
                         f"{'$' + format(lq, ',.0f') if isinstance(lq, (int, float)) else lq} | "
                         f"{'yes' if t.get('passed_all_three') else 'no'} | {'; '.join(t.get('failed_reasons') or []) or '-'} |")
        lines.append("")
    lines += ["## Newly flagged (instant alert now)", ""]
    if not newly:
        lines += ["None this run.", ""]
    else:
        for rec in newly:
            title = f"{rec.get('symbol') or 'UNKNOWN'} - {rec.get('name') or rec.get('mint')}"
            lines += coin_block(rec, title)

    if already:
        lines += ["## Already alerted (cooldown, no instant re-alert)", ""]
        for rec in already:
            title = f"{rec.get('symbol') or 'UNKNOWN'} - {rec.get('name') or rec.get('mint')} (cooldown)"
            lines += coin_block(rec, title)

    lines += ["## Top near-misses", ""]
    if not misses:
        lines += ["None.", ""]
    else:
        for rec in misses:
            title = f"{rec.get('symbol') or 'UNKNOWN'} - {rec.get('name') or rec.get('mint')} (near miss)"
            lines += coin_block(rec, title)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")

def _inspect_many(recs: list[dict[str, Any]], cfg: dict[str, Any], http: Http, rpc: Rpc, workers: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not recs:
        return out
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(inspect_one, rec, cfg, http, rpc): rec for rec in recs}
        for fut in as_completed(futs):
            rec = futs[fut]
            try:
                out.append(fut.result())
            except Exception as e:
                out.append({
                    "mint": rec["mint"],
                    "symbol": rec.get("symbol"),
                    "name": rec.get("name"),
                    "passed_all_three": False,
                    "failed_criteria": [{"criterion": 0, "name": "inspect_error", "reason": f"{type(e).__name__}: {e}"}],
                    "why_this_could_fail": [f"Inspection error: {e}"],
                    "bundle": {"passed": False, "reason": f"inspect error: {e}", "checks": [], "warnings": []},
                    "concentration": {"passed": False, "reason": f"inspect error: {e}", "metrics": {}, "warnings": []},
                    "upside": {"passed": False, "score": 0, "reason": f"inspect error: {e}"},
                })
    return out


_SOURCE_STAT_KEYS = {
    "solana_rpc:getProgramAccounts PumpSwap": "solana_rpc_pumpswap",
    "solana_rpc:getProgramAccounts Raydium AMM v4": "solana_rpc_raydium_v4",
    "solana_rpc:getProgramAccounts Meteora DBC (all pools, '...fomo' base mint)": "solana_rpc_meteora_dbc",
    "fomopad_api:/api/tokens (FomoPad verified launch registry)": "fomopad_registry",
}


def mark_source_activity(stats: dict[str, Any], sources: list[dict[str, Any]]) -> None:
    """Record '<data_source>_http_200' for enumeration sources that succeeded (dashboard 'active' dots)."""
    for src in sources:
        key = _SOURCE_STAT_KEYS.get(str(src.get("name")))
        if key and src.get("status") == "ok":
            stats[f"{key}_http_200"] = max(1, int(stats.get(f"{key}_http_200") or 0))



def fresh_scope(cfg: dict[str, Any]) -> dict[str, Any]:
    sc = dict(DEFAULT_FRESH_SCOPE)
    sc.update(cfg.get("fresh_scope") or {})
    return sc


def cfg_for_fresh(cfg: dict[str, Any], sc: dict[str, Any] | None = None) -> dict[str, Any]:
    """Copy config with relaxed market-gate floors so last-hour pairs can be inspected.

    Safety checks that still apply: bundle, insider/top-holder concentration,
    RugCheck mint/freeze authority, liquidity floor, non-parabolic cap.
    24h volume / txn floors are dropped because a 10-minute-old pair cannot
    accumulate the all-graduated 24h minimums.
    """
    sc = sc or fresh_scope(cfg)
    out = dict(cfg)
    out["min_pair_age_hours"] = float(sc["min_pair_age_hours"])
    out["market_cap_min_usd"] = float(sc["market_cap_min_usd"])
    out["min_volume_24h_usd"] = float(sc["min_volume_24h_usd"])
    out["min_vol_mcap_ratio"] = float(sc["min_vol_mcap_ratio"])
    out["min_txns_24h"] = float(sc["min_txns_24h"])
    out["near_grad_min_liquidity_usd"] = float(sc["near_grad_min_liquidity_usd"])
    out["max_candidates_per_run"] = int(sc["max_inspect"])
    out["_unlimited_candidates"] = False
    return out


def parse_created_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        ms = float(value)
        if ms <= 0:
            return None
        if ms < 1e12:
            ms *= 1000.0
        try:
            return datetime.fromtimestamp(ms / 1000.0, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        return parse_iso(value)
    return None


def is_within_window(value: Any, now: datetime | None = None, window_sec: float = 3600) -> bool:
    dt = parse_created_ts(value)
    if dt is None:
        return False
    now = now or now_utc()
    age = (now - dt).total_seconds()
    return 0 <= age <= float(window_sec)


def public_links(rec: dict[str, Any]) -> dict[str, str]:
    """Fomo + pump.fun + DexScreener links for every coin (page / widget / ntfy)."""
    mint = rec.get("mint") or ""
    links = dict(rec.get("links") or coin_links(rec))
    if mint:
        links["fomo"] = FOMO_APP_TOKEN_URL.format(mint=mint)
        links.setdefault("dexscreener", rec.get("dexscreener_url") or f"https://dexscreener.com/solana/{mint}")
        links.setdefault("solscan", f"https://solscan.io/token/{mint}")
        links.setdefault("rugcheck", f"https://rugcheck.xyz/tokens/{mint}")
        if mint.endswith("pump") or rec.get("launchpad") == LAUNCHPAD_PUMPFUN:
            links.setdefault("pumpfun", PUMPFUN_COIN_URL.format(mint=mint))
    return links


def pump_bonding_curve_pda(mint: str) -> str | None:
    try:
        raw = b58decode(mint)
    except Exception:
        return None
    if len(raw) != 32:
        return None
    res = find_program_address([PUMP_BC_SEED, raw], PUMP_PROGRAM)
    return res[0] if res else None


def parse_bonding_curve(raw: bytes) -> dict[str, Any] | None:
    """Decode a pump.fun BondingCurve account (Anchor discriminator + reserve fields)."""
    if not raw or len(raw) < 49:
        return None
    return {
        "virtual_token_reserves": int.from_bytes(raw[8:16], "little"),
        "virtual_sol_reserves_sol": int.from_bytes(raw[16:24], "little") / 1e9,
        "real_token_reserves": int.from_bytes(raw[24:32], "little"),
        "real_sol_reserves_sol": int.from_bytes(raw[32:40], "little") / 1e9,
        "token_total_supply": int.from_bytes(raw[40:48], "little"),
        "complete": raw[48] != 0,
    }


def bonding_curve_progress(curve: dict[str, Any], initial_virtual_sol: float = PUMP_INITIAL_VIRTUAL_SOL,
                           graduation_virtual_sol: float = PUMP_GRADUATION_VIRTUAL_SOL) -> dict[str, Any]:
    """Progress toward pump.fun graduation: real SOL / (graduation virt - initial virt).

    Standard curve starts at 30 virtual SOL and completes when virtual SOL reaches 85
    (55 real SOL deposited). complete=True is 100%.
    """
    real = float(curve.get("real_sol_reserves_sol") or 0.0)
    target = max(0.0, float(graduation_virtual_sol) - float(initial_virtual_sol))
    pct = 100.0 if curve.get("complete") else (100.0 * real / target if target else 0.0)
    return {
        "progress_pct": round(pct, 2),
        "real_sol": round(real, 4),
        "target_real_sol": target,
        "virtual_sol": curve.get("virtual_sol_reserves_sol"),
        "complete": bool(curve.get("complete")),
    }


def _gt_volume(attrs: dict[str, Any], key: str) -> float | None:
    vol = attrs.get("volume_usd")
    if isinstance(vol, dict):
        return fnum(vol.get(key))
    return None


def ingest_gt_pool(it: dict[str, Any], found: dict[str, dict[str, Any]], source: str,
                   cfg: dict[str, Any]) -> str | None:
    """Add the base mint of a GeckoTerminal pool item. Returns the dex id."""
    if not isinstance(it, dict):
        return None
    attrs = it.get("attributes") or {}
    rel = it.get("relationships") or {}
    base = _mint_from_gt_id(((rel.get("base_token") or {}).get("data") or {}).get("id"))
    quote = _mint_from_gt_id(((rel.get("quote_token") or {}).get("data") or {}).get("id"))
    dex = ((rel.get("dex") or {}).get("data") or {}).get("id")
    if not base or is_excluded_mint(base, cfg):
        return dex if isinstance(dex, str) else None
    rec = found.setdefault(base, {"mint": base, "sources": []})
    if source not in rec["sources"]:
        rec["sources"].append(source)
    extra = {
        "gt_fdv_usd": fnum(attrs.get("fdv_usd")),
        "gt_mcap_usd": fnum(attrs.get("market_cap_usd")),
        "gt_reserve_usd": fnum(attrs.get("reserve_in_usd")),
        "gt_volume_24h": _gt_volume(attrs, "h24"),
        "gt_volume_h1": _gt_volume(attrs, "h1"),
        "gt_change_24h": fnum((attrs.get("price_change_percentage") or {}).get("h24")
                              if isinstance(attrs.get("price_change_percentage"), dict) else None),
        "gt_pool_created_at": attrs.get("pool_created_at"),
        "gt_pool_name": attrs.get("name"),
        "gt_txns_24h": _gt_txn_count(attrs),
        "gt_dex": dex,
        "gt_pool_address": attrs.get("address"),
        "gt_quote_mint": quote,
        "symbol": rec.get("symbol") or _symbol_from_pool_name(attrs.get("name"), "base"),
    }
    for k, v in extra.items():
        if v is not None and rec.get(k) is None:
            rec[k] = v
    return dex if isinstance(dex, str) else None


def fetch_gt_pages(http: Http, url: str, pages: int, label: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items: list[dict[str, Any]] = []
    last_status = None
    for page in range(1, max(1, int(pages)) + 1):
        sep = "&" if "?" in url else "?"
        data = http.get(f"{url}{sep}page={page}", "geckoterminal")
        if not isinstance(data, dict):
            last_status = "partial" if items else "error"
            break
        rows = data.get("data")
        if not isinstance(rows, list):
            last_status = "partial" if items else "error"
            break
        items.extend(r for r in rows if isinstance(r, dict))
        last_status = "ok"
        if len(rows) < 20:
            break
    return items, {"name": label, "url": url, "pages_fetched": page if last_status else 0,
                   "items": len(items), "status": last_status or "error",
                   "fully_paginated": last_status == "ok"}


def fetch_recent_pumpswap_migrations(http: Http, cfg: dict[str, Any], sc: dict[str, Any],
                                     now: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """List pump.fun -> PumpSwap graduations in the last window via MigrateV2 signatures."""
    url = str(cfg.get("rpc_url") or DEFAULT_RPC)
    account = str(sc.get("migrate_account") or PUMP_MIGRATE_ACCOUNT)
    window = float(sc["window_sec"])
    cap = int(sc["migrate_sig_limit"])
    meta: dict[str, Any] = {"name": "solana_rpc:getSignaturesForAddress pump MigrateV2",
                            "account": account, "url": url, "status": "error",
                            "signatures": 0, "transactions": 0, "mints": 0}
    sigs: list[dict[str, Any]] = []
    before = None
    cutoff = int((now - timedelta(seconds=window)).timestamp())
    pages = 0
    while len(sigs) < cap and pages < 8:
        params: dict[str, Any] = {"limit": min(1000, cap - len(sigs))}
        if before:
            params["before"] = before
        result = rpc_post(http, url, "getSignaturesForAddress", [account, params],
                          timeout=max(20.0, float(cfg.get("http_timeout_sec") or 15)),
                          source="rpc_fresh")
        pages += 1
        if not isinstance(result, list) or not result:
            break
        stop = False
        for row in result:
            if not isinstance(row, dict) or not row.get("signature"):
                continue
            bt = row.get("blockTime")
            if isinstance(bt, (int, float)) and int(bt) < cutoff:
                stop = True
                continue
            sigs.append(row)
        if stop or len(result) < params["limit"]:
            break
        before = result[-1].get("signature")
        if not before:
            break
    meta["signatures"] = len(sigs)
    if not sigs:
        meta["status"] = "ok" if pages else "error"
        meta["note"] = "no signatures in window" if pages else "RPC did not return signatures"
        return [], meta

    def load_tx(sig: str) -> Any:
        return rpc_post(http, url, "getTransaction",
                        [sig, {"encoding": "json", "maxSupportedTransactionVersion": 1}],
                        timeout=max(20.0, float(cfg.get("http_timeout_sec") or 15)),
                        retries=2, source="rpc_fresh")

    workers = max(1, int(sc.get("migrate_tx_workers") or 4))
    unique = list(dict.fromkeys(s["signature"] for s in sigs))
    txs: list[Any] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(load_tx, sig) for sig in unique]
        for fut in as_completed(futs):
            txs.append(fut.result())
    meta["transactions"] = sum(1 for t in txs if isinstance(t, dict))

    by_mint: dict[str, dict[str, Any]] = {}
    for tx in txs:
        if not isinstance(tx, dict):
            continue
        logs = " ".join((tx.get("meta") or {}).get("logMessages") or [])
        # CreatePool is the PumpSwap pool init; MigrateV2-only follow-up txs can mention older mints.
        if "CreatePool" not in logs:
            continue
        bt = (tx.get("blockTime")
              or ((((tx.get("transaction") or {}).get("message") or {}).get("recentBlockhash") and None)))
        # blockTime is at the top level of getTransaction result
        bt = tx.get("blockTime")
        mints: list[str] = []
        msg = (tx.get("transaction") or {}).get("message") or {}
        keys = msg.get("accountKeys") or []
        pubs: list[Any] = [k.get("pubkey") if isinstance(k, dict) else k for k in keys]
        loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
        pubs += list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])
        for pkey in pubs:
            if isinstance(pkey, str) and len(pkey) >= 32 and (pkey.endswith("pump") or pkey.endswith(FOMO_SUFFIX)):
                mints.append(pkey)
        for bal in (tx.get("meta") or {}).get("postTokenBalances") or []:
            mid = bal.get("mint") if isinstance(bal, dict) else None
            if mid and mid not in (WSOL,) and not is_excluded_mint(str(mid), cfg):
                if str(mid).endswith("pump") or str(mid).endswith(FOMO_SUFFIX):
                    mints.append(str(mid))
        seen = set()
        for mint in mints:
            if mint in seen or is_excluded_mint(mint, cfg):
                continue
            seen.add(mint)
            rec = by_mint.setdefault(mint, {"mint": mint, "sources": []})
            if "rpc_pumpswap_migrate" not in rec["sources"]:
                rec["sources"].append("rpc_pumpswap_migrate")
            rec["graduated"] = True
            rec["fresh_graduation"] = True
            rec["venue"] = "pumpswap"
            rec["launchpad"] = launchpad_for_mint(mint, rec["sources"])
            if isinstance(bt, (int, float)) and rec.get("migrate_block_time") is None:
                rec["migrate_block_time"] = int(bt)
                rec["oldest_pair_created_at_ms"] = int(bt) * 1000
    meta["mints"] = len(by_mint)
    meta["status"] = "ok"
    return list(by_mint.values()), meta


def fetch_bonding_curves(http: Http, cfg: dict[str, Any], mints: list[str],
                         sc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    url = str(cfg.get("rpc_url") or DEFAULT_RPC)
    out: dict[str, dict[str, Any]] = {}
    addrs: list[tuple[str, str]] = []
    for mint in mints:
        pda = pump_bonding_curve_pda(mint)
        if pda:
            addrs.append((mint, pda))
    for i in range(0, len(addrs), 100):
        chunk = addrs[i:i + 100]
        result = rpc_post(http, url, "getMultipleAccounts",
                          [[pda for _, pda in chunk], {"encoding": "base64"}],
                          timeout=max(20.0, float(cfg.get("http_timeout_sec") or 15)),
                          source="rpc_fresh")
        vals = (result or {}).get("value") if isinstance(result, dict) else None
        if not isinstance(vals, list):
            continue
        for (mint, pda), acc in zip(chunk, vals):
            if not isinstance(acc, dict):
                continue
            data = acc.get("data")
            raw = None
            if isinstance(data, list) and data:
                try:
                    raw = base64.b64decode(data[0])
                except (ValueError, TypeError):
                    raw = None
            elif isinstance(data, str):
                try:
                    raw = base64.b64decode(data)
                except (ValueError, TypeError):
                    raw = None
            curve = parse_bonding_curve(raw or b"")
            if not curve:
                continue
            prog = bonding_curve_progress(
                curve,
                initial_virtual_sol=float(sc["initial_virtual_sol"]),
                graduation_virtual_sol=float(sc["graduation_virtual_sol"]),
            )
            out[mint] = {"pda": pda, **curve, **prog}
    return out


def discover_fresh_candidates(http: Http, rpc: Rpc, cfg: dict[str, Any],
                              now: datetime | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Gather last-hour Solana pairs, recent PumpSwap graduations, and near-grad pump.fun coins."""
    now = now or now_utc()
    sc = fresh_scope(cfg)
    window = float(sc["window_sec"])
    found: dict[str, dict[str, Any]] = {}
    sources_meta: list[dict[str, Any]] = []
    counts = {"gt_new_pools": 0, "gt_new_pools_in_window": 0, "dex_profiles_boosts": 0,
              "migrations": 0, "near_graduation": 0, "dex_pool_lists": 0}

    def merge(rec: dict[str, Any]) -> dict[str, Any]:
        mint = rec["mint"]
        dest = found.setdefault(mint, {"mint": mint, "sources": []})
        for k, v in rec.items():
            if k == "sources":
                for s in v or []:
                    if s not in dest["sources"]:
                        dest["sources"].append(s)
            elif k == "mint":
                continue
            elif v is not None and dest.get(k) is None:
                dest[k] = v
        return dest

    # 1) GeckoTerminal new_pools (free plan: max page 10, typically covers only the last few minutes)
    items, meta = fetch_gt_pages(http, f"{GECKO}/networks/solana/new_pools",
                                 int(sc["gt_new_pool_pages"]), "geckoterminal:new_pools")
    in_window = 0
    oldest = None
    for it in items:
        dex = ingest_gt_pool(it, found, "gt_new_pools", cfg)
        created = ((it.get("attributes") or {}).get("pool_created_at"))
        if is_within_window(created, now, window):
            in_window += 1
            base = _mint_from_gt_id(((((it.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id")))
            if base and base in found:
                found[base]["fresh_pair"] = True
                if dex in FRESH_GRADUATION_DEXES:
                    found[base]["graduated"] = True
                    found[base]["fresh_graduation"] = True
                    found[base]["venue"] = dex
        dt = parse_created_ts(created)
        if dt and (oldest is None or dt < oldest):
            oldest = dt
    counts["gt_new_pools"] = len(items)
    counts["gt_new_pools_in_window"] = in_window
    meta["in_window"] = in_window
    meta["oldest_pool_created_at"] = fmt_iso(oldest)
    meta["note"] = ("GeckoTerminal free plan caps new_pools at page 10, so this feed often "
                    "covers only the most recent few minutes, not a full hour.")
    sources_meta.append(meta)

    # 2) Dex lists for PumpSwap / Raydium / Meteora (volume-sorted; keep those created in-window)
    for dex in ("pumpswap", "raydium", "raydium-clmm", "raydium-launchlab", "meteora-damm-v2"):
        items, meta = fetch_gt_pages(http, f"{GECKO}/networks/solana/dexes/{dex}/pools",
                                     int(sc["gt_dex_pool_pages"]), f"geckoterminal:{dex}/pools")
        kept = 0
        for it in items:
            created = ((it.get("attributes") or {}).get("pool_created_at"))
            if not is_within_window(created, now, window):
                continue
            ingest_gt_pool(it, found, f"gt_{dex}_pools", cfg)
            base = _mint_from_gt_id(((((it.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id")))
            if base and base in found:
                found[base]["fresh_pair"] = True
                found[base]["graduated"] = True
                found[base]["fresh_graduation"] = True
                found[base]["venue"] = dex
                kept += 1
        counts["dex_pool_lists"] += kept
        meta["in_window"] = kept
        sources_meta.append(meta)

    # 3) DexScreener latest token profiles and boosts (Solana only); age filtered after enrich
    for label, path in (
        ("dex_profiles_latest", f"{DEX}/token-profiles/latest/v1"),
        ("dex_boosts_latest", f"{DEX}/token-boosts/latest/v1"),
        ("dex_boosts_top", f"{DEX}/token-boosts/top/v1"),
    ):
        data = http.get(path, "dexscreener")
        n = 0
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                if str(item.get("chainId") or "").lower() != "solana":
                    continue
                mint = item.get("tokenAddress") or ""
                if not mint or is_excluded_mint(mint, cfg):
                    continue
                rec = merge({"mint": mint, "sources": [label],
                             "has_profile": bool(item.get("description") or item.get("links") or item.get("icon")),
                             "social_links": item.get("links") if isinstance(item.get("links"), list) else []})
                n += 1
        counts["dex_profiles_boosts"] += n
        sources_meta.append({"name": f"dexscreener:{label}", "url": path, "status": "ok" if isinstance(data, list) else "error",
                             "items": n, "fully_paginated": False})

    # 4) Recent PumpSwap graduations from on-chain MigrateV2 signatures (full ~60 minutes)
    migrated, mig_meta = fetch_recent_pumpswap_migrations(http, cfg, sc, now)
    for rec in migrated:
        merge(rec)
    counts["migrations"] = mig_meta.get("mints") or 0
    sources_meta.append(mig_meta)

    # 5) pump.fun coins close to graduating: GeckoTerminal pump-fun pools + on-chain curve progress
    # pump.fun frontend API is Cloudflare 403 from this host; we do not try to bypass it.
    pf_items: list[dict[str, Any]] = []
    for sort_q, tag in (("", "default"), ("?sort=h24_volume_usd_desc", "h24_volume")):
        url = f"{GECKO}/networks/solana/dexes/pump-fun/pools" + sort_q
        items, meta = fetch_gt_pages(http, url, int(sc["gt_pumpfun_pool_pages"]),
                                     f"geckoterminal:pump-fun/pools:{tag}")
        pf_items.extend(items)
        sources_meta.append(meta)
    pf_mints: list[str] = []
    for it in pf_items:
        ingest_gt_pool(it, found, "gt_pumpfun_pools", cfg)
        base = _mint_from_gt_id(((((it.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id")))
        if base:
            pf_mints.append(base)
    # Also consider pump-suffix mints from other sources that are not already marked graduated
    curve_targets = []
    for mint, rec in found.items():
        if rec.get("fresh_graduation") or rec.get("graduated"):
            continue
        if mint.endswith("pump") or rec.get("gt_dex") == "pump-fun":
            curve_targets.append(mint)
    for m in pf_mints:
        if m not in curve_targets:
            curve_targets.append(m)
    curves = fetch_bonding_curves(http, cfg, curve_targets, sc)
    min_pct = float(sc["near_grad_min_progress_pct"])
    for mint, curve in curves.items():
        rec = found.setdefault(mint, {"mint": mint, "sources": []})
        rec["bonding_curve"] = curve
        rec["graduation_progress_pct"] = curve.get("progress_pct")
        if "rpc_bonding_curve" not in rec["sources"]:
            rec["sources"].append("rpc_bonding_curve")
        if curve.get("complete"):
            rec["graduated"] = True
            continue
        if (curve.get("progress_pct") or 0) >= min_pct:
            rec["near_graduation"] = True
            rec["curve_reserve_usd"] = None  # filled after SOL price if needed
            counts["near_graduation"] += 1
    sol_price = fetch_sol_price(http)
    if sol_price:
        for rec in found.values():
            curve = rec.get("bonding_curve") or {}
            real = fnum(curve.get("real_sol_reserves_sol"))
            if real is not None:
                rec["curve_reserve_usd"] = real * sol_price
                if rec.get("gt_reserve_usd") is None:
                    rec["gt_reserve_usd"] = rec["curve_reserve_usd"]
            if rec.get("near_graduation") and rec.get("market_cap_usd") is None and rec.get("gt_fdv_usd") is None:
                # leave mcap to DexScreener enrich / gt_fdv already on rec
                pass

    # Enrich everyone we have so pairCreatedAt / liquidity / mcap are real
    candidates = list(found.values())
    enrich_meta = enrich_with_dexscreener(http, candidates, cfg, force=True, workers=4)
    # Age filter: a migrate-account hit on an older coin is not a last-hour graduation.
    # Keep last-hour pairs, true last-hour graduations, and near-grad bonding-curve coins.
    kept: list[dict[str, Any]] = []
    dropped_stale = 0
    window_hours = window / 3600.0
    for rec in candidates:
        rec["launchpad"] = rec.get("launchpad") or launchpad_for_mint(rec.get("mint"), rec.get("sources"))
        rec["age_hours"] = pair_age_hours(rec, now)
        age = rec.get("age_hours")
        if rec.get("fresh_graduation") and age is not None and age > window_hours + 0.25:
            rec["fresh_graduation"] = False
        created = rec.get("oldest_pair_created_at_ms") or rec.get("pair_created_at_ms") or rec.get("gt_pool_created_at") or rec.get("migrate_block_time")
        if is_within_window(created, now, window):
            rec["fresh_pair"] = True
        in_window = bool(rec.get("fresh_pair")) or (age is not None and age <= window_hours + 0.25)
        if rec.get("near_graduation"):
            kept.append(rec)
            continue
        if rec.get("fresh_graduation") and (in_window or age is None):
            kept.append(rec)
            continue
        if rec.get("fresh_pair"):
            dex = rec.get("dex_id") or rec.get("gt_dex")
            if dex in FRESH_GRADUATION_DEXES and (age is None or age <= window_hours + 0.25):
                rec["graduated"] = True
                rec["fresh_graduation"] = True
            kept.append(rec)
            continue
        dropped_stale += 1
    for rec in kept:
        rec["links"] = public_links(rec)
    disco = {
        "mode": "fresh",
        "window_sec": window,
        "unique_mints_discovered": len(found),
        "unique_mints_in_scope": len(kept),
        "dropped_stale_profiles_boosts": dropped_stale,
        "counts": counts,
        "sources": sources_meta,
        "dexscreener_enrich": enrich_meta,
        "sol_price_usd": sol_price,
        "pumpfun_api": {"status": "skipped", "reason": "pump.fun frontend API returns Cloudflare 403 from this host; not bypassed. Near-graduation uses GeckoTerminal pump-fun pools plus on-chain bonding-curve accounts via public RPC."},
        "gt_new_pools_coverage_note": "Free GeckoTerminal pagination stops at page 10 (~few minutes of new pools). Hour-long PumpSwap graduations come from on-chain MigrateV2 signatures.",
    }
    return kept, disco


def compact_fresh(rec: dict[str, Any]) -> dict[str, Any]:
    row = compact_candidate(rec)
    row["links"] = public_links(rec)
    row["fresh_pair"] = bool(rec.get("fresh_pair"))
    row["fresh_graduation"] = bool(rec.get("fresh_graduation"))
    row["near_graduation"] = bool(rec.get("near_graduation"))
    row["graduation_progress_pct"] = rec.get("graduation_progress_pct")
    row["price_usd"] = rec.get("price_usd")
    row["why_this_could_fail"] = rec.get("why_this_could_fail") or []
    return row


def merge_fresh_payload(existing: dict[str, Any] | None, fresh_block: dict[str, Any],
                        newly: list[dict[str, Any]], notify_now: bool) -> dict[str, Any]:
    """Write a fresh block into latest_scan.json without wiping all-graduated fields."""
    base = dict(existing) if isinstance(existing, dict) else {}
    out: dict[str, Any] = {}
    # Start with a skeleton so a first-run fresh scan still has required keys
    out["version"] = base.get("version") or 1
    out["notification_mode"] = "instant"
    for k in ALL_GRADUATED_PRESERVE_KEYS:
        if k in base:
            out[k] = base[k]
    # Keep previous data_sources and overlay fresh ones under the fresh block
    if "data_sources" in base:
        out["data_sources"] = dict(base["data_sources"])
    if "http_stats" in base:
        out["http_stats"] = base["http_stats"]
    if "http_errors" in base:
        out["http_errors"] = base["http_errors"]
    out["fresh"] = fresh_block
    out["notify_now"] = bool(notify_now)
    out["newly_flagged"] = [compact_fresh(r) if "failed_criteria" in r or "why_this_could_fail" in r else r for r in newly]
    out["newly_flagged_count"] = len(out["newly_flagged"])
    return out


def run_fresh_scan(cfg: dict[str, Any], dry_run: bool = False,
                   max_candidates: int | None = None) -> dict[str, Any]:
    t0 = time.time()
    now = now_utc()
    sc = fresh_scope(cfg)
    fcfg = cfg_for_fresh(cfg, sc)
    if max_candidates is not None:
        fcfg["max_candidates_per_run"] = int(max_candidates)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    http = Http(timeout=float(cfg.get("http_timeout_sec") or 15))
    rpc = Rpc(
        url=str(cfg.get("rpc_url") or DEFAULT_RPC),
        pacing_ms=int(cfg.get("rpc_pacing_ms") or 250),
        timeout=float(cfg.get("http_timeout_sec") or 15),
        http=http,
    )
    log("fresh mode: discovering last-hour pairs, PumpSwap graduations, and near-grad pump.fun coins")
    discovered, disco_meta = discover_fresh_candidates(http, rpc, cfg, now)
    deep, skipped = prefilter(discovered, fcfg)
    log(f"fresh market gate passed: {len(deep)} of {len(discovered)}; inspecting up to {fcfg.get('max_candidates_per_run')}")
    workers = max(1, int(cfg.get("deep_inspect_concurrency") or 4))
    inspected = _inspect_many(deep, fcfg, http, rpc, workers)
    # No long retry sleeps in fresh mode (must finish in ~3 minutes)
    for r in inspected:
        inc = inspection_incomplete(r)
        if inc:
            r["inspection_incomplete"] = inc
        r["links"] = public_links(r)
        if not r.get("why_this_could_fail"):
            r["why_this_could_fail"] = why_this_could_fail(r, r.get("bundle") or {}, r.get("concentration") or {}, r.get("upside") or {})

    passed = [r for r in inspected if r.get("passed_all_three")]
    failed = [r for r in inspected if not r.get("passed_all_three")]

    def miss_key(r: dict[str, Any]) -> tuple:
        nfail = len(r.get("failed_criteria") or [])
        score = (r.get("upside") or {}).get("score") or 0
        prog = r.get("graduation_progress_pct") or 0
        return (nfail, -score, -prog)

    failed.sort(key=miss_key)
    near = failed[:8]

    state = load_state()
    newly, already = classify_alerts(passed, state, cfg, now)
    notify_now = bool(newly)

    runtime = round(time.time() - t0, 2)
    fresh_block = {
        "run_at_utc": fmt_iso(now),
        "run_at_hst": fmt_hst(now),
        "runtime_sec": runtime,
        "window_sec": sc["window_sec"],
        "discovered": disco_meta.get("unique_mints_discovered"),
        "in_scope": disco_meta.get("unique_mints_in_scope"),
        "market_gate_passed": len(deep),
        "inspected": len(inspected),
        "passed_count": len(passed),
        "near_pass_count": len(near),
        "newly_flagged_count": len(newly),
        "already_alerted_count": len(already),
        "passed": [compact_fresh(r) for r in passed],
        "near_passes": [compact_fresh(r) for r in near],
        "newly_flagged": [compact_fresh(r) for r in newly],
        "already_alerted": [compact_fresh(r) for r in already],
        "discovery": disco_meta,
        "http_stats": http.stats,
        "http_errors": http.errors[:20],
        "rpc_methods_ok": sorted(rpc.methods_ok),
        "rpc_methods_fail": sorted(rpc.methods_fail),
        "data_sources": {
            "geckoterminal_new_pools": f"{GECKO}/networks/solana/new_pools",
            "geckoterminal_dex_pools": f"{GECKO}/networks/solana/dexes/{{pumpswap,raydium,raydium-clmm,raydium-launchlab,meteora-damm-v2,pump-fun}}/pools",
            "dexscreener_profiles_boosts": f"{DEX}/token-profiles/latest/v1 and /token-boosts/latest/v1 + /top/v1 (Solana only)",
            "dexscreener_tokens": f"{DEX}/tokens/v1/solana/{{mints}}",
            "solana_rpc_migrations": f"getSignaturesForAddress {sc.get('migrate_account') or PUMP_MIGRATE_ACCOUNT} (pump.fun MigrateV2 / PumpSwap CreatePool)",
            "solana_rpc_bonding_curve": f"getMultipleAccounts on PDA [\"bonding-curve\", mint] under {PUMP_PROGRAM}",
            "rugcheck": f"{RUGCHECK}/tokens/{{mint}}/report",
        },
        "caveats": [
            "GeckoTerminal's free new_pools feed stops at page 10 and usually covers only the last few minutes, not a full hour.",
            "Hour-long PumpSwap graduation coverage comes from on-chain MigrateV2 signatures on a migrate-associated account, not a full getProgramAccounts sweep.",
            "pump.fun's frontend API is blocked with Cloudflare 403 from this host and was not used or bypassed. Near-graduation uses GeckoTerminal pump-fun pools plus on-chain bonding-curve progress.",
            "Fresh mode relaxes 24h volume, txn, and pair-age floors so last-hour pairs can be inspected. Bundle, insider concentration, mint/freeze, liquidity floor, and the parabolic cap still apply.",
            "This is research/alerting only — never a trade recommendation.",
        ],
        "dry_run": dry_run,
    }

    existing = load_json(LATEST_JSON, {})
    if not isinstance(existing, dict):
        existing = {}
    payload = merge_fresh_payload(existing, fresh_block, newly, notify_now)
    payload["runtime_sec"] = runtime  # this-run time; all-graduated coverage.runtime_sec stays in coverage
    write_json(LATEST_JSON, payload)
    write_report_md(LATEST_MD, payload)

    if not dry_run:
        state["run_count"] = int(state.get("run_count") or 0) + 1
        state["last_fresh_run_utc"] = fmt_iso(now)
        state["last_fresh_run_hst"] = fmt_hst(now)
        state["notification_mode"] = "instant"
        alerted = dict(state.get("alerted") or {})
        for rec in newly:
            alerted[rec["mint"]] = snapshot_alert(rec, now, alerted.get(rec["mint"]))
        state["alerted"] = alerted
        write_json(STATE_PATH, state)
        if newly:
            append_flagged_csv(FLAGGED_CSV, newly, now)

    log(f"fresh scan done in {runtime}s | in_scope={disco_meta.get('unique_mints_in_scope')} "
        f"inspected={len(inspected)} passed={len(passed)} newly={len(newly)}")
    return payload



def resolve_mode(cfg: dict[str, Any], all_graduated: bool | None = None,
                 fresh: bool | None = None) -> str:
    if fresh is True:
        return "fresh"
    if all_graduated is True:
        return "all_graduated"
    if all_graduated is False:
        return "subset"
    mode = str(cfg.get("scan_mode") or "subset")
    if mode == "fresh":
        return "fresh"
    return "all_graduated" if mode == "all_graduated" else "subset"


def run_scan(cfg: dict[str, Any], dry_run: bool = False, max_candidates: int | None = None,
             mode: str | None = None) -> dict[str, Any]:
    t0 = time.time()
    now = now_utc()
    mode = mode or resolve_mode(cfg)
    if mode == "fresh":
        return run_fresh_scan(cfg, dry_run=dry_run, max_candidates=max_candidates)
    cfg = dict(cfg)
    if max_candidates is not None:
        cfg["max_candidates_per_run"] = int(max_candidates)
    elif mode == "all_graduated":
        cfg["_unlimited_candidates"] = True
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    http = Http(timeout=float(cfg.get("http_timeout_sec") or 15))
    rpc = Rpc(
        url=str(cfg.get("rpc_url") or DEFAULT_RPC),
        pacing_ms=int(cfg.get("rpc_pacing_ms") or 250),
        timeout=float(cfg.get("http_timeout_sec") or 15),
        http=http,
    )

    grad_meta: dict[str, Any] | None = None
    legacy_extras = 0
    if mode == "all_graduated":
        sc = graduated_scope(cfg)
        alive, grad_meta = enumerate_graduated(http, rpc, cfg)
        universe_mints = grad_meta.pop("_universe_mints", set())
        discovered = list(alive)
        disco_meta: dict[str, Any] = {"mode": "all_graduated", "unique_mints": grad_meta["total_graduated_enumerated"],
                                      "alive_graduated": len(alive)}
        if sc["include_legacy_feeds"]:
            legacy, lmeta = discover_candidates(http, cfg)
            extras = [r for r in legacy if r["mint"] not in universe_mints]
            for r in extras:
                r["graduated"] = False
                r["liveness"] = "legacy_feed"
                r["launchpad"] = launchpad_for_mint(r.get("mint"), r.get("sources"))
            enrich_with_dexscreener(http, extras, cfg)
            legacy_extras = len(extras)
            discovered += extras
            disco_meta["legacy_feeds"] = lmeta
            disco_meta["legacy_feed_extras_not_in_graduated_set"] = legacy_extras
    else:
        discovered, disco_meta = discover_candidates(http, cfg)
        cap_disco = int(cfg.get("max_discovery_tokens") or 150)
        if len(discovered) > cap_disco:
            discovered = discovered[:cap_disco]
        enrich_with_dexscreener(http, discovered, cfg)
    deep, skipped = prefilter(discovered, cfg)
    log(f"market gate passed: {len(deep)} of {len(discovered)}; deep-inspecting all")
    gate_passed_graduated = sum(1 for r in deep if r.get("graduated"))

    workers = max(1, int(cfg.get("deep_inspect_concurrency") or 4))
    inspected = _inspect_many(deep, cfg, http, rpc, workers)
    errors_initial = [r for r in inspected if inspection_incomplete(r)]
    retry_rounds_used = 0
    sc_retry = graduated_scope(cfg)
    by_mint_deep = {r["mint"]: r for r in deep}
    pending = [by_mint_deep[r["mint"]] for r in errors_initial if r["mint"] in by_mint_deep]
    log(f"inspected {len(inspected)}; incomplete={len(errors_initial)}")
    while pending and retry_rounds_used < int(sc_retry["inspect_retry_rounds"]):
        retry_rounds_used += 1
        log(f"retry round {retry_rounds_used} for {len(pending)} incomplete inspections")
        time.sleep(float(sc_retry["inspect_retry_sleep_sec"]) * retry_rounds_used)
        redo = _inspect_many(pending, cfg, http, rpc, max(1, workers // 2))
        redo_by = {r["mint"]: r for r in redo}
        inspected = [redo_by.get(r["mint"], r) for r in inspected]
        pending = [by_mint_deep[r["mint"]] for r in redo if inspection_incomplete(r) and r["mint"] in by_mint_deep]
    uninspected = [r for r in inspected if inspection_incomplete(r)]
    for r in uninspected:
        r["inspection_incomplete"] = inspection_incomplete(r)

    passed = [r for r in inspected if r.get("passed_all_three")]
    failed = [r for r in inspected if not r.get("passed_all_three")]

    def miss_key(r: dict[str, Any]) -> tuple:
        nfail = len(r.get("failed_criteria") or [])
        score = (r.get("upside") or {}).get("score") or 0
        return (nfail, -score)

    failed.sort(key=miss_key)
    near = failed[:8]

    state = load_state()
    newly, already = classify_alerts(passed, state, cfg, now)
    notify_now = bool(newly)

    caveats = [
        "Public Solana RPC often rate-limits getTokenLargestAccounts (HTTP 429). "
        "When that happens, holder percentages use getTokenSupply + getMultipleAccounts "
        "on RugCheck's top accounts (live balances, snapshot set) or a RugCheck snapshot.",
        "getSignaturesForAddress does not include signers, so early-buyer clustering is only a light check.",
        "RugCheck insider-network percentages can overlap; the scanner treats the sum as an upper bound.",
        ("All-graduated mode: universe = every canonical PumpSwap graduation pool + pump-suffix Raydium v4 pools read on-chain, "
         "plus every Fomo-launchpad ('...fomo' mint) Meteora DBC pool read on-chain and the FomoPad registry, "
         "plus pump.fun API / GeckoTerminal / DexScreener search where reachable; legacy trending feeds are added as extras."
         if mode == "all_graduated" else
         "Coin lists come from today's public trending/boost/profile/search feeds (survivorship bias)."),
        "This is research/alerting only - never a trade recommendation.",
    ]
    if rpc.largest_blocked:
        caveats.insert(0, "This run: official RPC blocked getTokenLargestAccounts with 429.")
    if any("rate-limit" in e.lower() or "429" in e for e in http.errors):
        caveats.append("One or more public APIs returned 429/rate-limit this run; some checks may have used fallbacks.")

    data_sources = {
        "dexscreener": "https://api.dexscreener.com (boosts, profiles, search, tokens/v1)",
        "geckoterminal": "https://api.geckoterminal.com/api/v2 (trending/new/top pools)",
        "rugcheck": "https://api.rugcheck.xyz/v1/tokens/{mint}/report and /report/summary",
        "solana_rpc": cfg.get("rpc_url") or DEFAULT_RPC,
    }
    if mode == "all_graduated":
        data_sources.update({
            "solana_rpc_pumpswap": f"getProgramAccounts {PUMPSWAP_PROGRAM} (canonical graduation pools)",
            "solana_rpc_raydium_v4": f"getProgramAccounts {RAYDIUM_V4_PROGRAM} (pre-PumpSwap pump.fun graduates)",
            "pumpfun_api": f"{PUMPFUN_API}/coins?complete=true",
            "geckoterminal_dex_pools": f"{GECKO}/networks/solana/dexes/{{pumpswap,raydium}}/pools",
            "solana_rpc_meteora_dbc": f"getProgramAccounts {METEORA_DBC_PROGRAM} ('...fomo' base mints = Fomo launchpad)",
            "fomopad_registry": f"{FOMOPAD_API}/api/tokens",
        })

    if mode == "all_graduated" and grad_meta:
        mark_source_activity(http.stats, grad_meta.get("sources") or [])

    in_band = len(deep) + sum(1 for s in skipped if s.get("skip_reason") == "beyond_max_candidates")
    status_429 = {k[:-4]: v for k, v in http.stats.items() if k.endswith("_429")}
    for k, v in http.stats.items():
        if k.endswith("_http_429") and not k.startswith(("dexscreener_http", "geckoterminal_http", "rugcheck_http")):
            status_429[k] = v
    for k, v in http.stats.items():
        if k.startswith("rpc_") and k.endswith("_429"):
            status_429[k] = v
    coverage: dict[str, Any] = {
        "mode": mode,
        "scan_timestamp_utc": fmt_iso(now),
        "scan_timestamp_hst": fmt_hst(now),
        "market_gate_passed": len(deep),
        "market_gate_passed_graduated": gate_passed_graduated,
        "fully_inspected": len(inspected) - len(uninspected),
        "inspection_errors_initial": len(errors_initial),
        "retry_rounds_used": retry_rounds_used,
        "uninspected_after_retries": len(uninspected),
        "uninspected_mints": [{"mint": r.get("mint"), "symbol": r.get("symbol"), "why": r.get("inspection_incomplete")} for r in uninspected],
        "rate_limit_429s": status_429,
        "deep_inspect_cap": None if cfg.get("_unlimited_candidates") else cfg.get("max_candidates_per_run"),
    }
    if grad_meta:
        coverage.update(grad_meta)
        coverage["legacy_feed_extras_not_in_graduated_set"] = legacy_extras
        bl = coverage.get("by_launchpad") or {}
        for lp in LAUNCHPADS:
            d = bl.setdefault(lp, {"enumerated": 0, "alive": 0, "dead": 0})
            d["gated"] = sum(1 for r in deep if r.get("graduated") and (r.get("launchpad") or launchpad_for_mint(r.get("mint"), r.get("sources"))) == lp)
            ins = [r for r in inspected if r.get("graduated") and not inspection_incomplete(r)
                   and (r.get("launchpad") or launchpad_for_mint(r.get("mint"), r.get("sources"))) == lp]
            d["inspected"] = len(ins)
            d["passed"] = sum(1 for r in ins if r.get("passed_all_three"))
        coverage["by_launchpad"] = bl
        coverage["launchpad_notes"] = {
            LAUNCHPAD_PUMPFUN: "Every canonical PumpSwap graduation pool + pump-suffix Raydium v4 pools read on-chain (complete for PumpSwap; Raydium v4 is a suffix heuristic).",
            LAUNCHPAD_FOMO: ("FomoPad (fomopad.name, independent of FOMO Labs): all Meteora DBC pools read on-chain and filtered to '...fomo' mints, "
                             "plus the FomoPad registry (DBC + Stonk.fun). Stonk.fun launches are only known via the registry; their graduation is "
                             "detected by a live DEX pair. The Fomo app (fomo.family) itself is not a launchpad - its 'Graduated' list shows "
                             "other launchpads' (mostly pump.fun) graduates <24h old, which the pump.fun set already covers."),
        }
    else:
        coverage["sources"] = [{"name": k, "status": "ok", "items": v, "fully_paginated": False} for k, v in (disco_meta.get("per_source") or {}).items()]
        coverage["total_graduated_enumerated"] = None
    coverage["runtime_sec"] = round(time.time() - t0, 2)
    payload = {
        "version": 1,
        "notification_mode": "instant",
        "notify_now": notify_now,
        "newly_flagged": [compact_candidate(r) for r in newly],
        "already_alerted": [compact_candidate(r) for r in already],
        "passed_all_three": [compact_candidate(r) for r in passed],
        "near_misses": [compact_candidate(r) for r in near],
        "top_potential": build_top_potential(inspected, 15),
        "newly_flagged_count": len(newly),
        "already_alerted_count": len(already),
        "passed_count": len(passed),
        "scanned_discovery_count": disco_meta.get("unique_mints", len(discovered)),
        "prefiltered_in_band_count": in_band,
        "deeply_inspected_count": len(inspected),
        "runtime_sec": round(time.time() - t0, 2),
        "scan_mode": mode,
        "coverage": coverage,
        "data_sources": data_sources,
        "discovery_meta": disco_meta,
        "http_stats": http.stats,
        "http_errors": http.errors[:20],
        "rpc_methods_ok": sorted(rpc.methods_ok),
        "rpc_methods_fail": sorted(rpc.methods_fail),
        "caveats": caveats,
        "run_at_utc": fmt_iso(now),
        "run_at_hst": fmt_hst(now),
        "dry_run": dry_run,
        "thresholds": {
            "market_cap_usd": [cfg["market_cap_min_usd"], cfg["market_cap_max_usd"]],
            "min_liquidity_usd": cfg["min_liquidity_usd"],
            "min_liq_mcap_ratio": cfg["min_liq_mcap_ratio"],
            "min_volume_24h_usd": cfg["min_volume_24h_usd"],
            "min_vol_mcap_ratio": cfg["min_vol_mcap_ratio"],
            "min_pair_age_hours": cfg["min_pair_age_hours"],
            "max_price_change_24h_pct": cfg["max_price_change_24h_pct"],
            "max_top10_non_lp_pct": cfg["max_top10_non_lp_pct"],
            "max_single_non_lp_wallet_pct": cfg["max_single_non_lp_wallet_pct"],
            "max_insider_network_pct": cfg["max_insider_network_pct"],
            "max_creator_pct": cfg["max_creator_pct"],
            "min_lp_locked_pct": cfg["min_lp_locked_pct"],
            "min_primary_lp_locked_pct": cfg["min_primary_lp_locked_pct"],
            "min_upside_score": cfg["min_upside_score"],
            "alert_cooldown_hours": cfg["alert_cooldown_hours"],
        },
    }

    write_json(LATEST_JSON, payload)
    write_report_md(LATEST_MD, payload)

    if not dry_run:
        state["run_count"] = int(state.get("run_count") or 0) + 1
        state["last_run_utc"] = fmt_iso(now)
        state["last_run_hst"] = fmt_hst(now)
        state["notification_mode"] = "instant"
        alerted = dict(state.get("alerted") or {})
        for rec in newly:
            alerted[rec["mint"]] = snapshot_alert(rec, now, alerted.get(rec["mint"]))
        state["alerted"] = alerted
        write_json(STATE_PATH, state)
        if newly:
            append_flagged_csv(FLAGGED_CSV, newly, now)

    return payload

def _cfg_for_test() -> dict[str, Any]:
    return load_config(DEFAULT_CONFIG)


def run_selftest() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"  PASS  {name}")
        else:
            msg = f"  FAIL  {name}" + (f" - {detail}" if detail else "")
            print(msg)
            failures.append(msg)

    print("Selftest: Meme Coin Scanner (offline unit tests)")
    cfg = _cfg_for_test()

    report = {
        "token": {"supply": 1_000_000_000_000, "decimals": 6, "mintAuthority": None, "freezeAuthority": None},
        "creator": "Creator11111111111111111111111111111111111",
        "creatorBalance": 0,
        "markets": [{
            "pubkey": "PoolPubkey11111111111111111111111111111111",
            "marketType": "pump_fun_amm",
            "liquidityA": "LpTokenAccount111111111111111111111111111",
            "liquidityB": "OtherLpAccount11111111111111111111111111",
            "lp": {"baseUSD": 100000, "quoteUSD": 100000, "lpLockedPct": 100},
        }, {
            "pubkey": "MeteoraPool111111111111111111111111111111",
            "marketType": "meteoraDlmm",
            "liquidityA": "MeteoraVault11111111111111111111111111111",
            "liquidityB": "MeteoraVaultB111111111111111111111111111",
            "lp": {"baseUSD": 40000, "quoteUSD": 20000, "lpLockedPct": 0},
        }],
        "knownAccounts": {
            "PoolPubkey11111111111111111111111111111111": {"name": "Pump Fun AMM", "type": "AMM"},
            "MeteoraPool111111111111111111111111111111": {"name": "Meteora DLMM Pool", "type": "AMM"},
        },
        "insiderNetworks": [],
        "risks": [],
        "rugged": False,
        "mintAuthority": None,
        "freezeAuthority": None,
        "topHolders": [],
    }
    live = {
        "source": "rpc_getMultipleAccounts_on_rugcheck_top_accounts",
        "supply_ui": 1_000_000,
        "supply_raw": 1_000_000_000_000,
        "decimals": 6,
        "holders": [
            {"address": "LpTokenAccount111111111111111111111111111", "owner": "PoolPubkey11111111111111111111111111111111", "ui_amount": 300_000},
            {"address": "MeteoraVault11111111111111111111111111111", "owner": "MeteoraPool111111111111111111111111111111", "ui_amount": 80_000},
            {"address": "BurnAcct11111111111111111111111111111111", "owner": SYSTEM_BURN, "ui_amount": 30_000},
            {"address": "W1", "owner": "Wallet11111111111111111111111111111111111", "ui_amount": 40_000},
            {"address": "W2", "owner": "Wallet22222222222222222222222222222222222", "ui_amount": 30_000},
            {"address": "W3", "owner": "Wallet33333333333333333333333333333333333", "ui_amount": 20_000},
        ],
        "notes": [],
    }
    rec = {"mint": "TestMint111111111111111111111111111111111", "age_hours": 48}
    conc = check_concentration(rec, report, {"lpLockedPct": 76.9}, live, cfg)
    m = conc["metrics"]
    check("lp_vault_excluded", "PoolPubkey11111111111111111111111111111111" not in [w["owner"] for w in m["wallet_holders"]],
          f"wallets={m['wallet_holders']}")
    check("meteora_pool_excluded", "MeteoraPool111111111111111111111111111111" not in [w["owner"] for w in m["wallet_holders"]])
    check("burn_excluded", all(w["owner"] != SYSTEM_BURN for w in m["wallet_holders"]))
    check("top10_is_wallets_only", abs((m["top10_non_lp_pct"] or 0) - 9.0) < 0.15, f"got {m['top10_non_lp_pct']}")
    check("max_wallet_is_4pct", abs((m["max_single_non_lp_pct"] or 0) - 4.0) < 0.15, f"got {m['max_single_non_lp_pct']}")
    check("concentration_passes_clean_wallets", conc["passed"] is True, conc["reason"])
    check("primary_lp_100", m["primary_lp_locked_pct"] == 100, str(m["primary_lp_locked_pct"]))
    check("unlocked_secondary_noted", len(m["unlocked_secondary_pools"]) >= 1)

    live_bad = dict(live)
    live_bad["holders"] = [
        {"address": "LpTokenAccount111111111111111111111111111", "owner": "PoolPubkey11111111111111111111111111111111", "ui_amount": 400_000},
        {"address": "W1", "owner": "Wallet11111111111111111111111111111111111", "ui_amount": 20_000},
    ]
    conc2 = check_concentration(rec, report, {"lpLockedPct": 76.9}, live_bad, cfg)
    check("lp_not_counted_as_max_wallet", (conc2["metrics"]["max_single_non_lp_pct"] or 99) < 8,
          f"got {conc2['metrics']['max_single_non_lp_pct']}")

    sweet = score_upside(1_000_000, 120_000, 800_000, 0.12, 0.8, 0.55, 3000, 20, 48, {"has_socials": True, "pair_count": 3})
    mega = score_upside(14_000_000, 120_000, 800_000, 0.12, 0.05, 0.55, 3000, 20, 48, {"has_socials": True, "pair_count": 3})
    s_sweet = sum(p["points"] for p in sweet)
    s_mega = sum(p["points"] for p in mega)
    check("score_parts_sum_le_100", s_sweet <= 100 and all(p["points"] <= p["max"] for p in sweet), str(sweet))
    check("score_named_breakdown", {p["name"] for p in sweet} == {
        "mcap_headroom", "liquidity_depth", "volume_velocity", "buy_sell_balance",
        "non_parabolic_entry", "socials_profile", "multi_pool_traction",
    })
    check("sweet_spot_scores_higher_than_top_of_band", s_sweet > s_mega, f"{s_sweet} vs {s_mega}")

    check("denylist_wsol", is_excluded_mint("So11111111111111111111111111111111111111112", cfg) is True)
    too_big = {"mint": "SomeMint1111111111111111111111111111111111", "symbol": "MEGA",
               "market_cap_usd": 80_000_000, "liquidity_usd": 1_000_000, "volume_24h_usd": 2_000_000}
    r = prefilter_reason(too_big, cfg)
    check("mcap_above_band_skipped", r is not None and "mcap_above_band" in r, str(r))
    too_small = dict(too_big, market_cap_usd=50_000)
    r = prefilter_reason(too_small, cfg)
    check("mcap_below_band_skipped", r is not None and "mcap_below_band" in r, str(r))
    para = dict(too_big, market_cap_usd=1_000_000, price_change_24h_pct=5000)
    r = prefilter_reason(para, cfg)
    check("parabolic_skipped", r is not None and "parabolic" in r, str(r))

    young = {
        "mint": "YoungMint111111111111111111111111111111111",
        "symbol": "YNG",
        "market_cap_usd": 1_000_000,
        "liquidity_usd": 100_000,
        "volume_24h_usd": 200_000,
        "oldest_pair_created_at_ms": (now_utc() - timedelta(hours=2)).timestamp() * 1000,
    }
    r = prefilter_reason(young, cfg)
    check("young_pair_skipped", r is not None and "pair_too_young" in r, str(r))

    dirty_report = dict(report)
    dirty_report["risks"] = [{"name": "Bundled supply", "level": "danger", "description": "Large bundled launch", "value": "40%"}]
    b = check_bundle(rec, dirty_report, rpc=None, cfg=cfg)
    check("bundled_supply_fails_criterion1", b["passed"] is False, b["reason"])
    huge_report = dict(report)
    huge_report["risks"] = []
    huge_report["insiderNetworks"] = [{"id": "odd-scarlet-seahorse", "size": 858, "currentHolding": 400_000_000_000, "tokenAmount": 400_000_000_000, "activeAccounts": 858, "type": "transfer"}]
    b = check_bundle(rec, huge_report, rpc=None, cfg=cfg)
    check("huge_insider_cluster_fails_criterion1", b["passed"] is False, b["reason"])
    clean_b = check_bundle(rec, report, rpc=None, cfg=cfg)
    check("clean_risks_pass_criterion1", clean_b["passed"] is True, clean_b["reason"])
    b = check_bundle(rec, None, rpc=None, cfg=cfg)
    check("missing_rugcheck_fails_closed", b["passed"] is False)

    insider_report = dict(report)
    insider_report["insiderNetworks"] = [
        {"id": "net-a", "size": 4, "currentHolding": 250_000_000_000, "activeAccounts": 4},
    ]
    conc_i = check_concentration(rec, insider_report, {"lpLockedPct": 76.9}, live, cfg)
    check("high_insider_fails_criterion2", conc_i["passed"] is False, conc_i["reason"])
    check("insider_pct_about_25", abs((conc_i["metrics"]["insider_network_pct"] or 0) - 25) < 0.5,
          str(conc_i["metrics"]["insider_network_pct"]))

    auth = dict(report)
    auth["mintAuthority"] = "StillActiveMint11111111111111111111111111"
    conc_a = check_concentration(rec, auth, {"lpLockedPct": 76.9}, live, cfg)
    check("active_mint_auth_fails", conc_a["passed"] is False, conc_a["reason"])

    mid = {
        "mcap_used_usd": 1_200_000,
        "liquidity_used_usd": 90_000,
        "volume_used_usd": 400_000,
        "change_24h_used_pct": 25,
        "age_hours": 36,
        "buys_24h": 1200,
        "sells_24h": 900,
        "has_socials": True,
        "pair_count": 2,
    }
    u = check_upside(mid, cfg)
    check("midcap_healthy_passes_upside", u["passed"] is True, u["reason"])
    blown = dict(mid, change_24h_used_pct=2500)
    u = check_upside(blown, cfg)
    check("blowoff_fails_upside", (u["passed"] is False) and ("Already up" in u["reason"] or "parabolic" in u["reason"].lower()), u["reason"])

    now = now_utc()
    passed_coin = {
        "mint": "AlertMint11111111111111111111111111111111",
        "symbol": "ALRT",
        "name": "AlertCoin",
        "passed_all_three": True,
        "upside": {"score": 70},
        "concentration": {"metrics": {"insider_network_pct": 10, "top10_non_lp_pct": 16, "lp_locked_pct": 55}},
    }
    newly, already = classify_alerts([passed_coin], {"alerted": {}}, cfg, now)
    check("first_pass_is_newly_flagged", len(newly) == 1 and newly[0]["notify_reason"] == "new" and already == [])
    state_cd = {"alerted": {passed_coin["mint"]: {
        "last_alerted_at_utc": fmt_iso(now - timedelta(hours=1)),
        "last_upside_score": 70,
        "last_insider_pct": 10,
        "last_top10_non_lp_pct": 16,
        "last_lp_locked_pct": 55,
    }}}
    newly, already = classify_alerts([passed_coin], state_cd, cfg, now)
    check("cooldown_suppresses_rerun", len(already) == 1 and newly == [], f"newly={len(newly)} already={len(already)}")
    upgraded = {
        **passed_coin,
        "concentration": {"metrics": {"insider_network_pct": 3, "top10_non_lp_pct": 16, "lp_locked_pct": 55}},
        "upside": {"score": 70},
    }
    newly, already = classify_alerts([upgraded], state_cd, cfg, now)
    check("material_upgrade_breaks_cooldown", len(newly) == 1 and "material_upgrade" in (newly[0].get("notify_reason") or ""),
          newly[0].get("notify_reason") if newly else "none")
    state_old = {"alerted": {passed_coin["mint"]: {
        "last_alerted_at_utc": fmt_iso(now - timedelta(hours=25)),
        "last_upside_score": 70,
        "last_insider_pct": 10,
        "last_top10_non_lp_pct": 16,
        "last_lp_locked_pct": 55,
    }}}
    newly, already = classify_alerts([passed_coin], state_old, cfg, now)
    check("cooldown_expiry_reflags", len(newly) == 1 and newly[0]["notify_reason"] == "cooldown_expired")

    dummy_scan = {
        "version": 1,
        "notification_mode": "instant",
        "notify_now": True,
        "newly_flagged": [compact_candidate({**passed_coin, "notify_reason": "new", "mcap_used_usd": 1e6, "liquidity_used_usd": 9e4, "volume_used_usd": 4e5, "why_this_could_fail": ["High meme-coin beta"], "links": {"dexscreener": "https://dexscreener.com/solana/x", "solscan": "https://solscan.io/token/x"}})],
        "already_alerted": [],
        "passed_all_three": [],
        "near_misses": [],
        "newly_flagged_count": 1,
        "passed_count": 1,
        "scanned_discovery_count": 10,
        "deeply_inspected_count": 5,
        "runtime_sec": 1.2,
        "data_sources": {"dexscreener": "x"},
        "caveats": ["test"],
        "run_at_utc": fmt_iso(now),
        "run_at_hst": fmt_hst(now),
    }
    required = {"notify_now", "newly_flagged", "already_alerted", "passed_all_three", "near_misses",
                "scanned_discovery_count", "deeply_inspected_count", "data_sources", "runtime_sec",
                "run_at_utc", "run_at_hst", "notification_mode"}
    check("latest_scan_schema_keys", required.issubset(dummy_scan.keys()), str(required - set(dummy_scan)))
    check("notify_now_true_when_newly_flagged", dummy_scan["notify_now"] is True)
    check("notification_mode_instant", dummy_scan["notification_mode"] == "instant")

    rec_w = {"age_hours": 10}
    why = why_this_could_fail(
        rec_w,
        {"warnings": ["RPC rate-limit or error prevented early-slot bundle verification"],
         "rpc_launch_check": {"performed": False, "reason": "RPC rate-limited"}},
        {"metrics": {"unlocked_secondary_pools": [{"market_type": "meteoraDlmm", "pubkey": "Abcdefgh", "usd": 40000, "lp_locked_pct": 0}],
                     "lp_locked_pct": 42, "insider_network_pct": 11, "max_single_non_lp_pct": 5, "top10_non_lp_pct": 20},
         "warnings": []},
        {"inputs": {"price_change_24h_pct": -70, "liquidity_usd": 40000, "age_hours": 10}},
    )
    blob = " ".join(why).lower()
    check("fail_note_mentions_unlocked_pool", "unlocked" in blob, str(why))
    check("fail_note_mentions_rpc_gap", "early-slot" in blob or "rate-limit" in blob, str(why))
    check("fail_note_mentions_meme_beta", "beta" in blob, str(why))

    tag = classify_holder(
        "SomeAccount11111111111111111111111111111",
        "7auPqL9zzUx1uMsY8SMBW4oMsZpJ3Q98P9JYCALfemTR",
        {"markets": [], "knownAccounts": {"7auPqL9zzUx1uMsY8SMBW4oMsZpJ3Q98P9JYCALfemTR": {"name": "Pump Fun", "type": "AMM"}}},
    )
    check("known_amm_owner_tagged_as_lp", tag is not None and "amm" in tag.lower(), str(tag))

    clones = [{"pct": 1.00 + i * 0.002} for i in range(12)]
    hit = detect_uniform_wallet_cluster(clones, cfg)
    check("uniform_1pct_split_detected", hit is not None and hit["size"] >= 8, str(hit))
    varied = [{"pct": p} for p in (8, 3, 2, 1.5, 1.1, 0.8, 0.6, 0.4, 0.3, 0.2)]
    miss = detect_uniform_wallet_cluster(varied, cfg)
    check("varied_wallets_not_a_split", miss is None, str(miss))

    # --- all-graduated mode tests ---
    raw = bytes(range(32))
    check("b58_roundtrip", b58decode(b58encode(raw)) == raw)
    check("b58_leading_zero", b58encode(b"\0\0\x01") == "112" and b58decode("112") == b"\0\0\x01")
    check("b58_known_wsol", b58encode(b58decode(WSOL)) == WSOL and len(b58decode(WSOL)) == 32)
    dcty_mint = "GN16BDCTYWHf1FHdfzyNU7QfpcTpBk1w8XuAigNbpump"
    check("pump_pool_authority_pda", pump_pool_authority(dcty_mint) == "3SrCTEPuUp5spZcgDV73M3rPpEyKapWncxUJZfPK8qfg",
          str(pump_pool_authority(dcty_mint)))
    blob = bytearray(203)
    blob[9:11] = (0).to_bytes(2, "little")
    blob[11:43] = b58decode("3SrCTEPuUp5spZcgDV73M3rPpEyKapWncxUJZfPK8qfg")
    blob[43:75] = b58decode(dcty_mint)
    blob[75:107] = b58decode(WSOL)
    blob[171:203] = b58decode("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
    pool = parse_pumpswap_pool(bytes(blob))
    check("parse_pumpswap_pool_fields", pool is not None and pool["base_mint"] == dcty_mint and pool["quote_mint"] == WSOL
          and pool["index"] == 0 and pool["quote_vault"] == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", str(pool))
    check("canonical_pool_accepted", is_canonical_graduation_pool(pool, True))
    fake = dict(pool, creator=WSOL)
    check("non_pda_creator_rejected", not is_canonical_graduation_pool(fake, True))
    check("non_pda_creator_ok_when_pda_off", is_canonical_graduation_pool(fake, False))
    check("index1_pool_rejected", not is_canonical_graduation_pool(dict(pool, index=1), True))
    check("non_wsol_quote_rejected", not is_canonical_graduation_pool(dict(pool, quote_mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"), True))
    check("short_pool_blob_none", parse_pumpswap_pool(b"\0" * 50) is None)
    rv = parse_raydium_v4_slice(b58decode(WSOL) + b58decode(dcty_mint))
    check("parse_raydium_slice", rv == {"quote_vault": WSOL, "base_mint": dcty_mint}, str(rv))
    sc = graduated_scope({"graduated_scope": {"dead_if_liquidity_below_usd": 5000, "dead_if_no_activity_days": 7}})
    check("graduated_scope_defaults_merge", sc["inspect_retry_rounds"] >= 1 and sc["dead_if_liquidity_below_usd"] == 5000)
    nowu = 1_800_000_000
    check("liveness_low_liq_dead", classify_liveness({"liquidity_usd": 4000, "buys_24h": 50}, sc, nowu) == "dead_low_liquidity")
    check("liveness_onchain_low_liq_dead", classify_liveness({"onchain_liquidity_est_usd": 1200}, sc, nowu) == "dead_low_liquidity")
    check("liveness_dex_liq_overrides_onchain", classify_liveness({"liquidity_usd": 9000, "onchain_liquidity_est_usd": 100, "sells_24h": 3}, sc, nowu) == "alive")
    check("liveness_traded_alive", classify_liveness({"liquidity_usd": 50000, "buys_24h": 3, "sells_24h": 0}, sc, nowu) == "alive")
    check("liveness_needs_check", classify_liveness({"liquidity_usd": 50000}, sc, nowu) == "needs_activity_check")
    check("liveness_recent_sig_alive", classify_liveness({"liquidity_usd": 50000, "last_activity_unix": nowu - 3 * 86400}, sc, nowu) == "alive")
    check("liveness_stale_sig_dead", classify_liveness({"liquidity_usd": 50000, "last_activity_unix": nowu - 8 * 86400}, sc, nowu) == "dead_inactive")
    check("inspection_incomplete_rugcheck", inspection_incomplete({"rugcheck_report_missing": True}) == "rugcheck_unavailable")
    check("inspection_incomplete_exception", inspection_incomplete({"failed_criteria": [{"name": "inspect_error"}]}) == "exception")
    check("inspection_complete_ok", inspection_incomplete({"rugcheck_report_missing": False, "failed_criteria": [{"name": "bundle"}]}) is None)
    many = [{"mint": f"M{i}", "market_cap_usd": 1_000_000, "liquidity_usd": 100_000, "volume_24h_usd": 500_000,
             "price_change_24h_pct": 10, "pair_created_at_ms": int((now_utc() - timedelta(hours=48)).timestamp() * 1000)} for i in range(30)]
    capped_cfg = dict(cfg, max_candidates_per_run=20)
    d1, s1 = prefilter([dict(r) for r in many], capped_cfg)
    check("subset_mode_caps_at_20", len(d1) == 20 and sum(1 for r in s1 if r.get("skip_reason") == "beyond_max_candidates") == 10, f"{len(d1)}")
    d2, s2 = prefilter([dict(r) for r in many], dict(capped_cfg, _unlimited_candidates=True))
    check("all_graduated_mode_no_cap", len(d2) == 30 and not any(r.get("skip_reason") == "beyond_max_candidates" for r in s2), f"{len(d2)}")
    check("resolve_mode_flag_overrides", resolve_mode({"scan_mode": "subset"}, True) == "all_graduated"
          and resolve_mode({"scan_mode": "all_graduated"}, False) == "subset")
    check("resolve_mode_config_default", resolve_mode(cfg) == ("all_graduated" if cfg.get("scan_mode") == "all_graduated" else "subset"))
    # --- Fomo launchpad + top potential ---
    mint_b = b58decode("2bVKQK6UJzEkARvvggD2UQebypCrSj7DSmx6QwtMfomo")
    raw = bytearray(DBC_POOL_SIZE)
    raw[DBC_OFF_BASE_MINT:DBC_OFF_BASE_MINT + 32] = mint_b
    raw[DBC_OFF_QUOTE_RESERVE:DBC_OFF_QUOTE_RESERVE + 8] = (10_464_068_402).to_bytes(8, "little")
    raw[DBC_OFF_IS_MIGRATED] = 1
    pdb = parse_dbc_pool(bytes(raw))
    check("dbc_pool_parse", pdb and pdb["base_mint"].endswith("fomo") and pdb["is_migrated"] and pdb["quote_reserve_raw"] == 10_464_068_402)
    check("dbc_pool_parse_short", parse_dbc_pool(b"x" * 100) is None)
    check("launchpad_fomo_suffix", launchpad_for_mint("Abcfomo", []) == "fomo" and launchpad_for_mint("Abcpump", ["rpc_pumpswap"]) == "pumpfun")
    check("launchpad_fomo_source", launchpad_for_mint("Xyz", ["fomo_dbc_onchain"]) == "fomo")
    check("launchpad_pump_source_wins_suffix", launchpad_for_mint("Abcfomo", ["rpc_pumpswap"]) == "pumpfun")
    lf = coin_links({"mint": "Abcfomo", "launchpad": "fomo", "fomopad_registered": True})
    lpump = coin_links({"mint": "Abcpump", "launchpad": "pumpfun"})
    check("links_fomo_no_pumpfun", "pumpfun" not in lf and lf.get("fomo") == "https://fomo.family/tokens/solana/Abcfomo"
          and lf.get("fomopad", "").endswith("#token/Abcfomo") and lf.get("rugcheck"))
    check("links_pump_has_pumpfun", lpump.get("pumpfun") == "https://pump.fun/coin/Abcpump" and "fomo" not in lpump)
    tp_in = [
        {"mint": f"M{i}pump", "symbol": f"S{i}", "launchpad": "pumpfun", "upside": {"score": i * 5}, "passed_all_three": i % 2 == 0,
         "failed_criteria": [] if i % 2 == 0 else [{"criterion": 2, "name": "insider_concentration", "reason": "top10 40%"}],
         "mcap_used_usd": 1e6, "liquidity_used_usd": 5e4, "graduated": True} for i in range(20)
    ] + [{"mint": "Zfomo", "symbol": "Z", "launchpad": "fomo", "upside": {"score": 99}, "passed_all_three": False, "graduated": True,
          "failed_criteria": [{"criterion": 1, "name": "bundle", "reason": "bundled"}]},
         {"mint": "Errpump", "upside": {"score": 100}, "graduated": True, "failed_criteria": [{"name": "inspect_error", "reason": "x"}]},
         {"mint": "LegacyXyz", "upside": {"score": 100}, "graduated": False, "passed_all_three": True, "failed_criteria": []}]
    tp = build_top_potential(tp_in, 15)
    check("top_potential_len_15", len(tp) == 15, str(len(tp)))
    check("top_potential_sorted", [t["upside_score"] for t in tp] == sorted([t["upside_score"] for t in tp], reverse=True))
    check("top_potential_excludes_incomplete", all(t["mint"] != "Errpump" for t in tp))
    check("top_potential_graduates_only", all(t["mint"] != "LegacyXyz" for t in tp))
    check("launchpad_other_for_non_launchpad_mint", launchpad_for_mint("ApZuxdpzMrbEYTGEzeY9afh5pj9d6qPRJCTgQYiipbKg", ["dexscreener_boosts"]) == "other"
          and "pumpfun" not in coin_links({"mint": "ApZux", "launchpad": "other"}))
    check("top_potential_fomo_first", tp[0]["mint"] == "Zfomo" and tp[0]["launchpad"] == "fomo" and tp[0]["failed_reasons"]
          and "pumpfun" not in tp[0]["links"])
    check("top_potential_fields", all(k in tp[1] for k in ("symbol", "mint", "launchpad", "upside_score", "market_cap_usd",
                                                         "liquidity_usd", "passed_all_three", "failed_reasons", "links")))
    check("top_potential_pass_has_no_reasons", all((not t["failed_reasons"]) for t in tp if t["passed_all_three"]))
    # --- fresh mode ---
    check("resolve_mode_fresh_flag", resolve_mode({"scan_mode": "all_graduated"}, fresh=True) == "fresh")
    check("resolve_mode_fresh_does_not_break_all_grad", resolve_mode({"scan_mode": "all_graduated"}) == "all_graduated")
    now_f = now_utc()
    check("is_within_window_recent", is_within_window(now_f - timedelta(minutes=20), now_f, 3600) is True)
    check("is_within_window_old", is_within_window(now_f - timedelta(hours=2), now_f, 3600) is False)
    check("is_within_window_iso", is_within_window(fmt_iso(now_f - timedelta(minutes=5)), now_f, 3600) is True)
    check("is_within_window_ms", is_within_window((now_f - timedelta(minutes=10)).timestamp() * 1000, now_f, 3600) is True)
    check("is_within_window_none", is_within_window(None, now_f, 3600) is False)
    bc_raw = bytearray(49)
    # discriminator ignored; virt_tok=1000, virt_sol=76.6 SOL, real_tok=1, real_sol=46.6 SOL, supply=1e15, complete=0
    bc_raw[8:16] = (1000).to_bytes(8, "little")
    bc_raw[16:24] = int(76.6 * 1e9).to_bytes(8, "little")
    bc_raw[24:32] = (1).to_bytes(8, "little")
    bc_raw[32:40] = int(46.6 * 1e9).to_bytes(8, "little")
    bc_raw[40:48] = (10**15).to_bytes(8, "little")
    bc_raw[48] = 0
    curve = parse_bonding_curve(bytes(bc_raw))
    check("parse_bonding_curve_fields", curve is not None and abs(curve["real_sol_reserves_sol"] - 46.6) < 0.01
          and curve["complete"] is False, str(curve))
    prog = bonding_curve_progress(curve)
    # 46.6 / 55 = 84.727...
    check("bonding_progress_near_grad", prog["progress_pct"] >= 70 and abs(prog["progress_pct"] - (46.6 / 55 * 100)) < 0.2, str(prog))
    done = dict(curve, complete=True)
    check("bonding_progress_complete_is_100", bonding_curve_progress(done)["progress_pct"] == 100.0)
    check("parse_bonding_curve_short", parse_bonding_curve(b"\x00" * 20) is None)
    utm_mint = "BzLRo9cRvi7KL9XJ9EGAEi4oGCKLFkBEKvJx9D6Apump"
    check("bonding_curve_pda_known", pump_bonding_curve_pda(utm_mint) == "2UQENQc7bk2kTNpib4hJ8aJrZYzxay8E6bYMw4BewQNJ",
          str(pump_bonding_curve_pda(utm_mint)))
    fcfg = cfg_for_fresh(cfg)
    check("fresh_cfg_relaxes_age", fcfg["min_pair_age_hours"] == 0)
    check("young_pair_kept_in_fresh_prefilter", prefilter_reason(young, fcfg) is None, str(prefilter_reason(young, fcfg)))
    check("parabolic_still_skipped_in_fresh", prefilter_reason(para, fcfg) is not None and "parabolic" in (prefilter_reason(para, fcfg) or ""))
    liq_fail = {"mint": "LiqMint11111111111111111111111111111111111", "symbol": "LQ",
                "market_cap_usd": 80_000, "liquidity_usd": 500, "volume_24h_usd": 1000,
                "oldest_pair_created_at_ms": now_f.timestamp() * 1000}
    check("fresh_liquidity_floor_still_applies", prefilter_reason(liq_fail, fcfg) is not None and "low_liquidity" in (prefilter_reason(liq_fail, fcfg) or ""),
          str(prefilter_reason(liq_fail, fcfg)))
    near_rec = dict(liq_fail, liquidity_usd=4000, near_graduation=True, market_cap_usd=40_000)
    check("fresh_near_grad_lower_liq_floor", prefilter_reason(near_rec, fcfg) is None, str(prefilter_reason(near_rec, fcfg)))
    pl = public_links({"mint": "Abcpump", "launchpad": "pumpfun"})
    check("public_links_has_fomo_and_pump_and_dex", pl.get("fomo", "").endswith("/Abcpump") and pl.get("pumpfun", "").endswith("/Abcpump") and "dexscreener" in pl)
    existing = {"version": 1, "scan_mode": "all_graduated", "passed_all_three": [{"mint": "KEEP"}],
                "top_potential": [{"mint": "TOP"}], "coverage": {"mode": "all_graduated", "alive": 9},
                "run_at_hst": "2026-11-19 21:38:02 HST", "already_alerted": [{"mint": "OLD"}],
                "passed_count": 10, "data_sources": {"dexscreener": "x"}}
    fresh_block = {"run_at_hst": "2026-11-20 09:50:00 HST", "passed": [], "near_passes": []}
    merged = merge_fresh_payload(existing, fresh_block, [], False)
    check("fresh_merge_preserves_graduates", merged.get("passed_all_three") == [{"mint": "KEEP"}]
          and merged.get("top_potential") == [{"mint": "TOP"}]
          and (merged.get("coverage") or {}).get("mode") == "all_graduated"
          and merged.get("run_at_hst") == "2026-11-19 21:38:02 HST"
          and merged.get("fresh", {}).get("run_at_hst") == "2026-11-20 09:50:00 HST"
          and merged.get("notify_now") is False)
    # cooldown still suppresses a re-alert in fresh classify
    newly_f, already_f = classify_alerts([passed_coin], state_cd, cfg, now)
    check("fresh_respects_cooldown", len(already_f) == 1 and newly_f == [], f"newly={len(newly_f)} already={len(already_f)}")
    import argparse as _ap
    # argparse: --fresh is exclusive with --all-graduated
    check("fresh_schema_block_keys", {"run_at_utc", "run_at_hst", "passed", "near_passes", "runtime_sec"}.issubset(set(fresh_block) | {"run_at_utc", "runtime_sec"}))
    # dummy fresh schema on a payload
    dummy_scan["fresh"] = {"run_at_utc": fmt_iso(now), "run_at_hst": fmt_hst(now), "passed": [], "near_passes": [], "runtime_sec": 1.0}
    check("fresh_block_optional_on_scan", "fresh" in dummy_scan)

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        lp = Path(td) / "x.lock"
        l1 = acquire_lock(lp)
        l2 = acquire_lock(lp)
        check("lock_blocks_second_run", l1 is not None and l2 is None)
        release_lock(lp)
        check("lock_released", acquire_lock(lp) is not None)
        release_lock(lp)

    # end-to-end enumerate_graduated with stubbed network calls (catches wiring bugs)
    g = globals()
    saved = {k: g[k] for k in ("enumerate_pumpswap_graduates", "enumerate_raydium_legacy_graduates", "enumerate_pumpfun_api",
                                "enumerate_geckoterminal", "enumerate_dexscreener_search", "fetch_sol_price",
                                "fetch_vault_balances", "enrich_with_dexscreener", "rpc_post", "_cache_get", "_cache_put",
                                "enumerate_fomo_graduates")}
    try:
        nowu2 = time.time()
        g["enumerate_pumpswap_graduates"] = lambda h, c, s_: ({
            "Alivepump": {"venue": "pumpswap", "pool": "P1", "quote_vault": "V1"},
            "Deadliqpump": {"venue": "pumpswap", "pool": "P2", "quote_vault": "V2"},
            "Quietpump": {"venue": "pumpswap", "pool": "P3", "quote_vault": "V3"},
            "Stalepump": {"venue": "pumpswap", "pool": "P4", "quote_vault": "V4"},
        }, {"name": "stub_ps", "status": "ok", "fully_paginated": True, "graduates": 4})
        g["enumerate_raydium_legacy_graduates"] = lambda h, c, s_: ({"Raypump": {"venue": "raydium_v4", "pool": "R1", "quote_vault": "V5"}},
                                                                   {"name": "stub_ray", "status": "ok", "fully_paginated": True})
        g["enumerate_pumpfun_api"] = lambda h, s_: ({}, {"name": "stub_pf", "status": "blocked", "fully_paginated": False})
        g["enumerate_geckoterminal"] = lambda h, s_: ({"Alivepump": {"venue": "gt_pumpswap", "pool": "P1"}}, [{"name": "stub_gt", "status": "ok"}])
        g["enumerate_dexscreener_search"] = lambda h, s_: ({}, {"name": "stub_ds", "status": "ok"})
        g["enumerate_fomo_graduates"] = lambda h, c, s_: ({"Livecoinfomo": {"venue": "meteora_damm_v2", "pool": "F1", "fomopad_registered": True},
                                                           "Deadcoinfomo": {"venue": "meteora_damm_v2", "pool": "F2", "onchain_liquidity_est_usd": 4400.0}},
                                                          [{"name": "stub_dbc", "status": "ok", "fully_paginated": True, "graduates": 2},
                                                           {"name": "stub_reg", "status": "ok", "items": 3}], {"graduated": 2})
        g["fetch_sol_price"] = lambda h: 100.0
        g["fetch_vault_balances"] = lambda h, c, s_, vs: ({"V1": 500.0, "V2": 1.0, "V3": 300.0, "V4": 300.0, "V5": 200.0}, [])
        def fake_enrich(h, recs, c, force=False, retries=2, workers=1):
            for r in recs:
                if r["mint"] in ("Alivepump", "Livecoinfomo"):
                    r.update(liquidity_usd=100000, buys_24h=50, sells_24h=40, volume_24h_usd=200000, pair_address="P1")
                elif r["mint"] == "Deadcoinfomo":
                    pass
                elif r["mint"] == "Raypump":
                    r.update(liquidity_usd=3000, pair_address="R1")
                else:
                    r.update(liquidity_usd=60000, buys_24h=0, sells_24h=0, volume_24h_usd=0)
            return {"batches": 1, "batches_failed": 0}
        g["enrich_with_dexscreener"] = fake_enrich
        g["rpc_post"] = lambda h, u, m, prm, **kw: ([{"blockTime": int(nowu2 - 86400)}] if prm[0] == "P3" else [{"blockTime": int(nowu2 - 30 * 86400)}])
        g["_cache_get"] = lambda name, ttl: None
        g["_cache_put"] = lambda name, obj: None
        alive_t, meta_t = enumerate_graduated(Http(), None, dict(cfg))
        names = sorted(r["mint"] for r in alive_t)
        dd = meta_t["dropped_dead"]
        check("e2e_alive_set", names == ["Alivepump", "Livecoinfomo", "Quietpump"], str(names))
        check("e2e_dead_counts", meta_t["total_graduated_enumerated"] == 7 and dd["low_liquidity_onchain"] == 2
              and dd["low_liquidity_dexscreener"] == 1 and dd["inactive_7d"] == 1 and dd["total"] == 4, str(dd))
        check("e2e_alive_plus_dead_equals_total", meta_t["alive"] + dd["total"] == meta_t["total_graduated_enumerated"])
        check("e2e_activity_checks_counted", meta_t["activity_checks_rpc"] == 2, str(meta_t["activity_checks_rpc"]))
        check("e2e_universe_mints_exposed", meta_t.get("_universe_mints") == {"Alivepump", "Deadliqpump", "Quietpump", "Stalepump", "Raypump",
                                                                               "Livecoinfomo", "Deadcoinfomo"})
        bl = meta_t.get("by_launchpad") or {}
        check("e2e_by_launchpad_counts", bl.get("pumpfun") == {"enumerated": 5, "alive": 2, "dead": 3}
              and bl.get("fomo") == {"enumerated": 2, "alive": 1, "dead": 1}, str(bl))
        check("e2e_launchpad_tagged", all(r.get("launchpad") == ("fomo" if r["mint"].endswith("fomo") else "pumpfun") for r in alive_t))
        check("e2e_fomo_sources_listed", any(m_.get("name") == "stub_dbc" and m_.get("launchpad") == "fomo" for m_ in meta_t["sources"])
              and all(m_.get("launchpad") for m_ in meta_t["sources"]))
    finally:
        g.update(saved)

    print()
    if failures:
        print(f"{len(failures)} TEST(S) FAILED")
        return 1
    print("ALL TESTS PASSED")
    return 0


LOCK_PATH = OUT_DIR / ".scan.lock"


def acquire_lock(path: Path | None = None, stale_sec: float = 3600) -> Path | None:
    """Prevent overlapping scans (an all-graduated run can outlast a 15-minute routine tick)."""
    path = path or LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            pid = int(path.read_text().strip() or 0)
        except ValueError:
            pid = 0
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        if alive and time.time() - path.stat().st_mtime < stale_sec:
            return None
        path.unlink(missing_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))
    return path


def release_lock(path: Path | None = None) -> None:
    (path or LOCK_PATH).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Read-only Solana meme-coin scanner with instant alerts")
    p.add_argument("--selftest", action="store_true", help="Run offline unit tests and exit")
    p.add_argument("--dry-run", action="store_true", help="Full live scan but do not update state.json or flagged.csv")
    p.add_argument("--max-candidates", type=int, default=None, help="Cap deep-inspect count (overrides config; no cap by default in all-graduated mode)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--all-graduated", action="store_true", default=None,
                   help="Enumerate every pump.fun graduated token (PumpSwap + legacy Raydium) and inspect all that pass the market gate (config default)")
    g.add_argument("--subset", action="store_true", default=None,
                   help="Legacy fast mode: trending/boost/search feeds only, capped deep inspection")
    g.add_argument("--fresh", action="store_true", default=None,
                   help="Fast mode (~3 min): Solana pairs created in the last hour, recent PumpSwap/Raydium graduations, and pump.fun coins close to graduating")
    p.add_argument("--json", action="store_true", dest="as_json", help="Print latest_scan.json to stdout after the run")
    p.add_argument("--config", type=Path, default=None, help="Path to config.json")
    args = p.parse_args(argv)

    if args.selftest:
        return run_selftest()

    cfg = load_config(args.config)
    flag = True if args.all_graduated else (False if args.subset else None)
    mode = resolve_mode(cfg, flag, fresh=True if getattr(args, "fresh", None) else None)
    lock = acquire_lock()
    if lock is None:
        print(f"Another scan is already running (lock {LOCK_PATH}); exiting without changes.")
        return 0
    try:
        result = run_scan(cfg, dry_run=args.dry_run, max_candidates=args.max_candidates, mode=mode)
    except Exception:
        traceback.print_exc()
        return 2
    finally:
        release_lock()

    n_new = result.get("newly_flagged_count", 0)
    n_pass = result.get("passed_count", 0)
    print(
        f"Scan done in {result.get('runtime_sec')}s | discovered={result.get('scanned_discovery_count')} "
        f"| inspected={result.get('deeply_inspected_count')} | passed_all_3={n_pass} "
        f"| newly_flagged={n_new} | notify_now={result.get('notify_now')} "
        f"| dry_run={args.dry_run} | mode={result.get('scan_mode')}"
    )
    cov = result.get("coverage") or {}
    if cov.get("mode") == "all_graduated":
        print(f"Coverage: enumerated={cov.get('total_graduated_enumerated')} alive={cov.get('alive')} "
              f"dead={(cov.get('dropped_dead') or {}).get('total')} gate_passed={cov.get('market_gate_passed')} "
              f"inspected={cov.get('fully_inspected')} uninspected={cov.get('uninspected_after_retries')}")
    fresh = result.get("fresh") or {}
    if fresh:
        print(f"Fresh: at={fresh.get('run_at_hst')} window={fresh.get('window_sec')}s discovered={fresh.get('discovered')} "
              f"in_scope={fresh.get('in_scope')} gated={fresh.get('market_gate_passed')} inspected={fresh.get('inspected')} "
              f"passed={fresh.get('passed_count')} near={fresh.get('near_pass_count')} newly={fresh.get('newly_flagged_count')}")
    print(f"Wrote {LATEST_JSON}")
    print(f"Wrote {LATEST_MD}")
    if args.dry_run:
        print("Dry run: state.json and flagged.csv were not updated.")
    if args.as_json:
        json.dump(result, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
