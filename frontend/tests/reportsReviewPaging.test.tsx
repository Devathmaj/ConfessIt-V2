import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { ReportsReview } from '@/pages/ReportsReview';

/**
 * Paging the three report queues through the paginated endpoints.
 *
 * Three properties are pinned here, each failing in a way the old first-page-only
 * screen could not have caught:
 *
 * * **Search is submitted, not typed.** The box is a draft; the Search button (or Enter)
 *   submits the query every queue is read with, so a half-typed word never costs three
 *   queue reads. Typing alone fires nothing.
 * * **Every queue grows downwards.** Each tab holds its own contiguous window and a
 *   "Show more" click appends the next page beneath the reports already on screen --
 *   the request carries the offset of what is shown, and the tab badges read server
 *   totals rather than loaded rows.
 * * **A ruling refreshes its own queue.** Dismissing or removing re-reads the touched
 *   queue's window, so the decided report comes back in its new state with true totals
 *   beside it instead of sitting patched.
 */

const api = vi.hoisted(() => ({
  getAdminConfessionReports: vi.fn(),
  getAdminLoveNoteReports: vi.fn(),
  getAdminMessageReports: vi.fn(),
  updateAdminConfessionReportStatus: vi.fn(),
  updateAdminLoveNoteReportStatus: vi.fn(),
  updateAdminMessageReportStatus: vi.fn(),
}));

const toast = vi.hoisted(() => ({
  info: vi.fn(),
  error: vi.fn(),
  success: vi.fn(),
  warning: vi.fn(),
}));

vi.mock('@/services/api', () => api);
vi.mock('sonner', () => ({ toast, Toaster: () => null }));

type QueueRow = {
  id: string;
  reason: string;
  status: string;
  reported_at: string;
  [key: string]: unknown;
};

/** The in-test stand-ins for the three queue tables, served the way the endpoints serve. */
let confessionDb: QueueRow[];
let loveNoteDb: QueueRow[];
let messageDb: QueueRow[];

const stamp = (day: number) => `2026-03-${String(day).padStart(2, '0')}T10:00:00.000Z`;

const confessionRow = (id: string, reason: string, overrides: Partial<QueueRow> = {}): QueueRow => ({
  id,
  target_type: 'confession',
  reason,
  status: 'pending',
  reported_at: stamp(10),
  reporter: { id: 'r1', name: 'Rita Reporter', regno: 'R1' },
  content: `reported post ${id}`,
  anonymous: false,
  ...overrides,
});

const loveNoteRow = (id: string, reason: string, overrides: Partial<QueueRow> = {}): QueueRow => ({
  id,
  love_note_id: `note-${id}`,
  reason,
  status: 'pending',
  reported_at: stamp(10),
  reporter: { id: 'r1', name: 'Rita Reporter', regno: 'R1' },
  reported_user: { id: 's1', name: 'Sam Sender', regno: 'S1' },
  body: `reported note ${id}`,
  ...overrides,
});

const messageRow = (id: string, reason: string, overrides: Partial<QueueRow> = {}): QueueRow => ({
  id,
  message_id: `msg-${id}`,
  reason,
  status: 'pending',
  reported_at: stamp(10),
  reporter: { id: 'r1', name: 'Rita Reporter', regno: 'R1' },
  reported_user: { id: 's1', name: 'Sam Sender', regno: 'S1' },
  body: `reported message ${id}`,
  conversation_id: null,
  conversation: null,
  ...overrides,
});

/**
 * Serves one queue table the way its endpoint does: status and search narrow first,
 * then one slice comes back with the total beside it. Takes the table by reference so
 * rows filed or ruled between reads are visible to the next one.
 */
const serveQueue = (getDb: () => QueueRow[]) => async (params: Record<string, unknown> = {}) => {
  const db = getDb();
  const status = params.status as string | undefined;
  const q = ((params.q as string | undefined) ?? '').trim().toLowerCase();
  const skip = (params.skip as number | undefined) ?? 0;
  const limit = (params.limit as number | undefined) ?? 20;
  const rows = db.filter((row) => {
    if (status && row.status !== status) return false;
    if (q && !JSON.stringify(row).toLowerCase().includes(q)) return false;
    return true;
  });
  return { reports: rows.slice(skip, skip + limit), total: rows.length, skip, limit };
};

const ruleOn = (dbName: 'confessionDb' | 'loveNoteDb' | 'messageDb') =>
  async (reportId: string, status: string) => {
    const dbs = { confessionDb, loveNoteDb, messageDb };
    const updated = dbs[dbName].map((row) =>
      row.id === reportId ? { ...row, status, reviewed_at: new Date().toISOString() } : row,
    );
    if (dbName === 'confessionDb') confessionDb = updated;
    if (dbName === 'loveNoteDb') loveNoteDb = updated;
    if (dbName === 'messageDb') messageDb = updated;
    return { id: reportId, status, content_removed: status === 'removed' };
  };

