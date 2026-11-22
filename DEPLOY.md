# Deploy the 10-minute scanner and Shield paper bot on free GitHub Actions

This package is ready to push to an **empty public GitHub repo**. Nothing here holds API keys, wallets, or exchange credentials. Both workflows are paper/research only. They use **no Grok Bot usage** once they are running on GitHub.

You still need to: connect GitHub, create the public repo, push this folder, turn on Pages, and add one ntfy secret.

## 1. Create the public repo and push

1. Create a new **public** GitHub repository (empty, no README if you want a clean first push).
2. From this folder (`gh-deploy/`):

```bash
git init
git add .
git commit -m "Add meme scanner and paper Shield GitHub Actions"
git branch -M main
git remote add origin https://github.com/YOUR_USER/YOUR_REPO.git
git push -u origin main
```

## 2. Enable GitHub Pages from `/docs` on `main`

1. In the repo: **Settings → Pages**.
2. Under **Build and deployment**, set **Source** to **Deploy from a branch**.
3. Set the branch to **main** and the folder to **/docs**.
4. Save. After a minute the site is:

`https://YOUR_USER.github.io/YOUR_REPO/`

The mobile page reads `coins.json` in that same folder and shows live DexScreener up/down prices. The first scheduled scan (or a manual run) refreshes `docs/coins.json`.

## 3. Add the ntfy secret and subscribe on your phone

Alerts use [ntfy](https://ntfy.sh), not email. The topic name is a **repo secret**, never committed.

1. Pick a long random topic, for example `jf-memes-` plus 16 random letters (not a password you use anywhere else, but not guessable).
2. In the repo: **Settings → Secrets and variables → Actions → New repository secret**.
3. Name it exactly `NTFY_TOPIC`. Value: the topic string only (no `https://`, no slash).
4. On your iPhone, install [ntfy](https://apps.apple.com/app/ntfy/id1625396347), tap **Subscribe to topic**, and enter the same topic name.

New coins that pass all three scanner checks get a push with a Fomo link. Shield halt alerts also go to that topic, but only between 7 AM and 10 PM HST.

## 4. Allow Actions to write back state

The workflows already request `contents: write` and commit with `[skip ci]` so they do not retrigger themselves. That keeps the 24-hour alert cooldown (`memescanner/state.json`) and the paper book (`quantteam/state.json`, `config/halt.json`, trade CSVs) on the repo.

If the first run cannot push, check **Settings → Actions → General → Workflow permissions** and allow **Read and write**.

## 5. Run once by hand

1. **Actions → Fresh meme scan → Run workflow**.
2. **Actions → Shield + paper bot → Run workflow**.

The scan cron is `*/10 17-23,0-7 * * *` (UTC), which is every 10 minutes from 7 AM to about 9:50 PM HST. The Shield/paper bot cron is every 15 minutes, all day, so exits can still be managed overnight; ntfy stays silent outside 7 AM–10 PM HST.

## 6. Point the iPhone widget at Pages

1. Install [Scriptable](https://apps.apple.com/app/scriptable/id1405459188).
2. Copy `widget/CoinWidget.js`.
3. At the top, set:

```js
const COINS_JSON_URL = "https://YOUR_USER.github.io/YOUR_REPO/coins.json";
```

4. Add a Scriptable widget to the home screen and attach this script. Until that URL is filled in, the widget uses the same 12-coin fallback list it had before. Tapping a row still opens Fomo. Prices still come from DexScreener.

## What each workflow does

| Workflow | Command | Writes | Notify |
|---|---|---|---|
| `.github/workflows/scan.yml` | `python scanner.py --fresh --json` then `publish_docs.py` | `docs/coins.json`, `memescanner/state.json`, scan snapshot | ntfy on newly flagged passers, with a Fomo click link |
| `.github/workflows/shield.yml` | `python gha_cycle.py` (runs `bot.py` then `shield.py`) | paper state, halt flag, trades | ntfy on new Shield flags, 7 AM–10 PM HST only |

The Shield + paper bot was already a pure Python paper system under `/workspace/quantteam/` (OKX public market data only). No LLM is used. Halt rules are unchanged and deterministic:

- Halt new paper entries for the rest of the HST day if daily P&L ≤ -3% of day-start equity, or 3 straight losing trades (third close today), or the price feed is older than 30 minutes.
- Exits keep being managed during a halt.
- Runner-up shadow books are compared over 7 paper days; a swap is recommended, not executed automatically.

**Difference vs the previous Grok Bot 15-minute routine:** that routine could notify a *new* Shield halt at any hour. This GitHub Action stays silent outside 7 AM–10 PM HST, including for halts, as requested.

Radar, Lab, Forge, the 7:30 AM brief, and the Sunday weekly are not on this 15-minute workflow. They are still in `quantteam/` if you want to add separate scheduled workflows later.

## Local test commands (already run when this package was built)

```bash
python3 memescanner/scanner.py --selftest
python3 memescanner/publish_docs.py
python3 memescanner/notify_ntfy.py          # skips if NTFY_TOPIC is unset
python3 quantteam/bot.py --selftest
python3 quantteam/shield.py
python3 -c "import yaml,pathlib; ..."      # workflow YAML parse
node widget/test_widget.mjs
```
