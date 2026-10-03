import { useCallback, useEffect, useMemo, useState } from 'react';
import { Inbox, Minus, Zap } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useContext } from 'react';
import { GamesPageHeader } from '@/components/games/GamesPageHeader';
import { GameCategorySection } from '@/components/games/GameCategorySection';
import { AuthContext } from '@/contexts/AuthContext';
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
  getCouplesLeaderboard,
  getIndividualLeaderboard,
  getMyLeagueStanding,
  type LeagueMyStanding,
} from '@/services/api';
import { describeApiError } from '@/lib/apiError';
import { cn } from '@/lib/utils';

/**
 * League leaderboards, served by the backend award ledger.
 *
 * Three dimensions -- competition (individual vs couples), period (daily /
 * weekly / monthly / all-time, UTC) and scope (overall vs one game) -- all
 * answered by `GET /leaderboards/...`. Scores are server-calculated league
 * points; the page renders them, never computes them. A game that cannot
 * feed the selected competition is never offered as a filter.
 */

const RANK_ACCENTS = ['bg-gradient-hero', 'bg-gradient-romantic', 'bg-gradient-passion'];
const PAGE_SIZE = 25;

type BoardEntry = LeagueIndividualEntry | LeagueCoupleEntry;

const isCoupleEntry = (entry: BoardEntry): entry is LeagueCoupleEntry =>
  !('display_name' in entry);

/** One-line native context for a game board row, where the game is known. */
const nativeSummary = (gameId: string | null, entry: BoardEntry): string | null => {
  const native = entry.native ?? {};
  const num = (value: unknown): number | null =>
    typeof value === 'number' ? value : null;
  switch (gameId) {
    case 'wordle':
    case 'anagram': {
      const attempts = num(native.attempts);
      if (native.solved === true) return attempts !== null ? `${attempts} ${attempts === 1 ? 'try' : 'tries'}` : 'Solved';
      if (native.solved === false) return 'Unsolved';
      return null;
    }
    case 'connections': {
      const groups = num(native.correct_groups);
      return groups !== null ? `${groups}/4 groups` : null;
    }
    case 'trivia-drop': {
      const ms = num(native.response_time_ms);
      if (native.is_correct === true) return ms !== null ? `${ms} ms` : 'Correct';
      if (native.is_correct === false) return 'Missed';
      return null;
    }
    case 'reaction-time': {
      const best = num(native.best_ms);
      return best !== null ? `${best} ms best` : null;
    }
    case 'couple-questionnaire': {
      const correct = num(native.correct_predictions);
      return correct !== null ? `${correct} correct` : null;
    }
    case 'two-key-escape': {
      const elapsed = num(native.elapsed_seconds);
      return elapsed !== null ? `${elapsed}s escape` : null;
    }
    default:
      return null;
  }
};

const formatScore = (score: number): string =>
  score.toLocaleString(undefined, { maximumFractionDigits: 2 });

