import { useMemo, useState } from 'react';
import { Eye, Minus, RefreshCcw, Trash2, TrendingDown, TrendingUp, Trophy, UserX } from 'lucide-react';
import { toast } from 'sonner';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Card, CardContent } from '@/components/ui/card';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { StatCard } from '@/components/games/StatCard';
import { useGamesConfig } from '@/contexts/GamesConfigContext';
import { AVAILABLE_GAMES, GAME_TYPES, gameTypeLabel } from '@/lib/gamesCatalog';
import {
  ADMIN_BOARD_PERIODS,
  ADMIN_BOARD_SCOPES,
  ALL_GAMES_FILTER,
  adminResultLabel,
  getAdminLeaderboardRows,
  summarizeAdminBoard,
  type AdminBoardGameTypeFilter,
  type AdminBoardPeriod,
  type AdminBoardScope,
  type AdminLeaderboardRow,
} from '@/lib/gamesLeaderboard';
import { formatActivityTime } from '@/lib/adminGamesAnalytics';

/** The destructive choices, one dialog for all of them -- the pattern the other admin queues use. */
type PendingAction =
  | { kind: 'score'; row: AdminLeaderboardRow }
  | { kind: 'player'; row: AdminLeaderboardRow }
  | { kind: 'board' };

const RESULT_VARIANT: Record<AdminLeaderboardRow['result'], 'secondary' | 'outline' | 'destructive'> = {
  completed: 'secondary',
  forfeited: 'outline',
  abandoned: 'destructive',
};

