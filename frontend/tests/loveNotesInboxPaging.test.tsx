import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { LoveNotesPage } from '@/pages/LoveNotesPage';

/**
 * Paging the love-note inbox through the paginated endpoint.
 *
 * Three properties are pinned here, each failing in a way the old fetch-everything
 * inbox could not have caught:
 *
 * * **The grid grows downwards.** The screen holds one contiguous window with the
 *   server total behind it, and "Show more" appends the next page beneath the notes
 *   already on screen -- the request carries the offset of what is shown.
 * * **A deep link finds its note past page one.** A notification can name a note the
 *   first page does not hold, so the opener keeps reading further pages until the
 *   note turns up or the inbox runs out -- bounded by the total either way.
 * * **A missing note ends the search.** A note taken down since its notification was
 *   written loads every page once and then stops, instead of fetching forever.
 */

// The page loads its six card templates over `fetch` at mount.
vi.stubGlobal('fetch', vi.fn(async () => ({ json: async () => [] })));

const api = vi.hoisted(() => ({
  getAllUsers: vi.fn(),
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

type InboxRow = {
  id: string;
  from: string;
  body: string;
  image_url: string;
  anonymous: boolean;
  created_at: string;
  read_at: string | null;
};

/** The in-test stand-in for the inbox table, served newest-first like the endpoint. */
let db: InboxRow[];

const note = (id: string, day: number): InboxRow => ({
  id,
  from: 'Ada',
  body: `love note ${id}`,
  image_url: `http://example.test/${id}.png`,
  anonymous: false,
  created_at: `2026-07-${String(day).padStart(2, '0')}T10:00:00.000Z`,
  read_at: null,
});

const authValue = {
  user: { id: 'me', name: 'Me', regno: 'CS-9' },
  logout: vi.fn(),
  isAuthenticated: true,
  isLoading: false,
  token: 't',
  verifyMagicToken: vi.fn(),
  fetchAndSetUser: vi.fn(),
  can: () => false,
  signInPath: '/login',
};

async function renderAt(url: string) {
  api.getAllUsers.mockResolvedValue([]);
  api.getCohorts.mockResolvedValue([]);
  render(
    <AuthContext.Provider value={authValue as never}>
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route path="/love-notes" element={<LoveNotesPage />} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );
  // The inbox arrives over an effect, so no card exists until the first page lands.
  await waitFor(() => expect(api.getReceivedLoveNotes).toHaveBeenCalled());
}

/**
 * The full-screen viewer, identified by the image URL it is showing rather than by
 * accessible name: the viewer's `<img>` is labelled "Love note", the same label every
 * note in the inbox carries.
 */
const viewerShowing = (imageUrl: string) => {
  const images = document.querySelectorAll<HTMLImageElement>(`img[src="${imageUrl}"]`);
  for (const image of images) {
    if (image.closest('.fixed')) return image;
  }
  return null;
};

describe('LoveNotesPage: paged inbox', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    db = [];
    api.getReceivedLoveNotes.mockImplementation(async (params: Record<string, unknown> = {}) => {
      const skip = (params.skip as number | undefined) ?? 0;
      const limit = (params.limit as number | undefined) ?? 12;
      const rows = [...db].sort((a, b) => b.created_at.localeCompare(a.created_at));
      return { notes: rows.slice(skip, skip + limit), total: rows.length, skip, limit };
    });
  });

  it('loads one page with the total and grows the grid downwards on Show more', async () => {
    db = Array.from({ length: 13 }, (_, index) => note(`n${index}`, index + 1));
    await renderAt('/love-notes?tab=inbox');
    await screen.findByText('love note n12');

    // Twelve on screen, one still on the server, and the tab counts the inbox.
    expect(screen.getByText(/Inbox \(13\)/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Show more love notes' })).toHaveTextContent(
      'Show more (1 remaining)',
    );
    expect(screen.queryByText('love note n0')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Show more love notes' }));
    await screen.findByText('love note n0');

    // The second read carried the offset of what was shown; nothing was re-read.
    const skips = api.getReceivedLoveNotes.mock.calls.map((call) => call[0]?.skip ?? 0);
    expect(skips).toContain(12);
    expect(screen.getByText('love note n12')).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Show more love notes' }),
    ).not.toBeInTheDocument();
  });

  it('finds a deep-linked note past the first page and opens it', async () => {
    db = Array.from({ length: 13 }, (_, index) => note(`n${index}`, index + 1));
    await renderAt('/love-notes?tab=inbox&note=n0');

    // Oldest of thirteen: two further pages down, then open.
    await waitFor(() => expect(viewerShowing('http://example.test/n0.png')).not.toBeNull());
    const skips = api.getReceivedLoveNotes.mock.calls.map((call) => call[0]?.skip ?? 0);
    expect(skips).toContain(12);
  });

  it('stops searching when the deep-linked note is gone instead of fetching forever', async () => {
    db = Array.from({ length: 13 }, (_, index) => note(`n${index}`, index + 1));
    await renderAt('/love-notes?tab=inbox&note=note-does-not-exist');

    // The oldest page arriving ends the search: thirteen notes render, nothing opens,
    // and no fourth read goes out. The mount read and the one chained read are the
    // only two -- the tab effect never duplicates the first page.
    await screen.findByText('love note n0');
    expect(api.getReceivedLoveNotes.mock.calls.length).toBe(2);
    expect(viewerShowing('http://example.test/n0.png')).toBeNull();
    expect(screen.getByText('love note n12')).toBeInTheDocument();
  });

  it('still renders legacy bare-array answers with no pager', async () => {
    api.getReceivedLoveNotes.mockResolvedValue([note('n1', 1), note('n2', 2)]);

    await renderAt('/love-notes?tab=inbox');
    await screen.findByText('love note n1');

    expect(screen.getByText('love note n2')).toBeInTheDocument();
    expect(screen.getByText(/Inbox \(2\)/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /show more/i })).not.toBeInTheDocument();
  });

  it('reads an empty inbox once and then stops', async () => {
    await renderAt('/love-notes?tab=inbox');
    await screen.findByText(/no love notes yet/i);

    // An empty inbox is a steady state: give any refetch loop time to fire more
    // reads, then require the mount read to still be the only one.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(api.getReceivedLoveNotes.mock.calls.length).toBe(1);
  });

  it('revisiting an empty inbox re-reads once and then stops', async () => {
    await renderAt('/love-notes');
    await screen.findByText('Inbox (0)');
    expect(api.getReceivedLoveNotes.mock.calls.length).toBe(1);

    // Opening the tab refreshes the empty list in case a note arrived meanwhile.
    fireEvent.click(screen.getByRole('button', { name: /inbox \(0\)/i }));
    await screen.findByText(/no love notes yet/i);
    expect(api.getReceivedLoveNotes.mock.calls.length).toBe(2);

    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(api.getReceivedLoveNotes.mock.calls.length).toBe(2);
  });
});
