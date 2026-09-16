import { describe, it, expect, vi } from 'vitest';
import type { ContextType } from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';

import { CapabilityRoute } from '@/App';
import { AuthContext, type User } from '@/contexts/AuthContext';
import { CAPABILITIES, type Capability, can } from '@/lib/capabilities';

/**
 * `CapabilityRoute`'s exclusion, which exists for one route: `/moderation`.
 *
 * The hub is gated on `reports:review`, which an admin holds as well, so the capability alone
 * would leave the hub one typed URL away from an account whose own landing page is the console.
 * That is a second staff landing page for the same person, offering a subset of what the
 * console already shows. `without` closes it.
 *
 * The redirect that does the closing is the account's landing page rather than a hardcoded
 * `/admin`, so the rule is not restated: the same `landingPathFor` that signed the account in
 * decides where a refused account ends up, whatever combination of capabilities caused it.
 */

type AuthContextValue = ContextType<typeof AuthContext>;

const asUser = (overrides: Partial<User> = {}): User => ({
  id: '1',
  name: 'Test User',
  regno: 'TEST001',
  emoji: '🙂',
  ...overrides,
});

const MODERATION_CAPABILITIES = [
  CAPABILITIES.CONFESSIONS_REVIEW,
  CAPABILITIES.REPORTS_REVIEW,
  CAPABILITIES.LOVE_NOTES_REVIEW,
];

const ADMIN_CAPABILITIES = [...MODERATION_CAPABILITIES, CAPABILITIES.STATS_READ];

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

/** The hub's route as `App.tsx` declares it, with the three staff destinations as targets. */
const renderAtHub = (value: AuthContextValue) =>
  render(
    <AuthContext.Provider value={value}>
      <MemoryRouter initialEntries={['/moderation']}>
        <Routes>
          <Route
            path="/moderation"
            element={
              <CapabilityRoute
                capability={CAPABILITIES.REPORTS_REVIEW}
                without={CAPABILITIES.STATS_READ}
              >
                <div>MODERATION HUB</div>
              </CapabilityRoute>
            }
          />
          <Route path="/admin" element={<div>ADMIN CONSOLE</div>} />
          <Route path="/dashboard" element={<div>USER DASHBOARD</div>} />
          <Route path="/login/moderator" element={<div>MODERATOR SIGN-IN</div>} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );

describe("CapabilityRoute: a route that is one account kind's own landing page", () => {
  it('opens for the moderator it was made for', async () => {
    renderAtHub(authValue(asUser({ account_type: 'moderator' }), true, MODERATION_CAPABILITIES));

    await waitFor(() => {
      expect(screen.getByText('MODERATION HUB')).toBeInTheDocument();
    });
  });

  it('turns an admin away, because the console is already their landing page', async () => {
    renderAtHub(authValue(asUser({ account_type: 'admin' }), true, ADMIN_CAPABILITIES));

    await waitFor(() => {
      expect(screen.getByText('ADMIN CONSOLE')).toBeInTheDocument();
    });
    expect(screen.queryByText('MODERATION HUB')).not.toBeInTheDocument();
  });

  it('sends an account with no capabilities to its own landing page, not to the console', async () => {
    // The redirect is `landingPathFor(can)`, not a literal path. An account holding none of the
    // staff capabilities has no business on either page, and following it to `/admin` would put
    // a participant on a route that then refuses them.
    renderAtHub(authValue(asUser(), true, []));

    await waitFor(() => {
      expect(screen.getByText('USER DASHBOARD')).toBeInTheDocument();
    });
  });

  it('sends an anonymous visitor to the door this browser signed in through', async () => {
    renderAtHub(authValue(null, false, [], '/login/moderator'));

    await waitFor(() => {
      expect(screen.getByText('MODERATOR SIGN-IN')).toBeInTheDocument();
    });
  });
});