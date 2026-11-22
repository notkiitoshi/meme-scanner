# QuantTeam: paper-only meme-perp research and paper trading

**PAPER ONLY.** Every script reads free, unauthenticated public endpoints: OKX public
market data (`/api/v5/market/*`, `/api/v5/public/*`, `/api/v5/rubik/*`) and the CoinGecko free API.
There are no API keys, no wallets, and no account or order endpoints. `okx_get()` refuses any
other path, and `bot.py --selftest` checks the source for private endpoints.
`/workspace/memesim/` is a separate project. Nothing here reads from it or writes to it.

## Universe and account
- The 20 OKX USDT-SWAP meme perps: DOGE, SHIB, PEPE, WIF, BONK, FLOKI, BRETT, POPCAT, TRUMP,
  FARTCOIN, PENGU, SPX, MEME, NEIRO, PNUT, BOME, MEW, TURBO, MOODENG and PEOPLE. BTC is the regime reference.
- Timeframes are 1h and 4h. 4h bars are built from 1h bars, aligned to UTC, and only complete bars are used.
- The paper account is $146. Each trade risks 1% of equity: notional = 1% × equity / stop distance,
  capped at 3x equity. At most 3 positions are open at once.
- Costs: 0.1% fee per side, 0.05% slippage per side, and funding at every settlement (longs pay
  positive funding, shorts receive it).

