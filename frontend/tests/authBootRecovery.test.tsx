import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthProvider, useAuth } from '@/contexts/AuthContext';
import { clearAccessToken, getAccessToken } from '@/lib/tokenStore';

/**
 * The boot handshake, and what it must not do (F6).
 *
 * The boot is a handshake: one silent refresh mints the access token, and one
 * read of the account spends it. Two things used to go wrong there.
 *
 * The read had no budget at all -- axios waits for ever by default -- so a
 * backend that stopped answering held the whole app on its splash screen, with
 * no way out but a reload. And any failure of the read was read as a refusal:
 * the catch called the logout, which revokes the session server-side as well,
 * so a 500 while the backend was deploying signed the person out for good while
 * their refresh cookie was still valid for another day.
 *
 * These tests pin the difference that fixes both: a refused token ends the
 * session, a failed request does not.
 */

const api = vi.hoisted(() => ({
  mode: 'ok' as 'ok' | 'hang' | 'server-error' | 'rejected',
  reads: 0,
  logouts: 0,
  refreshUser: null as Record<string, unknown> | null,
  current: {
    id: 'a-uuid',
    name: 'Someone',
    regno: 'SOME-REG',
    account_type: 'users',
    capabilities: [] as string[],
  },
}));

vi.mock('axios', () => ({
  default: {
    defaults: {},
    // The API layer registers a response interceptor at import time, so the
    // stub has to be shaped like the object that registration expects.
    interceptors: { response: { use: () => {} } },
    get: async (_url: string, config?: { signal?: AbortSignal }) => {
      api.reads += 1;
      if (api.mode === 'hang') {
        // A request that never answers, until its own budget runs out.
        return new Promise((_resolve, reject) => {
          config?.signal?.addEventListener('abort', () => reject(new Error('timeout')));
        });
      }
      if (api.mode === 'server-error') {
        const error = Object.assign(new Error('Internal Server Error'), {
          response: { status: 500 },
        });
        throw error;
      }
      if (api.mode === 'rejected') {
        const error = Object.assign(new Error('Could not validate credentials'), {
          response: { status: 401 },
        });
        throw error;
      }
      return { data: api.current };
    },
    post: async (url?: string) => {
      if (typeof url === 'string' && url.endsWith('/auth/refresh')) {
        return {
          data: {
            access_token: 'boot-token',
            ...(api.refreshUser ? { user: api.refreshUser } : {}),
          },
        };
      }
      if (typeof url === 'string' && url.endsWith('/auth/logout')) {
        api.logouts += 1;
        return { data: {} };
      }
      return { data: {} };
    },
  },
}));

// The real budget is fifteen seconds; the test cannot wait that long, and the
// point of the budget is its existence rather than its exact size.
vi.mock('@/lib/apiTimeouts', () => ({ SESSION_REQUEST_TIMEOUT_MS: 50 }));

const Probe = () => {
  const { isLoading, user } = useAuth();
  if (isLoading) return <p>LOADING</p>;
  return <p>{user ? `SIGNED IN:${user.regno}` : 'NO ACCOUNT'}</p>;
};

const renderApp = () =>
  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  );

beforeEach(() => {
  api.mode = 'ok';
  api.reads = 0;
  api.logouts = 0;
  api.refreshUser = null;
  clearAccessToken();
  vi.clearAllMocks();
});

describe('the boot handshake', () => {
  it('initializes from the refresh payload without a second account read', async () => {
    api.refreshUser = { ...api.current };

    renderApp();

    await waitFor(() => expect(screen.getByText('SIGNED IN:SOME-REG')).toBeInTheDocument(), {
      timeout: 2000,
    });
    expect(api.reads).toBe(0);
    expect(getAccessToken()).toBe('boot-token');
  });

  it('falls back to /auth/me when an older refresh response omits the account', async () => {
    renderApp();

    await waitFor(() => expect(screen.getByText('SIGNED IN:SOME-REG')).toBeInTheDocument(), {
      timeout: 2000,
    });
    expect(api.reads).toBe(1);
  });

  it('leaves the loading state when the account read never answers', async () => {
    // The backend is up enough to refresh, and then stops answering. The app has
    // to come out of its splash screen anyway: the read has a budget, so the
    // attempt ends rather than waiting for ever.
    api.mode = 'hang';
    renderApp();

    await waitFor(() => expect(screen.getByText('NO ACCOUNT')).toBeInTheDocument(), {
      timeout: 2000,
    });
    expect(screen.queryByText('LOADING')).toBeNull();
  });

  it('keeps the session when the account read fails with a server error', async () => {
    // A 500 while the backend is deploying says nothing about the token: the
    // server never got to judge it. Ending the session here would revoke it
    // server-side too, and sign the person out over a bad moment.
    api.mode = 'server-error';
    renderApp();

    await waitFor(() => expect(screen.getByText('NO ACCOUNT')).toBeInTheDocument(), {
      timeout: 2000,
    });
    expect(api.logouts).toBe(0);
    expect(getAccessToken()).toBe('boot-token');
  });

  it('keeps the session when the account read times out', async () => {
    api.mode = 'hang';
    renderApp();

    await waitFor(() => expect(screen.getByText('NO ACCOUNT')).toBeInTheDocument(), {
      timeout: 2000,
    });
    expect(api.logouts).toBe(0);
    expect(getAccessToken()).toBe('boot-token');
  });

  it('ends the session when the token itself is refused', async () => {
    // The other half of the distinction: a 401 is the server's verdict on the
    // token, so the session is over and has to end -- with the server told.
    api.mode = 'rejected';
    renderApp();

    await waitFor(() => expect(api.logouts).toBe(1), { timeout: 2000 });
    expect(getAccessToken()).toBeNull();
  });

  it('tries the read again when the backend comes back', async () => {
    // The session was never gone, so the app keeps trying: the person is signed
    // in again without a reload.
    api.mode = 'server-error';
    renderApp();
    await waitFor(() => expect(screen.getByText('NO ACCOUNT')).toBeInTheDocument(), {
      timeout: 2000,
    });

    api.mode = 'ok';
    await waitFor(() => expect(screen.getByText('SIGNED IN:SOME-REG')).toBeInTheDocument(), {
      timeout: 8000,
    });
    expect(api.reads).toBeGreaterThan(1);
    expect(api.logouts).toBe(0);
  });
});
