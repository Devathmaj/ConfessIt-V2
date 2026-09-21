import { describe, it, expect, vi } from 'vitest';
import type { ContextType } from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { PublicOnlyRoute } from '@/App';
import { AuthContext, type User } from '@/contexts/AuthContext';
import {
  CAPABILITIES,
  type Capability,
  can,
} from '@/lib/capabilities';

type AuthContextValue = ContextType<typeof AuthContext>;

/** A signed-in account. User requires these four fields; account_type and capabilities are optional. */
const asUser = (overrides: Partial<User> = {}): User => ({
  id: '1',
  name: 'Test User',
  regno: 'TEST001',
  email: 'test@example.com',
  ...overrides,
});

/**
 * What the server sends a moderator: the content queues, and nothing else. `stats:read` is
 * deliberately absent -- it belongs to the admin table alone, which is why a moderator never
 * sees the administration console.
 */
const MODERATION_CAPABILITIES = [
  CAPABILITIES.CONFESSIONS_REVIEW,
  CAPABILITIES.REPORTS_REVIEW,
  CAPABILITIES.LOVE_NOTES_REVIEW,
];

/** What the server sends an admin: the queues plus the platform census and account lifecycle. */
const ADMIN_CAPABILITIES = [
  ...MODERATION_CAPABILITIES,
  CAPABILITIES.STATS_READ,
  CAPABILITIES.APP_CONFIG_READ,
];

const authValue = (
  user: User | null,
  isAuthenticated: boolean,
  capabilities: string[] = [],
  signInPath = '/login'
): AuthContextValue => ({
  user,
  isAuthenticated,
  isLoading: false,
  token: 'token',
  logout: vi.fn(),
  verifyMagicToken: vi.fn(),
  fetchAndSetUser: vi.fn(),
  can: (capability: Capability) => can(capabilities, capability),
  signInPath,
});

const renderAtLogin = (value: AuthContextValue) =>
  render(
    <AuthContext.Provider value={value}>
      <MemoryRouter initialEntries={['/login']}>
        <Routes>
          <Route
            path="/login"
            element={
              <PublicOnlyRoute>
                <div>LOGIN SCREEN</div>
              </PublicOnlyRoute>
            }
          />
          <Route path="/dashboard" element={<div>USER DASHBOARD</div>} />
          <Route path="/admin" element={<div>ADMIN DASHBOARD</div>} />
          <Route path="/moderation" element={<div>MODERATION DASHBOARD</div>} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>
  );

describe('PublicOnlyRoute', () => {
  it('redirects a signed-in user away from the login screen to their dashboard', async () => {
    renderAtLogin(authValue(asUser(), true));

    await waitFor(() => {
      expect(screen.getByText('USER DASHBOARD')).toBeInTheDocument();
    });
    expect(screen.queryByText('LOGIN SCREEN')).not.toBeInTheDocument();
  });

  it('sends an admin to the admin dashboard rather than the user one', async () => {
    const user = asUser({ name: 'Admin', account_type: 'admin' });
    renderAtLogin(authValue(user, true, ADMIN_CAPABILITIES));

    await waitFor(() => {
      expect(screen.getByText('ADMIN DASHBOARD')).toBeInTheDocument();
    });
  });

  it('sends a moderator to the moderation dashboard, not the admin one', async () => {
    // Staff are separated from each other, not only from participants. The admin dashboard is
    // where the platform counters live, which is `stats:read`, and a moderator does not hold
    // it -- so signing in must not put those figures in front of one.
    const user = asUser({ name: 'Mod', account_type: 'moderator' });
    renderAtLogin(authValue(user, true, MODERATION_CAPABILITIES));

    await waitFor(() => {
      expect(screen.getByText('MODERATION DASHBOARD')).toBeInTheDocument();
    });
    expect(screen.queryByText('ADMIN DASHBOARD')).not.toBeInTheDocument();
  });

  it('does not treat the account table as a grant', async () => {
    // An admin account with no capabilities is not staff as far as this guard is concerned.
    // Capabilities are what the server decided; the table an account is stored in is a fact
    // about where the row lives, and on its own it grants nothing.
    const user = asUser({ name: 'Impostor', account_type: 'admin' });
    renderAtLogin(authValue(user, true, []));

    await waitFor(() => {
      expect(screen.getByText('USER DASHBOARD')).toBeInTheDocument();
    });
  });

  it('does not treat a moderator account as a reason to see the admin dashboard', async () => {
    // The mirror of the test above, and the reason this can be checked either way round: the
    // destination follows the capability list, so a row in the `moderator` table that somehow
    // carried `stats:read` would land on the console, and a row in the `admin` table without it
    // would not. The table alone decides nothing.
    const user = asUser({ name: 'Mod', account_type: 'moderator' });
    renderAtLogin(authValue(user, true, [CAPABILITIES.STATS_READ]));

    await waitFor(() => {
      expect(screen.getByText('ADMIN DASHBOARD')).toBeInTheDocument();
    });
  });

  it('still shows the login screen to a signed-out visitor', async () => {
    renderAtLogin(authValue(null, false));

    await waitFor(() => {
      expect(screen.getByText('LOGIN SCREEN')).toBeInTheDocument();
    });
    expect(screen.queryByText('USER DASHBOARD')).not.toBeInTheDocument();
  });
});
