# Meme scanner + paper Shield, for GitHub Actions

Ready-to-push tree for a public GitHub repo. See **[DEPLOY.md](DEPLOY.md)** for the exact steps (Pages from `/docs`, `NTFY_TOPIC` secret, widget URL, first manual run).

- `memescanner/` — `python scanner.py --fresh` every 10 minutes (7 AM–10 PM HST)
- `quantteam/` — paper bot + Shield every 15 minutes (paper only, no exchange keys)
- `docs/` — GitHub Pages mobile dashboard (`index.html` + `coins.json`)
- `widget/CoinWidget.js` — iPhone Scriptable widget that reads `coins.json`

Research / paper trading only. Not financial advice.
