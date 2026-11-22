// Node test for the Pages-backed widget list + DexScreener quotes.
// Run: node widget/test_widget.mjs
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = await readFile(join(here, "CoinWidget.js"), "utf8");
if (!src.includes('const COINS_JSON_URL = "https://<user>.github.io/<repo>/coins.json"')) {
  throw new Error("missing placeholder COINS_JSON_URL");
}
if (!src.includes("const FALLBACK_COINS")) {
  throw new Error("missing FALLBACK_COINS");
}
if (!src.includes("async function loadCoinList()")) {
  throw new Error("missing loadCoinList");
}

function coinsJsonUrlIsPlaceholder(url) {
  if (!url) return true;
  return url.indexOf("<user>") !== -1 || url.indexOf("<repo>") !== -1;
}
function coinsFromPayload(data) {
  const rows = data && data.coins ? data.coins : data;
  if (!Array.isArray(rows)) return [];
  const out = [];
  for (const row of rows) {
    if (row && row.mint) out.push({ symbol: row.symbol || row.mint.slice(0, 6), mint: row.mint });
  }
  return out;
}

if (!coinsJsonUrlIsPlaceholder("https://<user>.github.io/<repo>/coins.json")) {
  throw new Error("placeholder should be detected");
}
if (coinsJsonUrlIsPlaceholder("https://joey.github.io/memes/coins.json")) {
  throw new Error("real URL should not be a placeholder");
}

const empty = coinsFromPayload({ coins: [] });
if (empty.length !== 0) throw new Error("empty coins should be empty");
const parsed = coinsFromPayload({
  coins: [{ symbol: "ABC", mint: "MintABC" }, { symbol: "NOPE" }, { mint: "MintOnly" }],
});
if (parsed.length !== 2 || parsed[1].symbol !== "MintOn") {
  throw new Error("unexpected coinsFromPayload: " + JSON.stringify(parsed));
}
console.log("placeholder + parser checks passed");

const coinsPath = join(here, "..", "docs", "coins.json");
const payload = JSON.parse(await readFile(coinsPath, "utf8"));
if (!payload.scan_time_hst) throw new Error("coins.json missing scan_time_hst");
if (!Array.isArray(payload.coins)) throw new Error("coins.json missing coins array");
if (payload.coins.length > 12) throw new Error("coins.json has more than 12 coins");
for (const c of payload.coins) {
  if (!c.mint || !c.symbol) throw new Error("coin missing mint/symbol");
  if (!c.links || !c.links.fomo || !c.links.dexscreener) throw new Error("coin missing fomo/dex links: " + c.symbol);
  if (!Array.isArray(c.fail_note)) throw new Error("coin missing fail_note: " + c.symbol);
}
console.log("coins.json ok:", payload.coins.length, "coins at", payload.scan_time_hst);

const mints = payload.coins.map((c) => c.mint);
if (mints.length) {
  const res = await fetch("https://api.dexscreener.com/tokens/v1/solana/" + mints.join(","), {
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new Error("DexScreener HTTP " + res.status);
  const json = await res.json();
  if (!Array.isArray(json)) throw new Error("unexpected DexScreener type");
  console.log("DexScreener live pairs:", json.length, "for", mints.length, "mints");
}
console.log("widget tests passed");
