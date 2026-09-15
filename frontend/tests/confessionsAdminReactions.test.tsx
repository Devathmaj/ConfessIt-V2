import { render, screen, waitFor, within, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { ConfessionsAdmin } from '@/pages/ConfessionsAdmin';

/**
 * The Engagement snapshot expands a reaction into the accounts behind it.
 *
 * Moderating a pile of engagement means asking whether one account is behind it, and raw ids
 * cannot answer that from the review screen. So the emoji row is a control, not a label: clicking
 * it opens the names.
 *
 * What is worth pinning is narrower than "the click works". The two withheld cases must read
 * differently -- a privacy decision and a deleted account both render with no name, and if they
 * also render the same text then only one of them looks like something a reviewer can follow up
 * on. The count beside the emoji stays the denormalised column rather than the length of the
 * list, because the two are stored independently and a reviewer comparing them is doing an audit.
 *
 * `fireEvent` rather than `user-event`, matching the other component tests here: jsdom does not
 * synthesize Enter activating a button.
 */

const api = vi.hoisted(() => ({
  getAdminConfessions: vi.fn(),
  hideAdminConfession: vi.fn(),
  deleteAdminConfession: vi.fn(),
}));

vi.mock('@/services/api', () => api);
vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn() },
  Toaster: () => null,
}));

const NOW = '2026-01-15T10:00:00Z';

type Reactor = { id: string; name: string | null; withheld_reason: string | null };

/** A confession as the server sends it, with reaction names supplied per test. */
const confession = (overrides: Record<string, unknown> = {}) => ({
  id: '22222222-2222-2222-2222-222222222222',
  body: 'a confession with some engagement on it',
  recipient_name: 'Someone In Particular',
  anonymous: true,
  sender_hidden: true,
  comments_enabled: true,
  created_at: NOW,
  sender: { id: null, name: null, regno: null, email: null },
  report_count: 0,
  reported_by: [],
  reports: [],
  reactions: {},
  reaction_users: {},
  heart_count: 0,
  haha_count: 0,
  whoa_count: 0,
  heartbreak_count: 0,
  comment_count: 0,
  comments: [],
  content_removed: false,
  removed_at: null,
  removed_by_regno: null,
  ...overrides,
});

const reactor = (id: string, name: string | null, withheld: string | null = null): Reactor => ({
  id,
  name,
  withheld_reason: withheld,
});

