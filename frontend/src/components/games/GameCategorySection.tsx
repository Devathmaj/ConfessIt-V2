import { useCallback, useEffect, useMemo, useState } from 'react';
import { ChevronDown, Inbox, Medal } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { cn } from '@/lib/utils';
import type { LeaguePeriod } from '@/services/api';
import {
  getGameCategories,
  getGameCategoryBoard,
  getGameCategoryMe,
  type GameCategoryBoard,
  type GameCategoryEntry,
  type GameCategoryInfo,
} from '@/services/api';
import { describeApiError } from '@/lib/apiError';
import {
  categoryEntryKey,
  categoryEntryName,
  categoryUI,
  formatMetric,
  recordFieldFormat,
  recordFieldLabel,
} from '@/lib/gameCategoryBoards';

const PAGE_SIZE = 25;

interface GameCategorySectionProps {
  gameId: string;
  gameName: string;
  period: LeaguePeriod;
  anchor: string;
}

/**
 * Per-game leaderboard categories: metric rankings across sessions.
 *
 * Rendered in place of the points podium whenever one game is selected.
 * Record-scope categories answer all-time history no matter the rolling
 * window, and say so; period categories follow the page's timeframe.
 */
export const GameCategorySection = ({ gameId, gameName, period, anchor }: GameCategorySectionProps) => {
  const [categories, setCategories] = useState<GameCategoryInfo[]>([]);
  const [categoryId, setCategoryId] = useState<string | null>(null);
  const [board, setBoard] = useState<GameCategoryBoard | null>(null);
  const [me, setMe] = useState<GameCategoryEntry | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const activeCategory = categories.find((entry) => entry.id === categoryId) ?? null;
  const isRecord = activeCategory?.scope === 'record';
  const ui = useMemo(() => categoryUI(gameId, categoryId ?? ''), [gameId, categoryId]);

  // Game change: reload the category list and land on the default view.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setCategories([]);
    setCategoryId(null);
    setBoard(null);
    setMe(null);
    setExpanded(new Set());
    getGameCategories(gameId)
      .then((list) => {
        if (cancelled) return;
        setCategories(list);
        setCategoryId(list.find((entry) => entry.is_default)?.id ?? list[0]?.id ?? null);
      })
      .catch(() => {
        if (!cancelled) {
          setCategories([]);
          setLoading(false);
          setError('Could not load the leaderboard views for this game.');
        }
      });
    return () => {
      cancelled = true;
    };
  }, [gameId]);

  const fetchBoard = useCallback(
    async (offset: number, append: boolean) => {
      if (!categoryId) return;
      if (append) {
        setLoadingMore(true);
      } else {
        setLoading(true);
      }
      setError(null);
      try {
        const query = isRecord
          ? { skip: offset, limit: PAGE_SIZE }
          : { period, ...(period === 'all_time' ? {} : { anchor }), skip: offset, limit: PAGE_SIZE };
        const [answer, mine] = await Promise.all([
          getGameCategoryBoard(gameId, categoryId, query),
          append
            ? Promise.resolve(null)
            : getGameCategoryMe(
                gameId,
                categoryId,
                isRecord ? {} : { period, ...(period === 'all_time' ? {} : { anchor }) },
              ).catch(() => null),
        ]);
        if (append) {
          setBoard((prev) =>
            prev ? { ...answer, entries: [...prev.entries, ...answer.entries] } : answer,
          );
        } else {
          setBoard(answer);
          setMe(mine?.entries[0] ?? null);
        }
      } catch (fetchError) {
        if (!append) {
          setBoard(null);
          setMe(null);
        }
        setError(describeApiError(fetchError, 'Could not load the standings.'));
      } finally {
        setLoading(false);
        setLoadingMore(false);
      }
    },
    [gameId, categoryId, isRecord, period, anchor],
  );

  useEffect(() => {
    if (categoryId) void fetchBoard(0, false);
  }, [fetchBoard, categoryId]);

  const toggleExpanded = (key: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  };

  // Fallback columns for a future category the client does not know yet:
  // every metric key becomes a plain column rather than a blank table.
  const columns = useMemo(() => {
    if (ui.columns.length > 0) return ui.columns;
    const keys = new Set<string>();
    board?.entries.forEach((entry) => Object.keys(entry.metrics).forEach((key) => keys.add(key)));
    return [...keys].map((key) => ({
      key,
      label: recordFieldLabel(key),
      format: 'text' as const,
    }));
  }, [ui, board]);

  const canLoadMore = board !== null && (board?.entries.length ?? 0) < board.total;
  const mePrimary =
    me && ui.primary.key ? formatMetric(me.metrics[ui.primary.key], ui.primary.format) : null;

  return (
    <div>
      {/* Category tabs */}
      <div
        className="no-scrollbar -mx-3 mb-4 flex gap-2 overflow-x-auto px-3 pb-1 sm:mx-0 sm:flex-wrap sm:px-0"
        role="tablist"
        aria-label={`${gameName} leaderboard views`}
      >
        {categories.map((entry) => {
          const isActive = entry.id === categoryId;
          return (
            <button
              key={entry.id}
              type="button"
              role="tab"
              aria-selected={isActive}
              onClick={() => setCategoryId(entry.id)}
              className={cn(
                'inline-flex shrink-0 items-center gap-2 rounded-full border px-4 py-2 text-xs font-bold uppercase tracking-wider transition-all sm:text-sm',
                isActive
                  ? 'border-transparent bg-gradient-romantic text-white shadow-[var(--shadow-romantic)]'
                  : 'border-border/70 bg-card/60 text-muted-foreground hover:border-romantic/50 hover:text-foreground',
              )}
            >
              {entry.label}
              {entry.scope === 'record' && (
                <span
                  className={cn(
                    'rounded-full px-1.5 py-0.5 text-[10px] font-bold uppercase',
                    isActive ? 'bg-white/25 text-white' : 'bg-secondary text-muted-foreground',
                  )}
                >
                  All-time
                </span>
              )}
            </button>
          );
        })}
      </div>

      {activeCategory && (
        <p className="mb-4 text-xs text-muted-foreground">
          {activeCategory.description}
          {isRecord ? ' All-time record: the rolling timeframe does not apply.' : ''}
        </p>
      )}

      {/* Viewer's standing in this view */}
      {me && (
        <Card className="mb-4 rounded-2xl border-romantic/40 bg-romantic/10">
          <CardContent className="flex flex-wrap items-center gap-x-8 gap-y-2 p-4">
            <div className="flex items-center gap-2 text-sm font-semibold">
              <Medal className="h-4 w-4 text-romantic" />
              <span>
                You rank #{me.rank}
                {mePrimary ? ` · ${mePrimary} ${ui.primary.label}` : ''}
              </span>
            </div>
          </CardContent>
        </Card>
      )}

      {loading ? (
        <p className="py-14 text-center text-sm text-muted-foreground">Loading standings…</p>
      ) : error ? (
        <div className="rounded-2xl border border-dashed border-border/70 px-6 py-14 text-center">
          <p className="text-base font-bold">Standings are unavailable</p>
          <p className="mx-auto mt-1 max-w-sm text-sm text-muted-foreground">{error}</p>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="mt-5 rounded-full"
            onClick={() => void fetchBoard(0, false)}
          >
            Try again
          </Button>
        </div>
      ) : !board || board.entries.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-border/70 px-6 py-14 text-center">
          <Inbox className="mx-auto h-8 w-8 text-muted-foreground" />
          <p className="mt-3 text-base font-bold">No standings yet</p>
          <p className="mx-auto mt-1 max-w-sm text-sm text-muted-foreground">
            No finalized {gameName} results for this view yet. Events appear once they close.
          </p>
        </div>
      ) : (
        <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
          <CardHeader className="pb-3">
            <CardTitle className="text-base font-bold">
              {activeCategory?.label ?? 'Standings'}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {board.entries.map((entry) => {
              const key = categoryEntryKey(entry);
              const isOpen = expanded.has(key);
              return (
                <div
                  key={key}
                  className="rounded-xl border border-border/50 bg-background/40 p-3"
                >
                  <div className="flex items-center gap-3">
                    <span className="w-8 shrink-0 text-center text-sm font-extrabold tabular-nums text-muted-foreground">
                      {entry.rank}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-sm font-semibold">
                      {categoryEntryName(entry)}
                    </span>
                    <div className="hidden flex-wrap items-center gap-x-4 gap-y-1 sm:flex">
                      {columns.map((column) => (
                        <span key={column.key} className="text-right">
                          <span className="block text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
                            {column.label}
                          </span>
                          <span className="block text-sm font-bold tabular-nums">
                            {formatMetric(entry.metrics[column.key], column.format)}
                          </span>
                        </span>
                      ))}
                    </div>
                    {/* Compact metrics on small screens: primary value only. */}
                    <span className="text-sm font-extrabold tabular-nums text-romantic sm:hidden">
                      {ui.primary.key ? formatMetric(entry.metrics[ui.primary.key], ui.primary.format) : ''}
                    </span>
                    {entry.records.length > 0 && (
                      <button
                        type="button"
                        aria-expanded={isOpen}
                        aria-label={`${isOpen ? 'Hide' : 'Show'} records for ${categoryEntryName(entry)}`}
                        onClick={() => toggleExpanded(key)}
                        className="shrink-0 rounded-full border border-border/70 p-1.5 text-muted-foreground transition-colors hover:border-romantic/50 hover:text-foreground"
                      >
                        <ChevronDown
                          className={cn('h-4 w-4 transition-transform', isOpen && 'rotate-180')}
                        />
                      </button>
                    )}
                  </div>
                  {/* Full metric set on small screens. */}
                  <div className="mt-2 grid grid-cols-3 gap-2 sm:hidden">
                    {columns.map((column) => (
                      <span key={column.key}>
                        <span className="block text-[10px] font-bold uppercase tracking-wider text-muted-foreground">
                          {column.label}
                        </span>
                        <span className="block text-xs font-bold tabular-nums">
                          {formatMetric(entry.metrics[column.key], column.format)}
                        </span>
                      </span>
                    ))}
                  </div>
                  {isOpen && entry.records.length > 0 && (
                    <div className="mt-3 space-y-2 border-t border-border/50 pt-3">
                      {entry.records.map((record, index) => (
                        <dl
                          key={index}
                          className="grid grid-cols-2 gap-x-4 gap-y-1 rounded-lg bg-secondary/40 p-2.5 text-xs sm:grid-cols-3"
                        >
                          {Object.entries(record)
                            // Internal identifier: useful on the wire, never shown.
                            .filter(([field]) => field !== 'opportunity_id')
                            .map(([field, value]) => (
                            <div key={field} className="min-w-0">
                              <dt className="font-bold uppercase tracking-wider text-muted-foreground">
                                {recordFieldLabel(field)}
                              </dt>
                              <dd className="truncate font-semibold tabular-nums">
                                {field === 'word' && (value === null || value === '')
                                  ? 'Hidden (live puzzle)'
                                  : formatMetric(value, recordFieldFormat(field, value))}
                              </dd>
                            </div>
                          ))}
                        </dl>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </CardContent>
        </Card>
      )}

      {canLoadMore && !loading && (
        <div className="mt-4 text-center">
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="rounded-full"
            disabled={loadingMore}
            onClick={() => void fetchBoard(board?.entries.length ?? 0, true)}
          >
            {loadingMore ? 'Loading…' : `Show more (${(board?.total ?? 0) - (board?.entries.length ?? 0)} left)`}
          </Button>
        </div>
      )}
    </div>
  );
};

export default GameCategorySection;
