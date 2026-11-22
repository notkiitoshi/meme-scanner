# Meme Coin Scanner

Read-only Solana meme-coin research agent. It scans public feeds, then flags a coin **only if it passes all three filters**:

1. Low or no bundle activity
2. Low insider concentration
3. Potential for a 5x-10x run

This is research and alerting only. It never uses wallets, private keys, exchange keys, or places orders. Numbers come from live public APIs; missing data fails closed (the coin does not pass).

The scanner lives in `/workspace/memescanner/` and does **not** touch `/workspace/memesim/` or `/workspace/quantteam/`.

## Instant alerts (default — not a daily digest)

Default notification behavior is **instant**. The intended cadence is a frequent run (for example every 15 minutes). When a coin first passes all three filters, the run writes:

- `notify_now: true`
- that coin in `newly_flagged`

so a routine can alert immediately. There is no daily digest mode.

Cooldown dedup lives in `state.json`. Each mint triggers an instant alert **once**, then stays quiet for `alert_cooldown_hours` (default 24h) unless it is **materially upgraded**:

- upside score jumped by 15+ points, or
- insider-network % or top-10 non-LP % dropped by 5+ points, or
- weighted LP lock/burned % rose by 15+ points, or
- the 24h cooldown expired

`--dry-run` still writes `out/latest_scan.json` and `out/latest_report.md`, but does not update `state.json` or append `flagged.csv`.

## How to run

```bash
# Offline unit tests (no network)
python3 /workspace/memescanner/scanner.py --selftest

# Live scan (writes outputs + state; use this on the 15-minute cadence)
python3 /workspace/memescanner/scanner.py

# Live scan without touching state.json / flagged.csv
python3 /workspace/memescanner/scanner.py --dry-run

# Cap how many coins get the slow RugCheck + RPC pass
python3 /workspace/memescanner/scanner.py --max-candidates 10

# Also print the JSON result to stdout
python3 /workspace/memescanner/scanner.py --json
```

Optional `--config /path/to/config.json` overrides the default thresholds file.

## Outputs

| Path | What it is |
| --- | --- |
| `out/latest_scan.json` | Full structured result, including `notify_now`, `newly_flagged`, `already_alerted`, `passed_all_three`, top `near_misses` with exact fail reasons, data sources, runtime |
| `out/latest_report.md` | Human-readable summary with DexScreener, Solscan, and RugCheck links plus **WHY THIS COULD FAIL** on every card |
| `state.json` | Run count, last run UTC + HST, alerted mints and scores used for cooldown / upgrade detection |
| `flagged.csv` | Append-only log of newly flagged coins (instant-alert events only) |

## Scan modes

`config.json` → `"scan_mode": "all_graduated"` is the default. Override per run with `--all-graduated` or `--subset`.

### All-graduated mode (`--all-graduated`)

Token scope = every pump.fun token whose bonding curve completed and migrated, enumerated from public keyless sources:

1. **Solana RPC `getProgramAccounts` on PumpSwap** (`pAMMBay6…`): every WSOL-quoted pool account is read; a token counts as graduated when its pool is index 0, the base mint ends in `pump`, and the pool creator equals the pump.fun `pool-authority` PDA (the canonical migration pool; verified offline with a pure-Python PDA derivation).
2. **Solana RPC `getProgramAccounts` on Raydium AMM v4**: pump-suffix base / WSOL quote pools, which covers pre-March-2025 graduates (heuristic: Raydium v4 has no migrator field).
3. **pump.fun frontend API** `coins?complete=true`: paginated when reachable. It is Cloudflare-blocked from some machines; the scanner records that and does not try to bypass it.
4. **GeckoTerminal** `dexes/{pumpswap,raydium}/pools` (free tier: 10 pages × 20, volume / tx-count sorts) and **DexScreener search**: these supplement the list with fresh market data.

The gPA results are cached in `out/cache/` for `gpa_cache_ttl_hours`.

**Dead cutoff** (`graduated_scope` in config.json): a token is dropped as dead when its liquidity is below `dead_if_liquidity_below_usd` (default **$5,000**) or it has had no trade or pool activity in `dead_if_no_activity_days` (default **7**) days.

- Liquidity is checked first from the on-chain WSOL vault balance (×2 × SOL price, via batched `getMultipleAccounts`), then from DexScreener.
- Activity comes from DexScreener 24h txns. Liquid pools with zero 24h trades get one `getSignaturesForAddress(pool, limit 1)` check instead.

