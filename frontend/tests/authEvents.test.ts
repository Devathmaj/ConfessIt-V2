import { describe, it, expect, beforeAll, beforeEach, afterAll } from 'vitest';
import {
  AUTH_EXPIRED_EVENT,
  SESSION_SUPERSEDED_EVENT,
  SESSION_SUPERSEDED_MESSAGE,
  endSessionIfTokenWasRejected,
  isSessionSupersededError,
} from '@/lib/authEvents';

describe('isSessionSupersededError', () => {
  const rejected401 = (headers?: Record<string, string | undefined>) =>
    ({ response: { status: 401, headers: headers ?? {} } }) as never;

  it('is true only when the header carries the superseded marker', () => {
    expect(
      isSessionSupersededError(rejected401({ 'www-authenticate': 'Bearer error="session_superseded"' }))
    ).toBe(true);
  });

  it('is false for a plain 401, a 401 with other details, or no response', () => {
    expect(isSessionSupersededError(rejected401({ 'www-authenticate': 'Bearer' }))).toBe(false);
    expect(isSessionSupersededError(rejected401({ 'www-authenticate': 'Bearer error="invalid_token"' }))).toBe(false);
    expect(isSessionSupersededError(rejected401())).toBe(false);
    expect(isSessionSupersededError(undefined)).toBe(false);
  });
});

describe('endSessionIfTokenWasRejected', () => {
  let expired = 0;
  let superseded: { count: number; detail?: string } = { count: 0, detail: undefined };

  // Registered once for the suite. Adding a listener per test would leave the earlier
  // ones attached, so every event would be counted more than once.
  const countExpired = () => {
    expired += 1;
  };
  const countSuperseded = (event: Event) => {
    superseded.count += 1;
    superseded.detail = (event as CustomEvent<string>).detail;
  };

  beforeAll(() => {
    window.addEventListener(AUTH_EXPIRED_EVENT, countExpired);
    window.addEventListener(SESSION_SUPERSEDED_EVENT, countSuperseded);
  });
  afterAll(() => {
    window.removeEventListener(AUTH_EXPIRED_EVENT, countExpired);
    window.removeEventListener(SESSION_SUPERSEDED_EVENT, countSuperseded);
  });

  beforeEach(() => {
    localStorage.clear();
    expired = 0;
    superseded = { count: 0, detail: undefined };
  });

  const rejected401 = (overrides: Record<string, unknown> = {}) =>
    ({ response: { status: 401, ...overrides } }) as never;

  it('ends the session when a token-carrying request is rejected with 401', () => {
    localStorage.setItem('accessToken', 'expired-token');

    endSessionIfTokenWasRejected(rejected401());

    expect(localStorage.getItem('accessToken')).toBeNull();
    expect(expired).toBe(1);
  });

  it('ends the session only once when several in-flight requests fail together', () => {
    localStorage.setItem('accessToken', 'expired-token');

    endSessionIfTokenWasRejected(rejected401());
    endSessionIfTokenWasRejected(rejected401());
    endSessionIfTokenWasRejected(rejected401());

    expect(expired).toBe(1);
  });

  it('leaves the session alone for a 401 on a request that never carried a token', () => {
    // A failed login or a stale magic link answers 401 too, and that must not tear down a
    // session the user still has.
    endSessionIfTokenWasRejected(rejected401());

    expect(expired).toBe(0);
  });

  it('leaves the session alone for a 403, which is a business rule rather than an expiry', () => {
    // A blocked account answers 403, and that is not the same as the token going stale.
    localStorage.setItem('accessToken', 'valid-token');

    endSessionIfTokenWasRejected({ response: { status: 403 } });

    expect(localStorage.getItem('accessToken')).toBe('valid-token');
    expect(expired).toBe(0);
  });

  it('ignores responses that are not a rejection at all', () => {
    localStorage.setItem('accessToken', 'valid-token');

    endSessionIfTokenWasRejected(undefined);
    endSessionIfTokenWasRejected({ response: { status: 200 } });

    expect(localStorage.getItem('accessToken')).toBe('valid-token');
    expect(expired).toBe(0);
  });

  it('tells the holder that a replaced session ended, instead of reporting a mere expiry', () => {
    // The single-session rule: the token is valid but its session was superseded by a login
    // on another device. The server's explanation travels on the dedicated event.
    localStorage.setItem('accessToken', 'replaced-token');

    endSessionIfTokenWasRejected(
      rejected401({
        headers: { 'www-authenticate': 'Bearer error="session_superseded"' },
        data: { detail: 'You were signed out because a new session was opened on another device.' },
      })
    );

    expect(localStorage.getItem('accessToken')).toBeNull();
    expect(superseded.count).toBe(1);
    expect(superseded.detail).toBe(
      'You were signed out because a new session was opened on another device.'
    );
    expect(expired).toBe(0);
  });

  it('falls back to the shared wording when a replaced session carries no detail', () => {
    localStorage.setItem('accessToken', 'replaced-token');

    endSessionIfTokenWasRejected(
      rejected401({ headers: { 'www-authenticate': 'Bearer error="session_superseded"' } })
    );

    expect(superseded.count).toBe(1);
    expect(superseded.detail).toBe(SESSION_SUPERSEDED_MESSAGE);
    expect(expired).toBe(0);
  });
});