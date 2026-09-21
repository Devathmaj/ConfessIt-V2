import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { AdminDashboard } from '@/pages/AdminDashboard';
import { CAPABILITIES, type Capability, can } from '@/lib/capabilities';

/**
 * The administration console's tiles, and the one card that is not a tile.
 *
 * Three things are pinned here, all of which the console used to get wrong:
 *
 * * **"Active Today" is a figure, not a door.** It used to be "Active Sessions", a card that
 *   opened a panel listing every account live in the last 24 hours with their last login,
 *   browser and IP. Nothing on this page reads that, so the route behind it is gone and the
 *   card is a plain number. A tile that looks clickable and opens nothing is worse than one
 *   that never looked clickable, and the test asserts it is not a button at all.
 *
 * * **The backlog is a count of outstanding work.** "Love Notes" counted every note ever sent,
 *   which is a total of the archive rather than of anybody's queue. It reads "Pending Reports"
 *   now, and the number behind it is the three report queues' pending totals.
 *
 * * **The breakdown card is real data.** It was a hardcoded list of five sentences that said
 *   nothing and could never change. Each row is now a queue's own pending count, read from the
 *   same `/admin/stats` payload as the tile above it, and each row is a button into the screen
 *   that queue is ruled on.
 */

const api = vi.hoisted(() => ({
  getAdminStats: vi.fn(),
}));

vi.mock('@/services/api', () => api);
vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn() },
  Toaster: () => null,
}));

/** An admin holds everything, so the console renders its full set of tiles. */
const ADMIN_CAPABILITIES = [
  CAPABILITIES.STATS_READ,
  CAPABILITIES.USERS_REVIEW,
  CAPABILITIES.CONFESSIONS_REVIEW,
  CAPABILITIES.REPORTS_REVIEW,
  CAPABILITIES.LOVE_NOTES_REVIEW,
];

const EMPTY_STATS = {
  total_users: 0,
  active_sessions: 0,
  total_confessions: 0,
  total_love_notes: 0,
  pending_love_notes: 0,
  pending_reports: 0,
  pending_report_queues: { confessions: 0, love_notes: 0, messages: 0 },
  blocked_users: 0,
};

const givenStats = (overrides: Partial<typeof EMPTY_STATS> = {}) =>
  api.getAdminStats.mockResolvedValue({
    ...EMPTY_STATS,
    ...overrides,
    pending_report_queues: {
      ...EMPTY_STATS.pending_report_queues,
      ...(overrides.pending_report_queues ?? {}),
    },
  });

const renderDashboard = () =>
  render(
    <AuthContext.Provider
      value={
        {
          user: {
            id: 'admin-1',
            regno: 'ADM-1',
            name: 'An Admin',
            email: 'admin@example.com',
            account_type: 'admin',
            capabilities: ADMIN_CAPABILITIES,
            is_blocked: false,
          },
          isAuthenticated: true,
          isLoading: false,
          token: 'token',
          logout: vi.fn(),
          verifyMagicToken: vi.fn(),
          fetchAndSetUser: vi.fn(),
          can: (capability: Capability) => can(ADMIN_CAPABILITIES, capability),
        } as never
      }
    >
      <MemoryRouter>
        <AdminDashboard />
      </MemoryRouter>
    </AuthContext.Provider>,
  );

describe('AdminDashboard: the figures it reports', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    givenStats();
  });

  it('names the signed-in count "Active Today" and reports it', async () => {
    givenStats({ active_sessions: 7 });

    renderDashboard();

    await waitFor(() => {
      expect(screen.getByText('Active Today')).toBeInTheDocument();
    });
    expect(screen.getByText('7')).toBeInTheDocument();
    expect(screen.queryByText('Active Sessions')).not.toBeInTheDocument();
  });

  it('offers no panel behind "Active Today", because there is nothing to list', async () => {
    // Not merely un-clickable: the tile is not a button, and nothing on the page reads a
    // session. The route that used to answer with every account's last login, browser and IP
    // is gone, so a click here could only ever have opened a list nobody asked for.
    givenStats({ active_sessions: 7 });

    renderDashboard();

    await waitFor(() => {
      expect(screen.getByText('Active Today')).toBeInTheDocument();
    });
    const tile = screen.getByText('Active Today').closest('div.p-6');
    expect(tile).not.toBeNull();
    expect(tile?.querySelector('button')).toBeNull();
    expect(tile?.getAttribute('role')).toBeNull();
    // One request, and it is the stats read. Nothing here asks the server about sessions.
    expect(api.getAdminStats).toHaveBeenCalledTimes(1);
  });

  it('reports the pending-report backlog instead of a lifetime love-note total', async () => {
    givenStats({ total_love_notes: 240, pending_reports: 6, pending_love_notes: 2 });

    renderDashboard();

    await waitFor(() => {
      expect(screen.getByText('Pending Reports')).toBeInTheDocument();
    });
    // The archive total is not on the page at all: nothing acts on 240, and every one of those
    // notes is either approved, rejected or waiting, which the two tiles below already say.
    expect(screen.queryByText('240')).not.toBeInTheDocument();
    expect(screen.getByText('6')).toBeInTheDocument();
    // Love notes awaiting approval keep their own tile, because that queue is not a report.
    expect(screen.getByText('Pending Love Notes')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();
  });

  it('breaks the backlog down by queue, from the same read as the tile', async () => {
    givenStats({
      pending_reports: 6,
      pending_report_queues: { confessions: 4, love_notes: 1, messages: 1 },
    });

    renderDashboard();

    await waitFor(() => {
      expect(screen.getByText('Report Backlog')).toBeInTheDocument();
    });
    // Each row named by its own title and read from there, rather than by a loose name match:
    // "Love notes" alone would also match the "Moderate Love Notes" quick action, and two
    // different things on one page called the same thing is what this card exists to prevent.
    expect(screen.getByText('Posts & comments').closest('button')).toHaveTextContent('4');
    expect(screen.getByText('Love notes').closest('button')).toHaveTextContent('1');
    expect(screen.getByText('Chat messages').closest('button')).toHaveTextContent('1');
    // Every row is a way into the screen the ruling is made on, not just a figure.
    expect(screen.getByText('Chat messages').closest('button')?.tagName).toBe('BUTTON');
    // One payload, one set of numbers: the breakdown cannot disagree with the headline.
    expect(api.getAdminStats).toHaveBeenCalledTimes(1);
  });

  it('no longer shows the hardcoded admin-focus list', async () => {
    renderDashboard();

    await waitFor(() => {
      expect(screen.getByText('Report Backlog')).toBeInTheDocument();
    });
    // Five fixed sentences that described no measured state. Their absence is the assertion:
    // one of them back would read as an assertion that nothing was reported.
    expect(screen.queryByText(/daily review window started/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/awaiting new confession approvals/i)).not.toBeInTheDocument();
    expect(screen.queryByText('Recent Admin Focus')).not.toBeInTheDocument();
  });

  it('keeps the quick actions an admin holds the capability for', async () => {
    renderDashboard();

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /reports review/i })).toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: /profile review/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /moderate love notes/i })).toBeInTheDocument();
  });
});