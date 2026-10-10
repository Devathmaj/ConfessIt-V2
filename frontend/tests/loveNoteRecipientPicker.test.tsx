import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { LoveNotesPage } from '@/pages/LoveNotesPage';

/**
 * The recipient picker reads the participant table one page at a time.
 *
 * It used to download every eligible participant and filter them in the browser, which
 * made opening the send dialog a full read of the table and made the search a scan of
 * whatever had already arrived. These tests pin the replacement contract:
 *
 * * the first read is one page with the server total beside it, and "load more" appends
 *   the next page beneath the rows already on screen;
 * * the search is submitted to the server and matched there -- a name that did not come
 *   back is not on screen, however the already-loaded rows look;
 * * the gender switch is a server filter: off (the default) the server answers with the
 *   genders the viewer is not in, on it answers with both;
 * * opening the picker is what reads the list at all -- the page mount does not.
 */

// The page loads its six card templates over `fetch` at mount.
vi.stubGlobal('fetch', vi.fn(async () => ({ json: async () => [] })));

const api = vi.hoisted(() => ({
  getLoveNoteRecipients: vi.fn(),
  getCohorts: vi.fn(),
  getReceivedLoveNotes: vi.fn(),
  sendLoveNote: vi.fn(),
  markLoveNoteRead: vi.fn(),
}));

vi.mock('@/services/api', () => api);
vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn() },
  Toaster: () => null,
}));

type Candidate = {
  id: string;
  name: string;
  year: number;
  department: string;
  gender: string;
  interests: string[];
  profile_picture_url: string | null;
  is_blocked: boolean;
};

/** The in-test stand-in for the participant table, served the way the endpoint serves it. */
let candidates: Candidate[];

const candidate = (
  id: string,
  name: string,
  gender: string,
  over: Partial<Candidate> = {},
): Candidate => ({
  id,
  name,
  year: 2,
  department: 'CSE',
  gender,
  interests: [],
  profile_picture_url: null,
  is_blocked: false,
  ...over,
});

const authValue = {
  user: { id: 'me', name: 'Me', regno: 'CS-9', gender: 'female' },
  logout: vi.fn(),
  isAuthenticated: true,
  isLoading: false,
  token: 't',
  verifyMagicToken: vi.fn(),
  fetchAndSetUser: vi.fn(),
  can: () => false,
  signInPath: '/login',
};

