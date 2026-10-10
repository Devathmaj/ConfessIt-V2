import { useCallback, useEffect, useMemo, useState } from 'react';
import { Eye, Trophy, TrendingUp, Users, Zap } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { StatCard } from '@/components/games/StatCard';
import { GameCategorySection } from '@/components/games/GameCategorySection';
import {
  ALL_GAMES_FILTER,
  LEAGUE_COMPETITIONS,
  LEAGUE_PERIODS,
  entryDisplayName,
  gamesForCompetition,
  todayAnchor,
  type LeagueCompetitionType,
  type LeagueCoupleEntry,
  type LeagueIndividualEntry,
  type LeaguePeriod,
} from '@/lib/gamesLeaderboard';
import {
  getAdminCouplesLeaderboard,
  getAdminIndividualLeaderboard,
  type LeagueCoupleBoard,
  type LeagueIndividualBoard,
} from '@/services/api';
import { describeApiError } from '@/lib/apiError';
import { cn } from '@/lib/utils';

/**
 * The administrative leaderboard: the same server-calculated standings the
 * player board shows, with the columns an operator inspects. Selecting one
 * game swaps the points list for that game's expanded rankings (Performance,
 * Fastest Solve, All-Time Fastest and the rest) -- the same metric boards
 * players read. Read-only: awards are written by event finalization, never
 * edited here.
 */

const PAGE_SIZE = 25;

type BoardEntry = LeagueIndividualEntry | LeagueCoupleEntry;

const isCoupleEntry = (entry: BoardEntry): entry is LeagueCoupleEntry =>
  !('display_name' in entry);

const formatScore = (score: number): string =>
  score.toLocaleString(undefined, { maximumFractionDigits: 2 });

