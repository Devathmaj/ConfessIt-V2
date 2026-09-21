import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { ProtectedRoute, CapabilityRoute } from '@/App';
import { AuthProvider, useAuth } from '@/contexts/AuthContext';
import { CAPABILITIES } from '@/lib/capabilities';
import { AUTH_EXPIRED_EVENT } from '@/lib/authEvents';

/**
 * Where a session sends you when it ends.
 *
 * The hard part is not the redirect, it is that the redirect happens *after* the account is
 * gone. A guard asked "which door?" once the user is null has nothing to inspect, so the door
 * has to have been remembered before the logout. These tests drive the real provider and the
 * real guards so that timing is exercised rather than assumed -- a stubbed context would pass
 * no matter what the provider did, because the stub supplies the answer directly.
 */

/** Stands in for the profile a staff or participant screen shows, with a logout button. */
const SessionScreen = ({ label }: { label: string }) => {
  const { logout } = useAuth();
  return (
    <div>
      <p>{label}</p>
      <button onClick={logout}>LOG OUT</button>
    </div>
  );
};

/** Reports the door the context is currently pointing at. */
const DoorProbe = () => {
  const { signInPath } = useAuth();
  return <p>DOOR:{signInPath}</p>;
};

const DOORS = ['/login', '/login/admin', '/login/moderator'];

const renderSession = (startPath: string) =>
  render(
    <AuthProvider>
      <MemoryRouter initialEntries={[startPath]}>
        <Routes>
          <Route
            path="/admin"
            element={
              <CapabilityRoute capability={CAPABILITIES.STATS_READ}>
                <SessionScreen label="MODERATION AREA" />
              </CapabilityRoute>
            }
          />
          <Route
            path="/dashboard"
            element={
              <ProtectedRoute>
                <SessionScreen label="PARTICIPANT AREA" />
              </ProtectedRoute>
            }
          />
          {DOORS.map((door) => (
            <Route key={door} path={door} element={<DoorProbe />} />
          ))}
        </Routes>
      </MemoryRouter>
    </AuthProvider>
  );

/**
 * `getCurrentUser` goes through axios rather than fetch, and `vi.mock` is hoisted above the
 * imports, so the factory cannot close over a test argument. `vi.hoisted` gives both a value
 * that exists before the mock factory runs and a handle the test body can still write to.
 */
const authMe = vi.hoisted(() => ({
  current: null as null | Record<string, unknown>,
  // `src/services/api.ts` registers a response interceptor at import time, so the stub has to
  // be shaped like the object that registration expects or the module throws on import.
  interceptors: { response: { use: () => {} } },
}));

vi.mock('axios', () => ({
  default: {
    interceptors: authMe.interceptors,
    get: async () => ({ data: authMe.current }),
  },
}));

/** Signs an account in by seeding the token and answering `/auth/me`. */
const signIn = (accountType: 'users' | 'admin' | 'moderator') => {
  localStorage.setItem('accessToken', `token-for-${accountType}`);
  authMe.current = {
    id: 'a-uuid',
    name: 'Someone',
    regno: 'SOME-REG',
    account_type: accountType,
    capabilities:
      accountType === 'users'
        ? []
        : [CAPABILITIES.STATS_READ, CAPABILITIES.CONFESSIONS_REVIEW],
  };
};

/** Dispatches the expiry event the API layer fires, inside `act` because it updates state. */
const expireSession = async () => {
  const { act } = await import('@testing-library/react');
  localStorage.removeItem('accessToken');
  await act(async () => {
    window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
  });
};

const clickLogout = async () => {
  const { fireEvent } = await import('@testing-library/react');
  fireEvent.click(screen.getByRole('button', { name: 'LOG OUT' }));
};

beforeEach(() => {
  localStorage.clear();
  vi.unstubAllGlobals();
});

describe('session end returns an account to its own door', () => {
  it('sends a participant who logs out to the participant door', async () => {
    signIn('users');
    renderSession('/dashboard');
    await screen.findByText('PARTICIPANT AREA');

    await clickLogout();

    await waitFor(() => {
      expect(screen.getByText('DOOR:/login')).toBeInTheDocument();
    });
  });

  it('sends an admin who logs out to the admin door', async () => {
    signIn('admin');
    renderSession('/admin');
    await screen.findByText('MODERATION AREA');

    await clickLogout();

    // Not /login: a valid admin regno typed on the participant door is refused by the users
    // table, so sending them there would look like the account had been deleted.
    await waitFor(() => {
      expect(screen.getByText('DOOR:/login/admin')).toBeInTheDocument();
    });
  });

  it('sends a moderator who logs out to the moderator door', async () => {
    signIn('moderator');
    renderSession('/admin');
    await screen.findByText('MODERATION AREA');

    await clickLogout();

    await waitFor(() => {
      expect(screen.getByText('DOOR:/login/moderator')).toBeInTheDocument();
    });
  });

  it('sends staff to their own door when the session expires mid-tab', async () => {
    // Expiry is not a click. The API layer notices the 401, clears the token and fires the
    // event, and the guard then has to redirect -- with the same no-user-left-to-ask problem
    // as a deliberate logout.
    signIn('moderator');
    renderSession('/admin');
    await screen.findByText('MODERATION AREA');

    await expireSession();

    await waitFor(() => {
      expect(screen.getByText('DOOR:/login/moderator')).toBeInTheDocument();
    });
  });

  it('sends a participant to the participant door when their session expires', async () => {
    signIn('users');
    renderSession('/dashboard');
    await screen.findByText('PARTICIPANT AREA');

    await expireSession();

    await waitFor(() => {
      expect(screen.getByText('DOOR:/login')).toBeInTheDocument();
    });
  });

  it('does not reuse the previous account door for the next person at the same browser', async () => {
    // A moderator logs out at a shared machine, then a participant signs in. The second
    // session must end at the participant door, not inherit the moderator one from the
    // browser history.
    signIn('moderator');
    const first = renderSession('/admin');
    await screen.findByText('MODERATION AREA');
    await clickLogout();
    await waitFor(() => expect(screen.getByText('DOOR:/login/moderator')).toBeInTheDocument());
    first.unmount();

    signIn('users');
    renderSession('/dashboard');
    await screen.findByText('PARTICIPANT AREA');
    await clickLogout();

    await waitFor(() => {
      expect(screen.getByText('DOOR:/login')).toBeInTheDocument();
    });
  });

  it('starts at the participant door for a browser that has never signed in', async () => {
    // No session has existed, so there is no door to remember. A staff member who is sent
    // here by a bookmark gets the participant form and can navigate to their own; the
    // endpoint behind it still reads only its own table, so nothing is granted either way.
    renderSession('/dashboard');

    await waitFor(() => {
      expect(screen.getByText('DOOR:/login')).toBeInTheDocument();
    });
  });
});
