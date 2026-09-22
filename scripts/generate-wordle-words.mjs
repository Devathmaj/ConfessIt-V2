#!/usr/bin/env node
/**
 * Regenerates `backend/app/data/wordle_solutions.json` from the `wordles` package.
 *
 * The package's own entry points read `wordles.json` relative to the process working
 * directory, so they only resolve when the caller happens to be inside the package. The
 * path here is derived from the resolved module location instead, which makes the script
 * safe to run from anywhere:
 *
 *   node scripts/generate-wordle-words.mjs
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
const words = JSON.parse(readFileSync(join(packageDir, 'wordles.json'), 'utf8'));

const malformed = words.filter((word) => typeof word !== 'string' || !FIVE_LOWER.test(word));
if (malformed.length > 0) {
  throw new Error(
    `${malformed.length} entries are not five lowercase letters: ${malformed.slice(0, 5).join(', ')}`,
  );
}

const unique = new Set(words);
if (unique.size !== words.length) {
  const duplicate = words.find((word, index) => words.indexOf(word) !== index);
  throw new Error(`duplicate solution word: ${duplicate}`);
}

const scriptDir = dirname(fileURLToPath(import.meta.url));
const target = join(scriptDir, '..', 'backend', 'app', 'data', 'wordle_solutions.json');
mkdirSync(dirname(target), { recursive: true });
writeFileSync(target, `${JSON.stringify(words, null, 2)}\n`, 'utf8');

console.log(`Wrote ${words.length} solution words to ${target}`);