function renderPage() {
  return render(
    <AuthContext.Provider
      value={
        {
          user: {
            id: 'admin-1',
            regno: 'ADMIN-1',
            name: 'Admin',
            account_type: 'admin',
            capabilities: [],
            is_blocked: false,
          },
          isAuthenticated: true,
          loading: false,
          can: () => true,
        } as never
      }
    >
      <MemoryRouter>
        <ConfessionsAdmin />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

const reactionRow = (label: RegExp) => screen.getByRole('button', { name: label });

/** Open one collapsed confession row. The snapshot lives inside its body. */
async function expandRow() {
  const [trigger] = await screen.findAllByRole('button', { expanded: false });
  fireEvent.click(trigger);
  await screen.findAllByRole('button', { expanded: true });
}





beforeEach(() => {
  api.getAdminConfessions.mockReset();
});

describe('Confession Review reaction details', () => {
  it('reveals who reacted when the emoji row is clicked', async () => {
    api.getAdminConfessions.mockResolvedValue([
      confession({
        reactions: { heart: ['user-1', 'user-2'] },
        reaction_users: {
          heart: [reactor('user-1', 'Heart Reactor'), reactor('user-2', 'Second Reactor')],
        },
        heart_count: 2,
      }),
    ]);

    renderPage();
    await expandRow();
    await waitFor(() => expect(screen.getByText(/engagement snapshot/i)).toBeTruthy());

    // Closed by default: a snapshot that opened every list would be a wall of names.
    expect(screen.queryByText('Heart Reactor')).toBeNull();
    expect(screen.queryByText('Second Reactor')).toBeNull();

    fireEvent.click(reactionRow(/hearts/i));

    expect(screen.getByText('Heart Reactor')).toBeTruthy();
    expect(screen.getByText('Second Reactor')).toBeTruthy();
  });

  it('collapses the list again on a second click', async () => {
    api.getAdminConfessions.mockResolvedValue([
      confession({
        reaction_users: { heart: [reactor('user-1', 'Heart Reactor')] },
        heart_count: 1,
      }),
    ]);

    renderPage();
    await expandRow();
    await waitFor(() => expect(screen.getByText(/engagement snapshot/i)).toBeTruthy());

    fireEvent.click(reactionRow(/hearts/i));
    expect(screen.getByText('Heart Reactor')).toBeTruthy();

    fireEvent.click(reactionRow(/hearts/i));
    expect(screen.queryByText('Heart Reactor')).toBeNull();
  });

  it('marks the open row for assistive technology', async () => {
    api.getAdminConfessions.mockResolvedValue([
      confession({
        reaction_users: {
          heart: [reactor('user-1', 'Heart Reactor')],
          // Present but unnamed, so the Laughs row is a live control to move the open slot onto.
          haha: [reactor('user-2', 'Laugh Reactor')],
        },
        heart_count: 1,
        haha_count: 2,
      }),
    ]);

    renderPage();
    await expandRow();
    await waitFor(() => expect(screen.getByText(/engagement snapshot/i)).toBeTruthy());

    const hearts = reactionRow(/hearts/i);
    expect(hearts.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(hearts);
    expect(reactionRow(/hearts/i).getAttribute('aria-expanded')).toBe('true');

    // Opening one closes the other: the slot is single, so the reviewer is never comparing two
    // lists that each push the row below them down the screen.
    fireEvent.click(reactionRow(/laughs/i));
    expect(reactionRow(/hearts/i).getAttribute('aria-expanded')).toBe('false');
  });

  it('distinguishes a withheld author from a deleted account', async () => {
    api.getAdminConfessions.mockResolvedValue([
      confession({
        reaction_users: {
          heart: [
            reactor('user-1', null, 'anonymous_author'),
            reactor('user-9', null, 'unknown'),
            reactor('user-2', 'Named Reactor'),
          ],
        },
        heart_count: 3,
      }),
    ]);

    renderPage();
    await expandRow();
    await waitFor(() => expect(screen.getByText(/engagement snapshot/i)).toBeTruthy());
    fireEvent.click(reactionRow(/hearts/i));

    // Both withheld entries have no name. If they also shared one label, a privacy decision
    // would be indistinguishable from a data fault, and only one is worth chasing.
    expect(screen.getByText(/withheld.*anonymous confession/i)).toBeTruthy();
    expect(screen.getByText(/unknown.*no longer exists/i)).toBeTruthy();
    expect(screen.getByText('Named Reactor')).toBeTruthy();
  });

  it('keeps the entry for a withheld author so the list still adds up to the count', async () => {
    api.getAdminConfessions.mockResolvedValue([
      confession({
        reaction_users: {
          heart: [reactor('user-1', null, 'anonymous_author'), reactor('user-2', 'Named Reactor')],
        },
        heart_count: 2,
      }),
    ]);

    renderPage();
    await expandRow();
    await waitFor(() => expect(screen.getByText(/engagement snapshot/i)).toBeTruthy());
    fireEvent.click(reactionRow(/hearts/i));

    expect(screen.getAllByRole('listitem')).toHaveLength(2);
  });

  it('leaves a reaction nobody used in place, unclickable, rather than opening an empty list', async () => {
    api.getAdminConfessions.mockResolvedValue([
      confession({
        reaction_users: { heart: [], haha: [reactor('user-2', 'Haha Reactor')] },
        haha_count: 1,
      }),
    ]);

    renderPage();
    await expandRow();
    await waitFor(() => expect(screen.getByText(/engagement snapshot/i)).toBeTruthy());

    // The four rows stay a grid the reviewer scans across. An unused reaction is visibly empty,
    // not absent, and a row that opened to nothing would just be a dead control.
    expect(reactionRow(/hearts/i)).toBeDisabled();
    expect(reactionRow(/hearts/i).getAttribute('aria-expanded')).toBe('false');
    expect(reactionRow(/laughs/i)).not.toBeDisabled();
  });

  it('shows the stored counter rather than the length of the resolved list', async () => {
    api.getAdminConfessions.mockResolvedValue([
      confession({
        // One live account, counter says seven: the columns and the id array have drifted.
        reaction_users: { heart: [reactor('user-1', 'Heart Reactor')] },
        heart_count: 7,
      }),
    ]);

    renderPage();
    await expandRow();
    await waitFor(() => expect(screen.getByText(/engagement snapshot/i)).toBeTruthy());

    expect(within(reactionRow(/hearts/i)).getByText('7')).toBeTruthy();

    // Opening it must not silently replace the stored count with the length of the list. A
    // reviewer looking at "7" and then at one name is doing an audit, and quietly reconciling
    // the two would remove the discrepancy they are trying to find.
    fireEvent.click(reactionRow(/hearts/i));
    expect(screen.getByText('Heart Reactor')).toBeTruthy();
    expect(within(reactionRow(/hearts/i)).getByText('7')).toBeTruthy();
    expect(screen.getAllByRole('listitem')).toHaveLength(1);
  });

  it('renders a confession whose payload predates reaction names', async () => {
    const { reaction_users: _absent, ...withoutNames } = confession({ heart_count: 3 });

    api.getAdminConfessions.mockResolvedValue([withoutNames]);

    renderPage();
    await expandRow();
    await waitFor(() => expect(screen.getByText(/engagement snapshot/i)).toBeTruthy());

    // A cached response without the field must not take the screen down, and the row keeps its
    // count and stays visible instead of disappearing from the grid.
    expect(screen.getByText(/engagement snapshot/i)).toBeTruthy();
    expect(within(reactionRow(/hearts/i)).getByText('3')).toBeTruthy();
    expect(reactionRow(/hearts/i)).toBeDisabled();
  });

  it('does not carry an open reaction over to the next confession', async () => {
    api.getAdminConfessions.mockResolvedValue([
      confession({
        id: 'confession-a',
        reaction_users: { heart: [reactor('user-1', 'First Post Reactor')] },
        heart_count: 1,
      }),
      confession({
        id: 'confession-b',
        reaction_users: { heart: [reactor('user-2', 'Second Post Reactor')] },
        heart_count: 1,
      }),
    ]);

    renderPage();

    // The confession accordion is single, so the reviewer moves between posts by closing one and
    // opening the next. The open reaction is keyed per confession precisely so that moving on
    // cannot leave the previous post's names sitting under the new post's counts.
    await expandRow();
    fireEvent.click(reactionRow(/hearts/i));
    expect(screen.getByText('First Post Reactor')).toBeTruthy();

    // Close the first post and open the second. Both carry a Hearts reaction, so a slot keyed
    // only by the emoji would put the first post's name under the second post's count.
    const [openPost] = screen.getAllByRole('button', { expanded: true });
    fireEvent.click(openPost);
    await waitFor(() => expect(screen.queryByText(/engagement snapshot/i)).toBeNull());

    const [, nextPost] = screen.getAllByRole('button', { expanded: false });
    fireEvent.click(nextPost);
    await waitFor(() => expect(screen.getByText(/engagement snapshot/i)).toBeTruthy());

    expect(reactionRow(/hearts/i).getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText('First Post Reactor')).toBeNull();

    fireEvent.click(reactionRow(/hearts/i));
    expect(screen.getByText('Second Post Reactor')).toBeTruthy();
    expect(screen.queryByText('First Post Reactor')).toBeNull();
  });

  it('keeps the hidden marker on a confession whose author is also its reactor', async () => {
    api.getAdminConfessions.mockResolvedValue([
      confession({
        reaction_users: { heart: [reactor('user-1', null, 'anonymous_author')] },
        heart_count: 1,
      }),
    ]);

    renderPage();
    await expandRow();
    await waitFor(() => expect(screen.getByText(/engagement snapshot/i)).toBeTruthy());

    // The row is anonymous, so nothing on it may imply who wrote it. The withheld entry is the
    // only place the client's own decision could have undone that.
    expect(screen.getByRole('button', { name: /hide confession/i })).toBeTruthy();
    fireEvent.click(reactionRow(/hearts/i));
    expect(screen.getAllByRole('listitem')).toHaveLength(1);
    expect(screen.getByText(/withheld .* anonymous confession/i)).toBeTruthy();
    expect(screen.queryByText('user-1')).toBeNull();
  });
});