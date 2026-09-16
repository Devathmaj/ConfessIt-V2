import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { ModeratorDashboard } from '@/pages/ModeratorDashboard';
import { AdminNavigation } from '@/components/AdminNavigation';
import { CAPABILITIES, type Capability, can } from '@/lib/capabilities';

/**
 * The moderator landing page.
 *
 * Two properties are worth pinning, and they pull against each other. The page must show the
 * moderator's backlog as numbers -- that is the whole point of a landing page -- and every one
 * of those numbers must be a queue the account can act on rather than a platform census it
 * cannot. So the figures come from the four endpoints the queue pages themselves call, and
 * `getAdminStats` is required never to be called: a counter that happens to render as a dash is
 * still a counter the page went looking for.
 *
 * A wrong total is the failure mode a queue count must not have. A moderator who reads "2 open
 * reports" when there are five goes home, so the headline refuses to add up two queues out of
 * three and says the count could not be read instead.
 */

const api = vi.hoisted(() => ({
  getAdminStats: vi.fn(),
  getAdminActiveSessions: vi.fn(),
  getAdminConfessionReports: vi.fn(),
  getAdminLoveNoteReports: vi.fn(),
  getAdminMessageReports: vi.fn(),
  getAdminLoveNotes: vi.fn(),
  getAdminConfessions: vi.fn(),
}));

vi.mock('@/services/api', () => api);
vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn() },
  Toaster: () => null,
}));

/** Exactly what the server sends a moderator: the content queues, and no platform census. */
const MODERATION_CAPABILITIES = [
  CAPABILITIES.CONFESSIONS_REVIEW,
  CAPABILITIES.CONFESSIONS_DELETE,
  CAPABILITIES.LOVE_NOTES_REVIEW,
  CAPABILITIES.LOVE_NOTES_MODERATE,
  CAPABILITIES.LOVE_NOTES_DELETE,
  CAPABILITIES.REPORTS_REVIEW,
  CAPABILITIES.REPORTS_MODERATE,
];

const ADMIN_CAPABILITIES = [
  ...MODERATION_CAPABILITIES,
  CAPABILITIES.STATS_READ,
  CAPABILITIES.USERS_REVIEW,
];

const QUEUES_EMPTY = {
  confessionReports: 0,
  loveNoteReports: 0,
  messageReports: 0,
  pendingLoveNotes: 0,
};

/** Standing in for the `{ reports, total, skip, limit }` envelope the three queues answer with. */
const envelope = (total: number) => ({ reports: [], total, skip: 0, limit: 1 });

const givenQueues = ({
  confessionReports,
  loveNoteReports,
  messageReports,
  pendingLoveNotes,
}: typeof QUEUES_EMPTY) => {
  api.getAdminConfessionReports.mockResolvedValue(envelope(confessionReports));
  api.getAdminLoveNoteReports.mockResolvedValue(envelope(loveNoteReports));
  api.getAdminMessageReports.mockResolvedValue(envelope(messageReports));
  api.getAdminLoveNotes.mockResolvedValue(
    [
      ...Array.from({ length: pendingLoveNotes }, (_, index) => ({ id: `n${index}`, status: 'pending_review' })),
      { id: 'settled', status: 'approved' },
    ],
  );
};

const renderWith = (ui: React.ReactElement, capabilities: string[]) =>
  render(
    <AuthContext.Provider
      value={
        {
          user: {
            id: 'mod-1',
            regno: 'MOD-1',
            name: 'A Moderator',
            email: 'mod@example.com',
            emoji: '🛡️',
            account_type: 'moderator',
            capabilities,
            is_blocked: false,
          },
          isAuthenticated: true,
          loading: false,
          can: (capability: Capability) => can(capabilities, capability),
        } as never
      }
    >
      <MemoryRouter>{ui}</MemoryRouter>
    </AuthContext.Provider>,
  );

const renderDashboard = (capabilities: string[]) => renderWith(<ModeratorDashboard />, capabilities);