export const GamesLeaderboardPage = () => {
  // Tolerant read (not useAuth): the page is also rendered in contexts without
  // a provider (tests), where the hook's throw would take the whole board down.
  // Without a viewer there is simply no board row to fall back to.
  const user = useContext(AuthContext)?.user ?? null;
  const [competition, setCompetition] = useState<LeagueCompetitionType>('individual');
  const [period, setPeriod] = useState<LeaguePeriod>('weekly');
  const [gameId, setGameId] = useState<string>(ALL_GAMES_FILTER);
  const [anchor, setAnchor] = useState<string>(todayAnchor());
  const [entries, setEntries] = useState<BoardEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [opportunityCount, setOpportunityCount] = useState<number | null>(null);
  const [standing, setStanding] = useState<LeagueMyStanding | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // A game filter only makes sense for its own competition: switching sides
  // resets to the combined board rather than keeping a stale title.
  const onCompetitionChange = (next: LeagueCompetitionType) => {
    setCompetition(next);
    setGameId(ALL_GAMES_FILTER);
  };

  const scopeGames = useMemo(() => gamesForCompetition(competition), [competition]);
  const activePeriod = LEAGUE_PERIODS.find((option) => option.id === period);
  const selectedGameName =
    gameId === ALL_GAMES_FILTER
      ? 'All games'
      : scopeGames.find((game) => game.id === gameId)?.name ?? 'All games';

  // One game selected: the category section below owns that view, so the
  // combined/score fetch stands down instead of loading a board nobody shows.
  const gameSelected = gameId !== ALL_GAMES_FILTER;

  const fetchBoard = useCallback(
    async (offset: number, append: boolean) => {
      if (gameId !== ALL_GAMES_FILTER) {
        setLoading(false);
        return;
      }
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
        const board =
          competition === 'couple'
            ? await getCouplesLeaderboard(query)
            : await getIndividualLeaderboard(query);
        setEntries((prev) => (append ? [...prev, ...board.entries] : [...board.entries]));
        setTotal(board.total);
        setOpportunityCount(board.opportunity_count ?? null);
        if (!append) {
          // The standing read must never clear the board: it answers a
          // different question (the viewer's own rows) and a failure here
          // used to wipe the standings the reader was already looking at.
          // It is also global-only (no game_id), so on a per-game board the
          // card below falls back to the viewer's row on the visible board.
          try {
            setStanding(
              await getMyLeagueStanding(competition, {
                period,
                ...(period === 'all_time' ? {} : { anchor }),
              }),
            );
          } catch {
            setStanding(null);
          }
        }
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

  // The viewer's row on the board currently on screen, matched by identity
  // (never by display name: two accounts can share "Test User MA2"). On a
  // per-game board this is the game-specific standing `/me` cannot answer,
  // since that endpoint is global-only.
  const boardRowForViewer = user
    ? (entries.find((entry) =>
        isCoupleEntry(entry)
          ? entry.member_a_id === user.id || entry.member_b_id === user.id
          : entry.user_id === user.id,
      ) ?? null)
    : null;
  // The card promises "Your standing here follows the filters selected
  // above": on a per-game board the visible row is the truth; on the combined
  // board the `/me` answer comes first (it holds the global rank even when
  // the viewer sits past the loaded page) with the visible row as fallback.
  const summary =
    gameId === ALL_GAMES_FILTER
      ? (standing?.entries[0] ?? boardRowForViewer)
      : (boardRowForViewer ?? standing?.entries[0] ?? null);
  const topThree = entries.slice(0, 3);
  const remainingRows = entries.slice(3);
  const canLoadMore = entries.length < total;

  return (
    <div>
      <GamesPageHeader
        eyebrow="Leaderboards"
        title="Leaderboards"
        description="League standings across every game — switch the competition, timeframe or title."
      />

      {/* Competition switch */}
      <div className="mb-4 flex flex-wrap items-center gap-2">
        {LEAGUE_COMPETITIONS.map((option) => {
          const isActive = competition === option.id;
          return (
            <button
              key={option.id}
              type="button"
              aria-pressed={isActive}
              onClick={() => onCompetitionChange(option.id)}
              className={cn(
                'inline-flex shrink-0 items-center rounded-full border px-4 py-2 text-xs font-bold uppercase tracking-wider transition-all sm:text-sm',
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

      {/* Period + scope controls */}
      <div className="mb-5 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="no-scrollbar -mx-3 flex gap-2 overflow-x-auto px-3 pb-1 sm:mx-0 sm:flex-wrap sm:px-0 sm:pb-0">
          {LEAGUE_PERIODS.map((option) => {
            const isActive = period === option.id;
            return (
              <button
                key={option.id}
                type="button"
                aria-pressed={isActive}
                onClick={() => setPeriod(option.id)}
                className={cn(
                  'inline-flex shrink-0 items-center rounded-full border px-4 py-2 text-xs font-bold uppercase tracking-wider transition-all sm:text-sm',
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

        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          {period !== 'all_time' && (
            <Input
              type="date"
              value={anchor}
              max={todayAnchor()}
              onChange={(event) => {
                if (event.target.value) setAnchor(event.target.value);
              }}
              aria-label={
                period === 'daily'
                  ? 'Leaderboard day (UTC)'
                  : period === 'weekly'
                    ? 'A day inside the leaderboard week (UTC)'
                    : 'A day inside the leaderboard month (UTC)'
              }
              className="w-full rounded-full sm:w-44"
            />
          )}
          <Select
            value={gameId}
            onValueChange={setGameId}
          >
            <SelectTrigger className="w-full rounded-full sm:w-56" aria-label="Filter by game">
              <SelectValue placeholder="All games" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_GAMES_FILTER}>All games</SelectItem>
              {scopeGames.map((game) => (
                <SelectItem key={game.id} value={game.id}>
                  {game.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      <p className="mb-5 text-xs text-muted-foreground">
        {activePeriod?.hint} · showing {selectedGameName.toLowerCase()}
        {!gameSelected && opportunityCount !== null && opportunityCount > 0
          ? ` · ${opportunityCount} ${opportunityCount === 1 ? 'event' : 'events'} scored`
          : ''}
      </p>

      {gameSelected ? (
        <>
          <GameCategorySection
            gameId={gameId}
            gameName={selectedGameName}
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
      {/* Your rank */}
      <Card className="mb-6 rounded-2xl border-romantic/40 bg-romantic/10">
        <CardContent className="flex flex-wrap items-center gap-x-8 gap-y-4 p-5">
          <div>
            <p className="text-xs font-bold uppercase tracking-wider text-muted-foreground">
              Your rank
            </p>
            <p className="mt-1 text-3xl font-extrabold tabular-nums">
              {summary ? `#${summary.rank}` : '—'}
            </p>
          </div>
          <div>
            <p className="text-xs font-bold uppercase tracking-wider text-muted-foreground">
              Points
            </p>
            <p className="mt-1 text-3xl font-extrabold tabular-nums">
              {summary ? formatScore(summary.score) : '—'}
            </p>
          </div>
          <div>
            <p className="text-xs font-bold uppercase tracking-wider text-muted-foreground">
              Events played
            </p>
            <p className="mt-1 text-3xl font-extrabold tabular-nums">
              {summary ? summary.opportunities_played : '—'}
            </p>
          </div>
          <p className="ml-auto hidden max-w-[16rem] text-xs text-muted-foreground sm:block">
            {summary
              ? 'Your standing here follows the filters selected above.'
              : 'Scores update once each event closes. Play to take a rank.'}
          </p>
        </CardContent>
      </Card>

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
      ) : entries.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-border/70 px-6 py-14 text-center">
          <Inbox className="mx-auto h-8 w-8 text-muted-foreground" />
          <p className="mt-3 text-base font-bold">No standings yet</p>
          <p className="mx-auto mt-1 max-w-sm text-sm text-muted-foreground">
            This board has no finalized scores for the selected filters. Events
            are scored once they close — be the first to play and claim the top
            spot.
          </p>
        </div>
      ) : (
        <>
          {/* Podium */}
          <div className="grid grid-cols-3 gap-2 sm:gap-4">
            {topThree.map((entry, index) => (
              <Card
                key={isCoupleEntry(entry) ? `${entry.member_a_id}:${entry.member_b_id}` : entry.user_id}
                className={cn(
                  'rounded-2xl border p-3 text-center sm:p-5',
                  entry.rank === 1
                    ? 'border-romantic/60 bg-romantic/10 shadow-[var(--shadow-romantic)]'
                    : 'border-border/60 bg-card/70 backdrop-blur',
                )}
              >
                <span
                  className={cn(
                    'mx-auto flex h-12 w-12 items-center justify-center rounded-full text-sm font-extrabold text-white sm:h-14 sm:w-14 sm:text-base',
                    RANK_ACCENTS[index],
                  )}
                >
                  {entryDisplayName(entry)
                    .replace(/[^a-zA-Z]/g, '')
                    .slice(0, 2)
                    .toUpperCase() || '??'}
                </span>
                <p className="mt-2 truncate text-xs font-bold sm:text-sm">
                  {entryDisplayName(entry)}
                </p>
                <p className="truncate text-sm font-extrabold tabular-nums text-romantic sm:text-lg">
                  {formatScore(entry.score)}
                </p>
                <p className="mt-1 text-xs font-bold uppercase tracking-wider text-muted-foreground">
                  Rank #{entry.rank}
                </p>
              </Card>
            ))}
          </div>

          {/* Remaining rows */}
          {remainingRows.length > 0 && (
            <Card className="mt-4 rounded-2xl border-border/60 bg-card/70 backdrop-blur">
              <CardHeader className="pb-3">
                <CardTitle className="text-base font-bold">Full standings</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2">
                {remainingRows.map((entry) => (
                  <LeaderboardRow
                    key={isCoupleEntry(entry) ? `${entry.member_a_id}:${entry.member_b_id}` : entry.user_id}
                    entry={entry}
                    gameId={gameId === ALL_GAMES_FILTER ? null : gameId}
                  />
                ))}
              </CardContent>
            </Card>
          )}

          {canLoadMore && (
            <div className="mt-4 text-center">
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="rounded-full"
                disabled={loadingMore}
                onClick={() => void fetchBoard(entries.length, true)}
              >
                {loadingMore ? 'Loading…' : `Show more (${total - entries.length} left)`}
              </Button>
            </div>
          )}
        </>
      )}

      <p className="mt-6 text-center text-xs text-muted-foreground">
        Global points reward absolute performance (0–100 per event, same result
        always earns the same), averaged per game so frequent releases cannot
        dominate. A featured event awards double.
      </p>
      </>
      )}
    </div>
  );
};

const TrendPlaceholder = () => (
  <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
    <Minus className="h-3.5 w-3.5" />
    <span className="hidden sm:inline">League points</span>
  </span>
);

const LeaderboardRow = ({ entry, gameId }: { entry: BoardEntry; gameId: string | null }) => {
  const summary = nativeSummary(gameId, entry);
  return (
    <div className="flex items-center gap-3 rounded-xl border border-border/50 bg-background/40 p-3 transition-colors hover:border-romantic/30">
      <span className="w-6 text-center text-sm font-extrabold tabular-nums text-muted-foreground">
        {entry.rank}
      </span>
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-gradient-hero text-xs font-bold text-white">
        {entryDisplayName(entry)
          .replace(/[^a-zA-Z]/g, '')
          .slice(0, 2)
          .toUpperCase() || '??'}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <span className="truncate">{entryDisplayName(entry)}</span>
          {entry.is_featured && (
            <Badge variant="secondary" className="hidden shrink-0 items-center gap-1 sm:inline-flex">
              <Zap className="h-3 w-3" />
              2× featured
            </Badge>
          )}
        </div>
        <div className="mt-0.5 text-xs text-muted-foreground">
          {summary ?? <TrendPlaceholder />}
          {entry.opportunities_played > 0 && summary
            ? ` · ${entry.opportunities_played} ${entry.opportunities_played === 1 ? 'event' : 'events'}`
            : ''}
        </div>
      </div>
      <span className="text-sm font-extrabold tabular-nums sm:text-base">
        {formatScore(entry.score)}
      </span>
    </div>
  );
};

export default GamesLeaderboardPage;