async function renderPage() {
  render(
    <AuthContext.Provider
      value={{
        user: { regno: 'viewer', account_type: 'admin', capabilities: [], is_blocked: false },
        isAuthenticated: true,
        loading: false,
        can: () => true,
      } as never}
    >
      <MemoryRouter>
        <ReportsReview />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
  await screen.findByText(/Showing \d+ of \d+/);
}

/** Picks `option` from the Radix select labelled `label`, via the pointer dance it needs. */
async function choose(label: string, option: string) {
  fireEvent.pointerDown(screen.getByLabelText(label), {
    button: 0,
    ctrlKey: false,
    pointerType: 'mouse',
  });
  fireEvent.click(await screen.findByRole('option', { name: option }));
}

describe('ReportsReview: server-driven search, filter and paging', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    confessionDb = [];
    loveNoteDb = [];
    messageDb = [];
    api.getAdminConfessionReports.mockImplementation(serveQueue(() => confessionDb));
    api.getAdminLoveNoteReports.mockImplementation(serveQueue(() => loveNoteDb));
    api.getAdminMessageReports.mockImplementation(serveQueue(() => messageDb));
    api.updateAdminConfessionReportStatus.mockImplementation(ruleOn('confessionDb'));
    api.updateAdminLoveNoteReportStatus.mockImplementation(ruleOn('loveNoteDb'));
    api.updateAdminMessageReportStatus.mockImplementation(ruleOn('messageDb'));
  });

  it('fires nothing while typing; the Search button submits the query to all three queues', async () => {
    confessionDb = [confessionRow('c1', 'hateful content')];
    loveNoteDb = [loveNoteRow('l1', 'creepy note')];
    messageDb = [messageRow('m1', 'threatening text')];
    await renderPage();
    await screen.findByText('hateful content');

    // Three reads for the unfiltered first pages, and nothing else while the draft changes.
    expect(api.getAdminConfessionReports).toHaveBeenCalledTimes(1);
    fireEvent.change(screen.getByPlaceholderText(/Search reasons/), {
      target: { value: 'hateful' },
    });
    expect(api.getAdminConfessionReports).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'Search reports' }));
    await waitFor(() => {
      expect(screen.queryByText('creepy note')).not.toBeInTheDocument();
    });

    // The submitted query went out on every queue, and each tab counts its own matches.
    for (const getter of [
      api.getAdminConfessionReports,
      api.getAdminLoveNoteReports,
      api.getAdminMessageReports,
    ]) {
      expect(getter).toHaveBeenCalledWith(expect.objectContaining({ q: 'hateful' }));
    }
    expect(screen.getByText('hateful content')).toBeInTheDocument();
    expect(screen.getByText(/Showing 1 of 1/)).toBeInTheDocument();
  });

  it('sends the status filter to the database and pages the filtered question', async () => {
    confessionDb = [
      confessionRow('c1', 'open complaint'),
      confessionRow('c2', 'closed complaint', { status: 'dismissed' }),
    ];
    await renderPage();
    await screen.findByText('open complaint');

    await choose('Filter', 'Pending');

    expect(api.getAdminConfessionReports).toHaveBeenCalledWith(
      expect.objectContaining({ status: 'pending' }),
    );
    await waitFor(() => {
      expect(screen.queryByText('closed complaint')).not.toBeInTheDocument();
    });
    expect(screen.getByText('open complaint')).toBeInTheDocument();
  });

  it('grows one queue downwards on Show more without touching the other queues', async () => {
    confessionDb = Array.from({ length: 21 }, (_, index) =>
      confessionRow(`c${index}`, `complaint number ${index}`),
    );
    await renderPage();
    await screen.findByText(/Showing 20 of 21/);

    fireEvent.click(screen.getByRole('button', { name: 'Show more reported confessions' }));
    await screen.findByText(/Showing 21 of 21/);

    // The second read carried the offset of what was shown; the love-note and message
    // queues were never re-read for it.
    const skips = api.getAdminConfessionReports.mock.calls.map((call) => call[0]?.skip ?? 0);
    expect(skips).toContain(20);
    expect(api.getAdminLoveNoteReports).toHaveBeenCalledTimes(1);
    // The count and the appended row land in separate commits under load: await each.
    expect(await screen.findByText('complaint number 20')).toBeInTheDocument();
    await waitFor(() => {
      expect(
        screen.queryByRole('button', { name: 'Show more reported confessions' }),
      ).not.toBeInTheDocument();
    });
  });

  it('refetches the love-note queue after dismissing so the decided row comes back closed', async () => {
    loveNoteDb = [loveNoteRow('l1', 'creepy note')];
    await renderPage();

    // Radix Tabs activates its trigger on mousedown rather than on click, so a bare
    // click leaves the tab exactly where it was.
    const loveNotesTab = screen.getByRole('tab', { name: /love notes/i });
    fireEvent.mouseDown(loveNotesTab, { button: 0, ctrlKey: false });
    const toggle = await screen.findByRole('button', { name: /love note reported by/i });
    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole('button', { name: /^dismiss$/i }));
    const dialog = await screen.findByRole('alertdialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /^dismiss$/i }));

    // The ruling re-read the queue: the decided report is back with its new status, and
    // the queue offers no ruling buttons on a decided row.
    await waitFor(() => {
      expect(api.updateAdminLoveNoteReportStatus).toHaveBeenCalledWith('l1', 'dismissed');
    });
    await screen.findByText('Dismissed');
    expect(api.getAdminLoveNoteReports.mock.calls.length).toBeGreaterThan(1);
    expect(screen.queryByRole('button', { name: /^dismiss$/i })).not.toBeInTheDocument();
  });

  it('still renders legacy bare-array answers with no pager', async () => {
    api.getAdminConfessionReports.mockResolvedValue([confessionRow('c1', 'hateful content')]);
    api.getAdminLoveNoteReports.mockResolvedValue([]);
    api.getAdminMessageReports.mockResolvedValue([]);

    await renderPage();

    expect(screen.getByText('hateful content')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /show more/i })).not.toBeInTheDocument();
  });
});
