import { act, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthProvider, useAuth } from '@/contexts/AuthContext';
import { SESSION_CHANNEL_NAME } from '@/lib/sessionChannel';
import { clearAccessToken, getAccessToken } from '@/lib/tokenStore';

/**
 * Signing out has to survive a refresh that is already in flight.
 *
 * The refresh is started by the interceptor the moment an access token expires,
 * which is exactly what happens half an hour into a session. If the person
 * signs out while one is in flight, two things used to go wrong:
 *
 * * the refresh answered *after* the sign out and put its token back into the
 *   store, so the person was signed in again without doing anything;
 * * the revocation carried the cookie value the refresh had already replaced,
 *   so the server found nothing to revoke and the session it had just extended
 *   stayed alive -- with the browser holding the newer cookie, and a reload
 *   quietly signing the person back in.
 *
 * These tests drive the real provider: the refresh is held open, the sign out
 * happens in the middle of it, and the order of what reaches the server is
 * checked.
 */

const api = vi.hoisted(() => ({
  /** The order in which the server was asked to do things. */
  calls: [] as string[],
  /** Releases the held refresh when the test wants it to land. */
  releaseRefresh: null as null | (() => void),
  /** Whether the refresh is held open (true) or answers at once. */
  holdRefresh: true,
}));

vi.mock('axios', () => ({
  default: {
    defaults: {},
    // The API layer registers a response interceptor at import time, so the
    // stub has to be shaped like the object that registration expects.
    interceptors: { response: { use: () => {} } },
    get: async () => ({ data: { id: 'a-uuid', name: 'Someone', regno: 'SOME-REG', capabilities: [] } }),
    post: async (url?: string) => {
      if (typeof url === 'string' && url.endsWith('/auth/refresh')) {
        api.calls.push('refresh');
        if (!api.holdRefresh) {
          return { data: { access_token: 'rotated-token' } };
        }
        // A refresh that is still in flight: the test releases it when it
        // wants the answer to land.
        return new Promise((resolve) => {
          api.releaseRefresh = () => resolve({ data: { access_token: 'rotated-token' } });
        });
      }
      if (typeof url === 'string' && url.endsWith('/auth/logout')) {
        api.calls.push('logout');
        return { data: { message: 'Signed out' } };
      }
      return { data: {} };
    },
  },
}));

/**
 * The provider's log outs, captured for the test to await.
 *
 * A button rather than a captured function: the promise the log out returns
 * is what the test needs, and an event handler is the only place a component
 * may hand one out.
 */
const logouts: Promise<void>[] = [];

const Probe = () => {
  const { logout } = useAuth();
  return (
    <button onClick={() => { logouts.push(logout()); }}>
      SIGN OUT
    </button>
  );
};

beforeEach(() => {
  api.calls = [];
  api.releaseRefresh = null;
  api.holdRefresh = true;
  logouts.length = 0;
  clearAccessToken();
  vi.clearAllMocks();
});

const signOut = async (): Promise<void> => {
  const { fireEvent } = await import('@testing-library/react');
  fireEvent.click(screen.getByRole('button', { name: 'SIGN OUT' }));
  return logouts[logouts.length - 1];
};

describe('signing out while a refresh is in flight', () => {
  it('does not let the refresh put a token back', async () => {
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    // The boot's refresh is in flight.
    await waitFor(() => expect(api.calls).toContain('refresh'));

    await act(async () => {
      const pending = signOut();
      // The refresh answers while the sign out is still going.
      api.releaseRefresh?.();
      await pending;
    });

    // The answer arrived after the sign out, and it was discarded: nothing is
    // left in the store, so nobody is signed in.
    expect(getAccessToken()).toBeNull();
  });

  it('revokes only after the in-flight refresh has settled', async () => {
    // The refresh rotates the cookie, so a revocation sent before it settles
    // carries the value that has just been replaced -- and the server would
    // find nothing to revoke. The revocation has to wait for it.
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await waitFor(() => expect(api.calls).toContain('refresh'));

    let signedOut: Promise<void> | null = null;
    await act(async () => {
      signedOut = signOut();
      // Give the sign out a chance to send the revocation before the refresh
      // lands: it must not.
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(api.calls).not.toContain('logout');

    await act(async () => {
      api.releaseRefresh?.();
      await signedOut;
    });

    // The revocation went out, and it went out after the refresh.
    expect(api.calls).toEqual(['refresh', 'logout']);
  });

  it('waits for the revocation before it resolves', async () => {
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await waitFor(() => expect(api.calls).toContain('refresh'));

    let signedOut: Promise<void> | null = null;
    await act(async () => {
      signedOut = signOut();
      api.releaseRefresh?.();
    });

    await act(async () => {
      await signedOut;
    });

    // "Signed out" means the server has been told, not merely that the screen
    // says so.
    expect(api.calls).toContain('logout');
  });

  it('tells the other tabs of this browser that the session ended', async () => {
    // The refresh cookie is shared by every tab, so a log out in one of them
    // is a log out in all of them. The announcement has to reach them.
    api.holdRefresh = false;
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await waitFor(() => expect(getAccessToken()).toBe('rotated-token'));

    // A stand-in for the other tab: its own channel on the same name.
    const otherTab = new BroadcastChannel(SESSION_CHANNEL_NAME);
    const heard = new Promise<MessageEvent>((resolve) => {
      otherTab.onmessage = (event) => resolve(event);
    });

    await act(async () => {
      await signOut();
    });

    const message = await Promise.race([
      heard,
      new Promise<MessageEvent>((resolve) => setTimeout(() => resolve(null as never), 1000)),
    ]);
    otherTab.close();

    expect(message).not.toBeNull();
    expect((message as MessageEvent).data).toMatchObject({ type: 'session-ended' });
  });

  it('signs this tab out when another tab ends the session', async () => {
    // The other way round: another tab ended the session, so this tab has lost
    // it too -- the cookie is shared. The token goes, and nothing is revoked
    // twice: the tab that signed out already did that.
    api.holdRefresh = false;
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await waitFor(() => expect(getAccessToken()).toBe('rotated-token'));

    const otherTab = new BroadcastChannel(SESSION_CHANNEL_NAME);
    await act(async () => {
      otherTab.postMessage({ type: 'session-ended' });
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    otherTab.close();

    await waitFor(() => expect(getAccessToken()).toBeNull());
    expect(api.calls).not.toContain('logout');
  });
});
