import { useMemo, useState } from 'react';
import { Inbox, Minus, TrendingDown, TrendingUp } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { GamesPageHeader } from '@/components/games/GamesPageHeader';
import { useGamesConfig } from '@/contexts/GamesConfigContext';
import { AVAILABLE_GAMES } from '@/lib/gamesCatalog';
import {
  ALL_GAMES_FILTER,
  getLeaderboardEntries,
  LEADERBOARD_SCOPES,
  summarizeCurrentUser,
  type LeaderboardEntry,
  type LeaderboardScope,
} from '@/lib/gamesLeaderboard';
import { cn } from '@/lib/utils';

/**
 * Leaderboards.
 *
 * Scope and game filters select from the leaderboard query; with no
 * leaderboard backend the boards are empty and the page shows its empty
 * state.
 */

const RANK_ACCENTS = ['bg-gradient-hero', 'bg-gradient-romantic', 'bg-gradient-passion'];

const TrendIndicator = ({ delta }: { delta: number }) => {
  if (delta === 0) {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
        <Minus className="h-3.5 w-3.5" />
        <span className="hidden sm:inline">No change</span>
      </span>
    );
  }

  const isUp = delta > 0;
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 text-xs font-semibold',
        isUp ? 'text-emerald-500' : 'text-red-500',
      )}
    >
      {isUp ? <TrendingUp className="h-3.5 w-3.5" /> : <TrendingDown className="h-3.5 w-3.5" />}
      {Math.abs(delta)}
    </span>
  );
};