## The team (entry points)
| Step | Command | What it does | Output |
|---|---|---|---|
| Radar | `python3 radar.py [--no-refresh] [--skip-coingecko]` | Refreshes the data cache incrementally, ranks coins on trend, momentum, volume, funding, OI and volatility, labels regimes for each coin and BTC, publishes 31 setup ideas with live signals | `out/radar_YYYY-MM-DD.json`, `out/radar_latest.json` |
| Lab | `python3 lab.py [--workers 6] [--only ID,..] [--tf 1h,4h]` | Backtests every Radar setup on 1h and 4h: OOS split, walk-forward, Monte Carlo, double fees, parameter neighborhood, lookahead test. Scores 1-5, kills weak setups, writes strategy cards | `out/lab_YYYY-MM-DD.json`, `out/lab_latest.json` |
| Forge | `python3 forge.py [--promote KEY] [--reselect]` | Keeps the current leader while Lab still has it alive (otherwise takes Lab's leader), writes `config/active_setup.json` and adds the runner-ups as shadow books, then runs `bot.py --selftest` | `config/active_setup.json`, `out/forge_latest.json` |
| Bot | `python3 bot.py` (every 15 min) / `python3 bot.py --selftest` | One paper cycle: live OKX public prices, simulated limit orders with stops attached, exits on 15m candles, fees and funding | `state.json`, `trades.csv`, `trades_shadow.csv`, `logs/decisions.jsonl` |
| Shield | `python3 shield.py` (every 15 min, after bot) | Writes a halt flag for the rest of the HST day on a -3% day, 3 straight losses or a price feed older than 30 min. Also tracks the leader against the runner-ups | `config/halt.json`, `out/shield_status.json`, `out/shield_events.jsonl`, `data/paper_daily.json` |
| Brief | `python3 brief.py [--json]` (7:30 AM HST) | The morning brief: market in 3 lines, top 3 setups, last 24h of paper trades, Shield items, one decision | `out/brief_YYYY-MM-DD.md/.json` |
| Weekly | `python3 weekly.py [--json]` (Sunday) | Compares the leader with the runner-ups, flags fading edges, lists cut candidates | `out/weekly_YYYY-MM-DD.md/.json` |
| Test | `./run_all_test.sh [--skip-coingecko]` | Runs every step in order, times each one and stops at the first failure | `logs/test_*.out/.err` |

Every script logs to `logs/<name>.log`. Each one exits nonzero with a JSON `{"ok": false, "error": ...}` on failure.
Each one is idempotent: reruns overwrite the day's output, the data cache only adds new rows, and the bot
acts on each signal bar once.

## Data sources and coverage
- **OKX 1H history candles** go back up to 4 years. `data/coverage.json` records the actual start date for each coin.
  DOGE, SHIB, PEOPLE and BTC have the full 4 years. The rest start at their OKX listing date (2023-2025).
- **OKX funding history**: the public endpoint serves only about 3 months. For earlier periods, the backtest uses an
  ESTIMATE, the coin's median observed rate at its observed interval. Cards report the share of trades with estimated funding.
- **OKX open interest** (rubik, 1H): about 60 days only. OI setups can be tested only over that window,
  so most fail the 50-trade rule.
- **CoinGecko** (free tier, about 90 days hourly) cross-checks the last 7 days of prices and 24h volume. Results are cached
  for 20h, 429s back off, and other 4xx responses are not retried.

## Lab method (all results are ESTIMATES)
- Entry is at the next bar's open after a closed-bar signal. If the stop and the target are both hit in one bar, the stop counts.
  Gaps through the stop fill at the open.
- OOS is the last 30% of each coin's history. Walk-forward uses 5 segments with an expanding train window, picks the best
  neighborhood variant by train R and tests it on the next segment.
- The neighborhood is ±25% on each tunable parameter and on the stop multiple.
- Monte Carlo runs 1000 trade-order shuffles (for drawdown) and 1000 bootstraps (for the chance of a loss).
- The lookahead test recomputes features and signals on history that ends at signal bars and requires an exact match.
  It was verified to catch a deliberately cheating signal.
- **Kill rules:** fewer than 50 trades, OOS return after costs not above equal-weight buy-and-hold over the same OOS
  windows, or a lookahead failure.
- **Passes all filters** means it was not killed and also has positive OOS and in-sample returns, survives double fees,
  is profitable in walk-forward with at least 2 positive folds, has at least 60% of neighborhood variants positive,
  a bootstrap loss probability of 20% or less, and at least 30 OOS trades.
- **Score (1-5):** one point each for OOS profit above buy-and-hold, OOS max drawdown better than -15%, at least 100 trades
  with at least 30 OOS, a short losing streak, and positive avg R in at least 2 of 3 BTC regimes
  (bull, bear or sideways by BTC's 30-day move of ±10%).

## Bot behaviour
- `config/active_setup.json` is the single file that controls which setup runs. Editing it, or running
  `forge.py --promote KEY`, swaps the setup. Open positions keep their own stop, target and time exit.
- Each cycle: take a lock, read the config, check the halt flag, pull public data, fill or expire paper limit orders,
  apply funding, check stops and targets on 15m candles, apply time exits, then look for new signals on the last closed
  bar. A new signal becomes a marketable paper limit at the live price ±0.05%, valid for 60 minutes.
- A Shield halt blocks new entries in the main book for the rest of the HST day. Exits keep being managed.
- Runner-up shadow books trade the same way with their own $146 each. That lets Shield compare paper days.
  A swap is recommended after 7 completed HST days in which the runner-up beat the leader on at least 5 days and on
  cumulative return.
- Shield `notify_now` is true only 7 AM-10 PM HST, except for a new halt, which notifies at any time.

## Files
```
quantteam/
  common.py  data.py  setups.py  backtest.py      shared code (public HTTP, cache, indicators, catalog, engine)
  radar.py lab.py forge.py bot.py shield.py brief.py weekly.py run_all_test.sh
  config/active_setup.json  config/halt.json
  data/candles/*_1H.csv.gz  data/funding/*.csv  data/oi/*.csv  data/coingecko/*.json  data/coverage.json  data/paper_daily.json
  out/radar_*.json lab_*.json brief_*.md weekly_*.md shield_status.json forge_latest.json
  logs/*.log  logs/decisions.jsonl
  state.json  trades.csv  trades_shadow.csv
```

## Known limits
- Survivorship bias: the universe is today's listed memes, so coins that died are not in the test.
- Most memes have only 1-2 years of history, which means a single market cycle. The OOS windows are mostly the 2025-26
  meme downtrend, and that favours short setups. Equal-weight buy-and-hold was about -50% over those windows, so
  "beats buy-and-hold" is a low bar. Look at OOS > 0 and at the full filter set instead.
- The news field in Radar is a stub that the scheduled routine fills from real sources. Nothing is invented.
