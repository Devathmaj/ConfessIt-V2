import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { ConfessionsAdmin } from '@/pages/ConfessionsAdmin';

/**
 * Paging the confession review screen through the paginated endpoint.
 *
 * Three properties are pinned here, each failing in a way the old fetch-everything
 * screen could not have caught:
 *
 * * **Search is submitted, not typed.** The box is a draft; the Search button (or Enter)
 *   submits the query the database answers, so a half-typed word never costs a thread
 *   read. Typing alone fires nothing.
 * * **Filters and sorts travel as params.** The submitted query, the filter picker and
 *   the sort picker go out with every read, and the page on screen is displayed as
 *   returned -- never narrowed or reordered a second time, which would hide rows the
 *   server correctly sent and break the totals beside them.
 * * **The list grows downwards and actions refresh it.** "Show more" appends the next
 *   page beneath the confessions already on screen, and hiding a confession re-reads
 *   the window so the row comes back flagged instead of sitting patched.
 */

const api = vi.hoisted(() => ({
  getAdminConfessions: vi.fn(),
  searchAdminConfessions: vi.fn(),
  hideAdminConfession: vi.fn(),
  hideAdminComment: vi.fn(),
}));

const toast = vi.hoisted(() => ({
  info: vi.fn(),
  error: vi.fn(),
  success: vi.fn(),
  warning: vi.fn(),
}));

vi.mock('@/services/api', () => api);
vi.mock('sonner', () => ({ toast, Toaster: () => null }));

type ConfessionRow = {
  id: string;
  body: string;
  recipient_name: string | null;
  anonymous: boolean;
  sender: { id: string | null; name: string | null; regno: string | null; email: string | null };
  report_count: number;
  comment_count: number;
  heart_count: number;
  created_at: string;
  comments: unknown[];
  content_removed?: boolean;
};

/** The in-test stand-in for the table: filters, sorts and slices like the endpoint. */
let db: ConfessionRow[];

const stamp = (day: number) => `2026-02-${String(day).padStart(2, '0')}T10:00:00.000Z`;

const confession = (
  id: string,
  body: string,
  overrides: Partial<ConfessionRow> = {},
): ConfessionRow => ({
  id,
  body,
  recipient_name: 'Someone',
  anonymous: false,
  sender: { id: 'u1', name: 'Ada Author', regno: 'R100', email: 'ada@school.test' },
  report_count: 0,
  comment_count: 0,
  heart_count: 0,
  created_at: stamp(10),
  comments: [],
  ...overrides,
});

const engagementOf = (row: ConfessionRow) => row.heart_count;

const serveConfessions = async (params: Record<string, unknown> = {}) => {
  const q = ((params.q as string | undefined) ?? '').trim().toLowerCase();
  const filter = (params.filter as string | undefined) ?? 'all';
  const sort = (params.sort as string | undefined) ?? 'recent';
  const skip = (params.skip as number | undefined) ?? 0;
  const limit = (params.limit as number | undefined) ?? 10;
  let rows = db.filter((row) => {
    if (q) {
      const senderHaystack = row.anonymous
        ? ''
        : `${row.sender.name ?? ''} ${row.sender.regno ?? ''} ${row.sender.email ?? ''}`;
      const haystack = `${row.body} ${row.recipient_name ?? ''} ${senderHaystack}`.toLowerCase();
      if (!haystack.includes(q)) return false;
    }
    if (filter === 'reported' && row.report_count <= 0) return false;
    if (filter === 'anonymous' && !row.anonymous) return false;
    if (filter === 'with-comments' && row.comments.length === 0) return false;
    return true;
  });
  rows = [...rows].sort((a, b) => {
    if (sort === 'reports') return b.report_count - a.report_count;
    if (sort === 'comments') return b.comment_count - a.comment_count;
    if (sort === 'reactions') return engagementOf(b) - engagementOf(a);
    return b.created_at.localeCompare(a.created_at);
  });
  return { confessions: rows.slice(skip, skip + limit), total: rows.length, skip, limit };
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
        <ConfessionsAdmin />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
  await screen.findByText(/Total confessions:/);
}

