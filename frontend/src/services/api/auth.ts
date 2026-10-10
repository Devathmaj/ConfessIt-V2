// src/services/api/auth.ts -- authentication session helpers (split from services/api.ts).
//
// Owns the in-memory bearer store aliases, auth headers, refresh/logout flows and the
// single-flight 401 retry interceptor. All other api modules import the token helpers
// from here (via ../api barrel) rather than duplicating session logic.

import axios from 'axios';
import { API_URL } from '@/lib/apiBase';
import { endSessionIfTokenWasRejected, isSessionSupersededError } from '@/lib/authEvents';
import {
  clearAccessToken,
  getAccessToken,
  setAccessToken,
} from '@/lib/tokenStore';

// In-memory bearer store. The access token is deliberately NOT persisted to localStorage
// (or any other JS-readable storage): any XSS could read it from there and exfiltrate
// the session. It lives only in module state (see `@/lib/tokenStore`), mirrors into the
// auth context's React state for consumers, and is restored after a reload by a silent
// refresh through the HttpOnly cookie rather than by re-reading persistence.
export const setInMemoryToken = setAccessToken;
export const getInMemoryToken = getAccessToken;
export const clearInMemoryToken = clearAccessToken;

// Used to get authentication headers from the in-memory token
export const getAuthHeaders = () => {
  const token = getAccessToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
};

/**
 * Ask the server to mint a fresh access token for the session held by the refresh cookie.
 *
 * The cookie itself stays out of sight in page scripts; the server rotates it on every use,
 * so the proven holder of a cookie comes back with a new cookie and a short-lived access
 * token, which is kept in memory where the rest of the app reads it. Any failure — a lapsed or
 * revoked cookie, a superseded session, a network gap — answers null so the caller can end
 * the session instead of looping.
 */

/** How long a refresh may wait before the session is treated as absent. */
const REFRESH_TIMEOUT_MS = 15_000;

export const refreshAccessToken = async (): Promise<string | null> => {
  try {
    // Carries the refresh cookie: without credentials the cookie never travels and
    // there is no session to extend.
    const response = await axios.post(
      `${API_URL}/auth/refresh`,
      {},
      {
        withCredentials: true,
        // The boot path awaits this before rendering any route, so a backend that
        // stops answering must not leave the app on "Initializing..." forever.
        // After this budget the refresh is a failed one: null, and the sign-in
        // screen renders. Same budget as magic-link verification.
        signal: AbortSignal.timeout(REFRESH_TIMEOUT_MS),
      },
    );
    const accessToken = response.data?.access_token;
    if (typeof accessToken !== 'string') return null;
    setAccessToken(accessToken);
    return accessToken;
  } catch {
    return null;
  }
};

/**
 * Tell the server the session is over, without waiting for it.
 *
 * Signing out must not hang on the network, and the local session is cleared either way; a
 * failed call only means the refresh cookie lapses at its deadline instead of being revoked
 * now, which changes nothing the account can still reach.
 */
export const serverLogout = async (): Promise<void> => {
  try {
    // Carries the refresh cookie so the server can revoke this session's row now;
    // without it the cookie simply lapses at its deadline instead.
    await axios.post(`${API_URL}/auth/logout`, {}, { withCredentials: true });
  } catch {
    // The browser has already forgotten the session; a stale cookie is refused later.
  }
};

// 401s from the auth routes answer on their own terms: a failed login, a stale magic link,
// or a refused refresh must not be mistaken for an access token that needs extending. They
// are passed through untouched, and only they are -- `/auth/me` still gets the retry path.
export const isAuthRoute = (url: string | undefined) =>
  !!url && (url.includes('/auth/login/') || url.endsWith('/auth/refresh') || url.endsWith('/auth/logout'));

// One refresh at a time. A page with several requests in flight can fail together, and each
// failure shares this in-flight refresh instead of starting its own: exactly one rotation
// reaches the server, which matters because the server rotates on every use and two
// concurrent refreshes would race that rule.
let refreshInFlight: Promise<string | null> | null = null;

// A request replayed once is not replayed again; a second 401 after a fresh access token is
// authoritative rather than a reason to keep trying. Weak for the request's config object,
// so nothing is held onto after the request completes.
const alreadyRetried = new WeakSet<object>();

/**
 * The one place an access token stops being accepted. A 401 that the refresh cookie can
 * cure is retried once with a fresh token; a 401 that nothing can cure ends the session and
 * lets the auth context explain why.
 *
 * Requests that 401 for any reason other than the access token are passed through untouched
 * for their own callers, exactly as they were before the refresh existed.
 */
export const handleResponseError = async (error: unknown): Promise<unknown> => {
  const status = (error as { response?: { status?: number } })?.response?.status;
  const original = (error as { config?: Record<string, unknown> & { url?: string; headers?: Record<string, string> } })?.config;
  const url = typeof original?.url === 'string' ? original.url : undefined;

  if (status !== 401 || !original || isAuthRoute(url)) {
    return Promise.reject(error);
  }

  // A request that already rode a fresh access token and still failed is over; the retry
  // did not save it, so the session is not worth keeping.
  if (alreadyRetried.has(original)) {
    endSessionIfTokenWasRejected(error);
    return Promise.reject(error);
  }

  // A 401 from a request that never carried an access token is not a sign-out.
  if (!getAccessToken()) {
    return Promise.reject(error);
  }

  // A superseded session is over regardless of refresh: the newer login owns the account
  // now, and extending the old session would fight the single-session rule. The dedicated
  // event carries the server's explanation to the "signed in elsewhere" toast.
  if (isSessionSupersededError(error)) {
    endSessionIfTokenWasRejected(error);
    return Promise.reject(error);
  }

  if (!refreshInFlight) {
    refreshInFlight = refreshAccessToken().finally(() => {
      refreshInFlight = null;
    });
  }
  const freshToken = await refreshInFlight;
  if (!freshToken) {
    endSessionIfTokenWasRejected(error);
    return Promise.reject(error);
  }

  alreadyRetried.add(original);
  if (original.headers) {
    original.headers.Authorization = `Bearer ${freshToken}`;
  } else {
    original.headers = { Authorization: `Bearer ${freshToken}` };
  }
  return axios(original);
};

// Registered on the shared instance because every endpoint in this file uses it, so this is
// the one place an expired token is noticed and a stale one is refreshed. Requests that fail
// for any other reason are passed through untouched for their own caller to handle.
axios.interceptors.response.use(
  (response) => response,
  (error) => handleResponseError(error)
);
