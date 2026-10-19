import { act, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { ConversationDialog } from '@/components/ConversationDialog';
import { AUTH_EXPIRED_EVENT } from '@/lib/authEvents';
import { clearAccessToken, getAccessToken, setAccessToken } from '@/lib/tokenStore';

/**
 * The token a chat carries, and the session it must not end (F2).
 *
 * The access token is short-lived (30 minutes) while the session behind the
 * HttpOnly refresh cookie lasts a day. The auth context used to hold its own
 * copy of the token -- written at boot and at sign-in and never again -- while
 * the interceptor rotated the real one in the store every time it met a 401.
 * The chat paths read the context's copy, so half an hour into a session they
 * sent a bearer the server had already expired: the WebSocket ticket exchange
 * answered 401, and the refusal was read as "the session is over". Half an
 * hour of use, and the person was signed out while their refresh cookie was
 * still good for another day.
 *
 * Three properties are pinned here:
 *
 * * the auth context follows the token the store rotates to, so it cannot drift;
 * * the chat calls read the bearer that is current when the call is made;
 * * an expired bearer is refreshed and the call replayed, rather than ending
 *   the session.
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

const api = vi.hoisted(() => ({
  getCurrentConversation: vi.fn(),
  acceptConversation: vi.fn(),
  rejectConversation: vi.fn(),
  getConversationByMatch: vi.fn(),
  blockConversation: vi.fn(),
  unblockConversation: vi.fn(),
  // The auth provider's own calls. The session is restored by a silent refresh
  // at boot; there is none here, so the provider starts signed out.
  getCurrentUser: vi.fn(),
  refreshAccessToken: vi.fn(async () => null),
  serverLogout: vi.fn(),
  setInMemoryToken: vi.fn(),
  clearInMemoryToken: vi.fn(),
}));

vi.mock('@/services/api', () => api);

import axios from 'axios';

const mockedAxios = vi.mocked(axios);

/** A response the way the app reads it: status, headers, and a JSON body. */
const jsonResponse = (status: number, body: unknown) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: new Headers(),
  json: async () => body,
});

/** A socket that opens and then does nothing, so the client believes it connected. */
class FakeWebSocket {
  static OPEN = 1;
  readyState = 1;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  onclose: ((event: { code: number; reason: string }) => void) | null = null;
  send() {}
  close() {
    this.readyState = 3;
    this.onclose?.({ code: 1000, reason: '' });
  }
  constructor(public url: string) {
    setTimeout(() => this.onopen?.(), 0);
  }
}

const conversationData = () => ({
  match: {
    id: 'match-1',
    expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
    created_at: new Date(Date.now() - 60 * 60 * 1000).toISOString(),
    is_expired: false,
  },
  conversation: {
    id: 'conv-1',
    status: 'accepted',
    initiator_regno: 'ALICE',
    receiver_regno: 'BOB',
    created_at: '2026-05-01T10:00:00.000Z',
    accepted_at: '2026-05-01T10:00:00.000Z',
  },
  other_user: {
    regno: 'ALICE',
    name: 'Alice',
    profile_picture_url: null,
    year: 2,
    department: 'CSE',
    bio: '',
    interests: [],
  },
  is_initiator: false,
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
  api.getConversationByMatch.mockResolvedValue({ status: 'success', is_blocked: false });
});

afterEach(() => {
  vi.unstubAllGlobals();
  clearAccessToken();
});

describe('the token a chat carries', () => {
  it('follows the token the store rotates to', async () => {
    // The context's token used to be written twice -- at boot and after a
    // sign-in -- and never again, while the interceptor rotated the real one in
    // the store every time it met a 401. The context is now a mirror of the
    // store, so it cannot drift from it.
    const { AuthProvider, useAuth } = await import('@/contexts/AuthContext');

    const TokenProbe = () => {
      const { token } = useAuth();
      return <span data-testid="token">{token ?? 'none'}</span>;
    };

    render(
      <AuthProvider>
        <TokenProbe />
      </AuthProvider>,
    );

    await waitFor(() => expect(screen.getByTestId('token')).toHaveTextContent('none'));

    act(() => {
      setAccessToken('rotated-token');
    });

    await waitFor(() => expect(screen.getByTestId('token')).toHaveTextContent('rotated-token'));
  });

  it('reads the bearer that is current when the call is made', async () => {
    setAccessToken('current-token');
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(200, { messages: [], quota: null, total: 0 }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const { fetchMessages } = await import('@/lib/messaging');
    await fetchMessages('conv-1');

    const [, init] = fetchMock.mock.calls[0];
    expect(init.headers).toMatchObject({ Authorization: 'Bearer current-token' });
  });

  it('refreshes an expired bearer and replays the call instead of ending the session', async () => {
    setAccessToken('expired-token');
    mockedAxios.post.mockResolvedValue({ data: { access_token: 'fresh-token' } } as never);

    const fetchMock = vi.fn(async (_url: string, init: { headers: Record<string, string> }) => {
      if (init.headers.Authorization === 'Bearer expired-token') {
        return jsonResponse(401, { detail: 'Could not validate credentials' });
      }
      return jsonResponse(200, { messages: [], quota: null, total: 0 });
    });
    vi.stubGlobal('fetch', fetchMock);

    const { fetchMessages } = await import('@/lib/messaging');
    await fetchMessages('conv-1');

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const [, replayed] = fetchMock.mock.calls[1];
    expect(replayed.headers).toMatchObject({ Authorization: 'Bearer fresh-token' });
    // The session was extended, not ended.
    expect(expired).toBe(0);
    expect(getAccessToken()).toBe('fresh-token');
  });

  it('opens a conversation with the current bearer, not the one the dialog was opened with', async () => {
    // The regression the audit named: a stale token in the context and a valid
    // one in the store. The ticket exchange must use the store's, and the
    // refusal of a stale one must not sign the person out.
    setAccessToken('fresh-store-token');
    const ticketRequests: { url: string; authorization?: string }[] = [];
    const fetchMock = vi.fn(async (url: string, init: { headers?: Record<string, string> }) => {
      if (String(url).includes('/auth/ws-ticket')) {
        ticketRequests.push({ url: String(url), authorization: init?.headers?.Authorization });
        return jsonResponse(200, { ticket: 'one-use-ticket' });
      }
      return jsonResponse(200, { messages: [], quota: null, total: 0 });
    });
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('WebSocket', FakeWebSocket);

    render(
      <AuthContext.Provider
        value={{
          user: { regno: 'BOB', name: 'Bob' },
          // The stale copy the old code would have handed to the messaging layer.
          token: 'stale-context-token',
          isAuthenticated: true,
          loading: false,
        } as never}
      >
        <MemoryRouter>
          <ConversationDialog
            onClose={() => {}}
            onRefresh={() => {}}
            conversationData={conversationData() as never}
          />
        </MemoryRouter>
      </AuthContext.Provider>,
    );

    await waitFor(() => expect(ticketRequests.length).toBeGreaterThan(0));
    expect(ticketRequests[0].authorization).toBe('Bearer fresh-store-token');
    // Opening a chat is not how a session ends.
    expect(expired).toBe(0);
  });
});
