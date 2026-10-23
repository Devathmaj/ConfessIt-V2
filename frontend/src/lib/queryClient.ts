/**
 * The application's one query cache.
 *
 * TanStack Query already existed as a dependency and a provider, but every screen
 * fetched through its service function directly and kept the answer in component
 * state, so there was no shared cache, no stale window and no way to invalidate a
 * resource once a mutation had changed it. This module is that shared layer:
 *
 * * one `QueryClient` with conservative global defaults;
 * * one key factory, so an invalidation names the same resource the reader used;
 * * per-resource stale windows (feeds 20s, profiles 60s, leaderboards 30s, game
 *   configuration 2m, chat 0s);
 * * targeted invalidators called after writes;
 * * an identity boundary that clears everything when the signed-in account changes.
 *
 * Reads go through {@link cachedQuery}, which is `queryClient.query(...)`: fresh
 * cached data is returned without a request, stale data is fetched. Using the
 * singleton rather than only React observers keeps the cache available to the
 * service layer (where most of this app's reads live) and to screens rendered in
 * tests without a provider.
 *
 * Screen-owned reads should use `useApiQuery` from `useApiQuery.ts` instead:
 * it creates a real observer, so invalidation, reconnect and retry reach the
 * component. `runWithoutCache` is the bridge that lets that observer reuse the
 * existing service functions without recursing into the same query.
 *
 * Chat is deliberately absent from persistence: it lives only in this in-memory
 * cache and in bounded component state. The service worker never caches it, and
 * nothing here writes to localStorage or IndexedDB.
 */

import { QueryClient, type QueryKey } from '@tanstack/react-query';

/** Per-resource freshness windows. Kept together so the policy is auditable. */
export const CACHE_TIMES = {
  /** Profile pictures are handled by the service worker's 20-minute TTL. */
  profilePicture: 20 * 60 * 1000,
  /** Confession feeds and threads: 15-30 seconds. */
  confessions: 20 * 1000,
  /** Profiles and preferences: 30-120 seconds. */
  profiles: 60 * 1000,
  /** Game leaderboards: 15-60 seconds. */
  leaderboards: 30 * 1000,
  /** Game schedules and configuration: 1-5 minutes. */
  gameConfig: 2 * 60 * 1000,
  /** Chat and real-time state is never considered fresh: the socket owns it. */
  chat: 0,
  /** Small reference lists (cohorts, categories) that change only with a deploy. */
  reference: 5 * 60 * 1000,
} as const;

/** How long unused data is kept before garbage collection. */
export const CACHE_GC_TIME = 5 * 60 * 1000;

/**
 * Deterministic key material for parameter objects.
 *
 * TanStack hashes keys structurally, but object insertion order is part of that
 * hash. Building the same filters in a different order on two screens would then
 * create two entries for one resource and an invalidation would miss one of them.
 */
export const stableParams = (value: unknown): unknown => {
  if (Array.isArray(value)) return value.map(stableParams);
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .filter(([, item]) => item !== undefined)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([key, item]) => [key, stableParams(item)]),
    );
  }
  return value;
};

const ROOT = ['confessit'] as const;

