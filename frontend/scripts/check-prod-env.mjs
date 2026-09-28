// scripts/check-prod-env.mjs
//
// Fail-closed gate for production frontend builds (`npm run build:prod`).
//
// The API origin is baked into the bundle at build time through `VITE_API_URL`
// (see `src/lib/apiBase.ts`). A production bundle built without it silently falls
// back to a same-origin guess, and one built with plain `http://` ships credential-
// bearing requests over cleartext (and breaks outright on an HTTPS page, where the
// browser blocks the mixed content). Both failure modes look like "the app is up but
// nothing works", so the build refuses instead of guessing.
//
// Local `npm run build` is untouched: development and preview builds keep working
// with no variables set.

const apiUrl = (process.env.VITE_API_URL ?? "").trim();

if (!apiUrl) {
  console.error(
    "Refusing production build: VITE_API_URL is not set. " +
      "Set it to the public API origin, e.g. VITE_API_URL=https://api.example.com"
  );
  process.exit(1);
}

if (!/^https:\/\//.test(apiUrl)) {
  console.error(
    `Refusing production build: VITE_API_URL must be an https:// origin, got ${JSON.stringify(apiUrl)}. ` +
      "Credential-bearing requests over plain HTTP are sniffable on the network."
  );
  process.exit(1);
}

console.log(`Production API origin: ${apiUrl}`);
