// src/lib/apiBase.ts

/**
 * The one origin every API call and WebSocket in this app is built from.
 *
 * The API address used to be a `http://localhost:8001` literal copied into half a
 * dozen modules, while only the chat socket read `VITE_API_URL` -- so setting the
 * variable fixed chat and left auth, clock and admin calls pointed at localhost, and
 * any HTTPS deployment either broke on mixed content or needed a code change. There is
 * exactly one literal left, and it is the development fallback below.
 *
 * Resolution order:
 *
 * 1. `VITE_API_URL` when set (an origin, e.g. `https://api.example.com` -- baked in
 *    at build time, which is how Vite env vars work; a prod build without it fails
 *    closed, see `scripts/check-prod-env.mjs` and `npm run build:prod`).
 * 2. In a production bundle without the variable, the page's own origin with the
 *    dev-server port swapped for the API one -- which degrades to the bare origin
 *    when the page is not served from `:5173` at all (the reverse-proxied case).
 * 3. Anywhere else (dev, tests), plain local HTTP.
 */
const configured = (import.meta.env.VITE_API_URL as string | undefined)
  ?.trim()
  .replace(/\/+$/, "");

const prodFallback =
  typeof window !== "undefined"
    ? window.location.origin.replace(":5173", ":8001")
    : "http://localhost:8001";

export const API_URL =
  configured || (import.meta.env.PROD ? prodFallback : "http://localhost:8001");

/**
 * The WebSocket origin for the same API, so one variable fixes both transports.
 *
 * Derived from {@link API_URL} rather than read separately: the split brain this file
 * replaces was exactly these two disagreeing. `http` becomes `ws` and `https` becomes
 * `wss`, so a TLS deployment gets an encrypted socket without further configuration.
 */
export const apiWsBase = (): string => {
  const url = new URL(API_URL);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString().replace(/\/+$/, "");
};