/** Every resource the client caches, named once. */
export const queryKeys = {
  all: ROOT,
  confessions: {
    root: [...ROOT, 'confessions'] as const,
    feed: (sortBy: string, params?: unknown) =>
      [...ROOT, 'confessions', 'feed', sortBy, stableParams(params ?? {})] as const,
    comments: (confessionId: string, params?: unknown) =>
      [...ROOT, 'confessions', 'comments', confessionId, stableParams(params ?? {})] as const,
    cooldown: () => [...ROOT, 'confessions', 'cooldown'] as const,
    search: (query: string, limit: number) =>
      [...ROOT, 'confessions', 'search', query, limit] as const,
  },
  profiles: {
    root: [...ROOT, 'profiles'] as const,
    me: () => [...ROOT, 'profiles', 'me'] as const,
    detail: (userId: string) => [...ROOT, 'profiles', userId] as const,
    list: (params?: unknown) => [...ROOT, 'profiles', 'list', stableParams(params ?? {})] as const,
  },
  leaderboards: {
    root: [...ROOT, 'leaderboards'] as const,
    board: (competition: string, params?: unknown) =>
      [...ROOT, 'leaderboards', 'board', competition, stableParams(params ?? {})] as const,
    myStanding: (competition: string, params?: unknown) =>
      [...ROOT, 'leaderboards', 'me', competition, stableParams(params ?? {})] as const,
    categories: (gameId: string) =>
      [...ROOT, 'leaderboards', 'categories', gameId] as const,
    categoryBoard: (gameId: string, category: string, params?: unknown) =>
      [...ROOT, 'leaderboards', 'category', gameId, category, stableParams(params ?? {})] as const,
    categoryMe: (gameId: string, category: string, params?: unknown) =>
      [...ROOT, 'leaderboards', 'category-me', gameId, category, stableParams(params ?? {})] as const,
    adminRoot: [...ROOT, 'leaderboards', 'admin'] as const,
  },
  games: {
    root: [...ROOT, 'games'] as const,
    selection: () => [...ROOT, 'games', 'selection'] as const,
    featured: () => [...ROOT, 'games', 'featured'] as const,
    summary: () => [...ROOT, 'games', 'summary'] as const,
    config: () => [...ROOT, 'games', 'config'] as const,
    adminFeatured: () => [...ROOT, 'games', 'admin-featured'] as const,
    state: (gameId: string) => [...ROOT, 'games', 'state', gameId] as const,
    partners: (gameId: string, params?: unknown) =>
      [...ROOT, 'games', 'partners', gameId, stableParams(params ?? {})] as const,
  },
  chat: {
    root: [...ROOT, 'chat'] as const,
    conversations: () => [...ROOT, 'chat', 'conversations'] as const,
    conversation: (matchId: string) => [...ROOT, 'chat', 'conversation', matchId] as const,
    transcript: (conversationId: string) =>
      [...ROOT, 'chat', 'transcript', conversationId] as const,
    caseClosed: (sessionId?: string | null) =>
      [...ROOT, 'chat', 'case-closed', sessionId ?? 'current'] as const,
  },
  notifications: {
    root: [...ROOT, 'notifications'] as const,
    list: (params?: unknown) => [...ROOT, 'notifications', 'list', stableParams(params ?? {})] as const,
    unread: () => [...ROOT, 'notifications', 'unread'] as const,
  },
  admin: {
    root: [...ROOT, 'admin'] as const,
    reports: (params?: unknown) => [...ROOT, 'admin', 'reports', stableParams(params ?? {})] as const,
    stats: () => [...ROOT, 'admin', 'stats'] as const,
    config: () => [...ROOT, 'admin', 'config'] as const,
    notifications: (params?: unknown) =>
      [...ROOT, 'admin', 'notifications', stableParams(params ?? {})] as const,
  },
} as const;

/**
 * Global defaults. Per-resource stale windows are supplied by the call site; the
 * global value is the fallback for the long tail of one-off reads.
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: CACHE_TIMES.profiles,
      gcTime: CACHE_GC_TIME,
      retry: 1,
      refetchOnWindowFocus: false,
      refetchOnReconnect: true,
    },
    mutations: {
      retry: 0,
    },
  },
});

type CachedQueryOptions<T> = {
  queryKey: QueryKey;
  queryFn: () => Promise<T>;
  staleTime?: number;
  gcTime?: number;
  enabled?: boolean;
};

let cacheBypassDepth = 0;

/**
 * Read through the shared cache, fetching only when the entry is missing or stale.
 *
 * This is the imperative sibling of `useQuery`, used by the service layer and by
 * screens that keep their own pagination/optimistic state. It does not create a
 * React observer. A screen that needs to re-render on invalidation must use
 * `useApiQuery` with the same key (or explicitly refetch through a cache
 * invalidation subscription).
 */
export const cachedQuery = <T>({
  queryKey,
  queryFn,
  staleTime,
  gcTime,
}: CachedQueryOptions<T>): Promise<T> => {
  // A reactive query has already created the entry and is running its queryFn.
  // Calling queryClient.query for that same key would wait on the very fetch
  // that is waiting on this call. The service functions call cachedQuery
  // synchronously, so this depth counter is enough to let them run as the raw
  // loader inside useApiQuery.
  if (cacheBypassDepth > 0) return queryFn();
  return queryClient.query<T>({
    queryKey,
    queryFn,
    staleTime,
    gcTime,
  });
};

/**
 * Run a service function as a raw loader for a reactive query.
 *
 * The wrapped function must call `cachedQuery` synchronously (all the cached
 * service reads do). That call then invokes its queryFn directly instead of
 * opening a second query with the same key.
 */
export const runWithoutCache = <T>(operation: () => Promise<T>): Promise<T> => {
  cacheBypassDepth += 1;
  try {
    return operation();
  } finally {
    cacheBypassDepth -= 1;
  }
};

