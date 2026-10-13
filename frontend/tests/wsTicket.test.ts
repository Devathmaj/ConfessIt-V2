import { afterEach, describe, expect, it, vi } from 'vitest';

import { AUTH_EXPIRED_EVENT } from '@/lib/authEvents';
import { clearAccessToken, setAccessToken } from '@/lib/tokenStore';
import { requestWebSocketTicket } from '@/lib/wsTicket';

/**
 * The socket credential exchange (F-03).
 *
 * The WebSocket URL must carry a one-use ticket, never the access token. These
 * tests pin the HTTP half: the bearer goes to `POST /auth/ws-ticket`, the ticket
 * comes back, and a rejected bearer still ends the ordinary session.
 */

afterEach(() => {
  vi.unstubAllGlobals();
  clearAccessToken();
});

describe('requestWebSocketTicket', () => {
  it('exchanges the bearer for a ticket without putting it in a URL', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ ticket: 'one-use-ticket', expires_in: 60 }),
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(requestWebSocketTicket('access-token')).resolves.toBe(
      'one-use-ticket'
    );

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

  it('ends the session when the ticket exchange rejects the access token', async () => {
    setAccessToken('rejected-access-token');
    const expired = vi.fn();
    window.addEventListener(AUTH_EXPIRED_EVENT, expired);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 401,
        headers: new Headers(),
        json: async () => ({ detail: 'Could not validate credentials' }),
      })
    );

    try {
      await expect(requestWebSocketTicket('rejected-access-token')).rejects.toThrow(
        'Could not validate credentials'
      );
      expect(expired).toHaveBeenCalledOnce();
    } finally {
      window.removeEventListener(AUTH_EXPIRED_EVENT, expired);
    }
  });

  it('refuses a response that does not carry a ticket', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({}),
      })
    );

    await expect(requestWebSocketTicket('access-token')).rejects.toThrow(
      'The server did not return a WebSocket ticket'
    );
  });
});