/** Open the first collapsed confession row, like a reviewer triaging down the list. */
async function expandFirstConfession() {
  const [trigger] = await screen.findAllByRole('button', { expanded: false });
  fireEvent.click(trigger);
  await screen.findAllByRole('button', { expanded: true });
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

const callsWith = () =>
  api.getAdminConfessions.mock.calls.map((call) => call[0] as Record<string, unknown>);

const hybridCallsWith = () =>
  api.searchAdminConfessions.mock.calls.map((call) => call[0] as Record<string, unknown>);

/** Semantic status the hybrid mock answers with; tests set it before submitting. */
let hybridStatus = 'ok';

/** Hybrid stand-in: the keyword page plus meaning rows mentioning a marker token.
 * A degraded status carries no meaning rows, exactly like the server, which
 * makes no provider call when it degrades. */
const serveHybrid = async (params: Record<string, unknown> = {}) => {
  const skip = (params.skip as number | undefined) ?? 0;
  const limit = (params.limit as number | undefined) ?? 10;
  const page = await serveConfessions(params);
  const keywordIds = new Set(page.confessions.map((row) => row.id));
  const semantic =
    hybridStatus === 'ok'
      ? db
          .filter((row) => !keywordIds.has(row.id) && row.body.includes('semantic-echo'))
          .slice(0, 10)
      : [];
  return {
    query: params.q ?? '',
    filter: params.filter ?? 'all',
    sort: params.sort ?? 'recent',
    skip,
    limit,
    keyword: { items: page.confessions, total: page.total, skip, limit },
    semantic: { items: semantic, status: hybridStatus },
  };
};

describe('ConfessionsAdmin: server-driven search, filters and paging', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    db = [];
    hybridStatus = 'ok';
    api.getAdminConfessions.mockImplementation(serveConfessions);
    api.searchAdminConfessions.mockImplementation(serveHybrid);
    api.hideAdminConfession.mockImplementation(async (confessionId: string) => {
      // The endpoint stamps the row: the refetch below must find it hidden.
      const row = db.find((candidate) => candidate.id === confessionId);
      if (row) row.content_removed = true;
      return { id: confessionId, content_removed: true };
    });
  });

  it('fires nothing while typing; the Search button submits the query the database answers', async () => {
    db = [
      confession('c1', 'the lighthouse keeper'),
      confession('c2', 'an unrelated post'),
    ];
    await renderPage();
    await screen.findByText('the lighthouse keeper');

    // The first page loads unfiltered, and the draft costs no reads at all.
    expect(api.getAdminConfessions).toHaveBeenCalledTimes(1);
    fireEvent.change(screen.getByPlaceholderText(/Search by confession text/), {
      target: { value: 'lighthouse' },
    });
    expect(api.getAdminConfessions).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'Search confessions' }));
    await waitFor(() => {
      expect(screen.queryByText('an unrelated post')).not.toBeInTheDocument();
    });

    // A submitted query takes the hybrid endpoint: keyword page plus meaning.
    expect(hybridCallsWith().some((params) => params.q === 'lighthouse')).toBe(true);
    expect(screen.getByText('the lighthouse keeper')).toBeInTheDocument();
    expect(screen.getByText(/Showing 1 of 1/)).toBeInTheDocument();
  });

  it('shows keyword matches first and similar-in-meaning below, deduped', async () => {
    db = [
      confession('c1', 'the lighthouse keeper semantic-echo tale'),
      confession('c2', 'semantic-echo midnight thoughts'),
      confession('c3', 'an unrelated post'),
    ];
    await renderPage();
    await screen.findByText('the lighthouse keeper semantic-echo tale');

    fireEvent.change(screen.getByPlaceholderText(/Search by confession text/), {
      target: { value: 'lighthouse' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Search confessions' }));

    await screen.findByText('Keyword matches');
    await screen.findByText('Similar in meaning');
    // c1 matched the keyword, so it renders once overall, not once per section.
    // (Each review row shows its body once: the collapsed trigger line.)
    expect(screen.getAllByText('the lighthouse keeper semantic-echo tale')).toHaveLength(1);
    expect(screen.getByText('semantic-echo midnight thoughts')).toBeInTheDocument();
    expect(screen.queryByText('an unrelated post')).not.toBeInTheDocument();
  });

  it('shows a non-blocking notice when meaning search degrades, keeping keyword results', async () => {
    hybridStatus = 'rate_limited';
    db = [
      confession('c1', 'the lighthouse keeper'),
      confession('c2', 'semantic-echo midnight thoughts'),
    ];
    await renderPage();
    await screen.findByText('the lighthouse keeper');

    fireEvent.change(screen.getByPlaceholderText(/Search by confession text/), {
      target: { value: 'lighthouse' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Search confessions' }));

    await screen.findByText(/Meaning search is rate-limited/);
    expect(screen.getByText('the lighthouse keeper')).toBeInTheDocument();
    expect(screen.queryByText('Similar in meaning')).not.toBeInTheDocument();
  });

  it('sends the filter and sort pickers to the database and pages the filtered question', async () => {
    db = [
      confession('c1', 'quiet post', { report_count: 0, created_at: stamp(3) }),
      confession('c2', 'reported post', { report_count: 4, created_at: stamp(9) }),
    ];
    await renderPage();
    await screen.findByText('quiet post');

    await choose('Filter', 'Flagged only');

    expect(callsWith().some((params) => params.filter === 'reported')).toBe(true);
    await waitFor(() => {
      expect(screen.queryByText('quiet post')).not.toBeInTheDocument();
    });
    // The page on screen is the filtered page: one matching confession, total one, with
    // no second client-side narrowing hiding anything the server sent.
    expect(screen.getByText('reported post')).toBeInTheDocument();
    expect(screen.getByText(/Showing 1 of 1/)).toBeInTheDocument();
  });

  it('grows the list downwards on Show more without moving the confessions already shown', async () => {
    db = Array.from({ length: 11 }, (_, index) =>
      confession(`c${index}`, `post number ${index}`, { created_at: stamp(index + 1) }),
    );
    await renderPage();
    await screen.findByText(/Showing 10 of 11/);

    fireEvent.click(screen.getByRole('button', { name: 'Show more confessions' }));
    await screen.findByText(/Showing 11 of 11/);

    // The second read carried the offset of what was shown; nothing was re-read.
    const skips = callsWith().map((params) => params.skip ?? 0);
    expect(skips).toContain(10);
    expect(screen.getByText('post number 0')).toBeInTheDocument();
    expect(screen.getByText('post number 10')).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Show more confessions' }),
    ).not.toBeInTheDocument();
  });

  it('refetches the window after hiding so the row comes back flagged', async () => {
    db = [confession('c1', 'take this down')];
    await renderPage();
    await expandFirstConfession();

    fireEvent.click(screen.getByRole('button', { name: /hide confession/i }));
    fireEvent.click(
      within(screen.getByRole('alertdialog')).getByRole('button', { name: /hide confession/i }),
    );
    await screen.findByText(/Already hidden from public view/);

    expect(api.hideAdminConfession).toHaveBeenCalledWith('c1');
    // Hiding stamps the row server-side, so the window was re-read to fetch the newly
    // modified row back instead of patching the card.
    expect(api.getAdminConfessions.mock.calls.length).toBeGreaterThan(1);
    expect(
      screen.queryByRole('button', { name: /hide confession/i }),
    ).not.toBeInTheDocument();
  });

  it('still searches and sorts a legacy bare-array answer on the client', async () => {
    api.getAdminConfessions.mockResolvedValue([
      { ...confession('c1', 'alpha post'), report_count: 0 },
      { ...confession('c2', 'beta post'), report_count: 3 },
    ]);

    await renderPage();
    await screen.findByText('alpha post');

    // Legacy answers ignore request params, so the draft narrows immediately as before.
    fireEvent.change(screen.getByPlaceholderText(/Search by confession text/), {
      target: { value: 'beta' },
    });
    expect(screen.queryByText('alpha post')).not.toBeInTheDocument();
    expect(screen.getByText('beta post')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /show more/i })).not.toBeInTheDocument();
  });
});
