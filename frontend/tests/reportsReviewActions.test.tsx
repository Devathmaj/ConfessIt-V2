import { render, screen, waitFor, within, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { ReportsReview } from '@/pages/ReportsReview';

/**
 * Ruling on a report from the Reports Review screen.
 *
 * The cases below are as much about what must *not* happen on a click as about what must. Ruling
 * `removed` takes content out of every participant's view, and it is reachable from a row button,
 * so the property worth pinning is that a single click does nothing: the request is only sent
 * after the confirmation dialog is accepted. Two tests exist purely to hold that line — one
 * asserting nothing is sent when the dialog opens, one asserting nothing is sent when it is
 * cancelled.
 *
 * `fireEvent` rather than `user-event`, matching the other component tests here. The one thing it
 * cannot do is synthesize Enter activating a button -- jsdom does not implement the default
 * action -- so keyboard reachability of the dialog's confirm button is not asserted here.
 */

const api = vi.hoisted(() => ({
  getAdminConfessionReports: vi.fn(),
  getAdminLoveNoteReports: vi.fn(),
  getAdminMessageReports: vi.fn(),
  updateAdminConfessionReportStatus: vi.fn(),
  updateAdminLoveNoteReportStatus: vi.fn(),
  updateAdminMessageReportStatus: vi.fn(),
}));

vi.mock('@/services/api', () => api);
vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn() },
  Toaster: () => null,
}));

const NOW = '2026-01-15T10:00:00Z';

const confessionReport = {
  id: '11111111-1111-1111-1111-111111111111',
  target_type: 'confession',
  reason: 'hateful content',
  status: 'pending',
  reported_at: NOW,
  reported_by_name: 'Some Student',
  confession_id: '22222222-2222-2222-2222-222222222222',
  content: 'something rude',
  anonymous: false,
  recipient_name: null,
  author: null,
  author_hidden: false,
  comment_id: null,
  comment_content: null,
  commenter: null,
  comment_parent_recipient: null,
  comment_count: 0,
  created_at: NOW,
  reviewed_at: null,
  reviewed_by_regno: null,
  content_removed: false,
  removed_at: null,
  removed_by_regno: null,
};

const emptyQueue = { reports: [], total: 0, skip: 0, limit: 50 };

/** The page gates its close-out buttons on `reports:moderate`, so the test account has it. */
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
        <ReportsReview />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

const dismissButton = () => screen.getByRole('button', { name: /dismiss/i });
const removeButton = () => screen.getByRole('button', { name: /remove content/i });

/** Opens the report card and returns its header toggle.
 *
 * Cards are collapsed by default, so the close-out buttons do not exist until one is opened.
 * The header *is* the toggle, so it is found by the accessible name it already had as a title,
 * which is also the check that the button still carries a usable name after the markup changed.
 */
async function openCard() {
  const toggle = await screen.findByRole('button', { name: /confession reported by/i });
  expect(toggle).toHaveAttribute('aria-expanded', 'false');
  fireEvent.click(toggle);
  expect(toggle).toHaveAttribute('aria-expanded', 'true');
  return toggle;
}

describe('ReportsReview: ruling on a report', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // A mutable stand-in for the queue table: reads serve its current rows, and ruling
    // mutates the row the way the endpoint does, so the refresh the screen issues after
    // every ruling finds the decided report rather than the pending one it replaced.
    let confessionDb = [{ ...confessionReport }];
    api.getAdminConfessionReports.mockImplementation(async () => ({
      reports: confessionDb,
      total: confessionDb.length,
      skip: 0,
      limit: 50,
    }));
    api.updateAdminConfessionReportStatus.mockImplementation(
      async (reportId: string, status: string) => {
        confessionDb = confessionDb.map((report) =>
          report.id === reportId
            ? { ...report, status, reviewed_at: new Date().toISOString() }
            : report,
        );
        return { id: reportId, status, content_removed: status === 'removed' };
      },
    );
    api.getAdminLoveNoteReports.mockResolvedValue(emptyQueue);
    api.getAdminMessageReports.mockResolvedValue(emptyQueue);
  });

  it('offers both outcomes on a pending report', async () => {
    renderPage();
    await screen.findByText('hateful content');
    await openCard();
    expect(dismissButton()).toBeInTheDocument();
    expect(removeButton()).toBeInTheDocument();
  });

  it('sends nothing when the dialog opens', async () => {
    renderPage();
    await screen.findByText('hateful content');
    await openCard();

    fireEvent.click(removeButton());

    // The dialog is up and nothing has been sent. This is the misclick case: a moderator who
    // reaches for Dismiss and misses, or opens Remove by accident, has removed nothing.
    expect(await screen.findByRole('alertdialog')).toBeInTheDocument();
    expect(api.updateAdminConfessionReportStatus).not.toHaveBeenCalled();
  });

  it('removes the content once the dialog is confirmed', async () => {
    api.updateAdminConfessionReportStatus.mockResolvedValue({
      id: confessionReport.id,
      status: 'removed',
      content_removed: true,
    });
    renderPage();
    await screen.findByText('hateful content');
    await openCard();

    fireEvent.click(removeButton());
    const dialog = await screen.findByRole('alertdialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /remove content/i }));

    await waitFor(() => {
      expect(api.updateAdminConfessionReportStatus).toHaveBeenCalledWith(
        confessionReport.id,
        'removed',
      );
    });
  });

  it('sends nothing when the dialog is cancelled', async () => {
    renderPage();
    await screen.findByText('hateful content');
    await openCard();

    fireEvent.click(removeButton());
    const dialog = await screen.findByRole('alertdialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /cancel/i }));

    await waitFor(() => {
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    });
    expect(api.updateAdminConfessionReportStatus).not.toHaveBeenCalled();
  });

  it('dismisses without taking the content down', async () => {
    api.updateAdminConfessionReportStatus.mockResolvedValue({
      id: confessionReport.id,
      status: 'dismissed',
      content_removed: false,
    });
    renderPage();
    await screen.findByText('hateful content');
    await openCard();

    fireEvent.click(dismissButton());
    const dialog = await screen.findByRole('alertdialog');
    // The dialog leads with the consequence before it asks for the decision.
    expect(
      within(dialog).getByText(/the content will stay up/i),
    ).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: /^dismiss$/i }));

    await waitFor(() => {
      expect(api.updateAdminConfessionReportStatus).toHaveBeenCalledWith(
        confessionReport.id,
        'dismissed',
      );
    });
    // A dismissal leaves the report's own text on screen; only its status changes.
    expect(screen.getByText('hateful content')).toBeInTheDocument();
  });

  it('closes the report out so it cannot be ruled on twice', async () => {
    // No per-test override: the `beforeEach` ruling mutates the queue the way the endpoint
    // does, which is exactly what the refresh below has to find. A static override here
    // would answer the ruling while leaving the queue pending, and the re-read would put
    // the buttons this test asserts away right back.
    renderPage();
    await screen.findByText('hateful content');
    await openCard();

    fireEvent.click(removeButton());
    const dialog = await screen.findByRole('alertdialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /remove content/i }));

    // The card carries the decided state only once the refresh it triggers lands: the
    // dialog's own confirm button reads "Working…" meanwhile, so waiting on a button
    // disappearing would pass on the dialog before the queue has re-read.
    await screen.findByText('Content removed');
    expect(screen.queryByRole('button', { name: /remove content/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /dismiss/i })).not.toBeInTheDocument();
  });
});
