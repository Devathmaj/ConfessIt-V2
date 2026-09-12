import { describe, it, expect, beforeAll, beforeEach, afterAll } from 'vitest';
import { AUTH_EXPIRED_EVENT, endSessionIfTokenWasRejected } from './authEvents';

describe('endSessionIfTokenWasRejected', () => {
  let fired = 0;
  // Registered once for the suite. Adding a listener per test would leave the earlier
  // ones attached, so every event would be counted more than once.
  const count = () => {
    fired += 1;
  };

  beforeAll(() => window.addEventListener(AUTH_EXPIRED_EVENT, count));
  afterAll(() => window.removeEventListener(AUTH_EXPIRED_EVENT, count));

  beforeEach(() => {
    localStorage.clear();
    fired = 0;
  });

  it('ends the session when a token-carrying request is rejected with 401', () => {
    localStorage.setItem('accessToken', 'expired-token');

    endSessionIfTokenWasRejected(401);

    expect(localStorage.getItem('accessToken')).toBeNull();
    expect(fired).toBe(1);
  });

  it('ends the session only once when several in-flight requests fail together', () => {
    localStorage.setItem('accessToken', 'expired-token');

    endSessionIfTokenWasRejected(401);
    endSessionIfTokenWasRejected(401);
    endSessionIfTokenWasRejected(401);

    expect(fired).toBe(1);
  });

  it('leaves the session alone for a 401 on a request that never carried a token', () => {
    // A failed login or a stale magic link answers 401 too, and that must not tear down a
    // session the user still has.
    endSessionIfTokenWasRejected(401);

    expect(fired).toBe(0);
  });

  it('leaves the session alone for a 403, which is a business rule rather than an expiry', () => {
    // A blocked account answers 403, and that is not the same as the token going stale.
    localStorage.setItem('accessToken', 'valid-token');

    endSessionIfTokenWasRejected(403);

    expect(localStorage.getItem('accessToken')).toBe('valid-token');
    expect(fired).toBe(0);
  });

  it('ignores responses that are not a rejection at all', () => {
    localStorage.setItem('accessToken', 'valid-token');

    endSessionIfTokenWasRejected(undefined);
    endSessionIfTokenWasRejected(200);

    expect(localStorage.getItem('accessToken')).toBe('valid-token');
    expect(fired).toBe(0);
  });
});
