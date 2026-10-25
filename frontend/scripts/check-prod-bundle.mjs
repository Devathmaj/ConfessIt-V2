// scripts/check-prod-bundle.mjs
//
// Post-build gate for production frontend bundles (`npm run build:prod`).
//
// Two things must never reach the Nginx image:
//   * lovable-tagger (dev-only Vite plugin) injects `data-component` attributes
//     carrying source paths into the DOM. The plugin is gated to
//     `mode === 'development'` in vite.config.ts, and this script is the second
//     line of defence: it scans dist for tagger markers and fails the build if
//     one ever lands in a production bundle.
//   * console calls: vite.config.ts substitutes every console method with a
//     no-op in production. This gate proves the substitution covered the whole
//     bundle (including public/sw.js), so no request or message data can be
//     logged from a browser served by Nginx.

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const dist = fileURLToPath(new URL("../dist", import.meta.url));
const markers = ["data-component", "data-lovable", "lovable-tagger"];
const consoleCall =
  /\bconsole\s*\.\s*(?:log|info|warn|error|debug|trace|table|dir|dirxml|group|groupCollapsed|groupEnd|assert|count|countReset|clear|time|timeEnd|timeLog|timeStamp|profile|profileEnd)\s*\(/;

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
    if (consoleCall.test(text)) hits.push(`${file}: console call`);
  }
} catch (err) {
  console.error(`Refusing production build: cannot scan dist/ (${err.message})`);
  process.exit(1);
}

if (hits.length > 0) {
  console.error(
    "Refusing production build: dev-only markers or console calls found in dist/:\n" +
      hits.slice(0, 20).join("\n") +
      "\nEnsure lovable-tagger stays gated to development mode and console methods stay substituted."
  );
  process.exit(1);
}

console.log("Production bundle clean: no dev-only markers and no console calls.");