Every alive token gets the DexScreener batch market data and the unchanged cheap market gate. Every token that passes the gate gets the full bundle / insider / RugCheck / RPC inspection with no cap.

- Inspections that hit RugCheck or RPC errors are retried (`inspect_retry_rounds`, with increasing sleeps).
- Anything still unfinished is listed in `coverage.uninspected_mints`.
- The legacy trending/boost feeds are still added as extras (`include_legacy_feeds`).

A full run takes roughly 5–25 minutes. A lock file (`out/.scan.lock`) stops overlapping runs, so a 15-minute routine tick that lands mid-run exits cleanly without writing anything.

`out/latest_scan.json` → `coverage` reports:

- sources queried, with status and whether each was fully paginated
- total graduated tokens enumerated, alive vs dead (by reason)
- how many passed the market gate and how many were fully inspected
- initial inspection errors, retry rounds, and how many are still uninspected
- 429 counts and the scan timestamp

The dashboard shows this in its **Coverage** panel.

## Criteria and default thresholds

All thresholds are in `config.json` and can be changed without editing code.

### Cheap prefilter (before RugCheck / RPC)

Keeps a 15-minute run fast (~30-60s). Drops stables, SOL/WSOL, liquid-staking tokens, wrapped majors, infra tokens, and anything outside the 5x-10x band:

- Market cap **$200k-$15M** (uses DexScreener `marketCap`, falls back to FDV)
- Liquidity ≥ **$30k**
- 24h volume ≥ **$100k**
- Pair age ≥ **6 hours**
- 24h price change ≤ **+1500%** (parabolic blowoff cap)
- **All-graduated mode (default):** every coin that passes this gate is deep-inspected (no cap).
- **Subset mode (`--subset`):** at most `max_candidates_per_run` (default **20**) coins go to deep inspect, ranked toward the $500k-$5M sweet spot and higher volume

### Criterion 1 — bundle activity

Fails if any of these are observed:

- RugCheck risk flags for bundled supply, snipers, creator-linked insiders, copycats, or other high-severity safety flags (`rugged` included)
- A huge coordinated insider network (size ≥ 100 and ≥ 0.5% of supply) or a large cluster (size ≥ 50 and ≥ 3% of supply)
- A **uniform split-wallet cluster**: 8+ non-LP wallets each holding nearly the same % (together ≥ 6%), which looks like a bundled launch split

Mutable metadata is a warning, not an automatic fail (it is common even on established tokens).

A light RPC `getSignaturesForAddress` check is attempted on the primary pool when the pair is ≤ 72h old. Signature lists do **not** include signers, so this cannot fully prove or disprove an early-buyer bundle. If RPC is rate-limited, that gap is recorded and listed under WHY THIS COULD FAIL.

### Criterion 2 — insider concentration

Prefers **live** Solana RPC balances. Official public RPC often 429s `getTokenLargestAccounts`; the scanner then uses `getTokenSupply` + `getMultipleAccounts` to refresh balances on RugCheck's top token accounts **plus known LP vault accounts** from RugCheck `markets`. Only if RPC is unreachable does it fall back to a RugCheck snapshot (explicitly labeled as possibly stale).

LP / vault / market accounts are **excluded** from wallet concentration. Exclusion uses RugCheck `markets` (pool pubkeys and LP vault token accounts) and `knownAccounts` (Pump Fun AMM, Raydium, Meteora DLMM/DAMM, Orca, lockers, Streamflow, etc.). Burn / system / incinerator addresses are also excluded. A Raydium or PumpSwap LP vault is never treated as a top holder.

Pass requires all of:

- Top-10 non-LP wallets ≤ **25%** of supply
- Largest single non-LP wallet ≤ **8%**
- RugCheck insider-network holdings ≤ **15%** (sum is an upper bound; wallets may overlap)
- Creator balance ≤ **5%**
- Mint authority revoked
- Freeze authority revoked
- Liquidity-weighted LP locked/burned ≥ **40%**
- Primary (largest) pool LP locked/burned ≥ **90%**

Unlocked secondary pools (common on Meteora) do not automatically fail if the weighted/primary floors still hold, but they are called out in WHY THIS COULD FAIL.

### Criterion 3 — 5x-10x upside (rule-based 0-100 score)

Hard gates first (mcap band, liquidity, liq/mcap ≥ 4%, volume, vol/mcap ≥ 15%, pair age, blowoff cap, 24h txn count ≥ 200, buy ratio ≥ 35%). Then a transparent score, pass at **≥ 60 / 100**:

| Component | Max |
| --- | --- |
| Market-cap headroom (sweet spot $500k-$5M) | 25 |
| Liquidity depth + liq/mcap | 15 |
| 24h volume velocity + txn count | 20 |
| Buy/sell balance (not dumping) | 15 |
| Non-parabolic entry + pair-age floor | 15 |
| Socials / DexScreener profile (bonus) | 5 |
| Multi-pool traction (bonus) | 5 |

## Data sources (no API keys)

Verified working from this box at build time:

- **DexScreener** — `token-boosts/latest/v1`, `token-boosts/top/v1`, `token-profiles/latest/v1`, `latest/dex/search`, `tokens/v1/solana/{mints}` (batch up to 30)
- **GeckoTerminal** — `networks/solana/trending_pools`, `new_pools`, `pools` (paginated; the top-pools feed can 429 if hit too hard)
- **RugCheck** — `/v1/tokens/{mint}/report` and `/report/summary` (risks, insiderNetworks, topHolders, creator, authorities, markets / LP lock, knownAccounts, score)
- **Solana public RPC** (`https://api.mainnet-beta.solana.com`) — `getTokenSupply` and `getMultipleAccounts` work; `getTokenLargestAccounts` is often 429'd; `getSignaturesForAddress` works with gentle pacing

## Honest caveats

- Public RPC rate limits are real. Holder math prefers live balances, but the *set* of top accounts starts from RugCheck (plus known LP vaults) when `getTokenLargestAccounts` is blocked.
- RugCheck insider-network percentages can overlap; the scanner treats the sum as an upper bound (safer, sometimes stricter).
- Early-slot bundle verification is incomplete without `getTransaction` (too heavy / rate-limit prone for a 15-minute scan).
- Discovery is today's public trending / boost / profile / search set (survivorship bias). Boosted tokens are used for discovery only, not as a quality signal.
- GeckoTerminal may 429 the extra `pools` pages; trending + new pools + DexScreener feeds are enough to keep discovery useful.
- A coin that passes all three filters can still go to zero. Every card includes specific **WHY THIS COULD FAIL** bullets from the data that was actually observed.

## Fomo launchpad + Top Potential (added Nov 19, 2026)

**What "Fomo" is.** The Fomo app (fomo.family, FOMO Labs) is a trading app, not a launchpad. You
cannot create a coin there. Its "Graduated" list shows tokens that left *other* launchpads'
curves (mostly pump.fun) in the last 24h, and pump.fun graduates are already in the pump.fun set.
Fomo has no public API, and its terms ban bots and scripts, so the scanner does not read the app.
The launchpad that actually carries the Fomo name is **FomoPad**
(https://fomopad.name, GitHub fomopads/fomopad, X @fomoxpad). It is independent and "not an
official Fomo product". Coins get vanity mints ending in `fomo` and launch on **Meteora Dynamic
Bonding Curve** (`dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN`) or Stonk.fun. DBC graduates migrate
to **Meteora DAMM v2**. (A separate docs site, fomopad.app, describes a program
`6hi7ve53…` that does not exist on mainnet; its explorer link is devnet.)

**How Fomo coins are enumerated** (`enumerate_fomo_graduates`):
1. Memory-light streamed `getProgramAccounts` over **every** Meteora DBC pool (~1.89M, base-mint
   slice at offset 136), keeping base mints that end in `fomo`.
2. A full `getMultipleAccounts` read of those pools gives `is_migrated` (offset 305) and quote reserve.
3. The FomoPad registry `https://fomopad-api-production.up.railway.app/api/tokens` covers the DBC and Stonk rails.
4. A coin counts as graduated when `is_migrated=1`, the registry says migrated, or a non-curve DEX pair exists.
   For pools DexScreener doesn't index yet, liquidity and volume come from GeckoTerminal.

Every coin carries `launchpad` (`pumpfun` / `fomo`). `coverage.by_launchpad` holds enumerated,
alive, dead, gated, inspected and passed counts. `coverage.fomo` holds the Fomo detail. Fomo coins
link to `https://fomo.family/tokens/solana/<mint>` (verified 200), plus `https://fomopad.name/app#token/<mint>`
if registered. They never get a pump.fun link.

**Top Potential.** `top_potential` in latest_scan.json lists the top 15 fully inspected coins
from both launchpads by upside score. Each entry has its pass/fail status and failure reasons.
The dashboard shows it in the "Top Potential" section.
