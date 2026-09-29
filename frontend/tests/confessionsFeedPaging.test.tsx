import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { ConfessionsPage } from '@/pages/ConfessionsPage';

/**
 * Paging the participant confession feed through the paginated endpoint.
 *
 * Three properties are pinned here, each failing in a way the old fetch-everything
 * feed could not have caught:
 *
 * * **The feed grows downwards.** The screen holds one contiguous window with the
 *   server total behind it, and "Show more" appends the next page beneath the posts
 *   already on screen -- the request carries the offset of what is shown.
 * * **The sort picker re-asks from the top.** Switching sort re-reads page one in the
 *   new order; the previous offset may not exist under it, and staying on it would
 *   show an empty feed for a question that has answers.
 * * **Posting re-reads the head.** A new confession belongs at the head of a
 *   time-ordered feed but anywhere in the others, so posting re-reads the first page
 *   instead of prepending -- prepending would put it first under every sort.
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
}));

vi.mock('@/services/api', () => api);
vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn() },
  Toaster: () => null,
}));

type FeedRow = {
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

/** The in-test stand-in for the visible-posts table, served the way the endpoint serves. */
let db: FeedRow[];

const stamp = (day: number) => `2026-04-${String(day).padStart(2, '0')}T10:00:00.000Z`;

const post = (id: string, body: string, overrides: Partial<FeedRow> = {}): FeedRow => ({
  id,
  body,
  recipient_name: null,
  anonymous: true,
  comments_enabled: true,
  author_id: 'author-1',
  user_info: { id: 'author-1', name: 'Ada' },
  created_at: stamp(10),
  comments: [],
  heart_count: 0,
  haha_count: 0,
  whoa_count: 0,
  heartbreak_count: 0,
  comment_count: 0,
  user_reaction: null,
  report_count: 0,
  reported_by: [],
  ...overrides,
});

const engagementOf = (row: FeedRow) =>
  row.heart_count + row.haha_count + row.whoa_count + row.heartbreak_count;

const serveFeed = async (sortBy: string = 'time', params: Record<string, unknown> = {}) => {
  const skip = (params.skip as number | undefined) ?? 0;
  const limit = (params.limit as number | undefined) ?? 10;
  const rows = [...db].sort((a, b) => {
    if (sortBy === 'popularity') {
      return engagementOf(b) - engagementOf(a) || a.created_at.localeCompare(b.created_at);
    }
    if (sortBy === 'comments') {
      return b.comment_count - a.comment_count || a.created_at.localeCompare(b.created_at);
    }
    return b.created_at.localeCompare(a.created_at);
  });
  return { confessions: rows.slice(skip, skip + limit), total: rows.length, skip, limit };
};

async function renderFeed() {
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
  // The feed arrives over an effect, so no card exists until the first page lands.
  await waitFor(() => {
    expect(document.querySelector('.confession-card')).not.toBeNull();
  });
}

