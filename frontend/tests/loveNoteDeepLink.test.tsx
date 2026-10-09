import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AuthContext, type User } from '@/contexts/AuthContext';
import { LoveNotesPage } from '@/pages/LoveNotesPage';

/**
 * Following a notification to the love note it is about.
 *
 * The notification links to `/love-notes?tab=inbox&note=<id>`, and the reader clicked it to
 * read the note -- so landing on the inbox with the note still closed would have made the
 * whole feature pointless. The page has to do two things from that URL: switch tab, because
 * the note is listed nowhere else, and open the note, which cannot happen until the inbox
 * list has loaded.
 *
 * That second half is why this is a component test. The ordering between a URL parameter and
 * an async fetch is exactly the thing a pure-function test cannot see, and it is where a deep
 * link silently degrades into "navigated to the right page, showed nothing new".
 */

const api = vi.hoisted(() => ({
  getLoveNoteRecipients: vi.fn(),
  // `getCohorts`, not the `getClasses` this replaced. A mock missing an export the page imports
  // makes the whole `Promise.all` at mount reject, which the page catches and logs -- so the
  // tests still went green while users, cohorts and received notes all silently failed to load.
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

// The page loads its six card templates over `fetch` at mount and draws one onto a canvas.
vi.stubGlobal('fetch', vi.fn(async () => ({ json: async () => [] })));

const note = (over: Record<string, unknown> = {}) => ({
  id: 'note-7',
  from: 'Ada',
  body: 'thinking of you',
  image_url: 'http://example.test/note-7.png',
  anonymous: false,
  created_at: '2026-03-01T12:00:00Z',
  read_at: null,
  ...over,
});

const authValue = {
  user: { id: 'me', name: 'Me', regno: 'CS-9' } as User,
  logout: vi.fn(),
  isAuthenticated: true,
  isLoading: false,
  token: 't' as string | null,
  verifyMagicToken: vi.fn(),
  fetchAndSetUser: vi.fn(),
  can: () => false,
  signInPath: '/login',
};

const renderAt = async (url: string, notes = [note()]) => {
  api.getLoveNoteRecipients.mockResolvedValue({ users: [], total: 0, skip: 0, limit: 20 });
  api.getCohorts.mockResolvedValue([]);
  api.getReceivedLoveNotes.mockResolvedValue(notes);

  render(
    <AuthContext.Provider value={authValue}>
      <MemoryRouter initialEntries={[url]}>
        <LoveNotesPage />
      </MemoryRouter>
    </AuthContext.Provider>
  );

  // The inbox arrives over an effect, so nothing can be asserted before it lands.
  await waitFor(() => expect(api.getReceivedLoveNotes).toHaveBeenCalled());
};

/**
 * The full-screen viewer, identified by the image URL it is showing.
 *
 * Matched on `src` rather than accessible name: the viewer's `<img>` is labelled "Love note",
 * the same label every note in the inbox carries, so the name cannot distinguish the viewer
 * from the card behind it. The card carries the identical `src` too, so all matches are
 * scanned and the one inside the fixed overlay is the viewer -- taking the first match would
 * pick the card and report "not open" for an open note.
 */
const viewer = () => {
  const images = document.querySelectorAll<HTMLImageElement>(
    'img[src="http://example.test/note-7.png"]'
  );
  for (const image of images) {
    const overlay = image.closest('.fixed');
    if (overlay) return overlay as HTMLElement;
  }
  return null;
};

describe('following a love note notification', () => {
  beforeEach(() => vi.clearAllMocks());

  it('opens the note named by ?note=', async () => {
    await renderAt('/love-notes?tab=inbox&note=note-7');

    // Open rather than merely listed: the reader clicked a notification to read something.
    await waitFor(() => expect(viewer()).not.toBeNull());
  });

  it('opens the note even without tab=inbox, since the note lives nowhere else', async () => {
    await renderAt('/love-notes?note=note-7');

    await waitFor(() => expect(viewer()).not.toBeNull());
  });

  it('does not open anything when no note is named', async () => {
    await renderAt('/love-notes?tab=inbox');

    await waitFor(() => expect(screen.getByText('thinking of you')).toBeInTheDocument());
    expect(viewer()).toBeNull();
  });

  it('shows the inbox rather than opening anything for an unknown note id', async () => {
    // A notification can outlive the thing it points at -- the note may since have been
    // removed for a report. Landing on the inbox is the honest outcome; a spinner or an
    // error would be worse, because the note genuinely is not there to read.
    await renderAt('/love-notes?tab=inbox&note=note-does-not-exist', [note()]);

    await waitFor(() => expect(screen.getByText('thinking of you')).toBeInTheDocument());
    expect(viewer()).toBeNull();
  });
});