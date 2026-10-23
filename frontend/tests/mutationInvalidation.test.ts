import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('axios', async () => {
  const callable = Object.assign(
    vi.fn(async () => ({ data: {} })),
    {
      get: vi.fn(async () => ({ data: {} })),
      post: vi.fn(async () => ({ data: {} })),
      put: vi.fn(async () => ({ data: {} })),
      patch: vi.fn(async () => ({ data: {} })),
      delete: vi.fn(async () => ({ data: {} })),
      defaults: {},
      interceptors: { response: { use: vi.fn() } },
    } as never,
  );
  return { default: callable };
});

import axios from 'axios';

import {
  createWordleGame,
  deleteAdminConfession,
  finalizeLeagueOpportunity,
  hideAdminComment,
  submitWordleGuess,
} from '@/services/api';
import { queryClient, queryKeys } from '@/lib/queryClient';

const mockedAxios = vi.mocked(axios);

const feedKey = queryKeys.confessions.feed('time', { skip: 0, limit: 50 });
const commentsKey = queryKeys.confessions.comments('c1', { skip: 0, limit: 10 });
const otherCommentsKey = queryKeys.confessions.comments('c2', { skip: 0, limit: 10 });

const seed = () => {
  queryClient.setQueryData(feedKey, { confessions: [], total: 0 });
  queryClient.setQueryData(commentsKey, { comments: [], total: 0 });
  queryClient.setQueryData(otherCommentsKey, { comments: [], total: 0 });
  queryClient.setQueryData(queryKeys.games.config(), []);
  queryClient.setQueryData(queryKeys.games.selection(), { games: [] });
  queryClient.setQueryData(queryKeys.games.featured(), { game_id: null });
  queryClient.setQueryData(queryKeys.games.state('wordle'), {});
  queryClient.setQueryData(queryKeys.games.summary(), {});
  queryClient.setQueryData(
    queryKeys.leaderboards.board('individual', { period: 'weekly' }),
    { entries: [] },
  );
};

const invalidated = (key: readonly unknown[]) =>
  queryClient.getQueryState(key)?.isInvalidated === true;

beforeEach(() => {
  queryClient.clear();
  vi.clearAllMocks();
  mockedAxios.delete.mockResolvedValue({ data: {} } as never);
  mockedAxios.patch.mockResolvedValue({ data: {} } as never);
  mockedAxios.post.mockResolvedValue({ data: {} } as never);
});

describe('mutation invalidation boundaries', () => {
  it('invalidates every feed and only the named thread after an admin confession write', async () => {
    seed();

    await deleteAdminConfession('c1');

    expect(invalidated(feedKey)).toBe(true);
    expect(invalidated(commentsKey)).toBe(true);
    expect(invalidated(otherCommentsKey)).toBe(false);
  });

  it('uses the parent confession id from a hidden comment', async () => {
    seed();
    mockedAxios.patch.mockResolvedValue({ data: { confession_id: 'c1' } } as never);

    await hideAdminComment('comment-1');

    expect(invalidated(commentsKey)).toBe(true);
    expect(invalidated(otherCommentsKey)).toBe(false);
  });

  it('invalidates game configuration after an admin puzzle write', async () => {
    seed();
    mockedAxios.post.mockResolvedValue({ data: { id: 'wordle-1' } } as never);

    await createWordleGame({ mode: 'custom', word: 'APPLE' });

    expect(invalidated(queryKeys.games.config())).toBe(true);
    expect(invalidated(queryKeys.games.selection())).toBe(true);
    expect(invalidated(queryKeys.games.featured())).toBe(true);
  });

  it('does not invalidate the spotlight or league boards for an ordinary guess', async () => {
    seed();

    await submitWordleGuess('wordle-1', 'APPLE');

    expect(invalidated(queryKeys.games.state('wordle'))).toBe(true);
    expect(invalidated(queryKeys.games.summary())).toBe(true);
    expect(invalidated(queryKeys.games.featured())).toBe(false);
    expect(
      invalidated(queryKeys.leaderboards.board('individual', { period: 'weekly' })),
    ).toBe(false);
  });

  it('invalidates boards and the player summary when an opportunity is finalized', async () => {
    seed();

    await finalizeLeagueOpportunity('wordle', 'opportunity-1');

    expect(
      invalidated(queryKeys.leaderboards.board('individual', { period: 'weekly' })),
    ).toBe(true);
    expect(invalidated(queryKeys.games.summary())).toBe(true);
    expect(invalidated(queryKeys.games.featured())).toBe(false);
  });
});
