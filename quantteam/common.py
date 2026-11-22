"""Shared helpers for the QuantTeam paper-research system.

PAPER ONLY. Everything here reads free, unauthenticated public market-data
endpoints (OKX public REST, CoinGecko free API). There are no API keys, no
wallets, no account endpoints and no order endpoints anywhere in this project.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = ROOT / "out"
LOGS = ROOT / "logs"
CONFIG = ROOT / "config"
for _d in (DATA, OUT, LOGS, CONFIG, DATA / "candles", DATA / "funding", DATA / "oi", DATA / "coingecko"):
    _d.mkdir(parents=True, exist_ok=True)

HST = ZoneInfo("Pacific/Honolulu")
UTC = timezone.utc

UNIVERSE = [
    "DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI", "BRETT", "POPCAT",
    "TRUMP", "FARTCOIN", "PENGU", "SPX", "MEME", "NEIRO", "PNUT", "BOME",
    "MEW", "TURBO", "MOODENG", "PEOPLE",
]
BTC = "BTC"
ALL_COINS = UNIVERSE + [BTC]

COINGECKO_IDS = {
    "BTC": "bitcoin", "DOGE": "dogecoin", "SHIB": "shiba-inu", "PEPE": "pepe",
    "WIF": "dogwifcoin", "BONK": "bonk", "FLOKI": "floki", "BRETT": "based-brett",
    "POPCAT": "popcat", "TRUMP": "official-trump", "FARTCOIN": "fartcoin",
    "PENGU": "pudgy-penguins", "SPX": "spx6900", "MEME": "memecoin-2",
    "NEIRO": "neiro-3", "PNUT": "peanut-the-squirrel", "BOME": "book-of-meme",
    "MEW": "cat-in-a-dogs-world", "TURBO": "turbo", "MOODENG": "moo-deng",
    "PEOPLE": "constitutiondao",
}

# Paper-account and cost model (shared by Lab and the bot so they agree).
START_EQUITY = 146.0
RISK_FRAC = 0.01          # 1% of equity at risk per trade (distance to stop sizes it)
MAX_NOTIONAL_X = 3.0      # hard cap: notional <= 3x equity
MAX_OPEN = 3
FEE = 0.001               # 0.1% per side
SLIP = 0.0005             # 0.05% per side
TIMEFRAMES = ["1h", "4h"]
TF_HOURS = {"1h": 1, "4h": 4}

OKX = "https://www.okx.com"
UA = "quantteam-paper/1.0 (paper research only; no keys, no orders)"


def inst(coin: str) -> str:
    return f"{coin}-USDT-SWAP"


def now_utc() -> datetime:
    return datetime.now(UTC)


def hst_today() -> str:
    return datetime.now(HST).strftime("%Y-%m-%d")


def hst_str(ts_ms: float | int | None) -> str | None:
    if ts_ms is None:
        return None
    return datetime.fromtimestamp(ts_ms / 1000, HST).strftime("%Y-%m-%d %H:%M HST")


def next_hst_midnight() -> datetime:
    n = datetime.now(HST)
    return (n + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def get_logger(name: str) -> logging.Logger:
    lg = logging.getLogger(name)
    if lg.handlers:
        return lg
    lg.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    fh = logging.FileHandler(LOGS / f"{name}.log")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    lg.addHandler(fh)
    lg.addHandler(sh)
    return lg


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


class RateLimiter:
    def __init__(self, per_sec: float):
        self.gap = 1.0 / per_sec
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self):
        with self.lock:
            t = time.monotonic()
            if t < self.next:
                time.sleep(self.next - t)
                t = self.next
            self.next = t + self.gap


_SESSION = threading.local()


def _sess() -> requests.Session:
    s = getattr(_SESSION, "s", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = UA
        _SESSION.s = s
    return s


LIMITS = {
    "candles": RateLimiter(8.0),     # OKX history-candles: 20 req / 2 s
    "public": RateLimiter(4.0),      # OKX funding: 10 req / 2 s
    "rubik": RateLimiter(2.0),       # OKX rubik stats: 5 req / 2 s
    "coingecko": RateLimiter(0.25),  # CoinGecko free: be gentle (~15/min)
}


class FetchError(RuntimeError):
    pass


def okx_get(path: str, params: dict, bucket: str = "public", tries: int = 5) -> list:
    """GET a public OKX endpoint. Raises FetchError after retries."""
    assert path.startswith("/api/v5/market/") or path.startswith("/api/v5/public/") \
        or path.startswith("/api/v5/rubik/"), "only public endpoints allowed"
    last = None
    for i in range(tries):
        LIMITS[bucket].wait()
        try:
            r = _sess().get(OKX + path, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(1.5 * (i + 1))
                last = "HTTP 429"
                continue
            r.raise_for_status()
            j = r.json()
            if j.get("code") != "0":
                if j.get("code") in ("50011", "50061"):  # rate limit
                    time.sleep(1.5 * (i + 1))
                    last = j
                    continue
                raise FetchError(f"OKX {path} {params}: {j.get('code')} {j.get('msg')}")
            return j.get("data", [])
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(1.0 * (i + 1))
    raise FetchError(f"OKX {path} {params} failed after {tries} tries: {last}")


def coingecko_get(path: str, params: dict, tries: int = 4) -> Any:
    last = None
    for i in range(tries):
        LIMITS["coingecko"].wait()
        try:
            r = _sess().get("https://api.coingecko.com/api/v3" + path, params=params, timeout=30)
            if r.status_code == 429:
                wait = int(r.headers.get("retry-after", "0") or 0) or 20 * (i + 1)
                time.sleep(min(wait, 90))
                last = "HTTP 429"
                continue
            if 400 <= r.status_code < 500:
                raise FetchError(f"CoinGecko {path}: HTTP {r.status_code} (not retried)")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(5 * (i + 1))
    raise FetchError(f"CoinGecko {path} failed: {last}")


def fail(logger: logging.Logger, msg: str, code: int = 2):
    logger.error(msg)
    print(json.dumps({"ok": False, "error": msg}))
    sys.exit(code)


def ns(idx) -> "np.ndarray":
    """int64 nanoseconds for a DatetimeIndex regardless of its stored unit (pandas 3 safe)."""
    import pandas as pd
    return pd.DatetimeIndex(idx).as_unit("ns").asi8
