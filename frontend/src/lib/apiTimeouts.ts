// src/lib/apiTimeouts.ts

/**
 * How long a session call may wait before the browser stops waiting for it.
 *
 * One number for the whole session handshake, because the calls are one
 * handshake: the refresh mints the access token and the user read spends it. A
 * budget on the second call that was shorter than the first would abandon the
 * read while the refresh it depends on is still in flight.
 *
 * The budget exists at all because axios has none of its own: without one, a
 * backend that stops answering leaves the boot sequence waiting for ever, and
 * the whole app sits on its splash screen with no way out but a reload.
 *
 * The same number is used for the refresh because it is the call that decides
 * whether there is a session at all: a refresh that waited for ever would
 * strand the app on "Initializing..." just as surely as the read that follows
 * it.
 */
export const SESSION_REQUEST_TIMEOUT_MS = 15_000;