export const GamesLeaderboardPage = () => {
  const [scope, setScope] = useState<LeaderboardScope>('global');
  const [gameId, setGameId] = useState<string>(ALL_GAMES_FILTER);
  // Only live titles are offered as filters; a switched-off game still has history, it just
  // is not somewhere a player would go looking for it.
  const { isEnabled } = useGamesConfig();

  const entries = useMemo(() => getLeaderboardEntries({ scope, gameId }), [scope, gameId]);
  const summary = useMemo(() => summarizeCurrentUser(entries), [entries]);
  const activeScope = LEADERBOARD_SCOPES.find((option) => option.id === scope);
  const topThree = entries.slice(0, 3);
  const remainingRows = entries.slice(3);
  const selectedGameName =
    gameId === ALL_GAMES_FILTER
      ? 'All games'
      : AVAILABLE_GAMES.find((game) => game.id === gameId)?.name ?? 'All games';

  return (
    <div>
      <GamesPageHeader
        eyebrow="Leaderboards"
        title="Leaderboards"
        description="Rankings across every game — switch the timeframe or focus on a single title."
      />

      {/* Filter controls */}
      <div className="mb-5 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="no-scrollbar -mx-3 flex gap-2 overflow-x-auto px-3 pb-1 sm:mx-0 sm:flex-wrap sm:px-0 sm:pb-0">
          {LEADERBOARD_SCOPES.map((option) => {
            const isActive = scope === option.id;
            return (
              <button
                key={option.id}
                type="button"
                aria-pressed={isActive}
                onClick={() => setScope(option.id)}
                className={cn(
                  'inline-flex shrink-0 items-center rounded-full border px-4 py-2 text-xs font-bold uppercase tracking-wider transition-all sm:text-sm',
                  isActive
                    ? 'border-transparent bg-gradient-romantic text-white shadow-romantic'
                    : 'border-border/70 bg-card/60 text-muted-foreground hover:border-romantic/50 hover:text-foreground',
                )}
              >
                {option.label}
              </button>
            );
          })}
        </div>

        <Select value={gameId} onValueChange={setGameId}>
          <SelectTrigger className="w-full rounded-full sm:w-56" aria-label="Filter by game">
            <SelectValue placeholder="All games" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL_GAMES_FILTER}>All games</SelectItem>
            {AVAILABLE_GAMES.filter(
              (game) => game.status === 'available' && isEnabled(game.id),
            ).map((game) => (
              <SelectItem key={game.id} value={game.id}>
                {game.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <p className="mb-5 text-xs text-muted-foreground">
        {activeScope?.hint} · showing {selectedGameName.toLowerCase()}
      </p>

      {/* Your rank */}
      <Card className="mb-6 rounded-2xl border-romantic/40 bg-romantic/10">
        <CardContent className="flex flex-wrap items-center gap-x-8 gap-y-4 p-5">
          <div>
            <p className="text-[11px] font-bold uppercase tracking-wider text-muted-foreground">
              Your rank
            </p>
            <p className="mt-1 text-3xl font-extrabold tabular-nums">
              {summary ? `#${summary.rank}` : '—'}
            </p>
          </div>
          <div>
            <p className="text-[11px] font-bold uppercase tracking-wider text-muted-foreground">
              Points
            </p>
            <p className="mt-1 text-3xl font-extrabold tabular-nums">
              {summary ? summary.score.toLocaleString() : '—'}
            </p>
          </div>
          <div>
            <p className="text-[11px] font-bold uppercase tracking-wider text-muted-foreground">
              Standing
            </p>
            <p className="mt-1 text-3xl font-extrabold tabular-nums">
              {summary ? `Top ${summary.topPercent}%` : '—'}
            </p>
          </div>
          <p className="ml-auto hidden max-w-[16rem] text-xs text-muted-foreground sm:block">
            Scores update as you play. Your standing here follows the filters selected above.
          </p>
        </CardContent>
      </Card>

      {entries.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-border/70 px-6 py-14 text-center">
          <Inbox className="mx-auto h-8 w-8 text-muted-foreground" />
          <p className="mt-3 text-base font-bold">No standings yet</p>
          <p className="mx-auto mt-1 max-w-sm text-sm text-muted-foreground">
            This board has no scores for the selected filters. Be the first to play and claim
            the top spot.
          </p>
        </div>
      ) : (
        <>
          {/* Podium */}
          <div className="grid grid-cols-3 gap-2 sm:gap-4">
            {topThree.map((entry, index) => (
              <Card
                key={entry.playerId}
                className={cn(
                  'rounded-2xl border p-3 text-center sm:p-5',
                  entry.rank === 1
                    ? 'border-romantic/60 bg-romantic/10 shadow-romantic'
                    : 'border-border/60 bg-card/70 backdrop-blur',
                  entry.isCurrentUser && 'ring-2 ring-romantic/60',
                )}
              >
                <span
                  className={cn(
                    'mx-auto flex h-12 w-12 items-center justify-center rounded-full text-sm font-extrabold text-white sm:h-14 sm:w-14 sm:text-base',
                    RANK_ACCENTS[index],
                  )}
                >
                  {entry.monogram}
                </span>
                <p className="mt-2 truncate text-xs font-bold sm:text-sm">{entry.displayName}</p>
                <p className="truncate text-sm font-extrabold tabular-nums text-romantic sm:text-lg">
                  {entry.score.toLocaleString()}
                </p>
                <p className="mt-1 text-[10px] font-bold uppercase tracking-wider text-muted-foreground sm:text-xs">
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
                  <LeaderboardRow key={entry.playerId} entry={entry} />
                ))}
              </CardContent>
            </Card>
          )}
        </>
      )}

      <p className="mt-6 text-center text-xs text-muted-foreground">
        Standings go live with the games leaderboard backend.
      </p>
    </div>
  );
};

const LeaderboardRow = ({ entry }: { entry: LeaderboardEntry }) => (
  <div
    className={cn(
      'flex items-center gap-3 rounded-xl border p-3 transition-colors',
      entry.isCurrentUser
        ? 'border-romantic/50 bg-romantic/10'
        : 'border-border/50 bg-background/40 hover:border-romantic/30',
    )}
  >
    <span className="w-6 text-center text-sm font-extrabold tabular-nums text-muted-foreground">
      {entry.rank}
    </span>
    <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-gradient-hero text-[11px] font-bold text-white">
      {entry.monogram}
    </span>
    <div className="min-w-0 flex-1">
      {/* A div, not a p: the Badge below is a div, and <div> inside <p> is invalid DOM. */}
      <div className="flex items-center gap-2 text-sm font-semibold">
        <span className="truncate">{entry.isCurrentUser ? 'You' : entry.displayName}</span>
        {entry.isCurrentUser && (
          <Badge variant="secondary" className="hidden shrink-0 sm:inline-flex">
            Your rank
          </Badge>
        )}
      </div>
      <div className="mt-0.5">
        <TrendIndicator delta={entry.trendDelta} />
      </div>
    </div>
    <span className="text-sm font-extrabold tabular-nums sm:text-base">
      {entry.score.toLocaleString()}
    </span>
  </div>
);

export default GamesLeaderboardPage;
