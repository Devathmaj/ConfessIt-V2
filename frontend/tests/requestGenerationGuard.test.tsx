import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { PartnerPicker } from '@/components/games/PartnerPicker';
import { LoveNotesPage } from '@/pages/LoveNotesPage';
import { ReportsReview } from '@/pages/ReportsReview';

/**
 * A slow answer for an old filter must not overwrite a newer one.
 *
 * Every one of these screens reads its rows from the server under a filter, and
 * every one of them can have two such reads in flight at once: the reader changes
 * a filter while the previous filter's read is still on the wire. Nothing stopped
 * the slower answer from being written, so the screen ended up showing the rows for
 * the filter the reader had already changed away from -- people they had just
 * filtered *out*, under the count for the filter they had just chosen.
 *
 * The fix is the guard the confessions feed already had for its search: each read
 * claims the right to answer, and an answer that is no longer the latest is
 * ignored.
 */

// The page loads its six card templates over `fetch` at mount.
vi.stubGlobal('fetch', vi.fn(async () => ({ json: async () => [] })));

const api = vi.hoisted(() => ({
  getPartnerCohorts: vi.fn(),
  getLoveNoteRecipients: vi.fn(),
  getCohorts: vi.fn(),
  getReceivedLoveNotes: vi.fn(),
  sendLoveNote: vi.fn(),
  markLoveNoteRead: vi.fn(),
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

/** A promise whose settlement the test controls. */
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

const partner = (id: string, name: string) => ({
  id,
  name,
  year: 2,
  department: 'CSE',
  gender: 'other',
  interests: [],
  profile_picture_url: null,
  is_blocked: false,
});

const authValue = {
  user: { id: 'me', name: 'Me', regno: 'CS-9', email: 'me@example.com', gender: 'female' },
  logout: vi.fn(),
  isAuthenticated: true,
  isLoading: false,
  token: 't',
  verifyMagicToken: vi.fn(),
  fetchAndSetUser: vi.fn(),
  can: () => false,
  signInPath: '/login',
};

// ------------------------------------------------------------------ the partner picker


describe('PartnerPicker: a slow answer for an old filter', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getPartnerCohorts.mockResolvedValue([]);
  });

  it('keeps the newer filter when the older read answers after it', async () => {
    const first = deferred<{ partners: unknown[]; total: number }>();
    const second = deferred<{ partners: unknown[]; total: number }>();
    const loadPartners = vi
      .fn()
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);

    render(
      <PartnerPicker loadPartners={loadPartners as never} onChoose={() => {}} />,
    );

    // The first read is the opening one; the second is the filter the reader
    // changes to while the first is still on the wire.
    fireEvent.click(screen.getByRole('button', { name: /show same gender/i }));

    await act(async () => {
      second.resolve({ partners: [partner('b', 'Bea Second')], total: 1 });
    });
    expect(screen.getByText('Bea Second')).toBeInTheDocument();

    // Now the older read answers, with the rows for the filter the reader has
    // already changed away from.
    await act(async () => {
      first.resolve({ partners: [partner('a', 'Ann First')], total: 1 });
    });

    expect(screen.getByText('Bea Second')).toBeInTheDocument();
    expect(screen.queryByText('Ann First')).not.toBeInTheDocument();
  });

  it('does not show a failure from a read that has been overtaken', async () => {
    const first = deferred<{ partners: unknown[]; total: number }>();
    const second = deferred<{ partners: unknown[]; total: number }>();
    const loadPartners = vi
      .fn()
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);

    render(
      <PartnerPicker loadPartners={loadPartners as never} onChoose={() => {}} />,
    );
    fireEvent.click(screen.getByRole('button', { name: /show same gender/i }));

    await act(async () => {
      second.resolve({ partners: [partner('b', 'Bea Second')], total: 1 });
    });
    await act(async () => {
      first.reject(new Error('the older request failed'));
    });

    // The older failure is not the state of the screen: the newer read succeeded.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByText('Bea Second')).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------ the love-note recipients


describe('LoveNotesPage: a slow answer for an old recipient filter', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getCohorts.mockResolvedValue([]);
    api.getReceivedLoveNotes.mockResolvedValue({ notes: [], total: 0, skip: 0, limit: 12 });
  });

  it('keeps the newer search when the older read answers after it', async () => {
    const first = deferred<{ users: unknown[]; total: number }>();
    const second = deferred<{ users: unknown[]; total: number }>();
    api.getLoveNoteRecipients
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);

    render(
      <AuthContext.Provider value={authValue as never}>
        <MemoryRouter>
          <LoveNotesPage />
        </MemoryRouter>
      </AuthContext.Provider>,
    );
    const textarea = await screen.findByLabelText('Your Message');
    fireEvent.change(textarea, { target: { value: 'thinking of you' } });
    fireEvent.click(screen.getByRole('button', { name: /send your love note/i }));
    await screen.findByText('Select Your Recipient');

    // The sender searches while the opening read is still on the wire.
    fireEvent.change(screen.getByPlaceholderText('Search by name...'), {
      target: { value: 'alice' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Search recipients' }));

    await act(async () => {
      second.resolve({
        users: [
          {
            id: 'a1',
            name: 'Alice Found',
            year: 2,
            department: 'CSE',
            gender: 'male',
            interests: [],
            profile_picture_url: null,
            is_blocked: false,
          },
        ],
        total: 1,
      });
    });
    expect(screen.getByText('Alice Found')).toBeInTheDocument();

    await act(async () => {
      first.resolve({
        users: [
          {
            id: 'b1',
            name: 'Bob Filtered',
            year: 2,
            department: 'CSE',
            gender: 'male',
            interests: [],
            profile_picture_url: null,
            is_blocked: false,
          },
        ],
        total: 1,
      });
    });

    // The name the sender had just filtered out is not on screen.
    expect(screen.getByText('Alice Found')).toBeInTheDocument();
    expect(screen.queryByText('Bob Filtered')).not.toBeInTheDocument();
  });
});

// ------------------------------------------------------------------ the report queues


type QueueRow = {
  id: string;
  reason: string;
  status: string;
  reported_at: string;
  [key: string]: unknown;
};

const confessionRow = (id: string, reason: string): QueueRow => ({
  id,
  target_type: 'confession',
  reason,
  status: 'pending',
  reported_at: '2026-03-10T10:00:00.000Z',
  reporter: { id: 'r1', name: 'Rita Reporter', regno: 'R1' },
  content: `reported post ${id}`,
  anonymous: false,
});

describe('ReportsReview: a slow answer for an old question', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('does not append a page read for the old filter to the window of the new one', async () => {
    // The opening read answers at once and says there is more to come; the "show
    // more" page is held back, and the reviewer changes the filter while it is in
    // flight.
    const secondPage = deferred<{ reports: QueueRow[]; total: number }>();

    api.getAdminConfessionReports.mockImplementation(
      async (params: Record<string, unknown> = {}) => {
        const skip = (params.skip as number | undefined) ?? 0;
        if (skip > 0) return secondPage.promise;
        return {
          reports: [confessionRow('c1', 'open complaint')],
          total: 2,
          skip,
          limit: 20,
        };
      },
    );
    api.getAdminLoveNoteReports.mockResolvedValue({
      reports: [],
      total: 0,
      skip: 0,
      limit: 20,
    });
    api.getAdminMessageReports.mockResolvedValue({
      reports: [],
      total: 0,
      skip: 0,
      limit: 20,
    });

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
    await screen.findByText('open complaint');

    fireEvent.click(screen.getByRole('button', { name: 'Show more reported confessions' }));

    // The reviewer changes the question while that page is still on the wire.
    await act(async () => {
      api.getAdminConfessionReports.mockResolvedValue({
        reports: [confessionRow('c9', 'complaint under the new filter')],
        total: 1,
        skip: 0,
        limit: 20,
      });
    });
    fireEvent.change(screen.getByPlaceholderText(/Search reasons/), {
      target: { value: 'new filter' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Search reports' }));

    await waitFor(() => {
      expect(screen.getByText('complaint under the new filter')).toBeInTheDocument();
    });

    // The held-back page answers now. It belongs to the old question, so it must
    // not be appended to the window of the new one.
    await act(async () => {
      secondPage.resolve({
        reports: [confessionRow('c2', 'complaint from the old filter')],
        total: 2,
      });
    });

    await waitFor(() => {
      expect(screen.queryByText('complaint from the old filter')).not.toBeInTheDocument();
    });
    expect(screen.getByText('complaint under the new filter')).toBeInTheDocument();
  });

  it('leaves the "show more" button usable after a question change overtakes a page read', async () => {
    const secondPage = deferred<{ reports: QueueRow[]; total: number }>();

    api.getAdminConfessionReports.mockImplementation(
      async (params: Record<string, unknown> = {}) => {
        const skip = (params.skip as number | undefined) ?? 0;
        if (skip > 0) return secondPage.promise;
        return {
          reports: [confessionRow('c1', 'open complaint')],
          total: 2,
          skip,
          limit: 20,
        };
      },
    );
    api.getAdminLoveNoteReports.mockResolvedValue({ reports: [], total: 0, skip: 0, limit: 20 });
    api.getAdminMessageReports.mockResolvedValue({ reports: [], total: 0, skip: 0, limit: 20 });

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
    await screen.findByText('open complaint');

    const showMore = screen.getByRole('button', { name: 'Show more reported confessions' });
    fireEvent.click(showMore);

    // The question changes while that page is still on the wire.
    await act(async () => {
      api.getAdminConfessionReports.mockResolvedValue({
        reports: [confessionRow('c9', 'complaint under the new filter')],
        total: 2,
        skip: 0,
        limit: 20,
      });
    });
    fireEvent.change(screen.getByPlaceholderText(/Search reasons/), {
      target: { value: 'new filter' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Search reports' }));

    await waitFor(() => {
      expect(screen.getByText('complaint under the new filter')).toBeInTheDocument();
    });

    // The button is not left disabled for a request that no longer means anything.
    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: 'Show more reported confessions' }),
      ).toBeEnabled();
    });

    await act(async () => {
      secondPage.resolve({
        reports: [confessionRow('c2', 'complaint from the old filter')],
        total: 2,
      });
    });
  });
});