describe('ConfessionsPage: paged feed', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    db = [];
    api.getConfessions.mockImplementation((sortBy: string = 'time', params?: Record<string, unknown>) =>
      serveFeed(sortBy, params ?? {}),
    );
    api.getConfessionCooldown.mockResolvedValue({ status: 'eligible', message: 'Proceed?' });
  });

  it('loads one page with the total, then grows downwards on Show more', {
    // Two page renders plus dialog-adjacent polling: under a loaded parallel run this
    // legitimately exceeds the 5s default, so the budget -- not the assertions -- grows.
    timeout: 15000,
  }, async () => {
    db = Array.from({ length: 11 }, (_, index) =>
      post(`c${index}`, `feed post number ${index}`, { created_at: stamp(index + 1) }),
    );
    await renderFeed();
    await screen.findByText('feed post number 10');

    // Ten on screen, one still on the server, and the button says exactly that.
    expect(screen.getByRole('button', { name: 'Show more confessions' })).toHaveTextContent(
      'Show more (1 remaining)',
    );

    fireEvent.click(screen.getByRole('button', { name: 'Show more confessions' }));
    await screen.findByText('feed post number 0');

    // The second read carried the offset of what was shown; nothing was re-read.
    const skips = api.getConfessions.mock.calls.map((call) => call[1]?.skip ?? 0);
    expect(skips).toContain(10);
    // Awaited rather than read synchronously: the appended page and the first page
    // can land in separate commits under load.
    expect(await screen.findByText('feed post number 10')).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Show more confessions' }),
    ).not.toBeInTheDocument();
  });

  it('re-asks from the first page when the sort changes', async () => {
    db = [
      post('c1', 'quiet post', { heart_count: 0, created_at: stamp(3) }),
      post('c2', 'loved post', { heart_count: 9, created_at: stamp(9) }),
    ];
    await renderFeed();
    await screen.findByText('quiet post');

    fireEvent.click(screen.getByRole('button', { name: /popularity/i }));
    await screen.findByText('loved post');

    // The new order went out, reset to the first page rather than paging the old one.
    const calls = api.getConfessions.mock.calls.map((call) => ({
      sort: call[0],
      skip: call[1]?.skip ?? 0,
    }));
    expect(calls).toContainEqual({ sort: 'popularity', skip: 0 });
    expect(screen.getByText('loved post')).toBeInTheDocument();
  });

  it('re-reads the head after posting instead of prepending under every sort', async () => {
    db = [post('c1', 'first post')];
    api.createConfession.mockImplementation(async (payload: { body: string }) => {
      const row = post('c2', payload.body, { created_at: stamp(20) });
      db = [row, ...db];
      return row;
    });
    await renderFeed();
    await screen.findByText('first post');

    fireEvent.click(screen.getByRole('button', { name: /share your heart/i }));
    fireEvent.change(screen.getByPlaceholderText(/Share what's in your heart/), {
      target: { value: 'a brand new confession' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^share confession$/i }));
    await screen.findByText('a brand new confession');

    expect(api.createConfession).toHaveBeenCalled();
    // A fresh first-page read followed the write; the post was not spliced in by hand.
    const skips = api.getConfessions.mock.calls.map((call) => call[1]?.skip ?? 0);
    expect(skips.filter((skip) => skip === 0).length).toBeGreaterThan(1);
  });

  it('still renders legacy bare-array answers with no pager', async () => {
    api.getConfessions.mockResolvedValue([post('c1', 'legacy post')]);

    await renderFeed();

    expect(screen.getByText('legacy post')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /show more/i })).not.toBeInTheDocument();
  });
});

