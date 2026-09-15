import { render, screen, waitFor, within, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { ConfessionsAdmin } from '@/pages/ConfessionsAdmin';

/**
 * Hiding a single comment from the Confession Review screen.
 *
 * The comment-level counterpart to `confessionsAdminHide.test.tsx`, and it exists because hiding a
 * whole confession to deal with one bad comment in a hundred is the wrong trade. The properties
 * under test are the same three the confession case has:
 *
 *   * soft and one-way -- the comment row stays on screen, dimmed and marked, because a moderator
 *     deciding on the reports still filed against it has to be able to see what is already gone;
 *   * nothing happens on a single click -- the request is only sent after the dialog is accepted;
 *   * a failure changes nothing on screen.
 *
 * One thing deliberately does *not* change, and it is asserted here because it looks like a bug:
 * the comment total on the collapsed row stays put. This is the staff view, where a hidden comment
 * is still present and still countable -- a reviewer has to know the thread holds two comments,
 * one of which has been taken down. The `comment_count` column that participants see is decremented
 * server-side and covered by the backend tests; the row here reads `max(comment_count, comments
 * .length)`, so the number on screen is the length of the list the reviewer can actually see.
 *
 * The absence of an unhide control is asserted rather than described, since that absence is the
 * promise the confirmation dialog makes.
 *
 * `fireEvent` rather than `user-event`, matching the other component tests here.
 */

const api = vi.hoisted(() => ({
  getAdminConfessions: vi.fn(),
  hideAdminConfession: vi.fn(),
  hideAdminComment: vi.fn(),
}));

vi.mock('@/services/api', () => api);
vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn() },
  Toaster: () => null,
}));

const NOW = '2026-01-15T10:00:00Z';
const HIDDEN_AT = '2026-01-16T12:30:00Z';

const confessionId = '22222222-2222-2222-2222-222222222222';
const noisyCommentId = '33333333-3333-3333-3333-333333333333';
const quietCommentId = '44444444-4444-4444-4444-444444444444';

const aComment = (id: string, body: string, name: string) => ({
  id,
  body,
  created_at: NOW,
  user: { id: `u-${id.slice(0, 4)}`, name, avatar: null },
  report_count: 0,
  reported_by: [],
  like_count: 0,
  dislike_count: 0,
  reports: [],
  content_removed: false,
  removed_at: null,
  removed_by_regno: null,
});

/** A confession carrying one comment that should be hidden and one that should not. */
const liveConfession = {
  id: confessionId,
  body: 'a confession with a bad comment under it',
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
  comment_count: 2,
  comments: [
    aComment(noisyCommentId, 'a comment worth hiding', 'The Noisy One'),
    aComment(quietCommentId, 'a comment that stays', 'The Quiet One'),
  ],
  content_removed: false,
  removed_at: null,
  removed_by_regno: null,
};

