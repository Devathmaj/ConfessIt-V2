import { logger } from './logger';
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

/**
 * Everyone who wants to know when the token changes.
 *
 * The token used to be copied into the auth context's React state at boot and at
 * sign-in and never again, while the interceptor rotated it in this store every
 * time it met a 401. The copy went stale within half an hour -- and the chat
 * paths, which read the copy, then sent a bearer the server had already expired.
 * A listener is how the context learns about a rotation instead of keeping its
 * own copy: there is one token, and everything reads the same one.
 */
type AccessTokenListener = (token: string | null) => void;

const listeners = new Set<AccessTokenListener>();

const announce = (token: string | null): void => {
  // A listener that throws must not stop the others from hearing the change,
  // and must not break the setter that announced it.
  listeners.forEach((listener) => {
    try {
      listener(token);
    } catch (error) {
      logger.error('Access token listener failed:', error);
    }
  });
};

/** The current access token, or null when nobody is signed in. */
export const getAccessToken = (): string | null => inMemoryToken;

/** Remember a freshly minted access token. Passing null forgets it. */
export const setAccessToken = (token: string | null): void => {
  inMemoryToken = token;
  announce(token);
};

/** Forget the access token, e.g. on logout or when the server rejects it. */
export const clearAccessToken = (): void => {
  inMemoryToken = null;
  announce(null);
};

/**
 * Watch the token, and be told whenever it is minted, rotated or forgotten.
 *
 * Returns the unsubscribe function. A listener is told about the token as it
 * is now, not about how it got there: a subscriber that needs the current
 * value reads `getAccessToken()`.
 */
export const subscribeToAccessToken = (listener: AccessTokenListener): (() => void) => {
  listeners.add(listener);
  // The listener is told the token as it is now, so a subscriber that arrives
  // after the token was minted is not left believing nobody is signed in.
  listener(inMemoryToken);
  return () => {
    listeners.delete(listener);
  };
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
