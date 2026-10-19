import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { AUTH_EXPIRED_EVENT } from '@/lib/authEvents';
import { clearAccessToken, setAccessToken } from '@/lib/tokenStore';
import { requestWebSocketTicket } from '@/lib/wsTicket';

/**
 * The socket credential exchange (F-03), and the session it must not end (F2).
 *
 * The WebSocket URL must carry a one-use ticket, never the access token. These
 * tests pin the HTTP half: the bearer goes to `POST /auth/ws-ticket`, the ticket
 * comes back, and an expired bearer is refreshed and retried rather than read
 * as "the session is over".
 *
 * The bearer is read from the store when the exchange happens, not handed in by
 * a caller: the copy the auth context used to hold was written at boot and at
 * sign-in and never again, so half an hour into a session it was a bearer the
 * server had already expired -- and opening a chat was how a person got signed
 * out while their refresh cookie was still good for another day.
 */

vi.mock('axios', async () => {
  const post = vi.fn(async () => ({ data: {} }));
  const callable = Object.assign(
    vi.fn(async () => ({ data: {} })),
    {
      get: vi.fn(async () => ({ data: {} })),
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

const mockedAxios = vi.mocked(axios);

/** A response the way the app reads it: status, headers, and a JSON body. */
const jsonResponse = (status: number, body: unknown) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: new Headers(),
  json: async () => body,
});

let expired = 0;

// Registered once for the suite: a listener added per test would stay attached,
// and every event would then be counted once per test that came before.
const countExpired = () => {
  expired += 1;
};

beforeAll(() => {
  window.addEventListener(AUTH_EXPIRED_EVENT, countExpired);
});

afterAll(() => {
  window.removeEventListener(AUTH_EXPIRED_EVENT, countExpired);
});

beforeEach(() => {
  vi.clearAllMocks();
  clearAccessToken();
  expired = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
  clearAccessToken();
});

describe('requestWebSocketTicket', () => {
  it('exchanges the current bearer for a ticket without putting it in a URL', async () => {
    setAccessToken('access-token');
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, { ticket: 'one-use-ticket', expires_in: 60 }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(requestWebSocketTicket()).resolves.toBe('one-use-ticket');

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('http://localhost:8001/auth/ws-ticket');
    expect(init).toMatchObject({
      method: 'POST',
      headers: expect.objectContaining({
        Authorization: 'Bearer access-token',
      }),
    });
    expect(String(url)).not.toContain('access-token');
  });

  it('uses the bearer that is current at the time of the call', async () => {
    // The token the dialog opened with is not the token the exchange may use: by
    // the time the person opens a chat, the interceptor may have rotated it.
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, { ticket: 'one-use-ticket' }));
    vi.stubGlobal('fetch', fetchMock);

    setAccessToken('rotated-token');
    await requestWebSocketTicket();

    const [, init] = fetchMock.mock.calls[0];
    expect(init.headers).toMatchObject({ Authorization: 'Bearer rotated-token' });
  });

  it('refreshes an expired bearer and replays the exchange instead of ending the session', async () => {
    // The bug: the ticket exchange answered 401 for a token that had merely run
    // out, and the 401 was read as the end of the session -- the person was
    // signed out while their refresh cookie was still good for another day.
    setAccessToken('expired-token');
    mockedAxios.post.mockResolvedValue({ data: { access_token: 'fresh-token' } } as never);

    const fetchMock = vi.fn(async (_url: string, init: { headers: Record<string, string> }) => {
      if (init.headers.Authorization === 'Bearer expired-token') {
        return jsonResponse(401, { detail: 'Could not validate credentials' });
      }
      return jsonResponse(200, { ticket: 'one-use-ticket' });
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(requestWebSocketTicket()).resolves.toBe('one-use-ticket');

    // The exchange was replayed with the fresh token...
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const [, replayed] = fetchMock.mock.calls[1];
    expect(replayed.headers).toMatchObject({ Authorization: 'Bearer fresh-token' });
    // ...and the session was never ended.
    expect(expired).toBe(0);
  });

  it('ends the session when the refresh itself is refused', async () => {
    // Nothing can cure this one: the refresh cookie is gone, so the session is
    // over and the person has to sign in again.
    setAccessToken('expired-token');
    mockedAxios.post.mockRejectedValue({ response: { status: 401 } } as never);

    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(401, { detail: 'Could not validate credentials' })));

    await expect(requestWebSocketTicket()).rejects.toThrow('Could not validate credentials');
    expect(expired).toBe(1);
  });

  it('refuses a response that does not carry a ticket', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(200, {})));

    await expect(requestWebSocketTicket()).rejects.toThrow(
      'The server did not return a WebSocket ticket'
    );
  });
});