/** Opens the send dialog the way a sender does: write something, then ask for a recipient. */
async function openPicker() {
  render(
    <AuthContext.Provider value={authValue as never}>
      <MemoryRouter>
        <LoveNotesPage />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
  const textarea = await screen.findByPlaceholderText('Write your heartfelt message here...');
  fireEvent.change(textarea, { target: { value: 'thinking of you' } });
  fireEvent.click(screen.getByRole('button', { name: /send your love note/i }));
  await screen.findByText('Select Your Recipient');
}

describe('LoveNotesPage: recipient picker', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    candidates = [];
    api.getCohorts.mockResolvedValue([]);
    api.getReceivedLoveNotes.mockResolvedValue({ notes: [], total: 0, skip: 0, limit: 12 });
    api.getLoveNoteRecipients.mockImplementation(async (params: Record<string, unknown> = {}) => {
      const skip = (params.skip as number | undefined) ?? 0;
      const limit = (params.limit as number | undefined) ?? 20;
      const q = (params.q as string | undefined)?.toLowerCase();
      let rows = candidates.filter((row) =>
        params.includeSameGender ? true : row.gender !== authValue.user.gender,
      );
      if (q) rows = rows.filter((row) => row.name.toLowerCase().includes(q));
      if (params.year !== undefined) rows = rows.filter((row) => row.year === params.year);
      if (params.department) {
        rows = rows.filter(
          (row) => row.department.toLowerCase() === String(params.department).toLowerCase(),
        );
      }
      rows = [...rows].sort(
        (a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id),
      );
      return { users: rows.slice(skip, skip + limit), total: rows.length, skip, limit };
    });
  });

  it('does not read the recipients until the picker is opened', async () => {
    render(
      <AuthContext.Provider value={authValue as never}>
        <MemoryRouter>
          <LoveNotesPage />
        </MemoryRouter>
      </AuthContext.Provider>,
    );
    await screen.findByPlaceholderText('Write your heartfelt message here...');

    expect(api.getLoveNoteRecipients).not.toHaveBeenCalled();
  });

  it('loads one page with the total and grows the list downwards', async () => {
    candidates = Array.from({ length: 25 }, (_, index) =>
      candidate(
        `c${String(index).padStart(2, '0')}`,
        `Person ${String(index).padStart(2, '0')}`,
        'male',
      ),
    );
    await openPicker();
    await screen.findByText('Person 00');

    // The first read is one page, not the table, and it carries the default gender filter.
    expect(api.getLoveNoteRecipients).toHaveBeenCalledWith({
      skip: 0,
      limit: 20,
      includeSameGender: false,
    });
    expect(screen.queryByText('Person 20')).not.toBeInTheDocument();

    const loadMore = screen.getByRole('button', { name: 'Load more recipients' });
    expect(loadMore).toHaveTextContent('Load more (5 remaining)');

    fireEvent.click(loadMore);
    await screen.findByText('Person 20');

    // The second read carried the offset of what was shown; nothing was re-read.
    const skips = api.getLoveNoteRecipients.mock.calls.map((call) => call[0]?.skip ?? 0);
    expect(skips).toContain(20);
    expect(screen.getByText('Person 00')).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Load more recipients' }),
    ).not.toBeInTheDocument();
  });

  it('submits the search to the server instead of filtering the loaded page', async () => {
    candidates = [
      candidate('a1', 'Alice Anderson', 'male'),
      candidate('a2', 'Bob Brown', 'male'),
    ];
    await openPicker();
    await screen.findByText('Alice Anderson');
    expect(screen.getByText('Bob Brown')).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText('Search by name...'), {
      target: { value: 'ali' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Search recipients' }));

    await waitFor(() =>
      expect(api.getLoveNoteRecipients).toHaveBeenLastCalledWith({
        skip: 0,
        limit: 20,
        q: 'ali',
        includeSameGender: false,
      }),
    );
    // The name that did not match is gone because the server did not return it.
    await waitFor(() => expect(screen.queryByText('Bob Brown')).not.toBeInTheDocument());
    expect(screen.getByText('Alice Anderson')).toBeInTheDocument();
  });

  it("hides the viewer's own gender by default and widens on the switch", async () => {
    candidates = [
      candidate('she', 'Sheila Same', 'female'),
      candidate('he', 'Harvey Other', 'male'),
    ];
    await openPicker();

    // Default: the server answers "not my gender", so the same-gender participant is absent.
    await screen.findByText('Harvey Other');
    expect(screen.queryByText('Sheila Same')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('switch', { name: 'Include same gender' }));

    await waitFor(() =>
      expect(api.getLoveNoteRecipients).toHaveBeenLastCalledWith({
        skip: 0,
        limit: 20,
        includeSameGender: true,
      }),
    );
    await screen.findByText('Sheila Same');
    expect(screen.getByText('Harvey Other')).toBeInTheDocument();
  });

  it('shows the send button busy while the note is processed, and one click sends one note', async () => {
    candidates = [candidate('c1', 'Person 01', 'male')];
    await openPicker();
    await screen.findByText('Person 01');
    fireEvent.click(screen.getByText('Person 01'));

    // jsdom has no real canvas; the export is stubbed so the send path can be reached.
    const toDataURL = vi
      .spyOn(HTMLCanvasElement.prototype, 'toDataURL')
      .mockReturnValue('data:image/png;base64,AAAA');

    let rejectSend!: (reason: unknown) => void;
    api.sendLoveNote.mockImplementation(
      () => new Promise((_resolve, reject) => { rejectSend = reject; }),
    );

    fireEvent.click(screen.getByRole('button', { name: /confirm & send note/i }));

    // Busy: the label is replaced, and the disabled button swallows a second click.
    const busy = await screen.findByRole('button', { name: /sending/i });
    expect(busy).toBeDisabled();
    fireEvent.click(busy);
    expect(api.sendLoveNote).toHaveBeenCalledTimes(1);

    // A failure restores the button so the note can be retried.
    rejectSend(new Error('boom'));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /confirm & send note/i })).toBeEnabled(),
    );

    toDataURL.mockRestore();
  });
});