export const AdminGamesLeaderboard = () => {
  const [competition, setCompetition] = useState<LeagueCompetitionType>('individual');
  const [period, setPeriod] = useState<LeaguePeriod>('weekly');
  const [gameId, setGameId] = useState<string>(ALL_GAMES_FILTER);
  const [anchor, setAnchor] = useState<string>(todayAnchor());
  const [entries, setEntries] = useState<BoardEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [opportunityCount, setOpportunityCount] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const scopeGames = useMemo(() => gamesForCompetition(competition), [competition]);
  const periodOption = LEAGUE_PERIODS.find((option) => option.id === period);
  const currentGame = scopeGames.find((game) => game.id === gameId);
  // One game selected: the expanded rankings below own that view, so the
  // points list stands down (the fetch still feeds the summary tiles).
  const gameSelected = gameId !== ALL_GAMES_FILTER;
  const filterCaption =
    gameId === ALL_GAMES_FILTER
      ? `Overall · ${periodOption?.label ?? ''}`
      : `${currentGame?.name ?? 'Unknown game'} · ${periodOption?.label ?? ''}`;

  const fetchBoard = useCallback(
    async (offset: number, append: boolean) => {
      if (append) {
        setLoadingMore(true);
      } else {
        setLoading(true);
      }
      setError(null);
      try {
        const query = {
          period,
          ...(period === 'all_time' ? {} : { anchor }),
          ...(gameId === ALL_GAMES_FILTER ? {} : { gameId }),
          skip: offset,
          limit: PAGE_SIZE,
        };
        const board: LeagueIndividualBoard | LeagueCoupleBoard =
          competition === 'couple'
            ? await getAdminCouplesLeaderboard(query)
            : await getAdminIndividualLeaderboard(query);
        setEntries((prev) => (append ? [...prev, ...board.entries] : [...board.entries]));
        setTotal(board.total);
        setOpportunityCount(board.opportunity_count ?? null);
      } catch (fetchError) {
        if (!append) setEntries([]);
        setError(describeApiError(fetchError, 'Could not load the standings.'));
      } finally {
        setLoading(false);
        setLoadingMore(false);
      }
    },
    [competition, period, gameId, anchor],
  );

  useEffect(() => {
    void fetchBoard(0, false);
  }, [fetchBoard]);

  const topScore = entries.length > 0 ? Math.max(...entries.map((row) => row.score)) : 0;
  const averageScore =
    entries.length > 0
      ? Math.round((entries.reduce((sum, row) => sum + row.score, 0) / entries.length) * 100) / 100
      : 0;
  const featuredRows = entries.filter((row) => row.is_featured).length;
  const canLoadMore = entries.length < total;

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-col gap-2">
        <h1 className="text-3xl font-semibold">Leaderboard</h1>
        <p className="text-sm text-muted-foreground">
          League standings as players see them, per competition, period and
          game. Pick a title to read its expanded rankings — fastest solves,
          best times, most solved and the rest. Awards are written when events
          close; nothing here edits them.
        </p>
      </header>

      <section className="flex flex-wrap items-end gap-4">
        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-medium uppercase text-muted-foreground">Competition</span>
          <div className="flex gap-2">
            {LEAGUE_COMPETITIONS.map((option) => {
              const isActive = competition === option.id;
              return (
                <button
                  key={option.id}
                  type="button"
                  aria-pressed={isActive}
                  onClick={() => {
                    setCompetition(option.id);
                    setGameId(ALL_GAMES_FILTER);
                  }}
                  className={cn(
                    'inline-flex items-center rounded-full border px-4 py-2 text-xs font-bold uppercase tracking-wider transition-all',
                    isActive
                      ? 'border-transparent bg-gradient-romantic text-white shadow-[var(--shadow-romantic)]'
                      : 'border-border/70 bg-card/60 text-muted-foreground hover:border-romantic/50 hover:text-foreground',
                  )}
                >
                  {option.label}
                </button>
              );
            })}
          </div>
        </div>

        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-medium uppercase text-muted-foreground">Period</span>
          <Select value={period} onValueChange={(next) => setPeriod(next as LeaguePeriod)}>
            <SelectTrigger className="w-36">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {LEAGUE_PERIODS.map((option) => (
                <SelectItem key={option.id} value={option.id}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-medium uppercase text-muted-foreground">Game</span>
          <Select value={gameId} onValueChange={setGameId}>
            <SelectTrigger className="w-48">
              <SelectValue placeholder="Overall" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_GAMES_FILTER}>Overall</SelectItem>
              {scopeGames.map((game) => (
                <SelectItem key={game.id} value={game.id}>
                  {game.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        {period !== 'all_time' && (
          <div className="flex flex-col gap-1.5">
            <span className="text-xs font-medium uppercase text-muted-foreground">Date (UTC)</span>
            <Input
              type="date"
              value={anchor}
              max={todayAnchor()}
              onChange={(event) => {
                if (event.target.value) setAnchor(event.target.value);
              }}
              className="w-44"
            />
          </div>
        )}
      </section>

      <p className="-mt-3 text-xs text-muted-foreground">
        {periodOption?.hint} · {filterCaption}
        {opportunityCount !== null && opportunityCount > 0
          ? ` · ${opportunityCount} ${opportunityCount === 1 ? 'event' : 'events'} scored`
          : ''}
      </p>

      <section className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label={competition === 'couple' ? 'Couples ranked' : 'Players ranked'}
          value={total}
          hint={periodOption?.hint}
          icon={Trophy}
          accent="bg-gradient-romantic"
        />
        <StatCard
          label="Events scored"
          value={opportunityCount ?? 0}
          hint="Finalized in this window"
          icon={TrendingUp}
          accent="bg-gradient-love"
        />
        <StatCard
          label="Top score"
          value={formatScore(topScore)}
          hint={`Average ${formatScore(averageScore)}`}
          icon={Eye}
          accent="bg-gradient-romantic"
        />
        <StatCard
          label="Featured rows"
          value={featuredRows}
          hint="Holding a 2× award"
          icon={Users}
          accent="bg-gradient-love"
        />
      </section>

      {gameSelected ? (
        <>
          <GameCategorySection
            viewer="admin"
            gameId={gameId}
            gameName={currentGame?.name ?? 'this game'}
            period={period}
            anchor={anchor}
          />
          <p className="mt-6 text-center text-xs text-muted-foreground">
            Category boards rank finalized results by their own metric — never
            session placements — and never feed global scores.
          </p>
        </>
      ) : (
      <>
      <section aria-label="Standings" className="space-y-3">
        {loading ? (
          <Card className="rounded-2xl border-border/60 bg-card/70">
            <CardContent className="py-12 text-center text-sm text-muted-foreground">
              Loading standings…
            </CardContent>
          </Card>
        ) : error ? (
          <Card className="rounded-2xl border-border/60 bg-card/70">
            <CardContent className="py-12 text-center">
              <p className="text-sm font-semibold">Standings are unavailable</p>
              <p className="mt-1 text-sm text-muted-foreground">{error}</p>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="mt-4"
                onClick={() => void fetchBoard(0, false)}
              >
                Try again
              </Button>
            </CardContent>
          </Card>
        ) : entries.length === 0 ? (
          <Card className="rounded-2xl border-border/60 bg-card/70">
            <CardContent className="py-12 text-center text-sm text-muted-foreground">
              No standings match these filters.
            </CardContent>
          </Card>
        ) : (
          entries.map((row) => {
            const key = isCoupleEntry(row)
              ? `${row.member_a_id}:${row.member_b_id}`
              : row.user_id;
            return (
              <Card key={key} className="rounded-2xl border-border/60 bg-card/70">
                <CardContent className="flex flex-wrap items-center gap-3 p-3 sm:p-4">
                  <span className="w-7 shrink-0 text-center text-sm font-bold tabular-nums">
                    {row.rank}
                  </span>
                  <span className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-secondary text-xs font-bold">
                    {entryDisplayName(row)
                      .replace(/[^a-zA-Z]/g, '')
                      .slice(0, 2)
                      .toUpperCase() || '??'}
                  </span>

                  <div className="min-w-0 flex-1 basis-48">
                    <div className="flex items-center gap-2">
                      <p className="truncate text-sm font-semibold">{entryDisplayName(row)}</p>
                      {row.is_featured && (
                        <Badge variant="secondary" className="shrink-0">
                          <Zap className="h-3 w-3" />
                          2×
                        </Badge>
                      )}
                    </div>
                    <p className="truncate text-xs text-muted-foreground">
                      {row.opportunities_played} {row.opportunities_played === 1 ? 'event' : 'events'}
                      {'per_game' in row && row.per_game
                        ? ` · ${Object.entries(row.per_game)
                            .map(([game, score]) => `${game} ${formatScore(score)}`)
                            .join(' · ')}`
                        : ''}
                    </p>
                  </div>

                  <div className="shrink-0 text-right">
                    <p className="text-base font-bold tabular-nums">{formatScore(row.score)}</p>
                  </div>
                </CardContent>
              </Card>
            );
          })
        )}
      </section>

      {canLoadMore && !loading && !error && (
        <div className="text-center">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={loadingMore}
            onClick={() => void fetchBoard(entries.length, true)}
          >
            {loadingMore ? 'Loading…' : `Show more (${total - entries.length} left)`}
          </Button>
        </div>
      )}
      </>
      )}
    </div>
  );
};

export default AdminGamesLeaderboard;