const TrendIndicator = ({ delta }: { delta: number }) => {
  if (delta > 0) {
    return (
      <span className="inline-flex items-center gap-0.5 text-xs font-medium text-emerald-500">
        <TrendingUp className="h-3.5 w-3.5" />
        {delta}
      </span>
    );
  }
  if (delta < 0) {
    return (
      <span className="inline-flex items-center gap-0.5 text-xs font-medium text-rose-500">
        <TrendingDown className="h-3.5 w-3.5" />
        {Math.abs(delta)}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center text-xs text-muted-foreground">
      <Minus className="h-3.5 w-3.5" />
    </span>
  );
};

/**
 * The administrative leaderboard: standings with the columns and the controls.
 *
 * Filters are two dimensions -- scope and period -- rather than the player board's one list of
 * scopes, because the admin questions are "which game" and "since when" separately, and the
 * game selector only appears when it means something. Narrowing the global board to Single or
 * Couple re-ranks what is left, so positions still read 1, 2, 3...
 *
 * Removals are session state on top of the deterministic query: this prototype cannot delete a
 * score from a synthesiser, so the board is the query minus what this admin has removed, and
 * the actions log themselves into the shared activity feed the overview screen reads. When a
 * backend arrives, the same three buttons become three requests and the filtering disappears.
 */
export const AdminGamesLeaderboard = () => {
  const { logActivity } = useGamesConfig();

  const [scope, setScope] = useState<AdminBoardScope>('global');
  const [period, setPeriod] = useState<AdminBoardPeriod>('weekly');
  const [gameId, setGameId] = useState<string>(AVAILABLE_GAMES[0]?.id ?? ALL_GAMES_FILTER);
  const [gameType, setGameType] = useState<AdminBoardGameTypeFilter>('all');

  const [removedPlayers, setRemovedPlayers] = useState<string[]>([]);
  const [removedRows, setRemovedRows] = useState<string[]>([]);
  const [clearedForKey, setClearedForKey] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null);
  const [inspecting, setInspecting] = useState<AdminLeaderboardRow | null>(null);

  const query = useMemo(
    () => ({
      scope,
      period,
      gameId: scope === 'game' ? gameId : ALL_GAMES_FILTER,
      gameType: scope === 'global' ? gameType : 'all',
    }),
    [scope, period, gameId, gameType],
  );

  const queryKey = `${query.scope}|${query.period}|${query.gameId}|${query.gameType}`;
  const baseRows = useMemo(() => getAdminLeaderboardRows(query), [query]);
  const cleared = clearedForKey === queryKey;

  const rows = useMemo(() => {
    if (cleared) return [];
    const filtered = baseRows.filter(
      (row) =>
        !removedPlayers.includes(row.playerId) &&
        !removedRows.includes(`${row.playerId}::${row.gameId}`),
    );
    return filtered.map((row, index) => ({ ...row, rank: index + 1 }));
  }, [baseRows, removedPlayers, removedRows, cleared]);

  const summary = useMemo(() => summarizeAdminBoard(rows), [rows]);

  const scopeOption = ADMIN_BOARD_SCOPES.find((option) => option.id === scope);
  const periodOption = ADMIN_BOARD_PERIODS.find((option) => option.id === period);
  const currentGame = AVAILABLE_GAMES.find((game) => game.id === query.gameId);
  const filterCaption =
    query.scope === 'game'
      ? `${currentGame?.name ?? 'Unknown game'} · ${periodOption?.label ?? ''}`
      : `Global · ${periodOption?.label ?? ''} · ${
          query.gameType === 'all' ? 'Single and Couple' : gameTypeLabel(query.gameType)
        }`;

  const confirmAction = () => {
    if (!pendingAction) return;

    if (pendingAction.kind === 'score') {
      const { row } = pendingAction;
      const gameName = AVAILABLE_GAMES.find((game) => game.id === row.gameId)?.name ?? row.gameId;
      setRemovedRows((prev) => [...prev, `${row.playerId}::${row.gameId}`]);
      toast.success(`${row.displayName}'s score on ${gameName} removed.`);
      logActivity({
        kind: 'leaderboard',
        gameId: row.gameId,
        message: `${row.displayName}'s score on ${gameName} removed`,
      });
    } else if (pendingAction.kind === 'player') {
      const { row } = pendingAction;
      setRemovedPlayers((prev) => [...prev, row.playerId]);
      toast.success(`${row.displayName} reset on this board.`);
      logActivity({ kind: 'leaderboard', message: `${row.displayName} reset on the leaderboard` });
    } else {
      setClearedForKey(queryKey);
      toast.success('Leaderboard reset for these filters.');
      logActivity({ kind: 'leaderboard', message: 'Leaderboard reset for the current filters' });
    }

    setPendingAction(null);
  };

  const actionCopy = (action: PendingAction | null): { title: string; description: string; confirm: string } => {
    if (action?.kind === 'score') {
      return {
        title: "Remove this score?",
        description: `${action.row.displayName}'s ${action.row.score.toLocaleString()} point standing on this board will be taken down. The session history stays intact.`,
        confirm: 'Remove score',
      };
    }
    if (action?.kind === 'player') {
      return {
        title: 'Reset this player?',
        description: `Every standing ${action.row.displayName} holds on this board will be cleared. They start again from zero.`,
        confirm: 'Reset player',
      };
    }
    return {
      title: 'Reset the leaderboard?',
      description: 'Every standing on the board for the current filters will be cleared. You can restore the view afterwards.',
      confirm: 'Reset leaderboard',
    };
  };

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-col gap-2">
        <h1 className="text-3xl font-semibold">Leaderboard</h1>
        <p className="text-sm text-muted-foreground">
          Inspect standings, remove scores and reset boards. Scope and period apply together,
          so "Game · Daily" is one title's board since midnight.
        </p>
      </header>

      <section className="flex flex-wrap items-end gap-4">
        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-medium uppercase text-muted-foreground">Scope</span>
          <Select value={scope} onValueChange={(next) => setScope(next as AdminBoardScope)}>
            <SelectTrigger className="w-36">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {ADMIN_BOARD_SCOPES.map((option) => (
                <SelectItem key={option.id} value={option.id}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="flex flex-col gap-1.5">
          <span className="text-xs font-medium uppercase text-muted-foreground">Period</span>
          <Select value={period} onValueChange={(next) => setPeriod(next as AdminBoardPeriod)}>
            <SelectTrigger className="w-36">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {ADMIN_BOARD_PERIODS.map((option) => (
                <SelectItem key={option.id} value={option.id}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        {query.scope === 'global' ? (
          <div className="flex flex-col gap-1.5">
            <span className="text-xs font-medium uppercase text-muted-foreground">Game type</span>
            <Select
              value={gameType}
              onValueChange={(next) => setGameType(next as AdminBoardGameTypeFilter)}
            >
              <SelectTrigger className="w-44">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">Single and Couple</SelectItem>
                {GAME_TYPES.map((type) => (
                  <SelectItem key={type.id} value={type.id}>
                    {type.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        ) : (
          <div className="flex flex-col gap-1.5">
            <span className="text-xs font-medium uppercase text-muted-foreground">Game</span>
            <Select value={gameId} onValueChange={setGameId}>
              <SelectTrigger className="w-48">
                <SelectValue placeholder="Choose a game" />
              </SelectTrigger>
              <SelectContent>
                {AVAILABLE_GAMES.map((game) => (
                  <SelectItem key={game.id} value={game.id}>
                    {game.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}

        <div className="ml-auto">
          <Button
            type="button"
            variant="destructive"
            size="sm"
            disabled={cleared || rows.length === 0}
            onClick={() => setPendingAction({ kind: 'board' })}
          >
            <RefreshCcw />
            Reset leaderboard
          </Button>
        </div>
      </section>

      <p className="-mt-3 text-xs text-muted-foreground">
        {scopeOption?.hint} · {filterCaption}
      </p>

      <section className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="Players ranked"
          value={summary.rows}
          hint={scopeOption?.hint}
          icon={Trophy}
          accent="bg-gradient-romantic"
        />
        <StatCard
          label="Sessions"
          value={summary.totalPlays.toLocaleString()}
          hint="Played inside this period"
          icon={TrendingUp}
          accent="bg-gradient-love"
        />
        <StatCard
          label="Top score"
          value={summary.topScore.toLocaleString()}
          hint={`Average ${summary.averageScore.toLocaleString()}`}
          icon={Eye}
          accent="bg-gradient-romantic"
        />
        <StatCard
          label="Incomplete"
          value={summary.incompleteSessions}
          hint="Forfeited or abandoned"
          icon={UserX}
          accent="bg-gradient-love"
        />
      </section>

      <section aria-label="Standings" className="space-y-3">
        {cleared ? (
          <Card className="rounded-2xl border-border/60 bg-card/70">
            <CardContent className="py-12 text-center">
              <p className="text-sm text-muted-foreground">
                The leaderboard was reset for these filters.
              </p>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="mt-4"
                onClick={() => setClearedForKey(null)}
              >
                Restore standings
              </Button>
            </CardContent>
          </Card>
        ) : rows.length === 0 ? (
          <Card className="rounded-2xl border-border/60 bg-card/70">
            <CardContent className="py-12 text-center text-sm text-muted-foreground">
              No standings match these filters.
            </CardContent>
          </Card>
        ) : (
          rows.map((row) => {
            const gameName =
              AVAILABLE_GAMES.find((game) => game.id === row.gameId)?.name ?? row.gameId;
            return (
              <Card key={`${row.playerId}::${row.gameId}`} className="rounded-2xl border-border/60 bg-card/70">
                <CardContent className="flex flex-wrap items-center gap-3 p-3 sm:p-4">
                  <span className="w-7 shrink-0 text-center text-sm font-bold tabular-nums">
                    {row.rank}
                  </span>
                  <span className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-secondary text-xs font-bold">
                    {row.monogram}
                  </span>

                  <div className="min-w-0 flex-1 basis-48">
                    <div className="flex items-center gap-2">
                      <p className="truncate text-sm font-semibold">{row.displayName}</p>
                      <TrendIndicator delta={row.trendDelta} />
                    </div>
                    <p className="truncate text-xs text-muted-foreground">
                      {gameName} · {gameTypeLabel(row.gameType)} · {row.plays} plays ·{' '}
                      {formatActivityTime(row.lastPlayedAt)}
                    </p>
                  </div>

                  <div className="shrink-0 text-right">
                    <p className="text-base font-bold tabular-nums">
                      {row.score.toLocaleString()}
                    </p>
                    <Badge variant={RESULT_VARIANT[row.result]} className="mt-1">
                      {adminResultLabel(row.result)}
                    </Badge>
                  </div>

                  <div className="flex shrink-0 flex-wrap gap-1.5">
                    <Button type="button" variant="ghost" size="sm" onClick={() => setInspecting(row)}>
                      <Eye />
                      Inspect
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => setPendingAction({ kind: 'score', row })}
                    >
                      <Trash2 />
                      Remove score
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => setPendingAction({ kind: 'player', row })}
                    >
                      <UserX />
                      Reset player
                    </Button>
                  </div>
                </CardContent>
              </Card>
            );
          })
        )}
      </section>

      <AlertDialog
        open={pendingAction !== null}
        onOpenChange={(open) => {
          if (!open) setPendingAction(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{actionCopy(pendingAction).title}</AlertDialogTitle>
            <AlertDialogDescription>{actionCopy(pendingAction).description}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={confirmAction}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {actionCopy(pendingAction).confirm}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={inspecting !== null}
        onOpenChange={(open) => {
          if (!open) setInspecting(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{inspecting?.displayName ?? ''}</AlertDialogTitle>
            <AlertDialogDescription>
              The full standing behind this row, as the synthesiser reports it.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {inspecting && (
            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
              <dt className="text-muted-foreground">Game</dt>
              <dd className="text-right font-medium">
                {AVAILABLE_GAMES.find((game) => game.id === inspecting.gameId)?.name ??
                  inspecting.gameId}
              </dd>
              <dt className="text-muted-foreground">Type</dt>
              <dd className="text-right font-medium">{gameTypeLabel(inspecting.gameType)}</dd>
              <dt className="text-muted-foreground">Score</dt>
              <dd className="text-right font-medium tabular-nums">
                {inspecting.score.toLocaleString()}
              </dd>
              <dt className="text-muted-foreground">Sessions</dt>
              <dd className="text-right font-medium tabular-nums">{inspecting.plays}</dd>
              <dt className="text-muted-foreground">Result</dt>
              <dd className="text-right font-medium">{adminResultLabel(inspecting.result)}</dd>
              <dt className="text-muted-foreground">Rank trend</dt>
              <dd className="text-right font-medium">
                {inspecting.trendDelta > 0
                  ? `Up ${inspecting.trendDelta}`
                  : inspecting.trendDelta < 0
                    ? `Down ${Math.abs(inspecting.trendDelta)}`
                    : 'No change'}
              </dd>
              <dt className="text-muted-foreground">Last played</dt>
              <dd className="text-right font-medium">
                {new Date(inspecting.lastPlayedAt).toLocaleString()}
              </dd>
            </dl>
          )}
          <AlertDialogFooter>
            <AlertDialogAction>Close</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
};

export default AdminGamesLeaderboard;
