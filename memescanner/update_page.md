# How a 10-minute run updates the phone Page

The phone dashboard is the existing Page (not a new one):

- URL: https://cursor.com/canvas/store-11ce9b5b-3cc0-4aee-b29b-5931afd4c098/9a19b184-d9b0-4ed4-a297-f887f3827edc
- `canvas_id`: `9a19b184-d9b0-4ed4-a297-f887f3827edc`

The live `--fresh` scanner does **not** write the Page by itself (a Page has no network write API from the box). A routine that already ran `scanner.py --fresh` should edit only the Fresh section on that Page with `EditCanvas`, keeping the narrative picks, watchlist, and coverage blocks unchanged.

## 1. Run the scan

```bash
python3 /workspace/memescanner/scanner.py --fresh
```

Results land in `/workspace/memescanner/out/latest_scan.json` under the `fresh` key. All-graduated fields (`passed_all_three`, `top_potential`, `coverage`, `run_at_hst`, …) stay intact.

## 2. Build the replacement constants

From the same folder, print the TypeScript block to paste:

```bash
python3 /workspace/memescanner/fresh_page_block.py
```

That script reads `out/latest_scan.json` and writes a `const FRESH_HST` … `const FRESH_NEAR` block to stdout (and to `out/fresh_page_block.tsx`). Use those values as `new_string`.

The heading on the Page is:

`Fresh pairs & graduating (last scan {FRESH_HST})`

`FRESH_HST` is a short clock like `9:51 AM HST Nov 20, 2026`, derived from `fresh.run_at_hst`. `FRESH_UTC` is `fresh.run_at_utc`. `FRESH_STATS` comes from the fresh counts (`runtime_sec`, `discovered`, `in_scope`, `market_gate_passed`, `inspected`, `passed_count`, `near_pass_count`, `newly_flagged_count`, plus `discovery.counts.migrations`, `discovery.counts.near_graduation`, `discovery.sol_price_usd`).

`FRESH_PASSED` is `fresh.passed` (coins that passed all three checks). `FRESH_NEAR` is `fresh.near_passes` (strongest near-passes, already capped in the scanner). Each coin must include:

- `mint`, `symbol`, `name`
- `role` (e.g. `Near-grad 92.6% (pump.fun curve)` or `Fresh PumpSwap pair, 0.1h old`)
- `marketCap`, `liquidity`, `change24h`, `ageHours`, `upsideScore`, `progressPct`
- `why` — the `why_this_could_fail` list (required)
- `dexUrl` from `links.dexscreener`
- `hasPumpfun` true when `links.pumpfun` is present or the mint ends in `pump`

## 3. Edit only those constants

Call `EditCanvas` on canvas `9a19b184-d9b0-4ed4-a297-f887f3827edc`.

Replace the existing block that begins with `const FRESH_HST =` and ends at the closing `];` of `const FRESH_NEAR` (do not touch `NARRATIVE`, `WATCHLIST`, or the coverage copy). Copy `old_string` verbatim from `ReadCanvas` first, because the previous values must match exactly once.

Also, if the Page `sources` array still names an older fresh timestamp, replace that one source line with the new `fresh.run_at_hst` / `fresh.run_at_utc`. Do not rewrite the rest of the page.

The Fresh section, live up/down list, and `useLiveQuotes` already read `FRESH_PASSED` and `FRESH_NEAR`, so updating those arrays is enough to refresh the heading, the cards, the fail notes, the Fomo / pump.fun / DexScreener links, and the live prices.

## 4. Confirm

The tool result should name the same URL. If `EditCanvas` returns `no_match`, copy `old_string` again from a fresh `ReadCanvas`. If it type-checks, the Page is published; do not `WriteCanvas` a new page for a routine refresh.
