import { describe, it, expect, vi } from 'vitest';
import type { ContextType } from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { PublicOnlyRoute } from '@/App';
import { AuthContext, type User } from '@/contexts/AuthContext';
import { CAPABILITIES, type Capability, can } from '@/lib/capabilities';

type AuthContextValue = ContextType<typeof AuthContext>;

/** A signed-in user. User requires these four fields; role and capabilities are optional. */
const asUser = (overrides: Partial<User> = {}): User => ({
  id: '1',
  name: 'Test User',
  regno: 'TEST001',
  emoji: '🙂',
  ...overrides,
});

/**
 * The capability list the server would send for each role. Only `STATS_READ` is named
 * because it is the one this guard keys on, standing in for "holds any moderation
 * capability" -- the guard asks whether the account is staff at all.
 */
const MODERATION_CAPABILITIES = [CAPABILITIES.STATS_READ, CAPABILITIES.CONFESSIONS_REVIEW];

const authValue = (
  user: User | null,
  isAuthenticated: boolean,
  capabilities: string[] = []
): AuthContextValue => ({
  user,
  isAuthenticated,
  isLoading: false,
  token: 'token',
  logout: vi.fn(),
  verifyMagicToken: vi.fn(),
  fetchAndSetUser: vi.fn(),
  can: (capability: Capability) => can(capabilities, capability),
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
    const user = asUser({ name: 'Admin', role: 'admin' });
    renderAtLogin(authValue(user, true, MODERATION_CAPABILITIES));

    await waitFor(() => {
      expect(screen.getByText('ADMIN DASHBOARD')).toBeInTheDocument();
    });
  });

  it('sends a moderator to the admin dashboard too', async () => {
    // The destination is decided by the capability list, not by the role string, so a
    // moderator lands in the moderation area instead of a participant dashboard they have
    // no use for. A role check would send them to /dashboard.
    const user = asUser({ name: 'Mod', role: 'moderator' });
    renderAtLogin(authValue(user, true, MODERATION_CAPABILITIES));

    await waitFor(() => {
      expect(screen.getByText('ADMIN DASHBOARD')).toBeInTheDocument();
    });
  });

  it('does not trust the role string on its own', async () => {
    // A role claiming admin with no capabilities is not staff as far as this guard is
    // concerned. Capabilities are what the server decided; a role field that arrived
    // without them proves nothing.
    const user = asUser({ name: 'Impostor', role: 'admin' });
    renderAtLogin(authValue(user, true, []));

    await waitFor(() => {
      expect(screen.getByText('USER DASHBOARD')).toBeInTheDocument();
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
