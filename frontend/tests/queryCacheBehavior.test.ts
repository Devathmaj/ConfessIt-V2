import { focusManager, onlineManager, QueryObserver } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { queryClient } from '@/lib/queryClient';

const observe = (queryKey: string[], queryFn: () => Promise<unknown>) => {
  const observer = new QueryObserver(queryClient, {
    queryKey,
    queryFn,
    staleTime: 0,
    retryDelay: 0,
  });
  const unsubscribe = observer.subscribe(() => undefined);
  return { observer, unsubscribe };
};

describe('installed TanStack Query behaviour', () => {
  beforeEach(() => {
    queryClient.mount();
    queryClient.clear();
    onlineManager.setOnline(true);
    focusManager.setFocused(true);
  });

  afterEach(() => {
    queryClient.unmount();
    queryClient.clear();
    onlineManager.setOnline(true);
    focusManager.setFocused(true);
  });

  it('uses the configured conservative defaults', () => {
    expect(queryClient.getDefaultOptions().queries).toMatchObject({
      retry: 1,
      refetchOnWindowFocus: false,
      refetchOnReconnect: true,
    });
  });

  it('does not refetch on window focus', async () => {
    const loader = vi.fn().mockResolvedValue('value');
    const { observer, unsubscribe } = observe(['behavior', 'focus'], loader);

    await vi.waitFor(() => expect(observer.getCurrentResult().data).toBe('value'));
    focusManager.setFocused(false);
    focusManager.setFocused(true);
    await new Promise((resolve) => setTimeout(resolve, 25));

    expect(loader).toHaveBeenCalledTimes(1);
    unsubscribe();
  });

  it('refetches a stale active query on reconnect', async () => {
    const loader = vi.fn().mockResolvedValueOnce('before').mockResolvedValueOnce('after');
    const { observer, unsubscribe } = observe(['behavior', 'reconnect'], loader);

    await vi.waitFor(() => expect(observer.getCurrentResult().data).toBe('before'));
    await new Promise((resolve) => setTimeout(resolve, 10));
    onlineManager.setOnline(false);
    onlineManager.setOnline(true);

    await vi.waitFor(() => expect(loader).toHaveBeenCalledTimes(2));
    expect(observer.getCurrentResult().data).toBe('after');
    unsubscribe();
  });

  it('retries once before surfacing the error', async () => {
    const loader = vi.fn().mockRejectedValue(new Error('offline'));
    const { observer, unsubscribe } = observe(['behavior', 'retry'], loader);

    await vi.waitFor(() => expect(observer.getCurrentResult().status).toBe('error'), {
      timeout: 2500,
    });
    expect(loader).toHaveBeenCalledTimes(2);
    unsubscribe();
  });
});