describe('ConfessionsPage: thread ordering', () => {
  const comment = (id: string, body: string, likes = 0, dislikes = 0) => ({
    id,
    body,
    user_info: { id: 'u2', name: 'Commenter' },
    created_at: '2026-04-11T10:00:00.000Z',
    like_count: likes,
    dislike_count: dislikes,
    user_reaction: null,
    report_count: 0,
    reported_by: [],
  });

  /** The thread mock, ranking the way the endpoint ranks when asked. */
  const serveThread = (params: Record<string, unknown> = {}) => {
    const sort = (params.sort as string | undefined) ?? 'latest';
    const skip = (params.skip as number | undefined) ?? 0;
    const limit = (params.limit as number | undefined) ?? 10;
    const thread = [...(db[0]?.comments as ReturnType<typeof comment>[])];
    if (sort === 'relevant') {
      const share = (row: ReturnType<typeof comment>) =>
        row.like_count + row.dislike_count === 0
          ? 0
          : row.like_count / (row.like_count + row.dislike_count);
      thread.sort(
        (a, b) => share(b) - share(a) || b.like_count - a.like_count,
      );
    }
    return {
      comments: thread.slice(skip, skip + limit),
      total: thread.length,
      skip,
      limit,
    };
  };

  /** The feed mock, narrowed to the opening window the endpoint embeds per thread. */
  const serveWindowedFeed = async (sortBy: string = 'time', params: Record<string, unknown> = {}) => {
    const page = await serveFeed(sortBy, params);
    return {
      ...page,
      confessions: page.confessions.map((row) => ({
        ...row,
        comments: (row.comments as unknown[]).slice(0, 10),
      })),
    };
  };

  const bodiesInDialog = () =>
    screen
      .getAllByText(/unanimously|divisive remark|quiet remark|reply number \d+|fresh reply/)
      .map((element) => element.textContent);

  beforeEach(() => {
    vi.clearAllMocks();
    db = [];
    api.getConfessions.mockImplementation(serveWindowedFeed);
    api.getConfessionCooldown.mockResolvedValue({ status: 'eligible', message: 'Proceed?' });
    api.getConfessionComments.mockImplementation(
      async (_confessionId: string, params: Record<string, unknown> = {}) =>
        serveThread(params),
    );
    api.createComment.mockImplementation(async (_confessionId: string, body: string) => {
      const row = comment(`m-new-${db.length}`, body);
      (db[0]?.comments as ReturnType<typeof comment>[]).push(row);
      return row;
    });
  });

  it('re-ranks the thread from page one when Relevant is picked', async () => {
    db = [
      {
        ...post('c1', 'a judged thread'),
        comments: [
          comment('m1', 'quiet remark'),
          comment('m2', 'divisive remark', 6, 4),
          comment('m3', 'loved unanimously', 3, 0),
        ],
        comment_count: 3,
      },
    ];
    await renderFeed();

    fireEvent.click(screen.getByRole('button', { name: /a judged thread/ }));
    await screen.findByText('quiet remark');

    fireEvent.click(screen.getByRole('button', { name: 'Relevant' }));
    await waitFor(() => {
      expect(bodiesInDialog()).toEqual([
        'loved unanimously',
        'divisive remark',
        'quiet remark',
      ]);
    });

    // Three likes and no dislikes outrank six likes and four dislikes: the order went
    // out with the request rather than shuffling the loaded window.
    expect(api.getConfessionComments).toHaveBeenCalledWith(
      'c1',
      expect.objectContaining({ skip: 0, sort: 'relevant' }),
    );
  });

  it('carries the sort onto Show more pages', async () => {
    // More likes the later the reply: relevant order runs against insertion order.
    const thread = Array.from({ length: 12 }, (_, index) =>
      comment(`m${index}`, `reply number ${index}`, index, 0),
    );
    db = [{ ...post('c1', 'a long ranked thread'), comments: thread, comment_count: 12 }];
    await renderFeed();

    fireEvent.click(screen.getByRole('button', { name: /a long ranked thread/ }));
    await screen.findByText('reply number 0');
    fireEvent.click(screen.getByRole('button', { name: 'Relevant' }));
    await screen.findByText('reply number 11');

    fireEvent.click(screen.getByRole('button', { name: 'Show more comments' }));
    await screen.findByText('reply number 1');

    const calls = api.getConfessionComments.mock.calls.map((call) => call[1]);
    expect(calls).toContainEqual(expect.objectContaining({ skip: 10, sort: 'relevant' }));
    expect(screen.queryByRole('button', { name: 'Show more comments' })).not.toBeInTheDocument();
  });

  it('refetches the ranked window after posting instead of prepending', async () => {
    db = [
      {
        ...post('c1', 'a judged thread'),
        comments: [comment('m1', 'loved unanimously', 3, 0)],
        comment_count: 1,
      },
    ];
    await renderFeed();

    fireEvent.click(screen.getByRole('button', { name: /a judged thread/ }));
    await screen.findByText('loved unanimously');
    fireEvent.click(screen.getByRole('button', { name: 'Relevant' }));
    await waitFor(() => {
      expect(api.getConfessionComments).toHaveBeenCalledWith(
        'c1',
        expect.objectContaining({ sort: 'relevant' }),
      );
    });

    fireEvent.change(screen.getByPlaceholderText(/Write a thoughtful comment/), {
      target: { value: 'a fresh reply' },
    });
    fireEvent.click(screen.getByRole('button', { name: /send comment/i }));
    await screen.findByText('a fresh reply');

    // Ranked position, not top: the window was re-read in the current order.
    expect(api.getConfessionComments.mock.calls.length).toBeGreaterThan(1);
    expect(screen.getByText(/Comments \(2\)/)).toBeInTheDocument();
  });
});

