import { useQuery, type QueryKey, type UseQueryResult } from '@tanstack/react-query';

import { queryClient, runWithoutCache } from './queryClient';

type ApiQueryOptions<T> = {
  queryKey: QueryKey;
  queryFn: () => Promise<T>;
  staleTime?: number;
  enabled?: boolean;
};

/**
 * A screen-owned read through the shared cache.
 *
 * The service functions still own the URL, params and stale window. Wrapping one
 * in this hook creates the React observer that `cachedQuery` cannot: an
 * invalidation now re-renders the screen with fresh data instead of only
 * marking the entry stale for the next imperative read.
 */
export const useApiQuery = <T>({
  queryKey,
  queryFn,
  staleTime,
  enabled,
}: ApiQueryOptions<T>): UseQueryResult<T> =>
  useQuery<T>(
    {
      queryKey,
      queryFn: () => runWithoutCache(queryFn),
      staleTime,
      enabled,
    },
    queryClient,
  );