describe('ModeratorDashboard: the backlog it counts', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    givenQueues(QUEUES_EMPTY);
  });

  it('counts the three report queues and adds them into one figure', async () => {
    givenQueues({ confessionReports: 3, loveNoteReports: 1, messageReports: 2, pendingLoveNotes: 4 });

    renderDashboard(MODERATION_CAPABILITIES);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /open reports/i })).toHaveTextContent('6');
    });
    // The headline is a total of the three, so each of them is shown beside it rather than
    // hidden inside the sum: a moderator who is sent to one queue needs to know which one.
    expect(screen.getByRole('button', { name: /post & comment reports/i })).toHaveTextContent('3');
    expect(screen.getByRole('button', { name: /love note reports/i })).toHaveTextContent('1');
    expect(screen.getByRole('button', { name: /chat message reports/i })).toHaveTextContent('2');
  });

  it('counts the love notes still waiting on a decision', async () => {
    givenQueues({ ...QUEUES_EMPTY, pendingLoveNotes: 4 });

    renderDashboard(MODERATION_CAPABILITIES);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /love notes awaiting review/i })).toHaveTextContent('4');
    });
  });

  it('asks each queue for one row rather than the page a reviewer would read', async () => {
    renderDashboard(MODERATION_CAPABILITIES);

    await waitFor(() => {
      expect(api.getAdminConfessionReports).toHaveBeenCalled();
    });
    // `pending` is the filter the Reports Review screen opens on, and `1` is the whole point:
    // `total` counts every match regardless of how many came back, so a landing page does not
    // download fifty reported messages to add up a number the query already returned.
    expect(api.getAdminConfessionReports).toHaveBeenCalledWith('pending', 1);
    expect(api.getAdminLoveNoteReports).toHaveBeenCalledWith('pending', 1);
    expect(api.getAdminMessageReports).toHaveBeenCalledWith('pending', 1);
  });

  it('refuses to add up a total when one queue could not be read', async () => {
    givenQueues({ confessionReports: 3, loveNoteReports: 1, messageReports: 2, pendingLoveNotes: 0 });
    api.getAdminMessageReports.mockRejectedValue(new Error('network'));

    renderDashboard(MODERATION_CAPABILITIES);

    await waitFor(() => {
      expect(screen.getByText(/could not be read/i)).toBeInTheDocument();
    });
    // 3 + 1 would read as "4 open reports" when there are five. A dash is the honest answer.
    expect(screen.getByRole('button', { name: /open reports/i })).toHaveTextContent('—');
    // The queue that did answer still reports, so one refused request does not blank the page.
    expect(screen.getByRole('button', { name: /post & comment reports/i })).toHaveTextContent('3');
  });

  it('never asks for the platform counters', async () => {
    givenQueues({ confessionReports: 1, loveNoteReports: 0, messageReports: 0, pendingLoveNotes: 0 });

    renderDashboard(MODERATION_CAPABILITIES);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /open reports/i })).toHaveTextContent('1');
    });
    expect(api.getAdminStats).not.toHaveBeenCalled();
    expect(api.getAdminActiveSessions).not.toHaveBeenCalled();
    // Nor the whole confession feed: a confession is public the moment it is written, so there
    // is no pending figure for it and pulling the entire review screen to invent one would be
    // the most expensive request on the page.
    expect(api.getAdminConfessions).not.toHaveBeenCalled();
  });

  it('names no platform census on the page', async () => {
    givenQueues({ confessionReports: 1, loveNoteReports: 0, messageReports: 0, pendingLoveNotes: 0 });

    renderDashboard(MODERATION_CAPABILITIES);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /open reports/i })).toHaveTextContent('1');
    });
    ['Total Users', 'Active Sessions', 'Total Confessions', 'Blocked Users'].forEach((label) => {
      expect(screen.queryByText(label)).not.toBeInTheDocument();
    });
  });

  it('does not offer the account-management queue a moderator cannot reach', async () => {
    renderDashboard(MODERATION_CAPABILITIES);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /open reports/i })).toBeInTheDocument();
    });
    expect(screen.queryByRole('button', { name: /profile review/i })).not.toBeInTheDocument();
  });

  it('shows an admin the same counts, plus the account queue', async () => {
    // Rendered directly rather than through its route, because the page itself checks no
    // capability: `/moderation`'s guard is what refuses an admin, and a page that also asked
    // would be restating it. So the component is capability-agnostic, and this case exists to
    // say so -- if it ever stopped being agnostic, this would still pass and the guard would
    // have become load-bearing for the wrong reason.
    givenQueues({ confessionReports: 2, loveNoteReports: 0, messageReports: 0, pendingLoveNotes: 1 });

    renderDashboard(ADMIN_CAPABILITIES);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /open reports/i })).toHaveTextContent('2');
    });
    expect(screen.getByRole('button', { name: /profile review/i })).toBeInTheDocument();
  });

  it('reads no queue the account cannot open', async () => {
    // The love-note backlog is `love_notes:review`, not `reports:review`, so an account holding
    // only the report capability must not have it counted -- the endpoint would refuse, and the
    // card would sit there permanently dashed.
    renderDashboard([CAPABILITIES.REPORTS_REVIEW]);

    await waitFor(() => {
      expect(api.getAdminConfessionReports).toHaveBeenCalled();
    });
    expect(api.getAdminLoveNotes).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: /love notes awaiting review/i })).not.toBeInTheDocument();
  });
});

