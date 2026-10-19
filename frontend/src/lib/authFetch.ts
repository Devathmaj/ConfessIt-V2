// src/lib/authFetch.ts

/**
 * The `fetch` the chat paths use, and the reason they are not broken.
 *
 * The messaging calls talk to the API with `fetch` rather than the shared
 * axios instance, so they never met the response interceptor that extends an
 * expired session. They used the token the auth context held -- a copy that
 * was written at boot and at sign-in and never again, so half an hour into a
 * session it was a bearer the server had already expired. The ticket exchange
 * then answered 401, and the 401 was read as "the session is over": the person
 * was signed out while their refresh cookie was still good for another day.
 *
 * So the bearer is read from the store at the moment of the call (the store is
 * where the app's token lives, and the interceptor keeps it current), and an
 * expired one is refreshed and the request replayed rather than treated as the
 * end of the session.
 */

import { endSessionIfTokenWasRejected } from './authEvents';
import { refreshAccessToken } from './sessionRefresh';
import { getAccessToken } from './tokenStore';

/**
 * The request with the bearer attached.
 *
 * The headers arrive as a plain object (every caller in the app passes one),
 * so they are merged rather than rebuilt: a `Headers` instance would lowercase
 * the names, and the content type the server checks for would change shape.
 */
const withBearer = (init: RequestInit, token: string | null): RequestInit => {
  const headers: Record<string, string> = { ...(init.headers as Record<string, string> | undefined) };
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  } else {
    delete headers.Authorization;
  }
  return { ...init, headers };
};

/**
 * The shape `endSessionIfTokenWasRejected` reads: the status, the headers that
 * distinguish a superseded session from an ordinary expiry, and the body when
 * it has already been read.
 */
const rejectionOf = (response: Response, body?: unknown) => ({
  response: {
    status: response.status,
    headers: Object.fromEntries(response.headers.entries()),
    data: body as { detail?: unknown } | undefined,
  },
});

/**
 * A `fetch` that carries the current access token and survives its expiry.
 *
 * A 401 is answered with one refresh and one replay, because a 401 here is
 * almost always a token that ran out rather than a session that ended. Only a
 * refresh that fails, or a replay that is refused again, ends the session --
 * and then through the shared handler, so the person is told once and the
 * event carries the server's own explanation.
 */
export const authorizedFetch = async (url: string, init: RequestInit = {}): Promise<Response> => {
  const first = await fetch(url, withBearer(init, getAccessToken()));
  if (first.status !== 401) {
    return first;
  }

  // The refresh cookie may still be good: extend the session and try again.
  const freshToken = await refreshAccessToken();
  if (!freshToken) {
    endSessionIfTokenWasRejected(rejectionOf(first));
    return first;
  }

  const replayed = await fetch(url, withBearer(init, freshToken));
  if (replayed.status === 401) {
    // A fresh token that is still refused is not an expired token.
    endSessionIfTokenWasRejected(rejectionOf(replayed));
  }
  return replayed;
};
