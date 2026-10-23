import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  CACHE_TIMES,
  cachedQuery,
  clearPrivateQueryState,
  invalidateLeaderboardQueries,
  queryClient,
  queryKeys,
  stableParams,
} from '@/lib/queryClient';

/**
 * The service layer now reads through one shared cache. These tests pin the
 * behaviour that makes that safe: a fresh entry costs no request, a stale one
 * does, a write invalidates the resource it changed, and the identity boundary
 * removes private state rather than merely marking it stale.
 */
describe('the shared query cache', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-01-01T12:00:00Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('returns a fresh entry without calling the loader', async () => {
    const loader = vi.fn().mockResolvedValue({ rows: [1] });
    const options = {
      queryKey: queryKeys.confessions.feed('time', { skip: 0 }),
      queryFn: loader,
      staleTime: CACHE_TIMES.confessions,
    };

    await expect(cachedQuery(options)).resolves.toEqual({ rows: [1] });
    await expect(cachedQuery(options)).resolves.toEqual({ rows: [1] });

    expect(loader).toHaveBeenCalledTimes(1);
  });

  it('refetches once the stale window has passed', async () => {
    const loader = vi.fn().mockResolvedValueOnce('old').mockResolvedValueOnce('new');
    const options = {
      queryKey: queryKeys.leaderboards.board('individual', { period: 'weekly' }),
      queryFn: loader,
      staleTime: CACHE_TIMES.leaderboards,
    };

    await expect(cachedQuery(options)).resolves.toBe('old');
    vi.setSystemTime(new Date('2026-01-01T12:00:31Z'));
    await expect(cachedQuery(options)).resolves.toBe('new');

    expect(loader).toHaveBeenCalledTimes(2);
  });

  it('refetches after the leaderboard invalidator marks it stale', async () => {
    const loader = vi.fn().mockResolvedValueOnce('before').mockResolvedValueOnce('after');
    const options = {
      queryKey: queryKeys.leaderboards.board('couples', { period: 'weekly' }),
      queryFn: loader,
      staleTime: CACHE_TIMES.leaderboards,
    };

    await cachedQuery(options);
    await invalidateLeaderboardQueries();
    await expect(cachedQuery(options)).resolves.toBe('after');

    expect(loader).toHaveBeenCalledTimes(2);
  });

  it('removes private entries instead of leaving them to render', async () => {
    queryClient.setQueryData(queryKeys.profiles.me(), { id: 'me' });
    queryClient.setQueryData(queryKeys.chat.transcript('conversation-1'), { rows: [] });
    queryClient.setQueryData(queryKeys.admin.reports({ skip: 0 }), { rows: [] });

    await clearPrivateQueryState();

    expect(queryClient.getQueryData(queryKeys.profiles.me())).toBeUndefined();
    expect(queryClient.getQueryData(queryKeys.chat.transcript('conversation-1'))).toBeUndefined();
    expect(queryClient.getQueryData(queryKeys.admin.reports({ skip: 0 }))).toBeUndefined();
  });

  it('builds one key regardless of parameter insertion order', () => {
    expect(stableParams({ limit: 20, skip: 0, anchor: undefined })).toEqual({
      limit: 20,
      skip: 0,
    });
    expect(queryKeys.confessions.feed('time', { limit: 20, skip: 0 })).toEqual(
      queryKeys.confessions.feed('time', { skip: 0, limit: 20 }),
    );
  });
});
