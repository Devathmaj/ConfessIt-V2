// frontend/tests/apiRefresh.test.ts

/**
 * The access-token refresh path in src/services/api.ts, exercised against a mocked axios.
 *
 * `handleResponseError` is the interceptor body: it decides which 401s are worth a refresh
 * and which are not, runs a single shared refresh so a burst of simultaneous failures does
 * not race the server's rotate-on-use rule, and retries the original request exactly once
 * with the fresh token. Everything else -- failed logins, failed refreshes, superseded
 * sessions, requests that never carried a token -- is passed through without a session-wide
 * reaction.
 *
 * axios is replaced by a callable mock so the retry (`axios(original)`) and the refresh
 * call (`axios.post`) can be scripted without reaching a server.
 */

import { describe, it, expect, vi, beforeEach, afterAll, beforeAll } from 'vitest';

vi.mock('axios', async () => {
  const post = vi.fn(async () => ({ data: {} }));
  const get = vi.fn(async () => ({ data: {} }));
  const callable = Object.assign(
    vi.fn(async () => ({ data: {} })),
    {
      get,
      post,
      put: vi.fn(async () => ({ data: {} })),
      patch: vi.fn(async () => ({ data: {} })),
      delete: vi.fn(async () => ({ data: {} })),
      defaults: {},
      interceptors: { response: { use: vi.fn() } },
    } as never
  );
  return { default: callable };
});

import axios from 'axios';
import { clearInMemoryToken, getCurrentUser, handleResponseError, refreshAccessToken, serverLogout, setInMemoryToken, getInMemoryToken } from '@/services/api';
import { AUTH_EXPIRED_EVENT, SESSION_SUPERSEDED_EVENT } from '@/lib/authEvents';

const mockedAxios = vi.mocked(axios);

