// scripts/check-prod-bundle.mjs
//
// Post-build gate for production frontend bundles (`npm run build:prod`).
//
// lovable-tagger (dev-only Vite plugin) injects `data-component` attributes carrying
// source paths into the DOM. The plugin is gated to `mode === 'development'` in
// vite.config.ts, and this script is the second line of defence: it scans dist for
// tagger markers and fails the build if one ever lands in a production bundle.

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const dist = new URL("../dist", import.meta.url).pathname;
const markers = ["data-component", "data-lovable", "lovable-tagger"];

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) yield* walk(full);
    else if (/\.(js|jsx|ts|tsx|html|css)$/.test(entry)) yield full;
  }
}

let hits = [];
try {
  for (const file of walk(dist)) {
    let text;
    try {
      text = readFileSync(file, "utf8");
    } catch {
      continue;
    }
    for (const marker of markers) {
      if (text.includes(marker)) hits.push(`${file}: ${marker}`);
    }
  }
} catch (err) {
  console.error(`Refusing production build: cannot scan dist/ (${err.message})`);
  process.exit(1);
}

if (hits.length > 0) {
  console.error(
    "Refusing production build: dev-only tagger markers found in dist/:\n" +
      hits.slice(0, 20).join("\n") +
      "\nEnsure lovable-tagger stays gated to development mode."
  );
  process.exit(1);
}

console.log("Production bundle clean: no dev-only tagger markers.");