describe('ConfessionsPage: paged threads', () => {
  const comment = (id: string, body: string) => ({
    id,
    body,
    user_info: { id: 'u2', name: 'Commenter' },
    created_at: '2026-04-11T10:00:00.000Z',
    like_count: 0,
    dislike_count: 0,
    user_reaction: null,
    report_count: 0,
    reported_by: [],
  });

  beforeEach(() => {
    vi.clearAllMocks();
    db = [];
    api.getConfessions.mockImplementation((sortBy: string = 'time', params?: Record<string, unknown>) =>
      serveFeed(sortBy, params ?? {}),
    );
    api.getConfessionCooldown.mockResolvedValue({ status: 'eligible', message: 'Proceed?' });
    api.getConfessionComments.mockImplementation(
      async (confessionId: string, params: Record<string, unknown> = {}) => {
        const skip = (params.skip as number | undefined) ?? 0;
        const limit = (params.limit as number | undefined) ?? 10;
        const row = db.find((candidate) => candidate.id === confessionId);
        const thread = (row?.comments ?? []) as ReturnType<typeof comment>[];
        return {
          comments: thread.slice(skip, skip + limit),
          total: thread.length,
          skip,
          limit,
        };
      },
    );
  });

  /** The feed mock, narrowed to the opening window the endpoint embeds per thread. */
  const serveWindowedFeed = async (sortBy: string = 'time', params: Record<string, unknown> = {}) => {
    const page = await serveFeed(sortBy, params);
    return {
      ...page,
      confessions: page.confessions.map((row) => ({
        ...row,
        comments: (row.comments as unknown[]).slice(0, 10),
      })),
    };
  };

  it('opens with a bounded window and grows the thread downwards on Show more', async () => {
    const thread = Array.from({ length: 12 }, (_, index) =>
      comment(`m${index}`, `reply number ${index}`),
    );
    // Newest first, as the endpoint answers; the count stays true beside the window.
    db = [{ ...post('c1', 'a discussed post'), comments: thread, comment_count: 12 }];
    api.getConfessions.mockImplementation(serveWindowedFeed);
    await renderFeed();

    // The body button opens the dialog onto the thread.
    fireEvent.click(screen.getByRole('button', { name: /a discussed post/ }));
    await screen.findByText('reply number 0');

    expect(screen.getByRole('button', { name: 'Show more comments' })).toHaveTextContent(
      'Show more (2 remaining)',
    );
    expect(screen.queryByText('reply number 10')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Show more comments' }));
    await screen.findByText('reply number 10');

    // The thread read carried the offset of what the dialog was showing.
    expect(api.getConfessionComments).toHaveBeenCalledWith(
      'c1',
      expect.objectContaining({ skip: 10 }),
    );
    expect(screen.getByText('reply number 11')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Show more comments' })).not.toBeInTheDocument();
  });

  it('keeps the appended page when the dialog closes and reopens', async () => {
    const thread = Array.from({ length: 11 }, (_, index) =>
      comment(`m${index}`, `reply number ${index}`),
    );
    db = [{ ...post('c1', 'a discussed post'), comments: thread, comment_count: 11 }];
    api.getConfessions.mockImplementation(serveWindowedFeed);
    await renderFeed();

    fireEvent.click(screen.getByRole('button', { name: /a discussed post/ }));
    await screen.findByText('reply number 0');
    fireEvent.click(screen.getByRole('button', { name: 'Show more comments' }));
    await screen.findByText('reply number 10');

    // Away and back: the feed row kept the appended page, so the dialog reopens whole.
    fireEvent.click(screen.getByRole('button', { name: /close/i }));
    fireEvent.click(screen.getByRole('button', { name: /a discussed post/ }));
    await screen.findByText('reply number 10');
    expect(
      screen.queryByRole('button', { name: 'Show more comments' }),
    ).not.toBeInTheDocument();
  });
});
