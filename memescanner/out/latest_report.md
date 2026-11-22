# Meme Coin Scanner - 2026-11-19 21:38:02 HST

Read-only research / instant alerts. Fake signals are never invented. **Default notification is instant** (not a daily digest): `notify_now` is true when at least one coin is in `newly_flagged`.

## Run summary

- notify_now: **false**
- Newly flagged (instant alert): **0**
- Passed all 3 (including already-alerted): **10**
- Discovered: 228835 unique mints
- Deeply inspected: 53
- Runtime: 79.44s
- Data sources: dexscreener, geckoterminal, rugcheck, solana_rpc, solana_rpc_pumpswap, solana_rpc_raydium_v4, pumpfun_api, geckoterminal_dex_pools, solana_rpc_meteora_dbc, fomopad_registry

## Fresh pairs & graduating

- Fresh scan at 2026-11-21 17:30:44 HST (2026-11-22T03:30:44Z) in 79.44s
- Window: last 3600s
- Discovered: 223 | in scope: 111 | gated: 7 | inspected: 7
- Passed all 3: **0** | near passes: 7 | newly flagged: 0

- NEAR HUSSMAS `2ffmm3EfCjgueUr7MKWmdRuP1bSGgJF7a4jvqMnppump` score=37 [Fomo](https://fomo.family/tokens/solana/2ffmm3EfCjgueUr7MKWmdRuP1bSGgJF7a4jvqMnppump)
  - WHY THIS COULD FAIL: Top-10 non-LP wallets control 25.0% of supply
  - WHY THIS COULD FAIL: Already up 285% in 24h - late entry risk even if under the blowoff cap
- NEAR WIF `8VkAt7TQjpkwKg3Ywa9Ysg6AmwNNc4vvHvGrj8jQVrTZ` score=40 [Fomo](https://fomo.family/tokens/solana/8VkAt7TQjpkwKg3Ywa9Ysg6AmwNNc4vvHvGrj8jQVrTZ)
  - WHY THIS COULD FAIL: Only 0.0% of LP is locked/burned on a liquidity-weighted basis
  - WHY THIS COULD FAIL: Liquidity is only $31,586; a modest sell can move the price sharply
- NEAR Tiger `8HusqhhyrYwNSbxqtK56tucqMXhLx4EatWkYD3sQpump` score=39 [Fomo](https://fomo.family/tokens/solana/8HusqhhyrYwNSbxqtK56tucqMXhLx4EatWkYD3sQpump)
  - WHY THIS COULD FAIL: Top-10 non-LP wallets control 26.3% of supply
  - WHY THIS COULD FAIL: Already up 672% in 24h - late entry risk even if under the blowoff cap
- NEAR CATEMAS `G4zVeLq9PH8vxzb5uemh38vifc8KkLYrg6GhW1rTsnHb` score=34 [Fomo](https://fomo.family/tokens/solana/G4zVeLq9PH8vxzb5uemh38vifc8KkLYrg6GhW1rTsnHb)
  - WHY THIS COULD FAIL: Only 0.0% of LP is locked/burned on a liquidity-weighted basis
  - WHY THIS COULD FAIL: A single non-LP wallet holds 4.6% and can dump
- NEAR CATEMAS `4p81MKvUkndrrH92XX5jebsg5WgF4bbAnP4gk8S82X6h` score=33 [Fomo](https://fomo.family/tokens/solana/4p81MKvUkndrrH92XX5jebsg5WgF4bbAnP4gk8S82X6h)
  - WHY THIS COULD FAIL: Primary pool LP lock % unavailable
  - WHY THIS COULD FAIL: Only 0.0% of LP is locked/burned on a liquidity-weighted basis
- NEAR CATEMAS `7q7nNn1hPKoRAqYQkvDMsySZr8sqW1QhMbuLPHbvuU4U` score=32 [Fomo](https://fomo.family/tokens/solana/7q7nNn1hPKoRAqYQkvDMsySZr8sqW1QhMbuLPHbvuU4U)
  - WHY THIS COULD FAIL: Primary pool LP lock % unavailable
  - WHY THIS COULD FAIL: Only 0.0% of LP is locked/burned on a liquidity-weighted basis
- NEAR Xin chào `CMgXRqoR92HcRVfqiA8hYXXNmFD3pwZ54nQkatrtCijU` score=32 [Fomo](https://fomo.family/tokens/solana/CMgXRqoR92HcRVfqiA8hYXXNmFD3pwZ54nQkatrtCijU)
  - WHY THIS COULD FAIL: Primary pool LP lock % unavailable
  - WHY THIS COULD FAIL: Only 0.0% of LP is locked/burned on a liquidity-weighted basis

## Coverage

- Mode: all_graduated | scan at 2026-11-19 21:38:02 HST (2026-11-20T07:38:02Z)
- Graduated tokens enumerated: **228835**
- Alive: **2789** | dropped dead: **226046** (low liquidity on-chain 225382, low liquidity DexScreener 13, inactive 7d 651)
- Dead rule: dead = liquidity < $5,000 (on-chain WSOL vault x2 x SOL price, then DexScreener) OR no trade/pool tx in last 7 days (DexScreener 24h txns, else pool's last signature via RPC)

| Launchpad | Enumerated | Alive | Dead | Gated | Inspected | Passed all 3 |
|---|---|---|---|---|---|---|
| pumpfun | 228833 | 2789 | 226044 | 44 | 44 | 10 |
| fomo | 2 | 0 | 2 | 0 | 0 | 0 |
- pumpfun: Every canonical PumpSwap graduation pool + pump-suffix Raydium v4 pools read on-chain (complete for PumpSwap; Raydium v4 is a suffix heuristic).
- fomo: FomoPad (fomopad.name, independent of FOMO Labs): all Meteora DBC pools read on-chain and filtered to '...fomo' mints, plus the FomoPad registry (DBC + Stonk.fun). Stonk.fun launches are only known via the registry; their graduation is detected by a live DEX pair. The Fomo app (fomo.family) itself is not a launchpad - its 'Graduated' list shows other launchpads' (mostly pump.fun) graduates <24h old, which the pump.fun set already covers.

- Passed market gate: **53** | fully inspected: **53** | inspection errors left uninspected: **0** (initial 0, retry rounds 0)

| Source | Status | Fully paginated | Tokens | Note |
|---|---|---|---|---|
| solana_rpc:getProgramAccounts PumpSwap | ok | yes | 178704 | Every WSOL-quoted PumpSwap pool account read on-chain; kept index-0 pools whose creator is the pump.fun pool-authority PDA (canonical graduation pools). |
| solana_rpc:getProgramAccounts Raydium AMM v4 | ok | yes | 50113 | Pre-PumpSwap graduates (pump.fun migrated to Raydium AMM v4 until Mar 2025). Identified by pump-suffix base mint + WSOL quote; Raydium v4 has no on-chain migrator field, so this is a heuristic. |
| pump.fun frontend API coins?complete=true | blocked | no | 0 | blocked from this machine: HTTP 403 (Cloudflare bot challenge); not bypassed |
| geckoterminal:dexes/pumpswap/pools sort=h24_volume_usd_desc | ok | no | 8 | page 6 unavailable (rate limit or error) |
| geckoterminal:dexes/pumpswap/pools sort=h24_tx_count_desc | error | no | 0 | page 1 unavailable (rate limit or error) |
| geckoterminal:dexes/raydium/pools sort=h24_volume_usd_desc | error | no | 0 | page 1 unavailable (rate limit or error) |
| geckoterminal:dexes/raydium/pools sort=h24_tx_count_desc | error | no | 0 | page 1 unavailable (rate limit or error) |
| dexscreener:latest/dex/search | ok | no | 39 | search returns at most ~30 pairs per query (no pagination) |
| solana_rpc:getProgramAccounts Meteora DBC (all pools, '...fomo' base mint) | ok | yes | 2 | Read all 1,885,709 Meteora DBC pools on-chain; 116 have a '...fomo' base mint; 2 have is_migrated=1 (graduated to DAMM v2). |
| fomopad_api:/api/tokens (FomoPad verified launch registry) | ok | yes | 0 | API scope '50 most recent verified launches'; returned 26 (< cap, so this is the full registry) |

## Caveats

- This run: official RPC blocked getTokenLargestAccounts with 429.
- Public Solana RPC often rate-limits getTokenLargestAccounts (HTTP 429). When that happens, holder percentages use getTokenSupply + getMultipleAccounts on RugCheck's top accounts (live balances, snapshot set) or a RugCheck snapshot.
- getSignaturesForAddress does not include signers, so early-buyer clustering is only a light check.
- RugCheck insider-network percentages can overlap; the scanner treats the sum as an upper bound.
- All-graduated mode: universe = every canonical PumpSwap graduation pool + pump-suffix Raydium v4 pools read on-chain, plus every Fomo-launchpad ('...fomo' mint) Meteora DBC pool read on-chain and the FomoPad registry, plus pump.fun API / GeckoTerminal / DexScreener search where reachable; legacy trending feeds are added as extras.
- This is research/alerting only - never a trade recommendation.
- One or more public APIs returned 429/rate-limit this run; some checks may have used fallbacks.

## Highest potential (all fully inspected coins, both launchpads, by upside score)

| # | Symbol | Launchpad | Upside | Market cap | Liquidity | Passed all 3 | Why not |
|---|---|---|---|---|---|---|---|
| 1 | DCTY | pumpfun | 93 | $3,834,308 | $275,770 | no | Criterion 1 (bundle): Uniform split-wallet cluster: 8 non-LP wallets each hold ~0.86% (range 0.76-0.95%, together 6.89%) - looks like a bundled launch split |
| 2 | Max | pumpfun | 89 | $672,039 | $110,464 | no | Criterion 1 (bundle): RugCheck bundle/safety flags: Copycat token |
| 3 | HOGE | pumpfun | 87 | $1,144,321 | $136,062 | no | Criterion 2 (insider_concentration): Insider networks hold ~26.67% of supply (max 15%; sum may overlap) |
| 4 | RKC | pumpfun | 86 | $963,686 | $158,583 | yes | - |
| 5 | DIGS | pumpfun | 86 | $291,613 | $55,132 | no | Criterion 1 (bundle): Uniform split-wallet cluster: 11 non-LP wallets each hold ~1.05% (range 0.98-1.21%, together 11.53%) - looks like a bundled launch split; Criterion 2 (insider_concentration): Insider networks hold ~100.00% of supply (max 15%; sum may overlap) |
| 6 | SHIT | pumpfun | 85 | $804,434 | $73,200 | no | Criterion 1 (bundle): Uniform split-wallet cluster: 15 non-LP wallets each hold ~1.00% (range 1.00-1.01%, together 15.05%) - looks like a bundled launch split |
| 7 | JOB | pumpfun | 85 | $2,064,460 | $150,355 | no | Criterion 1 (bundle): Huge coordinated wallet cluster(s): anxious-magenta-possum size=349 holding=19.76%; Criterion 2 (insider_concentration): Insider networks hold ~19.76% of supply (max 15%; sum may overlap) |
| 8 | GATO | pumpfun | 85 | $2,035,966 | $155,323 | no | Criterion 1 (bundle): Uniform split-wallet cluster: 8 non-LP wallets each hold ~0.82% (range 0.75-0.96%, together 6.59%) - looks like a bundled launch split |
| 9 | WASTED | pumpfun | 85 | $917,815 | $93,943 | no | Criterion 1 (bundle): Huge coordinated wallet cluster(s): fancy-periwinkle-ostrich size=107 holding=2.64%; sharp-rust-ram size=671 holding=0.87%; Criterion 2 (insider_concentration): Top-10 non-LP wallets hold 26.67% (max 25%) |
| 10 | DacityCoin | pumpfun | 84 | $356,651 | $95,311 | no | Criterion 1 (bundle): Huge coordinated wallet cluster(s): odd-scarlet-seahorse size=858 holding=28.93%; smooth-apricot-moose size=335 holding=4.91%; fierce-cinnamon-boar size=111 holding=6.12%; hidden-silver-shrimp size=132 holding=5.15%; unique-orange-gorilla size=102 holding=1.92%; Criterion 2 (insider_concentration): Insider networks hold ~55.52% of supply (max 15%; sum may overlap) |
| 11 | Leonida | pumpfun | 83 | $809,448 | $70,252 | no | Criterion 1 (bundle): Uniform split-wallet cluster: 15 non-LP wallets each hold ~1.23% (range 1.21-1.25%, together 18.42%) - looks like a bundled launch split |
| 12 | NIO | pumpfun | 83 | $1,387,157 | $137,373 | no | Criterion 1 (bundle): Large insider/bundle-like cluster(s): lively-vanilla-mosquito size=64 holding=4.58% |
| 13 | DOOM | pumpfun | 83 | $723,831 | $69,009 | no | Criterion 2 (insider_concentration): Top-10 non-LP wallets hold 25.90% (max 25%); Insider networks hold ~29.69% of supply (max 15%; sum may overlap) |
| 14 | OTC | pumpfun | 83 | $1,088,758 | $224,170 | no | Criterion 1 (bundle): Huge coordinated wallet cluster(s): naughty-silver-rhino size=2257 holding=42.08%; Criterion 2 (insider_concentration): Insider networks hold ~48.99% of supply (max 15%; sum may overlap); Criterion 3 (upside_5x_10x): Vol/mcap 10.26% below 15.00% |
| 15 | mihao | pumpfun | 82 | $7,013,907 | $323,874 | no | Criterion 1 (bundle): Uniform split-wallet cluster: 10 non-LP wallets each hold ~0.96% (range 0.82-1.09%, together 9.56%) - looks like a bundled launch split |

## Newly flagged (instant alert now)

None this run.

## Already alerted (cooldown, no instant re-alert)

### OGTA - OFFICIALGTA (cooldown)

- Mint: `7bCiddGLe5aM8HE2pigT9jsqkmC1HSPxyXN6GkHEpump`
- Market cap: $577,994
- Liquidity: $57,824
- 24h volume: $2,677,723
- 24h change: +538.0%
- Age: 13.2h
- Upside score: 79/100
- Top-10 non-LP: 1.19% | max wallet: 0.142% | insider nets: 0.0% | creator: 0.0% | LP locked: 100.0%
- Holder source: rpc_getMultipleAccounts_on_rugcheck_top_accounts
- Notify reason: cooldown
- DexScreener: https://dexscreener.com/solana/2rdx58tndmemzcqy9qgafwhq9icrrmbduatruofrjyzu
- Solscan: https://solscan.io/token/7bCiddGLe5aM8HE2pigT9jsqkmC1HSPxyXN6GkHEpump
- RugCheck: https://rugcheck.xyz/tokens/7bCiddGLe5aM8HE2pigT9jsqkmC1HSPxyXN6GkHEpump

WHY THIS COULD FAIL:
- Already up 538% in 24h - late entry risk even if under the blowoff cap
- Liquidity is only $57,824; a modest sell can move the price sharply
- Primary trading history is only 13.2 hours old
- High meme-coin beta: even a clean setup can go to zero on a sector fade or a dev dump

### DESSA - Dessa (cooldown)

- Mint: `npz2FhoufQ7pKqhGpaipYWacrAkidyW2yDpVWSvpump`
- Market cap: $2,731,028
- Liquidity: $151,195
- 24h volume: $466,503
- 24h change: -40.4%
- Age: 594.6h
- Upside score: 77/100
- Top-10 non-LP: 2.769% | max wallet: 0.753% | insider nets: 0.0% | creator: 0.0% | LP locked: 100.0%
- Holder source: rpc_getMultipleAccounts_on_rugcheck_top_accounts
- Notify reason: cooldown
- DexScreener: https://dexscreener.com/solana/dextcer4k9dwbxtmu8znvvwgevvmvhmb183pf2xxzwvh
- Solscan: https://solscan.io/token/npz2FhoufQ7pKqhGpaipYWacrAkidyW2yDpVWSvpump
- RugCheck: https://rugcheck.xyz/tokens/npz2FhoufQ7pKqhGpaipYWacrAkidyW2yDpVWSvpump

WHY THIS COULD FAIL:
- Already down -40% in 24h - momentum is damaged and another leg down is common
- Early-slot bundle verification was incomplete (pair older than 72h; skipped expensive launch-signature walk)
- High meme-coin beta: even a clean setup can go to zero on a sector fade or a dev dump

### SWJ - Stand With Jensen (cooldown)

- Mint: `gHZhqEnFnitWCJ1MA2i9BzqTReFMhsGmbdL17xypump`
- Market cap: $652,093
- Liquidity: $58,580
- 24h volume: $730,390
- 24h change: +589.0%
- Age: 16.7h
- Upside score: 69/100
- Top-10 non-LP: 3.956% | max wallet: 0.777% | insider nets: 0.0% | creator: 0.0% | LP locked: 100.0%
- Holder source: rpc_getMultipleAccounts_on_rugcheck_top_accounts
- Notify reason: cooldown
- DexScreener: https://dexscreener.com/solana/488fdh1hgtfubtkwe5ycqa4ary1saebdqypbr6xnk6d5
- Solscan: https://solscan.io/token/gHZhqEnFnitWCJ1MA2i9BzqTReFMhsGmbdL17xypump
- RugCheck: https://rugcheck.xyz/tokens/gHZhqEnFnitWCJ1MA2i9BzqTReFMhsGmbdL17xypump

WHY THIS COULD FAIL:
- Already up 589% in 24h - late entry risk even if under the blowoff cap
- Liquidity is only $58,580; a modest sell can move the price sharply
- Primary trading history is only 16.7 hours old
- High meme-coin beta: even a clean setup can go to zero on a sector fade or a dev dump

### JESTER - JESTER (cooldown)

- Mint: `VAYMVikuYQpWz5sXCeCsQDeho2QcGqDSNq9rhCspump`
- Market cap: $2,585,309
- Liquidity: $120,265
- 24h volume: $413,107
- 24h change: +7.0%
- Age: 65.0h
- Upside score: 80/100
- Top-10 non-LP: 2.078% | max wallet: 0.316% | insider nets: 0.0% | creator: 0.0% | LP locked: 100.0%
- Holder source: rpc_getMultipleAccounts_on_rugcheck_top_accounts
- Notify reason: cooldown
- DexScreener: https://dexscreener.com/solana/ejhur4fvts85xqwskjuaoaubmavccedxutuppze9guen
- Solscan: https://solscan.io/token/VAYMVikuYQpWz5sXCeCsQDeho2QcGqDSNq9rhCspump
- RugCheck: https://rugcheck.xyz/tokens/VAYMVikuYQpWz5sXCeCsQDeho2QcGqDSNq9rhCspump

WHY THIS COULD FAIL:
- Pair is only 2.7 days old - typical meme death-rate is still high
- High meme-coin beta: even a clean setup can go to zero on a sector fade or a dev dump

### Huss - Huss (cooldown)

- Mint: `CUg7AzWL5p8imZVqqdjEPgRhUsXAkBMW5JxkSJ9fpump`
- Market cap: $362,586
- Liquidity: $46,085
- 24h volume: $583,683
- 24h change: +195.0%
- Age: 86.2h
- Upside score: 81/100
- Top-10 non-LP: 24.42% | max wallet: 3.906% | insider nets: 4.594% | creator: 0.0% | LP locked: 94.637%
- Holder source: rpc_getMultipleAccounts_on_rugcheck_top_accounts
- Notify reason: cooldown
- DexScreener: https://dexscreener.com/solana/gkhcyzexdi2sveqnagsg3qxrkzshdr5dxrd482km2hxx
- Solscan: https://solscan.io/token/CUg7AzWL5p8imZVqqdjEPgRhUsXAkBMW5JxkSJ9fpump
- RugCheck: https://rugcheck.xyz/tokens/CUg7AzWL5p8imZVqqdjEPgRhUsXAkBMW5JxkSJ9fpump

WHY THIS COULD FAIL:
- 1 secondary pool(s) appear unlocked
- Unlocked secondary meteoraDlmm HKfE9AfR... holds ~$2,690 and can be yanked
- Top-10 non-LP wallets control 24.4% of supply
- Liquidity is only $46,085; a modest sell can move the price sharply
- Early-slot bundle verification was incomplete (pair older than 72h; skipped expensive launch-signature walk)
- High meme-coin beta: even a clean setup can go to zero on a sector fade or a dev dump

### Macca - Macca The Gator (cooldown)

- Mint: `AKotzk1dhcNangAJBtvNKtT9bhbSVZc2mMhRFgkFpump`
- Market cap: $282,425
- Liquidity: $64,375
- 24h volume: $465,195
- 24h change: -56.5%
- Age: 858.0h
- Upside score: 78/100
- Top-10 non-LP: 19.689% | max wallet: 3.628% | insider nets: 4.804% | creator: 0.0% | LP locked: 91.402%
- Holder source: rpc_getMultipleAccounts_on_rugcheck_top_accounts
- Notify reason: cooldown
- DexScreener: https://dexscreener.com/solana/4fonnnmmkma66ppcia9557cg63s2bnn9uofcakxxbmvg
- Solscan: https://solscan.io/token/AKotzk1dhcNangAJBtvNKtT9bhbSVZc2mMhRFgkFpump
- RugCheck: https://rugcheck.xyz/tokens/AKotzk1dhcNangAJBtvNKtT9bhbSVZc2mMhRFgkFpump

WHY THIS COULD FAIL:
- 1 secondary pool(s) appear unlocked
- Unlocked secondary meteoraDlmm 5zbcXagp... holds ~$5,975 and can be yanked
- Top-10 non-LP wallets control 19.7% of supply
- Already down -56% in 24h - momentum is damaged and another leg down is common
- Liquidity is only $64,375; a modest sell can move the price sharply
- Early-slot bundle verification was incomplete (pair older than 72h; skipped expensive launch-signature walk)
- High meme-coin beta: even a clean setup can go to zero on a sector fade or a dev dump

### :3 - :3 (cooldown)

- Mint: `FSkyjfqB26u6c2GSgpACrjbxp9yQAR8rutxprVNppump`
- Market cap: $263,382
- Liquidity: $44,743
- 24h volume: $244,034
- 24h change: -42.5%
- Age: 805.8h
- Upside score: 71/100
- Top-10 non-LP: 21.255% | max wallet: 3.94% | insider nets: 3.046% | creator: 0.0% | LP locked: 77.391%
- Holder source: rpc_getMultipleAccounts_on_rugcheck_top_accounts
- Notify reason: cooldown
- DexScreener: https://dexscreener.com/solana/gxybnxjgr9meuvzxsgbi2y78rgkwbmrok4rs9smub8he
- Solscan: https://solscan.io/token/FSkyjfqB26u6c2GSgpACrjbxp9yQAR8rutxprVNppump
- RugCheck: https://rugcheck.xyz/tokens/FSkyjfqB26u6c2GSgpACrjbxp9yQAR8rutxprVNppump

WHY THIS COULD FAIL:
- 2 secondary pool(s) appear unlocked
- Unlocked secondary meteoraDlmm 7Vf8HRDd... holds ~$8,938 and can be yanked
- Top-10 non-LP wallets control 21.3% of supply
- Already down -42% in 24h - momentum is damaged and another leg down is common
- Liquidity is only $44,743; a modest sell can move the price sharply
- Early-slot bundle verification was incomplete (pair older than 72h; skipped expensive launch-signature walk)
- RugCheck graph still flags 91 related insider accounts
- High meme-coin beta: even a clean setup can go to zero on a sector fade or a dev dump

### GTA - GTA memes (cooldown)

- Mint: `BsbsB3WLq7vbY5En3MBCAsTrcwaCxNKY2Mp5pGuLpump`
- Market cap: $214,735
- Liquidity: $48,690
- 24h volume: $226,130
- 24h change: -29.5%
- Age: 2056.7h
- Upside score: 79/100
- Top-10 non-LP: 19.47% | max wallet: 3.206% | insider nets: 1.349% | creator: 0.0% | LP locked: 92.166%
- Holder source: rpc_getMultipleAccounts_on_rugcheck_top_accounts
- Notify reason: cooldown
- DexScreener: https://dexscreener.com/solana/5isvs31h7hmdggmyuvzmhdsywv7cydd6czjtbqzctlii
- Solscan: https://solscan.io/token/BsbsB3WLq7vbY5En3MBCAsTrcwaCxNKY2Mp5pGuLpump
- RugCheck: https://rugcheck.xyz/tokens/BsbsB3WLq7vbY5En3MBCAsTrcwaCxNKY2Mp5pGuLpump

WHY THIS COULD FAIL:
- 1 secondary pool(s) appear unlocked
- Unlocked secondary meteoraDlmm 69KuWUDK... holds ~$8,219 and can be yanked
- Top-10 non-LP wallets control 19.5% of supply
- Liquidity is only $48,690; a modest sell can move the price sharply
- Early-slot bundle verification was incomplete (pair older than 72h; skipped expensive launch-signature walk)
- High meme-coin beta: even a clean setup can go to zero on a sector fade or a dev dump

## Top near-misses

None.

