// src/lib/tokenStore.ts

/**
 * The single home of the short-lived access JWT on this page.
 *
 * The token lives in module state only -- never in `localStorage`,
 * `sessionStorage`, cookies readable by scripts, or any other JS-readable
 * persistence. Any cross-site script (a compromised dependency, an injected
 * `<svg>`, user content that escapes sanitisation) can read persistent
 * stores and exfiltrate the bearer; a variable it cannot enumerate without
 * already owning the page's JS context raises the bar to a full script
 * takeover, at which point no token placement would help anyway.
 *
 * The refresh credential is the HttpOnly cookie the server sets on
 * `Path=/auth` (see backend `app/routers/auth.py::_refresh_cookie_options`):
 * page scripts cannot read it, the browser only attaches it to `/auth`
 * requests, and the server rotates it on every use. When this page reloads
 * the in-memory token is gone by design, and the session is restored -- if
 * the cookie is still valid -- by one silent `POST /auth/refresh`, not by
 * re-reading anything persisted.
 */

let inMemoryToken: string | null = null;

/** The current access token, or null when nobody is signed in. */
export const getAccessToken = (): string | null => inMemoryToken;

/** Remember a freshly minted access token. Passing null forgets it. */
export const setAccessToken = (token: string | null): void => {
  inMemoryToken = token;
};

/** Forget the access token, e.g. on logout or when the server rejects it. */
export const clearAccessToken = (): void => {
  inMemoryToken = null;
};

/** Whether a token is currently held (without exposing it). */
export const hasAccessToken = (): boolean => inMemoryToken !== null;

/**
 * Delete a token persisted by older builds of the app.
 *
 * Builds before the in-memory store kept the bearer in `localStorage`,
 * where any XSS could read it. A browser upgrading from one of those
 * builds may still hold such a token, so it is wiped on import and from
 * the sign-in initialisation path rather than left to lapse.
 */
export const purgeLegacyPersistedToken = (): void => {
  try {
    if (typeof localStorage !== 'undefined') {
      localStorage.removeItem('accessToken');
    }
  } catch {
    // Storage may be unavailable (private mode, blocked); nothing to purge.
  }
};

// Wipe any token an older build left behind as soon as this module loads,
// before any request could attach it.
purgeLegacyPersistedToken();
