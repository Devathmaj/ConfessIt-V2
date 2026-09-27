import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { LoveNotesReview } from '@/pages/LoveNotesReview';

/**
 * Moderating love notes, page by page.
 *
 * Three properties are pinned here, each failing in a way the old fetch-everything
 * screen could not have caught:
 *
 * * **Sections grow downwards.** Each moderation state holds its own contiguous window
 *   and a "Show more" click appends the next page beneath the notes already on screen --
 *   the reader's place never moves, and the request carries the offset of what is shown.
 * * **Latest/oldest orders the timeline in SQL.** Flipping the order re-reads every
 *   section with the new direction; the oldest arrival comes first under "Oldest first".
 * * **An action refreshes what it touched.** Approving, rejecting or hiding re-reads the
 *   source and destination windows, so the moved note lands in its new section with true
 *   totals beside it instead of sitting patched in the old one.
 */

const api = vi.hoisted(() => ({
  getAdminLoveNotes: vi.fn(),
  updateAdminLoveNoteStatus: vi.fn(),
  deleteAdminLoveNote: vi.fn(),
}));

const toast = vi.hoisted(() => ({
  info: vi.fn(),
  error: vi.fn(),
  success: vi.fn(),
  warning: vi.fn(),
}));

vi.mock('@/services/api', () => api);
vi.mock('sonner', () => ({ toast, Toaster: () => null }));

type NoteRow = {
  id: string;
  body: string;
  status: 'pending_review' | 'approved' | 'rejected';
  created_at: string;
};

/** The in-test stand-in for the table: reads filter, order and slice it like the endpoint. */
let db: NoteRow[];

const note = (
  id: string,
  body: string,
  status: NoteRow['status'],
  createdAt: string,
): NoteRow => ({ id, body, status, created_at: createdAt });

const stamp = (day: number) => `2026-01-${String(day).padStart(2, '0')}T10:00:00.000Z`;

const serveLoveNotes = async (params: Record<string, unknown> = {}) => {
  const status = (params.status as string | undefined) ?? 'all';
  const order = (params.order as string | undefined) ?? 'newest';
  const skip = (params.skip as number | undefined) ?? 0;
  const limit = (params.limit as number | undefined) ?? 20;
  const rows = db
    .filter((row) => status === 'all' || row.status === status)
    .sort((a, b) =>
      order === 'newest'
        ? b.created_at.localeCompare(a.created_at)
        : a.created_at.localeCompare(b.created_at),
    );
  return { notes: rows.slice(skip, skip + limit), total: rows.length, skip, limit };
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
        <LoveNotesReview />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
  await screen.findByText(/Total notes:/);
}

const groupNamed = (heading: string) =>
  screen.getByRole('heading', { name: heading }).closest('section') as HTMLElement;

/**
 * The collapsible note triggers of one section, in the order the section shows them.
 *
 * Bodies live inside `AccordionContent`, which Radix unmounts while its item is shut and --
 * the accordion opens one item at a time -- unmounts again when the next opens. So order
 * is read off the triggers, which are always mounted and carry the arrival stamp, and a
 * body is read by opening its own trigger first. The "Show more" button carries no
 * `aria-expanded` and never leaks in here.
 */
const triggersIn = (section: HTMLElement) =>
  within(section)
    .queryAllByRole('button')
    .filter((button) => button.hasAttribute('aria-expanded'));

/**
 * Open the trigger at `index` and return the message body it reveals.
 *
 * Left alone when already open: the accordion toggles, so clicking an open trigger
 * would shut the very content being read -- exactly what happens when a re-read lands
 * while its trigger is still expanded.
 */
const openBodyAt = (section: HTMLElement, index: number, body: string) => {
  const trigger = triggersIn(section)[index];
  if (trigger.getAttribute('aria-expanded') === 'false') fireEvent.click(trigger);
  return within(section).getByText(body);
};

/** Picks `option` from the Radix select labelled `label`, via the pointer dance it needs. */
async function choose(label: string, option: string) {
  fireEvent.pointerDown(screen.getByLabelText(label), {
    button: 0,
    ctrlKey: false,
    pointerType: 'mouse',
  });
  fireEvent.click(await screen.findByRole('option', { name: option }));
}

