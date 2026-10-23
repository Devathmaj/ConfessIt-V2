// src/lib/sessionRefresh.ts

/**
 * The one place a session is extended.
 *
 * The access token is short-lived (30 minutes) while the session behind the
 * HttpOnly refresh cookie lasts a day. Every 401 the app meets is therefore
 * usually an expired token rather than an ended session, and the cure is a
 * refresh -- not a sign-out.
 *
 * The refresh lives here, in the lib layer, because two transports need it:
 * the axios interceptor (which carries every REST call in the app) and the
 * `fetch`-based chat paths, which bypass that interceptor entirely. Sharing
 * the code matters for more than tidiness: the server rotates the refresh
 * cookie on every use, so two concurrent refreshes race each other and one of
 * them loses the cookie. One refresh at a time, whoever asks for it.
 */

import axios from 'axios';
import { API_URL } from './apiBase';
import { SESSION_REQUEST_TIMEOUT_MS } from './apiTimeouts';
import { setAccessToken } from './tokenStore';

/** How long a refresh may wait before the session is treated as absent. */
const REFRESH_TIMEOUT_MS = SESSION_REQUEST_TIMEOUT_MS;

/**
 * The account shape a refresh may carry alongside its token.
 *
 * The server sends the same payload as `/auth/me`, but this layer deliberately
 * does not depend on the AuthContext's richer `User` type (that would create a
 * cycle). The context validates the fields it needs and falls back to
 * `/auth/me` if an older server omits this key.
 */
export type RefreshedUser = Record<string, unknown>;

export interface RefreshedSession {
  accessToken: string;
  user: RefreshedUser | null;
}

let refreshInFlight: Promise<RefreshedSession | null> | null = null;

/**
 * The session's generation.
 *
 * A refresh that was already in flight when the session ended must not put its
 * token back: a log out that a pending refresh undoes is not a log out. The
 * counter is how a refresh learns that its answer is no longer wanted -- it
 * started under one generation and the session has since moved to another.
 */
let sessionGeneration = 0;

/**
 * End the pending session work.
 *
 * Every refresh in flight becomes stale: its answer is discarded rather than
 * stored, so nothing can sign the person back in after they signed out. The
 * refresh itself is not cancelled -- the server has already been asked, and
 * its answer has to be seen before the revocation can carry the right cookie.
 */
export const invalidatePendingRefreshes = (): void => {
  sessionGeneration += 1;
};

/**
 * Wait for the refresh that is in flight, if any.
 *
 * The log out needs this before it revokes. A refresh that commits first
 * rotates the cookie, so a revocation sent before the refresh settles carries
 * the value that has just been replaced -- and the server finds nothing to
 * revoke, leaving the session alive. Waiting means the revocation carries the
 * value the browser actually holds.
 */
export const awaitPendingRefresh = async (): Promise<void> => {
  const pending = refreshInFlight;
  if (!pending) return;
  try {
    await pending;
  } catch {
    // A refresh reports its own failures as null; nothing to do here.
  }
};

const requestRefresh = async (generation: number): Promise<RefreshedSession | null> => {
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
    if (generation !== sessionGeneration) {
      // The session ended while this refresh was in flight. The answer is no
      // longer wanted, and storing it would sign the person back in after they
      // signed out -- the very thing the log out has to survive.
      return null;
    }
    // The server now returns the account it just validated with the refresh.
    // Keep it untyped here; AuthContext is the only place that knows its shape.
    const rawUser = response.data?.user;
    const user = rawUser && typeof rawUser === 'object' && !Array.isArray(rawUser)
      ? (rawUser as RefreshedUser)
      : null;
    // Into the store, which is also what tells the auth context the token
    // changed: a rotated token that only the store knew about would leave the
    // context holding the expired one.
    setAccessToken(accessToken);
    return { accessToken, user };
  } catch {
    return null;
  }
};

/**
 * Ask the server to mint a fresh access token for the session held by the
 * refresh cookie, and answer with it (or null when there is no session to
 * extend).
 *
 * The cookie itself stays out of sight in page scripts; the server rotates it
 * on every use, so the proven holder of a cookie comes back with a new cookie
 * and a short-lived access token, which is kept in memory where the rest of
 * the app reads it. Any failure -- a lapsed or revoked cookie, a superseded
 * session, a network gap -- answers null so the caller can end the session
 * instead of looping.
 *
 * Concurrent callers share one refresh: the server rotates on use, and two
 * rotations racing would cost one of them the cookie.
 */
export const refreshSession = async (): Promise<RefreshedSession | null> => {
  if (!refreshInFlight) {
    const pending = requestRefresh(sessionGeneration);
    refreshInFlight = pending;
    // The slot is freed when this refresh settles, whichever way it went -- but
    // only if it is still the one in the slot, so a later refresh is not
    // cancelled by an earlier one's cleanup.
    void pending.finally(() => {
      if (refreshInFlight === pending) {
        refreshInFlight = null;
      }
    });
  }
  return refreshInFlight;
};

/**
 * Token-only view of {@link refreshSession}, for the interceptor and chat paths.
 *
 * The boot path uses `refreshSession` directly so it can consume the account
 * payload; every other caller only needs the bearer and keeps this stable API.
 */
export const refreshAccessToken = async (): Promise<string | null> =>
  (await refreshSession())?.accessToken ?? null;
