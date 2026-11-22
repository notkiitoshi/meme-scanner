// Meme Coin Scanner — Scriptable home-screen widget
// Research only, not financial advice. Tapping a row opens the coin on Fomo.
//
// After GitHub Pages is on, replace the placeholder below with your real URL,
// for example: "https://YOUR_USER.github.io/YOUR_REPO/coins.json"
const COINS_JSON_URL = "https://<user>.github.io/<repo>/coins.json";

const FALLBACK_COINS = [
  { symbol: "RKC", mint: "7HgfXftRBBqsYtAEYcqjGLQrNJLL6Tww9ek4rE3Apump" },
  { symbol: "HNYDRIP", mint: "FyCaKh6wXuot5NmqQMt5oBrSHh3wFk14w5M9dHQDpump" },
  { symbol: "mihao", mint: "4CxjZyMNoF1p6eXHXHv1TWpS2X47CuUmLwtGe18spump" },
  { symbol: "DCTY", mint: "9hjBLBrc7WZW2H8UQvBAvG5WiTZotXUv2Bdcn4f4pump" },
  { symbol: "Huss", mint: "CUg7AzWL5p8imZVqqdjEPgRhUsXAkBMW5JxkSJ9fpump" },
  { symbol: "JESTER", mint: "VAYMVikuYQpWz5sXCeCsQDeho2QcGqDSNq9rhCspump" },
  { symbol: "GTA", mint: "BsbsB3WLq7vbY5En3MBCAsTrcwaCxNKY2Mp5pGuLpump" },
  { symbol: "Macca", mint: "AKotzk1dhcNangAJBtvNKtT9bhbSVZc2mMhRFgkFpump" },
  { symbol: "DESSA", mint: "npz2FhoufQ7pKqhGpaipYWacrAkidyW2yDpVWSvpump" },
  { symbol: "OGTA", mint: "7bCiddGLe5aM8HE2pigT9jsqkmC1HSPxyXN6GkHEpump" },
  { symbol: ":3", mint: "FSkyjfqB26u6c2GSgpACrjbxp9yQAR8rutxprVNppump" },
  { symbol: "SWJ", mint: "gHZhqEnFnitWCJ1MA2i9BzqTReFMhsGmbdL17xypump" },
];

const LARGE_LIMIT = 12;
const MEDIUM_LIMIT = 6;
const SMALL_LIMIT = 3;
const REFRESH_MINUTES = 15;
const DEX_ENDPOINT = "https://api.dexscreener.com/tokens/v1/solana/";
const CACHE_FILE = "meme-coin-widget-quotes.json";

function coinsJsonUrlIsPlaceholder(url) {
  if (!url) return true;
  return url.indexOf("<user>") !== -1 || url.indexOf("<repo>") !== -1;
}

function coinsFromPayload(data) {
  const rows = data && data.coins ? data.coins : data;
  if (!Array.isArray(rows)) return [];
  const out = [];
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];
    const mint = row && row.mint;
    if (!mint) continue;
    out.push({
      symbol: row.symbol || mint.slice(0, 6),
      mint: mint,
    });
  }
  return out;
}

async function loadCoinList() {
  if (coinsJsonUrlIsPlaceholder(COINS_JSON_URL)) {
    return FALLBACK_COINS.slice();
  }
  try {
    const req = new Request(COINS_JSON_URL);
    req.timeoutInterval = 15;
    req.headers = { Accept: "application/json" };
    const data = await req.loadJSON();
    const coins = coinsFromPayload(data);
    if (coins.length) return coins;
  } catch (e) {
    // Fall back to the last hardcoded watchlist if Pages is unreachable.
  }
  return FALLBACK_COINS.slice();
}


const BG = new Color("#0B1017");
const SURFACE = new Color("#151C26");
const TEXT = new Color("#E8EEF6");
const MUTED = new Color("#8B97A8");
const GREEN = new Color("#3EE0A0");
const RED = new Color("#FF7A7A");
const ACCENT = new Color("#6EE7D2");
const LINE = new Color("#2A3544");

function fomoUrl(mint) {
  return "https://fomo.family/tokens/solana/" + mint;
}

function parseDexPairs(pairs) {
  const byMint = {};
  if (!Array.isArray(pairs)) return byMint;
  for (let i = 0; i < pairs.length; i++) {
    const p = pairs[i];
    const mint = p && p.baseToken && p.baseToken.address;
    if (!mint) continue;
    const liq = Number(p.liquidity && p.liquidity.usd);
    const liquidity = isFinite(liq) ? liq : 0;
    const prev = byMint[mint];
    if (!prev || liquidity > prev.liquidity) {
      const rawPrice = p.priceUsd;
      const rawChg = p.priceChange ? p.priceChange.h24 : null;
      const price = rawPrice == null || rawPrice === "" ? null : Number(rawPrice);
      const change24h = rawChg == null || rawChg === "" ? null : Number(rawChg);
      byMint[mint] = {
        price: price != null && isFinite(price) ? price : null,
        change24h: change24h != null && isFinite(change24h) ? change24h : null,
        liquidity,
      };
    }
  }
  return byMint;
}

function formatPrice(n) {
  if (n == null || !isFinite(n)) return "—";
  const abs = Math.abs(n);
  let digits;
  if (abs >= 1000) digits = 2;
  else if (abs >= 1) digits = 4;
  else if (abs >= 0.01) digits = 5;
  else if (abs >= 0.0001) digits = 6;
  else digits = 8;
  const txt = n.toFixed(digits).replace(/\.?0+$/, "");
  return "$" + txt;
}