describe('AdminNavigation: what it calls the area', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    givenQueues(QUEUES_EMPTY);
  });

  it('says Moderation to a moderator, and Admin to an admin', () => {
    // Matched on the wordmark link's whole accessible name, "ConfessIt" plus the area beneath
    // it. Querying the bare word "Moderation" would be ambiguous -- the nav item below the
    // wordmark carries the same name, which is correct and not what is being tested here.
    const { unmount } = renderWith(<AdminNavigation />, MODERATION_CAPABILITIES);
    expect(screen.getByRole('link', { name: /confessit moderation/i })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /confessit admin/i })).not.toBeInTheDocument();
    unmount();

    renderWith(<AdminNavigation />, ADMIN_CAPABILITIES);
    expect(screen.getByRole('link', { name: /confessit admin/i })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /confessit moderation/i })).not.toBeInTheDocument();
  });

  it('offers the hub to a moderator and not to an admin', () => {
    // The bar and the route declare the same pair of conditions -- needs `reports:review`,
    // does not hold `stats:read` -- so the hub can never be linked where it would be refused,
    // nor refused where it was offered. An admin holds every moderation capability, so without
    // the exclusion this would point them at the hub as a second place to do the same work.
    const { unmount } = renderWith(<AdminNavigation />, MODERATION_CAPABILITIES);
    expect(screen.getByRole('link', { name: /^moderation$/i })).toBeInTheDocument();
    unmount();

    renderWith(<AdminNavigation />, ADMIN_CAPABILITIES);
    expect(screen.queryByRole('link', { name: /^moderation$/i })).not.toBeInTheDocument();
    // The queues the hub would have listed are still all offered, so nothing is lost with it.
    expect(screen.getByRole('link', { name: /reports review/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /confession review/i })).toBeInTheDocument();
  });

  it('says Moderation on a review screen too, not only on the hub', () => {
    // The bug this fixes: `/admin/reports` is a URL prefix, not a statement about the reader.
    // A moderator who follows a link into it is inside the moderation area, and a bar reading
    // "Admin" claims they are somewhere they are not allowed to be. Nothing on this bar knows
    // which page it was opened from, which is why the label cannot be passed in by the page
    // and has to be answered from the account's capabilities.
    renderWith(<AdminNavigation />, MODERATION_CAPABILITIES);

    expect(screen.getByRole('link', { name: /reports review/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /confessit moderation/i })).toBeInTheDocument();
  });

  it('sends the wordmark to the account it belongs to', () => {
    renderWith(<AdminNavigation />, MODERATION_CAPABILITIES);

    expect(screen.getByRole('link', { name: /confessit/i })).toHaveAttribute('href', '/moderation');
  });
});