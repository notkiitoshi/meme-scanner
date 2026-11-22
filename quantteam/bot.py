#!/usr/bin/env python3
"""FORGE PAPER BOT. Simulated trading only: pulls OKX PUBLIC prices, places
SIMULATED limit orders with stops attached, manages fills/exits on 15m candle
data, applies fees + funding, logs every decision. No keys, no wallets, no
real orders: there is no order-placing code anywhere in this project.

Run every 15 min by a scheduler (no daemon). Safe to re-run: a lock prevents
overlap, state is written atomically, each signal bar is acted on once, and
trade ids prevent duplicate rows after a crash.

Usage: python bot.py            # one paper cycle
       python bot.py --selftest # offline self-test (no network)
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import json
import os
import sys
import time
import uuid
from pathlib import Path

import numpy as np
import pandas as pd

from common import (ns, BTC, CONFIG, FEE, LOGS, MAX_NOTIONAL_X, MAX_OPEN, RISK_FRAC, ROOT, SLIP, START_EQUITY,
                    TF_HOURS, UNIVERSE, FetchError, atomic_write_json, get_logger, hst_today, inst,
                    now_utc, okx_get, read_json)

log = get_logger("bot")
P = {
    "state": ROOT / "state.json",
    "trades": ROOT / "trades.csv",
    "shadow_trades": ROOT / "trades_shadow.csv",
    "decisions": LOGS / "decisions.jsonl",
    "active": CONFIG / "active_setup.json",
    "halt": CONFIG / "halt.json",
    "lock": LOGS / "bot.lock",
}
TRADE_COLS = ["trade_id", "book", "setup_key", "coin", "dir", "entry_time_utc", "entry", "exit_time_utc", "exit",
              "qty", "notional", "gross_pnl", "fees", "funding", "net_pnl", "R", "exit_reason", "equity_after"]
ORDER_TTL_MIN = 60
DECISIONS = []


def decide(book, action, coin=None, reason="", **kw):
    rec = {"ts_utc": now_utc().isoformat(), "book": book, "action": action, "coin": coin, "reason": reason, **kw}
    DECISIONS.append(rec)
    with open(P["decisions"], "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def new_book(key):
    return {"setup_key": key, "cash": START_EQUITY, "positions": {}, "orders": {}, "last_signal_bar": {},
            "day_start": {}, "closed_trades": 0, "realized_pnl": 0.0, "fees_paid": 0.0, "funding_paid": 0.0,
            "last_equity": START_EQUITY}


def existing_trade_ids(path):
    if not path.exists():
        return set()
    with open(path) as f:
        return {r["trade_id"] for r in csv.DictReader(f)}


def append_trade(path, row):
    if row["trade_id"] in existing_trade_ids(path):
        return
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_COLS)
        if new:
            w.writeheader()
        w.writerow(row)
        f.flush()
        os.fsync(f.fileno())


# ---------------------------------------------------------------- market data (public only)
def fetch_bars(coin, tf, n_pages=2):
    bar = {"1h": "1H", "4h": "4H", "15m": "15m"}[tf]
    rows = okx_get("/api/v5/market/candles", {"instId": inst(coin), "bar": bar, "limit": "300"}, bucket="candles")
    after = rows[-1][0] if rows else None
    for _ in range(n_pages - 1):
        if not after:
            break
        more = okx_get("/api/v5/market/history-candles", {"instId": inst(coin), "bar": bar, "limit": "100", "after": after}, bucket="candles")
        if not more:
            break
        rows += more
        after = more[-1][0]
    df = pd.DataFrame([(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[7]), r[8]) for r in rows],
                      columns=["ts", "open", "high", "low", "close", "vol_quote", "confirm"])
    df = df.drop_duplicates("ts").sort_values("ts")
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def fetch_market(setups, coins_needed_15m):
    """Pull everything the cycle needs from OKX public endpoints."""
    import data as D
    from setups import features
    tick = okx_get("/api/v5/market/tickers", {"instType": "SWAP"}, bucket="public")
    last = {}
    newest_ts = 0
    for t in tick:
        iid = t["instId"]
        if iid.endswith("-USDT-SWAP"):
            c = iid.split("-")[0]
            if c in UNIVERSE or c == BTC:
                last[c] = float(t["last"])
                newest_ts = max(newest_ts, int(t["ts"]))
    tfs = sorted({s["tf"] for s in setups})
    need_oi = any(s["family"] == "oi_divergence" for s in setups)
    btc_close = {tf: fetch_bars(BTC, "1h", 28)["close"] for tf in tfs[:1]}
    btc_c = list(btc_close.values())[0] if btc_close else None
    mk = {"last": last, "ticker_ts_ms": newest_ts, "coins": {}}
    for c in UNIVERSE:
        m = {}
        try:
            fr = okx_get("/api/v5/public/funding-rate-history", {"instId": inst(c), "limit": "100"}, bucket="public")
            fund_new = pd.Series({pd.to_datetime(int(x["fundingTime"]), unit="ms", utc=True): float(x.get("realizedRate") or x["fundingRate"]) for x in fr}).sort_index()
            fund = pd.concat([D.load_funding(c), fund_new]).groupby(level=0).last().sort_index()
            m["funding"] = fund
            oi = D.load_oi(c)
            if need_oi:
                d = okx_get("/api/v5/rubik/stat/contracts/open-interest-history", {"instId": inst(c), "period": "1H", "limit": "100"}, bucket="rubik")
                oi_new = pd.Series({pd.to_datetime(int(x[0]), unit="ms", utc=True): float(x[3]) for x in d})
                oi = pd.concat([oi, oi_new]).groupby(level=0).last().sort_index()
            for tf in tfs:
                df = fetch_bars(c, tf, 4)
                closed = df[df["confirm"] == "1"].drop(columns=["ts", "confirm"])
                ctx = {"btc_close": btc_c, "funding": fund, "oi": oi, "bar_delta": pd.Timedelta(hours=TF_HOURS[tf])}
                m[tf] = {"bars": closed, "feat": features(closed, ctx)}
            if c in coins_needed_15m:
                m["m15"] = fetch_bars(c, "15m", 1).drop(columns=["ts"])
            mk["coins"][c] = m
        except FetchError as e:
            decide("all", "data_error", c, f"public data fetch failed: {e}")
    return mk


# ---------------------------------------------------------------- paper engine
def mark_equity(book, last):
    eq = book["cash"]
    for c, p in book["positions"].items():
        px = last.get(c, p["entry"])
        eq += p["dir"] * (px - p["entry"]) * p["qty"]
    return eq


def close_position(book_name, book, coin, px_raw, ts, why, trades_path):
    p = book["positions"].pop(coin)
    d = p["dir"]
    px = px_raw * (1 - d * SLIP)
    gross = d * (px - p["entry"]) * p["qty"]
    fee = FEE * px * p["qty"]
    book["cash"] += gross - fee
    book["fees_paid"] += fee
    net = gross - p["entry_fee"] - fee - p["funding_paid"]
    book["realized_pnl"] += net
    book["closed_trades"] += 1
    risk_usd = abs(p["entry"] - p["stop"]) * p["qty"]
    row = {"trade_id": p["id"], "book": book_name, "setup_key": p["setup_key"], "coin": coin, "dir": "long" if d > 0 else "short",
           "entry_time_utc": p["entry_time"], "entry": round(p["entry"], 10), "exit_time_utc": pd.Timestamp(ts).isoformat(),
           "exit": round(px, 10), "qty": p["qty"], "notional": round(p["entry"] * p["qty"], 4), "gross_pnl": round(gross, 4),
           "fees": round(p["entry_fee"] + fee, 4), "funding": round(p["funding_paid"], 4), "net_pnl": round(net, 4),
           "R": round(net / risk_usd, 3) if risk_usd else None, "exit_reason": why, "equity_after": round(book["cash"], 4)}
    append_trade(trades_path, row)
    decide(book_name, "exit", coin, why, price=px, net_pnl=round(net, 4), R=row["R"], trade_id=p["id"])
    return row


def manage_orders(book_name, book, m15_by_coin, now):
    for coin, o in list(book["orders"].items()):
        m15 = m15_by_coin.get(coin)
        filled = False
        if m15 is not None and len(m15):
            w = m15[m15.index + pd.Timedelta(minutes=15) > pd.Timestamp(o["placed"])]
            for ts, r in w.iterrows():
                if (o["dir"] > 0 and r["low"] <= o["limit"]) or (o["dir"] < 0 and r["high"] >= o["limit"]):
                    fill(book_name, book, coin, o, max(ts, pd.Timestamp(o["placed"])), "limit touched on 15m candle")
                    filled = True
                    break
        if not filled and now >= pd.Timestamp(o["expires"]):
            book["orders"].pop(coin)
            decide(book_name, "cancel_order", coin, f"limit {o['limit']} not filled within {ORDER_TTL_MIN} min")


def fill(book_name, book, coin, o, ts, why):
    book["orders"].pop(coin, None)
    fee = FEE * o["limit"] * o["qty"]
    book["cash"] -= fee
    book["fees_paid"] += fee
    book["positions"][coin] = {"id": o["id"], "setup_key": o["setup_key"], "dir": o["dir"], "entry": o["limit"],
                               "qty": o["qty"], "stop": o["stop"], "tp": o["tp"], "entry_time": pd.Timestamp(ts).isoformat(),
                               "max_exit_time": (pd.Timestamp(ts) + pd.Timedelta(hours=o["max_hold_h"])).isoformat(),
                               "entry_fee": fee, "funding_paid": 0.0, "last_checked": pd.Timestamp(ts).isoformat(),
                               "last_funding_ns": int(pd.Timestamp(ts).value)}
    decide(book_name, "fill", coin, why, price=o["limit"], qty=o["qty"], stop=o["stop"], tp=o["tp"], trade_id=o["id"])


def manage_positions(book_name, book, mk_coins, last, now, trades_path):
    closed = []
    for coin, p in list(book["positions"].items()):
        cm = mk_coins.get(coin, {})
        # funding settlements while open (long pays positive rate, short receives)
        fund = cm.get("funding")
        if fund is not None and len(fund):
            ev = fund[(ns(fund.index) > p["last_funding_ns"]) & (fund.index <= now)]
            for t, rate in ev.items():
                px = last.get(coin, p["entry"])
                cost = p["dir"] * rate * px * p["qty"]
                p["funding_paid"] += cost
                book["cash"] -= cost
                book["funding_paid"] += cost
                p["last_funding_ns"] = int(t.value)
                decide(book_name, "funding", coin, f"funding settlement {rate*100:.4f}%", cost=round(cost, 5))
        m15 = cm.get("m15")
        done = False
        if m15 is not None and len(m15):
            w = m15[m15.index + pd.Timedelta(minutes=15) > pd.Timestamp(p["last_checked"])]
            for ts, r in w.iterrows():
                d = p["dir"]
                hit_stop = (r["low"] <= p["stop"]) if d > 0 else (r["high"] >= p["stop"])
                hit_tp = (r["high"] >= p["tp"]) if d > 0 else (r["low"] <= p["tp"])
                if hit_stop:     # stop first if both in one candle (conservative)
                    gap = (d > 0 and r["open"] < p["stop"]) or (d < 0 and r["open"] > p["stop"])
                    closed.append(close_position(book_name, book, coin, r["open"] if gap else p["stop"], ts + pd.Timedelta(minutes=15), "stop", trades_path))
                    done = True
                    break
                if hit_tp:
                    closed.append(close_position(book_name, book, coin, p["tp"], ts + pd.Timedelta(minutes=15), "take_profit", trades_path))
                    done = True
                    break
            if not done:
                p["last_checked"] = (w.index[-1]).isoformat() if len(w) else p["last_checked"]
        if not done and now >= pd.Timestamp(p["max_exit_time"]) and coin in last:
            closed.append(close_position(book_name, book, coin, last[coin], now, "time_exit", trades_path))
    return closed


def place_entries(book_name, book, setup, mk_coins, last, now, halted, halt_reason):
    from setups import signals
    tf = setup["tf"]
    tfh = TF_HOURS[tf]
    placed = []
    scanned, fired, last_bar = 0, 0, None
    for coin in UNIVERSE:
        cm = mk_coins.get(coin)
        if not cm or tf not in cm:
            continue
        bars, feat = cm[tf]["bars"], cm[tf]["feat"]
        if len(bars) < 250:
            continue
        sig = cm[tf].get("sig")
        if sig is None:
            sig = signals(setup, bars, feat, tfh)
            cm[tf]["sig"] = sig
        s = int(sig.iloc[-1])
        bar_ts = bars.index[-1].isoformat()
        scanned += 1
        last_bar = bar_ts
        fired += int(s != 0)
        if s == 0 or book["last_signal_bar"].get(coin) == bar_ts:
            continue
        book["last_signal_bar"][coin] = bar_ts
        bar_close = bars.index[-1] + pd.Timedelta(hours=tfh)
        d = "long" if s > 0 else "short"
        if now - bar_close > pd.Timedelta(hours=tfh):
            decide(book_name, "skip_signal", coin, f"{d} signal on bar closed {bar_close} is older than one bar (missed run)")
            continue
        if halted:
            decide(book_name, "blocked_by_shield", coin, f"{d} signal ignored: {halt_reason}")
            continue
        if coin in book["positions"] or coin in book["orders"]:
            decide(book_name, "skip_signal", coin, f"{d} signal but already have a position/order in {coin}")
            continue
        if len(book["positions"]) + len(book["orders"]) >= MAX_OPEN:
            decide(book_name, "skip_signal", coin, f"{d} signal but {MAX_OPEN} positions/orders already open")
            continue
        px = last.get(coin)
        a = float(feat["atr"].iloc[-1])
        if not px or not np.isfinite(a) or a <= 0:
            decide(book_name, "skip_signal", coin, "no live price or ATR")
            continue
        limit = px * (1 + s * SLIP)
        dist = setup["exit"]["stop_atr"] * a
        stop = limit - s * dist
        tp = limit + s * setup["exit"]["tp_r"] * dist
        eq = mark_equity(book, last)
        stop_pct = dist / limit
        if stop <= 0 or stop_pct < 0.002 or stop_pct > 0.35:
            decide(book_name, "skip_signal", coin, f"stop distance {stop_pct:.4f} outside 0.2%-35%")
            continue
        qty = eq * RISK_FRAC / dist
        if qty * limit > MAX_NOTIONAL_X * eq:
            qty = MAX_NOTIONAL_X * eq / limit
        o = {"id": uuid.uuid4().hex[:12], "setup_key": setup["key"], "dir": s, "limit": limit, "qty": qty, "stop": stop,
             "tp": tp, "max_hold_h": setup["exit"]["max_hold_h"], "placed": now.isoformat(),
             "expires": (now + pd.Timedelta(minutes=ORDER_TTL_MIN)).isoformat(), "signal_bar": bar_ts}
        book["orders"][coin] = o
        decide(book_name, "place_limit", coin,
               f"{setup['key']} {d} signal on {tf} bar closing {bar_close}: {setup.get('reason', '')}",
               limit=limit, stop=stop, tp=tp, qty=qty, risk_usd=round(qty * dist, 4), equity=round(eq, 4), regime=str(feat['regime'].iloc[-1]))
        if (s > 0 and px <= limit) or (s < 0 and px >= limit):
            fill(book_name, book, coin, o, now, f"marketable limit: live {px} within limit {limit:.10g}")
        placed.append(coin)
    if not placed:
        decide(book_name, "no_entry", None,
               f"{setup['key']}: scanned {scanned} coins on the {tf} bar opened {last_bar}; {fired} signal(s), none taken"
               + (" (halted)" if halted else ""), scanned=scanned, signals=fired)
    return placed


def halt_status(now):
    h = read_json(P["halt"])
    if h and pd.Timestamp(h.get("until")) > now:
        return True, f"Shield halt until {h['until']}: {h.get('reason')}"
    return False, ""


def run_cycle(market=None, now=None):
    now = now or pd.Timestamp(now_utc())
    active = read_json(P["active"])
    if not active or "leader" not in active:
        raise RuntimeError(f"{P['active']} missing; run forge.py first")
    setups = [dict(active["leader"], book="main")] + [dict(s, book=f"shadow:{s['key']}") for s in active.get("shadows", [])]
    state = read_json(P["state"]) or {"created_utc": now.isoformat(), "books": {}, "runs": 0}
    for s in setups:
        state["books"].setdefault(s["book"], new_book(s["key"]))
        state["books"][s["book"]]["setup_key"] = s["key"]
    need15 = set()
    for b in state["books"].values():
        need15 |= set(b["positions"]) | set(b["orders"])
    t0 = time.time()
    if market is None:
        market = fetch_market(setups, need15)
    last = market["last"]
    halted, why = halt_status(now)
    summary = {}
    for s in setups:
        bn = s["book"]
        book = state["books"][bn]
        tp_ = P["trades"] if bn == "main" else P["shadow_trades"]
        m15 = {c: m.get("m15") for c, m in market["coins"].items()}
        manage_orders(bn, book, m15, now)
        closed = manage_positions(bn, book, market["coins"], last, now, tp_)
        if s.get("standby"):
            placed = []
            decide(bn, "standby", None, s.get("standby_reason", "no setup passed Lab filters; managing existing positions only"))
        else:
            placed = place_entries(bn, book, s, market["coins"], last, now, halted and bn == "main", why)
        # after a fill this run, new positions need 15m management next run (they are checked from fill time)
        eq = mark_equity(book, last)
        day = hst_today()
        book["day_start"].setdefault(day, eq)
        book["day_start"] = dict(list(book["day_start"].items())[-30:])
        book["last_equity"] = eq
        summary[bn] = {"setup": s["key"], "equity": round(eq, 4), "cash": round(book["cash"], 4),
                       "open_positions": list(book["positions"]), "open_orders": list(book["orders"]),
                       "closed_this_run": len(closed), "placed_this_run": placed}
        state["runs"] = state.get("runs", 0) + (1 if bn == "main" else 0)
        state["last_run_utc"] = now.isoformat()
        state["last_price_ts_utc"] = pd.Timestamp(market["ticker_ts_ms"], unit="ms", tz="UTC").isoformat() if market.get("ticker_ts_ms") else None
        atomic_write_json(P["state"], state)
    decide("all", "cycle_done", None, "halted: " + why if halted else "ok", seconds=round(time.time() - t0, 1))
    return {"ok": True, "halted": halted, "halt_reason": why, "books": summary,
            "last_price_ts_utc": state.get("last_price_ts_utc"), "decisions_logged": len(DECISIONS)}


# ---------------------------------------------------------------- self-test (offline)
def selftest():
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="qt_selftest_"))
    for k in P:
        P[k] = tmp / Path(P[k]).name
    results = []

    def check(name, cond, detail=""):
        results.append({"test": name, "ok": bool(cond), "detail": detail})

    idx = pd.date_range("2026-01-01", periods=300, freq="1h", tz="UTC")
    px = np.full(300, 1.0)
    bars = pd.DataFrame({"open": px, "high": px * 1.01, "low": px * 0.99, "close": px, "vol_quote": 1e6}, index=idx)
    feat = pd.DataFrame({"atr": np.full(300, 0.02), "regime": "trending"}, index=idx)
    sig = pd.Series(0, index=idx)
    sig.iloc[-1] = 1
    now = idx[-1] + pd.Timedelta(hours=1, minutes=5)
    setup = {"key": "TEST@1h", "tf": "1h", "family": "range_breakout", "reason": "selftest",
             "exit": {"stop_atr": 1.5, "tp_r": 2.0, "max_hold_h": 48}}
    atomic_write_json(P["active"], {"leader": setup, "shadows": []})
    mk = {"last": {"DOGE": 1.0}, "ticker_ts_ms": int(now.value // 10**6),
          "coins": {"DOGE": {"1h": {"bars": bars, "feat": feat, "sig": sig}, "funding": pd.Series(dtype=float)}}}
    r1 = run_cycle(mk, now)
    st = read_json(P["state"])
    pos = st["books"]["main"]["positions"].get("DOGE")
    check("entry placed and filled", pos is not None, str(r1["books"]["main"]))
    if pos:
        risk = abs(pos["entry"] - pos["stop"]) * pos["qty"]
        check("risk is 1% of equity", abs(risk - 1.46) < 0.02, f"risk ${risk:.4f}")
        check("stop attached below long entry", pos["stop"] < pos["entry"] < pos["tp"])
    r2 = run_cycle(mk, now + pd.Timedelta(minutes=15))
    st = read_json(P["state"])
    check("same signal bar not traded twice", len(st["books"]["main"]["positions"]) == 1 and r2["books"]["main"]["placed_this_run"] == [])
    # stop-out on a 15m candle
    t = now + pd.Timedelta(minutes=30)
    m15 = pd.DataFrame({"open": [0.99], "high": [0.995], "low": [0.95], "close": [0.96], "vol_quote": [1]},
                       index=[t.floor("15min")])
    mk["coins"]["DOGE"]["m15"] = m15
    mk["coins"]["DOGE"]["funding"] = pd.Series([0.0001], index=[t - pd.Timedelta(minutes=1)])
    mk["last"]["DOGE"] = 0.96
    run_cycle(mk, t + pd.Timedelta(minutes=16))
    st = read_json(P["state"])
    tr = pd.read_csv(P["trades"]) if P["trades"].exists() else pd.DataFrame()
    check("stop exit recorded in trades.csv", len(tr) == 1 and tr["exit_reason"].iloc[0] == "stop", tr.to_dict("records"))
    if len(tr):
        check("loss ~1R plus fees/funding", -1.65 < tr["net_pnl"].iloc[0] < -1.40, f"net {tr['net_pnl'].iloc[0]}")
        check("fees charged both sides", tr["fees"].iloc[0] > 0.05, f"fees {tr['fees'].iloc[0]}")
        check("funding applied", tr["funding"].iloc[0] > 0, f"funding {tr['funding'].iloc[0]}")
    # halt flag blocks new entries
    atomic_write_json(P["halt"], {"until": (t + pd.Timedelta(days=1)).isoformat(), "reason": "selftest"})
    sig2 = sig.copy()
    idx2 = idx + pd.Timedelta(hours=2)
    bars2 = bars.set_axis(idx2)
    feat2 = feat.set_axis(idx2)
    mk["coins"]["DOGE"]["1h"] = {"bars": bars2, "feat": feat2, "sig": sig2.set_axis(idx2)}
    mk["coins"]["DOGE"].pop("m15")
    mk["last"]["DOGE"] = 1.0
    r4 = run_cycle(mk, idx2[-1] + pd.Timedelta(hours=1, minutes=2))
    check("halt flag blocks entries", r4["halted"] and r4["books"]["main"]["placed_this_run"] == [])
    # crash safety: re-appending an existing trade id must not duplicate
    row = pd.read_csv(P["trades"]).iloc[0].to_dict()
    append_trade(P["trades"], {k: row[k] for k in TRADE_COLS})
    check("duplicate trade ids ignored after restart", len(pd.read_csv(P["trades"])) == 1)
    check("state.json survives reload", read_json(P["state"])["books"]["main"]["closed_trades"] == 1)
    src = "".join(open(Path(__file__).parent / f).read() for f in ("bot.py", "common.py", "data.py"))
    bad = [s for s in ("/api/v5/trade", "/api/v5/account", "OK-ACCESS-KEY", "/api/v5/asset") if s in src.replace('"/api/v5/trade", "/api/v5/account", "OK-ACCESS-KEY", "/api/v5/asset"', "")]
    check("no private/order endpoints or API keys in code", not bad, str(bad))
    ok = all(r["ok"] for r in results)
    return {"ok": ok, "selftest": results, "tmp_dir": str(tmp)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        r = selftest()
        print(json.dumps(r, indent=2, default=str))
        sys.exit(0 if r["ok"] else 1)
    lockf = open(P["lock"], "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({"ok": True, "skipped": "another bot.py run holds the lock"}))
        return
    r = run_cycle()
    log.info("cycle: %s", json.dumps(r["books"], default=str))
    print(json.dumps(r, indent=2, default=str))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # noqa
        log.exception("bot failed")
        print(json.dumps({"ok": False, "error": f"bot failed: {e}"}))
        sys.exit(1)