describe('LoveNotesReview: paged moderation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    db = [];
    api.getAdminLoveNotes.mockImplementation(serveLoveNotes);
    api.updateAdminLoveNoteStatus.mockImplementation(async (noteId: string, status: NoteRow['status']) => {
      const row = db.find((candidate) => candidate.id === noteId);
      if (row) row.status = status;
      return { note_id: noteId, status };
    });
  });

  it('loads one page per moderation state with server totals beside each', async () => {
    db = [
      note('p1', 'pending words', 'pending_review', stamp(3)),
      note('a1', 'accepted words', 'approved', stamp(2)),
      note('r1', 'rejected words', 'rejected', stamp(1)),
    ];
    await renderPage();

    // One bounded read per section, each carrying its own filter.
    expect(api.getAdminLoveNotes).toHaveBeenCalledTimes(3);
    const statuses = api.getAdminLoveNotes.mock.calls.map((call) => call[0]?.status);
    expect(statuses).toEqual(expect.arrayContaining(['pending_review', 'approved', 'rejected']));

    expect(triggersIn(groupNamed('Pending review'))).toHaveLength(1);
    expect(triggersIn(groupNamed('Accepted notes'))).toHaveLength(1);
    expect(triggersIn(groupNamed('Rejected notes'))).toHaveLength(1);
    expect(openBodyAt(groupNamed('Pending review'), 0, 'pending words')).toHaveTextContent('pending words');
    expect(openBodyAt(groupNamed('Accepted notes'), 0, 'accepted words')).toHaveTextContent('accepted words');
    expect(openBodyAt(groupNamed('Rejected notes'), 0, 'rejected words')).toHaveTextContent('rejected words');
    expect(screen.getByText(/Total notes: 3/)).toBeInTheDocument();
    expect(screen.getByText(/Pending decisions: 1/)).toBeInTheDocument();
  });

  it('grows a section downwards on Show more without moving the notes already shown', async () => {
    db = Array.from({ length: 21 }, (_, index) =>
      note(`p${index}`, `pending ${index}`, 'pending_review', stamp(1 + (index % 28))),
    );
    await renderPage();

    // Twenty on screen, one still on the server, and the button says exactly that.
    expect(screen.getByRole('button', { name: 'Show more pending review' })).toHaveTextContent(
      'Show more (1 remaining)',
    );

    expect(triggersIn(groupNamed('Pending review'))).toHaveLength(20);

    fireEvent.click(screen.getByRole('button', { name: 'Show more pending review' }));
    await screen.findByText(/21 of 21 notes/);

    // The second read carried the offset of what was shown; nothing was re-read.
    const skips = api.getAdminLoveNotes.mock.calls.map((call) => call[0]?.skip ?? 0);
    expect(skips).toContain(20);
    expect(triggersIn(groupNamed('Pending review'))).toHaveLength(21);
    // Newest first, so the appended oldest note sits beneath the twenty already shown.
    expect(openBodyAt(groupNamed('Pending review'), 0, 'pending 20')).toHaveTextContent('pending 20');
    expect(
      screen.queryByRole('button', { name: 'Show more pending review' }),
    ).not.toBeInTheDocument();
  });

  it('orders the timeline oldest-first when the order filter says so', async () => {
    db = [
      note('n1', 'newest words', 'pending_review', stamp(10)),
      note('n2', 'oldest words', 'pending_review', stamp(1)),
    ];
    await renderPage();
    expect(openBodyAt(groupNamed('Pending review'), 0, 'newest words')).toHaveTextContent('newest words');

    await choose('Order', 'Oldest first');

    const orders = api.getAdminLoveNotes.mock.calls.map((call) => call[0]?.order);
    expect(orders).toContain('oldest');
    // The re-read lands asynchronously: until it does the old trigger is still expanded,
    // and clicking it would shut it instead of opening the new order. Wait for the flip.
    await waitFor(() => {
      expect(triggersIn(groupNamed('Pending review'))[0]).toHaveTextContent('01/01/2026');
    });
    expect(openBodyAt(groupNamed('Pending review'), 0, 'oldest words')).toHaveTextContent('oldest words');
  });

  it('refetches what an approval touched so the note lands in its new section', async () => {
    db = [note('n1', 'borderline words', 'pending_review', stamp(5))];
    await renderPage();
    expect(openBodyAt(groupNamed('Pending review'), 0, 'borderline words')).toHaveTextContent('borderline words');

    // The ruling lives inside the opened note, beside the words it judges.
    fireEvent.click(
      within(groupNamed('Pending review')).getByRole('button', { name: /approve/i }),
    );
    await screen.findByText(/Pending decisions: 0/);

    expect(api.updateAdminLoveNoteStatus).toHaveBeenCalledWith('n1', 'approved');
    // The reads went out again after the ruling: the moved note was fetched, not patched.
    expect(api.getAdminLoveNotes.mock.calls.length).toBeGreaterThan(3);
    expect(triggersIn(groupNamed('Pending review'))).toHaveLength(0);
    expect(openBodyAt(groupNamed('Accepted notes'), 0, 'borderline words')).toHaveTextContent('borderline words');
    expect(screen.getByText(/Total notes: 1/)).toBeInTheDocument();
  });

  it('still splits a legacy bare-array answer by status with no pager', async () => {
    api.getAdminLoveNotes.mockResolvedValue([
      { id: 'p1', body: 'pending words', status: 'pending_review', created_at: stamp(3) },
      { id: 'a1', body: 'accepted words', status: 'approved', created_at: stamp(2) },
    ]);

    await renderPage();

    expect(openBodyAt(groupNamed('Pending review'), 0, 'pending words')).toHaveTextContent('pending words');
    expect(openBodyAt(groupNamed('Accepted notes'), 0, 'accepted words')).toHaveTextContent('accepted words');
    expect(screen.queryByRole('button', { name: /show more/i })).not.toBeInTheDocument();
  });
});
