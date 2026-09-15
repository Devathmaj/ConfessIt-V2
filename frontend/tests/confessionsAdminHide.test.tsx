import { render, screen, waitFor, within, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { ConfessionsAdmin } from '@/pages/ConfessionsAdmin';

/**
 * Hiding a confession from the Confession Review screen.
 *
 * Two properties are worth pinning, and they are different in kind.
 *
 * The first is that hiding is soft and one-way. The row stays on screen carrying `content_removed`,
 * because a reviewer working down a queue needs to see which posts are already handled, and the
 * screen says "Already hidden" instead of offering a button that does nothing. There is no unhide
 * control to test for -- the promise in the dialog is that none exists, and the assertion that
 * holds it is the absence of one.
 *
 * The second is that a single click does nothing. The hide request is only sent after the
 * confirmation dialog is accepted, and one test each exists for opening and cancelling it.
 *
 * `fireEvent` rather than `user-event`, matching the other component tests here. jsdom does not
 * synthesize Enter activating a button, so keyboard reachability of the confirm button is not
 * asserted here.
 */

const api = vi.hoisted(() => ({
  getAdminConfessions: vi.fn(),
  hideAdminConfession: vi.fn(),
}));

vi.mock('@/services/api', () => api);
vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn() },
  Toaster: () => null,
}));

const NOW = '2026-01-15T10:00:00Z';
const HIDDEN_AT = '2026-01-16T12:30:00Z';

const confessionId = '22222222-2222-2222-2222-222222222222';

/** A confession as the server sends it, with the removal fields at their default (not hidden). */
const liveConfession = {
  id: confessionId,
  body: 'something worth hiding',
  recipient_name: 'Someone In Particular',
  anonymous: false,
  sender_hidden: false,
  comments_enabled: true,
  created_at: NOW,
  sender: { id: 'author-1', name: 'The Author', regno: 'author', email: 'author@test.com' },
  report_count: 0,
  reported_by: [],
  reports: [],
  reactions: {},
  heart_count: 0,
  haha_count: 0,
  whoa_count: 0,
  heartbreak_count: 0,
  comment_count: 0,
  comments: [],
  content_removed: false,
  removed_at: null,
  removed_by_regno: null,
};

/** The same confession after a report ruling took it down. */
const removedConfession = {
  ...liveConfession,
  report_count: 2,
  content_removed: true,
  removed_at: HIDDEN_AT,
  removed_by_regno: 'moduser',
};

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

const hideButton = () => screen.getByRole('button', { name: /hide confession/i });
const alreadyHidden = () => screen.queryByText(/already hidden/i);

/**
 * The Hide control lives inside the expanded body of an accordion row, so every test has to open
 * the row first. Opening it is not part of what is under test, so it is done here.
 */
async function expandRow() {
  const trigger = await screen.findByRole('button', { expanded: false });
  fireEvent.click(trigger);
  await screen.findByRole('button', { expanded: true });
}

describe('ConfessionsAdmin: hiding a confession', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getAdminConfessions.mockResolvedValue([liveConfession]);
    api.hideAdminConfession.mockResolvedValue({
      id: confessionId,
      content_removed: true,
      already_hidden: false,
      removed_at: HIDDEN_AT,
      removed_by_regno: 'moduser',
    });
  });

  it('offers Hide on a confession that is not hidden yet', async () => {
    renderPage();
    await expandRow();

    expect(hideButton()).toBeInTheDocument();
    expect(alreadyHidden()).not.toBeInTheDocument();
  });

  it('warns that the confession cannot be unhidden before anything is sent', async () => {
    renderPage();
    await expandRow();

    fireEvent.click(hideButton());

    // The consequence is stated before the decision is asked for, and the irreversibility is the
    // point: this dialog is the only place a reviewer is told the door does not open both ways.
    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText(/cannot be\s+unhidden/i)).toBeInTheDocument();
    expect(api.hideAdminConfession).not.toHaveBeenCalled();
  });

  it('sends nothing when the dialog is cancelled', async () => {
    renderPage();
    await expandRow();

    fireEvent.click(hideButton());
    const dialog = await screen.findByRole('alertdialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /cancel/i }));

    await waitFor(() => {
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    });
    expect(api.hideAdminConfession).not.toHaveBeenCalled();
  });

  it('hides once the dialog is confirmed and keeps the row on screen', async () => {
    renderPage();
    await expandRow();

    fireEvent.click(hideButton());
    const dialog = await screen.findByRole('alertdialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /hide confession/i }));

    await waitFor(() => {
      expect(api.hideAdminConfession).toHaveBeenCalledWith(confessionId);
    });
    // The row stays, because a reviewer needs to see that this one is already handled. Filtering
    // it out of the list would make a reload look like it had never been actioned. The body shows
    // twice -- once clamped in the collapsed row, once in the expanded body -- so the count is
    // asserted rather than a single match.
    await waitFor(() => {
      expect(screen.getAllByText('something worth hiding').length).toBeGreaterThan(0);
    });
    expect(alreadyHidden()).toBeInTheDocument();
  });

  it('does not offer Hide again once the confession is hidden', async () => {
    renderPage();
    await expandRow();

    fireEvent.click(hideButton());
    const dialog = await screen.findByRole('alertdialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /hide confession/i }));

    await waitFor(() => {
      expect(screen.queryByRole('button', { name: /hide confession/i })).not.toBeInTheDocument();
    });
    // A second request would be a no-op that still shows a success toast, so the control is gone
    // rather than disabled -- there is nothing left to press.
    expect(alreadyHidden()).toBeInTheDocument();
  });

  it('shows a confession taken down by a report as already hidden', async () => {
    // The state has to survive a reload: this is the payload as it comes back from the server on
    // a fresh load, not the optimistic update written by the click above.
    api.getAdminConfessions.mockResolvedValue([removedConfession]);
    renderPage();
    await expandRow();

    expect(alreadyHidden()).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /hide confession/i })).not.toBeInTheDocument();
  });

  it('does not offer any way to bring a hidden confession back', async () => {
    api.getAdminConfessions.mockResolvedValue([removedConfession]);
    renderPage();
    await expandRow();

    // Stated as an absence rather than as a message, because that is what the guarantee is. A
    // future "unhide" control would show up here as an extra button name.
    expect(screen.queryByRole('button', { name: /unhide|restore|publish|show again/i })).toBeNull();
    expect(alreadyHidden()).toBeInTheDocument();
  });

  it('leaves the confession visible when the request fails', async () => {
    api.hideAdminConfession.mockRejectedValue(new Error('network'));
    renderPage();
    await expandRow();

    fireEvent.click(hideButton());
    const dialog = await screen.findByRole('alertdialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /hide confession/i }));

    await waitFor(() => {
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    });
    // Nothing was hidden, so nothing on screen should claim it was. Writing the hidden state
    // before the response came back would tell a reviewer a decision was applied that was not.
    expect(alreadyHidden()).not.toBeInTheDocument();
    expect(hideButton()).toBeInTheDocument();
  });
});