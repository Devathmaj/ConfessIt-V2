import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import {
  CACHE_TIMES,
  invalidateLeaderboardQueries,
  queryClient,
  queryKeys,
} from '@/lib/queryClient';
import { useApiQuery } from '@/lib/useApiQuery';
import { useQueryInvalidation } from '@/lib/useQueryInvalidation';

describe('useApiQuery', () => {
  it('refetches and rerenders the screen after its resource is invalidated', async () => {
    const loader = vi
      .fn()
      .mockResolvedValueOnce({ entries: ['before'] })
      .mockResolvedValueOnce({ entries: ['after'] });

    const { result } = renderHook(() =>
      useApiQuery({
        queryKey: queryKeys.leaderboards.board('individual', { period: 'weekly' }),
        queryFn: loader,
        staleTime: CACHE_TIMES.leaderboards,
      }),
    );

    await waitFor(() => expect(result.current.data).toEqual({ entries: ['before'] }));

    await act(async () => {
      await invalidateLeaderboardQueries();
    });

    await waitFor(() => expect(result.current.data).toEqual({ entries: ['after'] }));
    expect(loader).toHaveBeenCalledTimes(2);
  });

  it('coalesces one invalidation batch into one explicit reload', async () => {
    const onInvalidate = vi.fn();
    renderHook(() => useQueryInvalidation(queryKeys.leaderboards.root, onInvalidate));

    queryClient.setQueryData(
      queryKeys.leaderboards.board('individual', { period: 'weekly' }),
      { entries: [] },
    );
    queryClient.setQueryData(
      queryKeys.leaderboards.myStanding('individual', { period: 'weekly' }),
      { entries: [] },
    );

    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.leaderboards.root });
    });

    await waitFor(() => expect(onInvalidate).toHaveBeenCalledTimes(1));
  });

  it('runs the service function as a raw loader instead of nesting a second query', async () => {
    const loader = vi.fn().mockResolvedValue({ games_played: 2 });

    const { result } = renderHook(() =>
      useApiQuery({
        queryKey: queryKeys.games.summary(),
        queryFn: loader,
        staleTime: CACHE_TIMES.leaderboards,
      }),
    );

    await waitFor(() => expect(result.current.data).toEqual({ games_played: 2 }));
    expect(loader).toHaveBeenCalledTimes(1);
  });
});