const invalidate = (queryKey: QueryKey) => queryClient.invalidateQueries({ queryKey });

/** Invalidate every feed and, when named, one thread. */
export const invalidateConfessionQueries = async (confessionId?: string): Promise<void> => {
  await queryClient.invalidateQueries({
    queryKey: queryKeys.confessions.root,
    predicate: (query) => {
      const key = query.queryKey;
      if (key[2] === 'feed') return true;
      if (key[2] !== 'comments') return false;
      return confessionId === undefined || key[3] === confessionId;
    },
  });
};

export const invalidateProfileQueries = (): Promise<void> =>
  invalidate(queryKeys.profiles.root);

export const invalidateLeaderboardQueries = (): Promise<void> =>
  Promise.all([
    invalidate(queryKeys.leaderboards.root),
    invalidate(queryKeys.games.summary()),
  ]).then(() => undefined);

export const invalidateGameConfigQueries = (): Promise<void> =>
  Promise.all([
    invalidate(queryKeys.games.config()),
    invalidate(queryKeys.games.selection()),
    invalidate(queryKeys.games.featured()),
    invalidate(queryKeys.games.adminFeatured()),
    invalidate(queryKeys.admin.config()),
  ]).then(() => undefined);

export const invalidateGameStateQueries = (gameId?: string): Promise<void> => {
  if (gameId) {
    // A guess/move changes this game's state and the player's own summary. It
    // does not change the spotlight, and league boards only move when an
    // opportunity is finalized; invalidating those here made every guess refetch
    // unrelated data.
    return Promise.all([
      invalidate(queryKeys.games.state(gameId)),
      invalidate(queryKeys.games.summary()),
    ]).then(() => undefined);
  }
  // A scheduled reset opens/closes sessions across every game and can move the
  // boards, so the broad invalidation is intentional here.
  return Promise.all([
    invalidate(queryKeys.games.root),
    invalidate(queryKeys.leaderboards.root),
  ]).then(() => undefined);
};

export const invalidateChatQueries = (conversationId?: string): Promise<void> => {
  if (conversationId) {
    return Promise.all([
      invalidate(queryKeys.chat.transcript(conversationId)),
      invalidate(queryKeys.chat.conversations()),
    ]).then(() => undefined);
  }
  return invalidate(queryKeys.chat.root);
};

export const invalidateNotificationQueries = (): Promise<void> =>
  invalidate(queryKeys.notifications.root);

export const invalidateAdminQueries = (): Promise<void> =>
  Promise.all([
    invalidate(queryKeys.admin.root),
    invalidate(queryKeys.leaderboards.adminRoot),
  ]).then(() => undefined);

/**
 * Remove the query state that belongs to a signed-in identity.
 *
 * Used on logout, on a session superseded elsewhere, and whenever the account id
 * changes. Removing (rather than only invalidating) is what guarantees a sensitive
 * answer cannot be painted for the next person at the same browser: no entry is
 * left to render while a fresh request is in flight.
 */
export const clearPrivateQueryState = async (): Promise<void> => {
  await queryClient.cancelQueries();
  queryClient.removeQueries({ queryKey: queryKeys.admin.root });
  queryClient.removeQueries({ queryKey: queryKeys.chat.root });
  queryClient.removeQueries({ queryKey: queryKeys.notifications.root });
  queryClient.removeQueries({ queryKey: queryKeys.profiles.root });
  queryClient.removeQueries({ queryKey: queryKeys.confessions.root });
  queryClient.removeQueries({ queryKey: queryKeys.games.root });
  queryClient.removeQueries({ queryKey: queryKeys.leaderboards.root });
};

/** Drop everything. Used at the identity boundary; the cache holds no durable data. */
export const clearAllQueryState = async (): Promise<void> => {
  await queryClient.cancelQueries();
  queryClient.clear();
};

let currentIdentity: string | null = null;

/**
 * Announce the signed-in account. A change clears the cache before the new
 * account can read a previous account's entries. A token rotation for the same
 * account does not.
 */
export const syncQueryIdentity = (userId: string | null | undefined): void => {
  const next = userId ?? null;
  if (currentIdentity === next) return;
  currentIdentity = next;
  void clearAllQueryState();
};

/** Test seam: forget the identity without touching the cache. */
export const resetQueryIdentityForTests = (): void => {
  currentIdentity = null;
};

export default queryClient;
