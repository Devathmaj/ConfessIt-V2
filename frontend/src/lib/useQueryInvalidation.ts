import { partialMatchKey, type QueryKey } from '@tanstack/react-query';
import { useEffect, useRef } from 'react';

import { queryClient } from './queryClient';

/**
 * Run an explicit reload when the shared cache invalidates a resource.
 *
 * This is for screens that keep their own pagination/optimistic state and
 * therefore cannot hand the whole read to `useApiQuery`. The callback is kept in
 * a ref so a new render does not tear down the subscription, and one
 * invalidation batch is coalesced into a single reload.
 */
export const useQueryInvalidation = (
  queryKey: QueryKey,
  onInvalidate: () => void,
  enabled = true,
): void => {
  const callback = useRef(onInvalidate);

  useEffect(() => {
    callback.current = onInvalidate;
  }, [onInvalidate]);

  useEffect(() => {
    if (!enabled) return;

    let scheduled = false;
    const unsubscribe = queryClient.getQueryCache().subscribe((event) => {
      if (event.type !== 'updated') return;
      if (!event.query.state.isInvalidated) return;
      if (!partialMatchKey(event.query.queryKey, queryKey)) return;
      if (scheduled) return;

      scheduled = true;
      queueMicrotask(() => {
        scheduled = false;
        callback.current();
      });
    });

    return unsubscribe;
  }, [enabled, queryKey]);
};
