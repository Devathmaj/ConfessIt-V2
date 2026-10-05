import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { ConfessionsPage } from '@/pages/ConfessionsPage';

/**
 * The hybrid search box: one query, keyword matches first and similar-in-meaning
 * below.
 *
 * * **Typing is debounced.** Keystrokes schedule one request, not one per key.
 * * **Stale answers lose.** A slow first request resolving after a newer one
 *   never overwrites what is on screen.
 * * **Sections are labelled and deduped.** A row matching both signals renders
 *   once, under Keyword matches.
 * * **Degradation is a notice, not an error screen.** Keyword results stay up
 *   with one line explaining the missing meaning half.
 */

const api = vi.hoisted(() => ({
  getConfessions: vi.fn(),
  getConfessionComments: vi.fn(),
  createConfession: vi.fn(),
  getConfessionCooldown: vi.fn(),
  reactToConfession: vi.fn(),
  createComment: vi.fn(),
  likeComment: vi.fn(),
  dislikeComment: vi.fn(),
  reportComment: vi.fn(),
  reportConfession: vi.fn(),
  updateConfession: vi.fn(),
  searchConfessions: vi.fn(),
}));

vi.mock('@/services/api', () => api);
vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn() },
  Toaster: () => null,
}));

type HybridRow = {
  id: string;
  body: string;
  recipient_name: string | null;
  anonymous: boolean;
  comments_enabled: boolean;
  author_id: string;
  user_info: { id: string; name: string };
  created_at: string;
  comments: unknown[];
  heart_count: number;
  haha_count: number;
  whoa_count: number;
  heartbreak_count: number;
  comment_count: number;
  user_reaction: string | null;
  report_count: number;
  reported_by: string[];
};

let db: HybridRow[];
let semanticStatus = 'ok';

const row = (id: string, body: string): HybridRow => ({
  id,
  body,
  recipient_name: null,
  anonymous: true,
  comments_enabled: true,
  author_id: 'author-1',
  user_info: { id: 'author-1', name: 'Ada' },
  created_at: '2026-04-10T10:00:00.000Z',
  comments: [],
  heart_count: 0,
  haha_count: 0,
  whoa_count: 0,
  heartbreak_count: 0,
  comment_count: 0,
  user_reaction: null,
  report_count: 0,
  reported_by: [],
});

const serveHybrid = async (query: string, limit = 10) => {
  const q = query.trim().toLowerCase();
  const keyword = db.filter((item) => item.body.toLowerCase().includes(q)).slice(0, limit);
  const keywordIds = new Set(keyword.map((item) => item.id));
  const semantic = db
    .filter((item) => !keywordIds.has(item.id) && item.body.includes('semantic-echo'))
    .slice(0, 10);
  return {
    query,
    limit,
    keyword: { items: keyword, total: keyword.length },
    semantic: { items: semantic, status: semanticStatus },
  };
};

async function renderSearch() {
  render(
    <AuthContext.Provider
      value={{
        user: { id: 'me', name: 'Me', regno: 'CS-9', email: 'me@example.com' },
        isAuthenticated: true,
        loading: false,
        can: () => true,
      } as never}
    >
      <MemoryRouter>
        <ConfessionsPage />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
  await waitFor(() => {
    expect(document.querySelector('.confession-card')).not.toBeNull();
  });
}

describe('ConfessionsPage: hybrid search', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    semanticStatus = 'ok';
    db = [];
    api.getConfessions.mockImplementation(async () => ({
      confessions: [...db],
      total: db.length,
      skip: 0,
      limit: db.length,
    }));
    api.searchConfessions.mockImplementation(serveHybrid);
  });

  it('debounces typing into one request and labels both sections, deduped', async () => {
    db = [
      row('c1', 'the lighthouse keeper semantic-echo tale'),
      row('c2', 'semantic-echo midnight thoughts'),
      row('c3', 'an unrelated post'),
    ];
    await renderSearch();

    const box = screen.getByPlaceholderText(/Search confessions/);
    fireEvent.change(box, { target: { value: 'lighth' } });
    await new Promise((resolve) => setTimeout(resolve, 120));
    fireEvent.change(box, { target: { value: 'lighthouse' } });

    await screen.findByText('Keyword matches');
    await screen.findByText('Similar in meaning');
    // Two keystrokes inside the debounce window cost one request.
    expect(api.searchConfessions).toHaveBeenCalledTimes(1);
    expect(api.searchConfessions).toHaveBeenCalledWith(
      'lighthouse',
      expect.anything(),
      expect.anything(),
    );
    // c1 matched the keyword, so it renders once overall, not once per section.
    expect(screen.getAllByText('the lighthouse keeper semantic-echo tale')).toHaveLength(1);
    expect(screen.getByText('semantic-echo midnight thoughts')).toBeInTheDocument();
    expect(screen.queryByText('an unrelated post')).not.toBeInTheDocument();
  });

  it('drops a stale answer that resolves after a newer search', async () => {
    // The feed holds an unrelated row so result text can only come from search.
    db = [row('c0', 'an unrelated feed post')];
    const first = {
      query: 'first query',
      limit: 10,
      keyword: { items: [row('c1', 'first query match')], total: 1 },
      semantic: { items: [], status: 'ok' },
    };
    const second = {
      query: 'second query',
      limit: 10,
      keyword: { items: [row('c2', 'second query match')], total: 1 },
      semantic: { items: [], status: 'ok' },
    };
    let resolveFirst!: (value: unknown) => void;
    api.searchConfessions.mockImplementation(async (query: string) => {
      if (query === 'first query') {
        return new Promise((resolve) => {
          resolveFirst = resolve;
        });
      }
      return second;
    });
    await renderSearch();

    const box = screen.getByPlaceholderText(/Search confessions/);
    const search = screen.getByRole('button', { name: 'Search' });
    fireEvent.change(box, { target: { value: 'first query' } });
    fireEvent.click(search);
    // The second search starts before the first answers.
    await waitFor(() => expect(api.searchConfessions).toHaveBeenCalledTimes(1));
    fireEvent.change(box, { target: { value: 'second query' } });
    fireEvent.click(search);
    await screen.findByText('second query match');

    // The late first answer must not clobber the screen.
    resolveFirst(first);
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(screen.queryByText('first query match')).not.toBeInTheDocument();
    expect(screen.getByText('second query match')).toBeInTheDocument();
  });

  it('keeps keyword results with a notice when meaning search degrades', async () => {
    semanticStatus = 'rate_limited';
    db = [row('c1', 'the lighthouse keeper')];
    await renderSearch();

    fireEvent.change(screen.getByPlaceholderText(/Search confessions/), {
      target: { value: 'lighthouse' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));

    await screen.findByText(/Meaning search is rate-limited/);
    expect(screen.getByText('the lighthouse keeper')).toBeInTheDocument();
    expect(screen.queryByText('Similar in meaning')).not.toBeInTheDocument();
  });

  it('shows one empty state only when both halves are empty, and Clear restores the feed', async () => {
    db = [row('c1', 'a perfectly ordinary post')];
    await renderSearch();

    fireEvent.change(screen.getByPlaceholderText(/Search confessions/), {
      target: { value: 'nothing matches this' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    await screen.findByText(/No confessions match that yet/);
    expect(screen.queryByText('Keyword matches')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Clear search' }));
    await screen.findByText('a perfectly ordinary post');
    expect(screen.queryByPlaceholderText(/Search confessions/)).toHaveValue('');
  });
});