function formatChange(n) {
  if (n == null || !isFinite(n)) return { arrow: "", text: "—", up: null };
  const up = n > 0;
  const down = n < 0;
  const arrow = up ? "▲" : down ? "▼" : "•";
  const sign = up ? "+" : "";
  return { arrow, text: sign + n.toFixed(2) + "%", up: up ? true : down ? false : null };
}

function limitForFamily(family) {
  if (family === "small") return SMALL_LIMIT;
  if (family === "medium") return MEDIUM_LIMIT;
  return LARGE_LIMIT;
}

function cachePath() {
  const fm = FileManager.local();
  return fm.joinPath(fm.cacheDirectory(), CACHE_FILE);
}

function readCache() {
  try {
    const fm = FileManager.local();
    const path = cachePath();
    if (!fm.fileExists(path)) return null;
    return JSON.parse(fm.readString(path));
  } catch (e) {
    return null;
  }
}

function writeCache(quotes) {
  try {
    const fm = FileManager.local();
    fm.writeString(cachePath(), JSON.stringify({ savedAt: Date.now(), quotes }));
  } catch (e) {
    // Ignore cache write failures.
  }
}

async function fetchQuotes(mints) {
  const url = DEX_ENDPOINT + mints.join(",");
  const req = new Request(url);
  req.timeoutInterval = 20;
  req.headers = { Accept: "application/json" };
  const json = await req.loadJSON();
  if (!Array.isArray(json)) {
    throw new Error("Unexpected DexScreener response");
  }
  return parseDexPairs(json);
}

function addHeader(widget, family, statusText, ok) {
  const row = widget.addStack();
  row.layoutHorizontally();
  row.centerAlignContent();
  const title = row.addText(family === "small" ? "Memes" : "Meme Scanner");
  title.font = Font.boldSystemFont(family === "large" ? 16 : 14);
  title.textColor = TEXT;
  row.addSpacer();
  const stamp = row.addText(statusText);
  stamp.font = Font.systemFont(10);
  stamp.textColor = ok ? MUTED : RED;
  stamp.lineLimit = 1;
  widget.addSpacer(family === "large" ? 8 : 6);
}

function addRow(widget, coin, quote, family) {
  const row = widget.addStack();
  row.layoutHorizontally();
  row.centerAlignContent();
  row.url = fomoUrl(coin.mint);
  if (family === "large") {
    row.backgroundColor = SURFACE;
    row.cornerRadius = 8;
    row.setPadding(6, 8, 6, 8);
  } else {
    row.setPadding(2, 0, 2, 0);
  }

  const ticker = row.addText(coin.symbol);
  ticker.font = Font.semiboldSystemFont(family === "small" ? 12 : 13);
  ticker.textColor = TEXT;
  ticker.lineLimit = 1;
  ticker.minimumScaleFactor = 0.7;

  row.addSpacer();

  const price = quote ? quote.price : null;
  const chg = quote ? quote.change24h : null;
  const formatted = formatChange(chg);
  const tone = formatted.up == null ? MUTED : formatted.up ? GREEN : RED;

  const priceText = row.addText(formatPrice(price));
  priceText.font = Font.mediumSystemFont(family === "small" ? 11 : 12);
  priceText.textColor = TEXT;
  priceText.lineLimit = 1;
  priceText.rightAlignText();
  priceText.minimumScaleFactor = 0.7;

  row.addSpacer(8);

  const chgText = row.addText(
    formatted.arrow ? formatted.arrow + " " + formatted.text : formatted.text
  );
  chgText.font = Font.semiboldSystemFont(family === "small" ? 11 : 12);
  chgText.textColor = tone;
  chgText.lineLimit = 1;
  chgText.rightAlignText();
  chgText.minimumScaleFactor = 0.7;

  if (family !== "small") {
    // Keep change column width reasonably stable on medium/large.
    chgText.font = Font.semiboldSystemFont(12);
  }
}

async function buildWidget() {
  const family = config.widgetFamily || "large";
  const limit = limitForFamily(family);
  const coinList = await loadCoinList();
  const shown = coinList.slice(0, limit);
  const mints = shown.map((c) => c.mint);

  let quotes = {};
  let status = "Live";
  let ok = true;
  try {
    quotes = await fetchQuotes(mints);
    writeCache(quotes);
  } catch (e) {
    const cached = readCache();
    if (cached && cached.quotes) {
      quotes = cached.quotes;
      status = "Cached";
    } else {
      status = "No data";
    }
    ok = false;
  }

  const widget = new ListWidget();
  widget.backgroundColor = BG;
  widget.setPadding(family === "small" ? 10 : 12, family === "small" ? 12 : 14, 10, 12);
  widget.refreshAfterDate = new Date(Date.now() + REFRESH_MINUTES * 60 * 1000);

  if (family === "small") {
    widget.url = fomoUrl(shown[0].mint);
  }

  addHeader(widget, family, status, ok);

  for (let i = 0; i < shown.length; i++) {
    const coin = shown[i];
    addRow(widget, coin, quotes[coin.mint], family);
    if (i < shown.length - 1) {
      widget.addSpacer(family === "large" ? 5 : 3);
      if (family !== "large") {
        const rule = widget.addStack();
        rule.backgroundColor = LINE;
        rule.size = new Size(-1, 0.5);
        widget.addSpacer(3);
      }
    }
  }

  if (family === "large") {
    widget.addSpacer(8);
    const foot = widget.addText("Tap a row to open Fomo  ·  Research only, not financial advice");
    foot.font = Font.systemFont(9);
    foot.textColor = MUTED;
    foot.lineLimit = 2;
  }

  return widget;
}

const widget = await buildWidget();
if (config.runsInWidget) {
  Script.setWidget(widget);
} else {
  await widget.presentLarge();
}
Script.complete();
