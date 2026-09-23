#!/usr/bin/env node
/**
 * Regenerates `backend/app/data/wordle_guesses.json` from the `wordles` package.
 *
 * The playing surface of Wordle has two vocabularies: the words that can be an *answer*
 * (`wordles.json`, committed as `wordle_solutions.json`) and the far larger set of words
 * that can be *guessed* even though they could never win the day (`nonwordles.json`). A
 * player types either kind, and the manual / canonical game server accepts both, so the
 * guess vocabulary is the union of the two files.
 *
 * The package's own entry points read these files relative to the process working
 * directory, so they only resolve when the caller happens to be inside the package. The
 * paths here are derived from the resolved module location instead, which makes the script
 * safe to run from anywhere:
 *
 *   node scripts/generate-wordle-guesses.mjs
 *
 * The output is committed: the backend needs the list at runtime and does not depend on
 * npm at all. Re-run this only to refresh the list from the package.
 */

import { createRequire } from 'node:module';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const FIVE_LOWER = /^[a-z]{5}$/;

const require = createRequire(import.meta.url);
const packageDir = dirname(require.resolve('wordles'));
const sources = ['wordles.json', 'nonwordles.json'].map((name) =>
  JSON.parse(readFileSync(join(packageDir, name), 'utf8')),
);

const words = [...new Set(sources.flat())].sort();
const malformed = words.filter((word) => typeof word !== 'string' || !FIVE_LOWER.test(word));
if (malformed.length > 0) {
  throw new Error(
    `${malformed.length} entries are not five lowercase letters: ${malformed.slice(0, 5).join(', ')}`,
  );
}

const scriptDir = dirname(fileURLToPath(import.meta.url));
const target = join(scriptDir, '..', 'backend', 'app', 'data', 'wordle_guesses.json');
mkdirSync(dirname(target), { recursive: true });
writeFileSync(target, `${JSON.stringify(words, null, 2)}\n`, 'utf8');

console.log(`Wrote ${words.length} guess words to ${target}`);