/** The same payload after a report ruling took that one comment down. */
const withCommentRemoved = {
  ...liveConfession,
  comment_count: 1,
  comments: [
    { ...liveConfession.comments[0], content_removed: true, removed_at: HIDDEN_AT, removed_by_regno: 'moduser' },
    liveConfession.comments[1],
  ],
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

const hideCommentButton = () => screen.getByRole('button', { name: /hide comment/i });
const commentAlreadyHidden = () => screen.queryByText(/already hidden/i);

/**
 * Two nested accordions: the confession row, then the comment inside it. Both have to be open
 * before anything inside the comment is reachable, and neither is part of what is under test.
 *
 * Split in two rather than chained, because the tests that load an already-hidden comment need the
 * comment open *without* waiting for a Hide control that will never be there.
 */
async function expandConfession() {
  const [trigger] = await screen.findAllByRole('button', { expanded: false });
  fireEvent.click(trigger);
  await screen.findAllByRole('button', { expanded: true });
}

async function expandComment(name: RegExp) {
  fireEvent.click(screen.getByRole('button', { name }));
  await screen.findByText('a comment worth hiding');
}

describe('ConfessionsAdmin: hiding a comment', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getAdminConfessions.mockResolvedValue([liveConfession]);
    api.hideAdminComment.mockResolvedValue({
      id: noisyCommentId,
      confession_id: confessionId,
      content_removed: true,
      already_hidden: false,
      removed_at: HIDDEN_AT,
      removed_by_regno: 'moduser',
    });
  });

  it('offers Hide comment without touching the confession itself', async () => {
    renderPage();
    await expandConfession();
    await expandComment(/the noisy one/i);

    expect(hideCommentButton()).toBeInTheDocument();
    // The confession's own control is still offered: hiding one comment must not hide the post.
    expect(screen.getByRole('button', { name: /hide confession/i })).toBeInTheDocument();
    expect(screen.queryByText(/already hidden/i)).not.toBeInTheDocument();
  });

  it('warns that the comment cannot be unhidden before anything is sent', async () => {
    renderPage();
    await expandConfession();
    await expandComment(/the noisy one/i);

    fireEvent.click(hideCommentButton());

    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText(/cannot be\s+unhidden/i)).toBeInTheDocument();
    // The dialog has to say what is *not* happening, or a reviewer cannot tell the two controls
    // apart at the moment they are choosing between them.
    expect(within(dialog).getByText(/the confession itself stays up/i)).toBeInTheDocument();
    expect(api.hideAdminComment).not.toHaveBeenCalled();
  });

  it('sends nothing when the dialog is cancelled', async () => {
    renderPage();
    await expandConfession();
    await expandComment(/the noisy one/i);

    fireEvent.click(hideCommentButton());
    const dialog = await screen.findByRole('alertdialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /cancel/i }));

    await waitFor(() => {
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    });
    expect(api.hideAdminComment).not.toHaveBeenCalled();
  });

  it('hides the comment and keeps its body readable on screen', async () => {
    renderPage();
    await expandConfession();
    await expandComment(/the noisy one/i);

    fireEvent.click(hideCommentButton());
    const dialog = await screen.findByRole('alertdialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /hide comment/i }));

    await waitFor(() => {
      expect(api.hideAdminComment).toHaveBeenCalledWith(noisyCommentId);
    });
    // The comment is not filtered out of the list. A moderator with a report still open against it
    // has to be able to read what was written and see that it is already gone -- which is also how
    // the removal is audited.
    expect(commentAlreadyHidden()).toBeInTheDocument();
    expect(screen.getByText('a comment worth hiding')).toBeInTheDocument();
    // The sibling row is untouched and still listed, with its own Hide control still available
    // once it is opened. Asserted on the trigger rather than the body because the sibling's
    // accordion is closed, and what matters here is that the row survived.
    expect(screen.getByRole('button', { name: /the quiet one/i })).toBeInTheDocument();
  });

  it('keeps counting the hidden comment, because staff still see it', async () => {
    renderPage();
    await expandConfession();
    await expandComment(/the noisy one/i);

    // The row counts the comments the reviewer can see, and a hidden one is still visible here.
    await waitFor(() => {
      expect(screen.getAllByText(/Comments 2/).length).toBeGreaterThan(0);
    });

    fireEvent.click(hideCommentButton());
    const dialog = await screen.findByRole('alertdialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /hide comment/i }));

    // Dropping this to 1 would tell a moderator the thread lost a comment, when it lost none -- the
    // row is still there, marked. The participant-facing count is the one that moves, and that is
    // the column the server decrements.
    await waitFor(() => {
      expect(commentAlreadyHidden()).toBeInTheDocument();
    });
    expect(screen.getAllByText(/Comments 2/).length).toBeGreaterThan(0);
  });

  it('does not offer Hide comment again once that comment is hidden', async () => {
    renderPage();
    await expandConfession();
    await expandComment(/the noisy one/i);

    fireEvent.click(hideCommentButton());
    const dialog = await screen.findByRole('alertdialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /hide comment/i }));

    await waitFor(() => {
      expect(screen.queryByRole('button', { name: /hide comment/i })).not.toBeInTheDocument();
    });
    // A second request would be a no-op that still shows a success toast, so the control is gone
    // rather than disabled -- there is nothing left to press.
    expect(commentAlreadyHidden()).toBeInTheDocument();
  });

  it('shows a comment taken down by a report as already hidden', async () => {
    // The state has to survive a reload: this is the payload as it comes back from the server on
    // a fresh load, not the optimistic update written by the click above.
    api.getAdminConfessions.mockResolvedValue([withCommentRemoved]);
    renderPage();
    await expandConfession();
    await expandComment(/the noisy one/i);

    expect(commentAlreadyHidden()).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /hide comment/i })).not.toBeInTheDocument();
  });

  it('does not offer any way to bring a hidden comment back', async () => {
    api.getAdminConfessions.mockResolvedValue([withCommentRemoved]);
    renderPage();
    await expandConfession();
    await expandComment(/the noisy one/i);

    expect(screen.queryByRole('button', { name: /unhide|restore|publish|show again/i })).toBeNull();
    expect(commentAlreadyHidden()).toBeInTheDocument();
  });

  it('leaves the comment visible when the request fails', async () => {
    api.hideAdminComment.mockRejectedValue(new Error('network'));
    renderPage();
    await expandConfession();
    await expandComment(/the noisy one/i);

    fireEvent.click(hideCommentButton());
    const dialog = await screen.findByRole('alertdialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /hide comment/i }));

    await waitFor(() => {
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    });
    // Nothing was hidden, so nothing on screen should claim it was.
    expect(commentAlreadyHidden()).not.toBeInTheDocument();
    expect(hideCommentButton()).toBeInTheDocument();
  });
});