describe('handleResponseError (access-token refresh)', () => {
  let expired = 0;
  let superseded = 0;

  beforeAll(() => {
    window.addEventListener(AUTH_EXPIRED_EVENT, () => {
      expired += 1;
    });
    window.addEventListener(SESSION_SUPERSEDED_EVENT, () => {
      superseded += 1;
    });
  });
  afterAll(() => {
    window.removeEventListener(AUTH_EXPIRED_EVENT, () => {
      expired += 1;
    });
    window.removeEventListener(SESSION_SUPERSEDED_EVENT, () => {
      superseded += 1;
    });
  });

  beforeEach(() => {
    clearInMemoryToken();
    expired = 0;
    superseded = 0;
    vi.clearAllMocks();
  });

  // A 401 shaped like axios hands it to the interceptor, with a request config that carried
  // the stored access token.
  const rejected401 = (
    url: string,
    overrides: { headers?: Record<string, string | undefined>; config?: object } = {}
  ) => ({
    response: { status: 401, headers: overrides.headers ?? {} },
    config: { url, headers: {}, ...overrides.config },
  });

  const refreshSucceeds = (token = 'fresh-access') =>
    mockedAxios.post.mockResolvedValue({ data: { access_token: token } } as never);
  const refreshFails = () =>
    mockedAxios.post.mockRejectedValue({ response: { status: 401 } } as never);

  // The axios mock is untyped at the call boundary, so what a replayed call carried is
  // recovered here: the request config, with the headers these tests assert on. The
  // runtime value is untouched -- only the reading is typed.
  const retriedConfigOf = (call: readonly unknown[]) =>
    call[0] as { headers: Record<string, string | undefined> } & Record<string, unknown>;

  it('replays a 401 once with the fresh token after a successful refresh', async () => {
    setInMemoryToken('old-token');
    refreshSucceeds();

    const originalError = rejected401('/confessions');
    const result = await handleResponseError(originalError);

    expect(mockedAxios.post).toHaveBeenCalledWith(
      'http://localhost:8001/auth/refresh',
      {},
      { withCredentials: true },
    );
    expect(mockedAxios).toHaveBeenCalledTimes(1);
    const retryConfig = retriedConfigOf(mockedAxios.mock.calls[0]);
    expect(retryConfig).toBe(originalError.config);
    expect(retryConfig.headers.Authorization).toBe('Bearer fresh-access');
    // The retry is a Bearer-only API call: the refresh must not smuggle cookie
    // credentials onto it by mutating the config it was given.
    expect(retryConfig).not.toHaveProperty('withCredentials');
    expect(result.data).toEqual({});
    expect(expired).toBe(0);
  });

  it('adds an Authorization header when the original request never sent one', async () => {
    setInMemoryToken('old-token');
    refreshSucceeds();

    await handleResponseError({ response: { status: 401 }, config: { url: '/profile/update' } });

    const retryConfig = retriedConfigOf(mockedAxios.mock.calls[0]);
    expect(retryConfig.headers).toEqual({ Authorization: 'Bearer fresh-access' });
  });

  it('shares one refresh across simultaneous failures', async () => {
    setInMemoryToken('old-token');

    let resolvePost!: (value: { data: { access_token: string } }) => void;
    mockedAxios.post.mockReturnValueOnce(
      new Promise((resolve) => {
        resolvePost = resolve;
      }) as never
    );

    // Both 401s arrive before the refresh settles, then both wait on the same in-flight call.
    const first = handleResponseError(rejected401('/confessions'));
    const second = handleResponseError(rejected401('/comments'));
    await Promise.resolve();

    // Only one /auth/refresh reached the server.
    expect(mockedAxios.post).toHaveBeenCalledTimes(1);

    resolvePost({ data: { access_token: 'fresh-access' } });
    await Promise.all([first, second]);

    // Both original requests were replayed, each with the fresh token.
    expect(mockedAxios).toHaveBeenCalledTimes(2);
    const [a, b] = mockedAxios.mock.calls.map(
      (call) => retriedConfigOf(call).headers.Authorization
    );
    expect(a).toBe('Bearer fresh-access');
    expect(b).toBe('Bearer fresh-access');
  });

  it('ends the session when the refresh itself fails', async () => {
    setInMemoryToken('old-token');
    refreshFails();

    await expect(handleResponseError(rejected401('/confessions'))).rejects.toMatchObject({
      response: { status: 401 },
    });

    expect(getInMemoryToken()).toBeNull();
    expect(expired).toBe(1);
    expect(mockedAxios).not.toHaveBeenCalled();
  });

  it('stores the fresh token where the rest of the app reads it', async () => {
    setInMemoryToken('old-token');
    refreshSucceeds('rotated-access');

    await handleResponseError(rejected401('/confessions'));

    expect(getInMemoryToken()).toBe('rotated-access');
  });

  it('never persists the bearer to JS-readable storage', async () => {
    // Regression guard for the XSS finding: a rotated or freshly minted token must not
    // land in localStorage (or anywhere else page scripts of another origin path could
    // read), only in the in-memory store.
    setInMemoryToken('old-token');
    refreshSucceeds('rotated-access');

    await handleResponseError(rejected401('/confessions'));
    expect(await refreshAccessToken()).toBe('rotated-access');

    expect(localStorage.getItem('accessToken')).toBeNull();
    expect(getInMemoryToken()).toBe('rotated-access');
  });

  it('does not refresh a request that already rode a fresh token, and ends the session', async () => {
    setInMemoryToken('old-token');
    refreshSucceeds();

    const originalError = rejected401('/confessions');
    await handleResponseError(originalError);
    expect(mockedAxios).toHaveBeenCalledTimes(1);

    // The same request fails again, now carrying the token the first retry minted.
    await expect(handleResponseError(originalError)).rejects.toMatchObject({
      response: { status: 401 },
    });

    expect(mockedAxios.post).toHaveBeenCalledTimes(1);
    expect(getInMemoryToken()).toBeNull();
    expect(expired).toBe(1);
  });

  it('ends the session with the superseded event instead of refreshing', async () => {
    setInMemoryToken('replaced-token');

    await expect(
      handleResponseError(
        rejected401('/confessions', {
          headers: { 'www-authenticate': 'Bearer error="session_superseded"' },
        })
      )
    ).rejects.toMatchObject({ response: { status: 401 } });

    expect(getInMemoryToken()).toBeNull();
    expect(superseded).toBe(1);
    expect(expired).toBe(0);
    expect(mockedAxios.post).not.toHaveBeenCalled();
  });

  it('leaves auth-route 401s alone, even with a token in storage', async () => {
    setInMemoryToken('old-token');

    for (const url of [
      'http://localhost:8001/auth/login/magic/verify?token=abc',
      'http://localhost:8001/auth/refresh',
      'http://localhost:8001/auth/logout',
    ]) {
      await expect(handleResponseError(rejected401(url))).rejects.toMatchObject({
        response: { status: 401 },
      });
    }

    expect(mockedAxios.post).not.toHaveBeenCalled();
    expect(getInMemoryToken()).toBe('old-token');
    expect(expired).toBe(0);
  });

  it('passes through a 401 from a request that never carried a token', async () => {
    // No access token in storage: a failed login 401 must not tear down anything.
    await expect(handleResponseError(rejected401('/auth/login/email'))).rejects.toMatchObject({
      response: { status: 401 },
    });
    expect(mockedAxios.post).not.toHaveBeenCalled();
    expect(expired).toBe(0);
  });

  it('passes through anything that is not a 401', async () => {
    setInMemoryToken('valid-token');

    await expect(handleResponseError({ response: { status: 403 }, config: {} })).rejects.toMatchObject({
      response: { status: 403 },
    });
    await expect(handleResponseError(undefined)).rejects.toBeUndefined();

    expect(mockedAxios.post).not.toHaveBeenCalled();
    expect(getInMemoryToken()).toBe('valid-token');
  });

  it('refreshAccessToken answers null when the refresh is refused', async () => {
    refreshFails();
    expect(await refreshAccessToken()).toBeNull();
  });

  it('refreshAccessToken answers null when the reply has no access token', async () => {
    mockedAxios.post.mockResolvedValue({ data: { detail: 'rate limited' } } as never);
    expect(await refreshAccessToken()).toBeNull();
  });

  it('sends cookies only on the refresh call, not on retried API calls', async () => {
    // The refresh cookie is what the rotation is for; the replayed Bearer call must
    // not carry it. Assert both halves of that contract in one place.
    setInMemoryToken('old-token');
    refreshSucceeds();

    await handleResponseError(rejected401('/confessions'));

    const [refreshUrl, refreshBody, refreshConfig] = mockedAxios.post.mock.calls[0];
    expect(refreshUrl).toBe('http://localhost:8001/auth/refresh');
    expect(refreshBody).toEqual({});
    expect(refreshConfig).toEqual({ withCredentials: true });

    const [retryConfig] = mockedAxios.mock.calls[0];
    expect(retryConfig).not.toHaveProperty('withCredentials');
  });

  it('serverLogout carries cookies so the server can revoke the session', async () => {
    await serverLogout();

    expect(mockedAxios.post).toHaveBeenCalledWith(
      'http://localhost:8001/auth/logout',
      {},
      { withCredentials: true },
    );
  });

  it('ordinary API calls send no cookies', async () => {
    // /auth/me is Bearer-only on the server: attaching the refresh cookie to it would
    // be pure CSRF surface, so the client must not opt in.
    await getCurrentUser('valid-token');

    expect(mockedAxios.get).toHaveBeenCalledWith(
      'http://localhost:8001/auth/me',
      { headers: { Authorization: 'Bearer valid-token' } },
    );
  });
});