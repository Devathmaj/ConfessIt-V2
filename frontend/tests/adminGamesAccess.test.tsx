import { describe, it, expect, vi } from 'vitest';
import type { ContextType } from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Navigate, Route, Routes } from 'react-router-dom';

import { CapabilityRoute } from '@/App';
import { AdminGamesLayout } from '@/components/AdminGamesLayout';
import { AdminNavigation } from '@/components/AdminNavigation';
import { AuthContext, type User } from '@/contexts/AuthContext';
import { CAPABILITIES, type Capability, can } from '@/lib/capabilities';

/**
 * Who may reach the Games console.
 *
 * The section is gated the way `App.tsx` declares it: one `CapabilityRoute` on the layout,
 * three children underneath, and a stray child that retires to the overview. The interesting
 * cases are the refusals -- a moderator holds every moderation capability and still must not
 * land here, because `stats:read` is what the console is, and the nav must not offer a link
 * the route would bounce.
 *
 * The second half pins the other half of that contract: hiding the link is presentation, the
 * route is the rule. A participant who types the address meets the route, not the menu.
 */

vi.mock('sonner', () => ({
  toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn(), info: vi.fn() }),
  Toaster: () => null,
}));

type AuthContextValue = ContextType<typeof AuthContext>;

const asUser = (overrides: Partial<User> = {}): User => ({
  id: '1',
  name: 'Test User',
  regno: 'TEST001',
  email: 'test@example.com',
  ...overrides,
});

const STAFF_CAPABILITIES = [
  CAPABILITIES.CONFESSIONS_REVIEW,
  CAPABILITIES.REPORTS_REVIEW,
  CAPABILITIES.LOVE_NOTES_REVIEW,
];
const ADMIN_CAPABILITIES = [...STAFF_CAPABILITIES, CAPABILITIES.STATS_READ];

const authValue = (
  user: User | null,
  isAuthenticated: boolean,
  capabilities: string[] = [],
  signInPath = '/login',
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

/** The section's routes as `App.tsx` declares them, with stubs where a refusal lands. */
const renderSection = (path: string, value: AuthContextValue) =>
  render(
    <AuthContext.Provider value={value}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route
            path="/admin/games"
            element={
              <CapabilityRoute capability={CAPABILITIES.STATS_READ}>
                <AdminGamesLayout />
              </CapabilityRoute>
            }
          >
            <Route index element={<div>OVERVIEW SCREEN</div>} />
            <Route path="manage" element={<div>MANAGE SCREEN</div>} />
            <Route path="leaderboard" element={<div>LEADERBOARD SCREEN</div>} />
            <Route path="*" element={<Navigate to="/admin/games" replace />} />
          </Route>
          <Route path="/moderation" element={<div>MODERATION HUB</div>} />
          <Route path="/dashboard" element={<div>USER DASHBOARD</div>} />
          <Route path="/login" element={<div>SIGN IN</div>} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );

const admin = authValue(asUser({ account_type: 'admin' }), true, ADMIN_CAPABILITIES);
const moderator = authValue(asUser({ account_type: 'moderator' }), true, STAFF_CAPABILITIES);
const participant = authValue(asUser(), true, []);

describe('the /admin/games section: admin only', () => {
  it('opens the overview for an admin, with the section navigation', async () => {
    renderSection('/admin/games', admin);

    await waitFor(() => {
      expect(screen.getByText('OVERVIEW SCREEN')).toBeInTheDocument();
    });
    expect(screen.getByRole('link', { name: /manage games/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /leaderboard/i })).toBeInTheDocument();
  });

  it('lets the same admin into every screen under the one guard', async () => {
    renderSection('/admin/games/manage', admin);

    await waitFor(() => {
      expect(screen.getByText('MANAGE SCREEN')).toBeInTheDocument();
    });
  });

  it('refuses a moderator, sending them to the hub their capabilities do open', async () => {
    renderSection('/admin/games', moderator);

    await waitFor(() => {
      expect(screen.getByText('MODERATION HUB')).toBeInTheDocument();
    });
    expect(screen.queryByText('OVERVIEW SCREEN')).not.toBeInTheDocument();
  });

  it('refuses a participant to their own dashboard, not to the console', async () => {
    renderSection('/admin/games', participant);

    await waitFor(() => {
      expect(screen.getByText('USER DASHBOARD')).toBeInTheDocument();
    });
  });

  it('refuses an anonymous visitor to the door they signed in through', async () => {
    renderSection('/admin/games', authValue(null, false, [], '/login'));

    await waitFor(() => {
      expect(screen.getByText('SIGN IN')).toBeInTheDocument();
    });
  });

  it('retires a mistyped deep link to the overview instead of a blank shell', async () => {
    renderSection('/admin/games/does-not-exist', admin);

    await waitFor(() => {
      expect(screen.getByText('OVERVIEW SCREEN')).toBeInTheDocument();
    });
  });
});

describe('the Games item in the admin navigation', () => {
  const renderNav = (value: AuthContextValue) =>
    render(
      <AuthContext.Provider value={value}>
        <MemoryRouter>
          <AdminNavigation />
        </MemoryRouter>
      </AuthContext.Provider>,
    );

  it('offers the section to an admin', () => {
    renderNav(admin);

    expect(screen.getByRole('link', { name: 'Games' })).toBeInTheDocument();
  });

  it('keeps it from a moderator, who would be refused at the route', () => {
    renderNav(moderator);

    expect(screen.queryByRole('link', { name: 'Games' })).not.toBeInTheDocument();
    // Everything else the moderator can actually open is still offered. Neither landing page
    // is a pill for anyone now: the console sits behind `stats:read`, and the hub is the
    // wordmark's to open for this account (matched on its whole accessible name).
    expect(screen.getByRole('link', { name: 'Reports' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Dashboard' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /^moderation$/i })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: /confessit moderation/i })).toBeInTheDocument();
  });
});
