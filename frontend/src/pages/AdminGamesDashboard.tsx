import { useEffect, useMemo, useState } from 'react';
import { NavLink } from 'react-router-dom';
import { Activity, ArrowRight, CalendarClock, Gamepad2, SlidersHorizontal, Star, Users } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Progress } from '@/components/ui/progress';
import { StatCard } from '@/components/games/StatCard';
import { useGamesConfig } from '@/contexts/GamesConfigContext';
import {
  formatActivityTime,
  gamesAnalytics,
  type GamesActivityKind,
} from '@/lib/adminGamesAnalytics';
import { AVAILABLE_GAMES, getGameById } from '@/lib/gamesCatalog';
import {
  categoryEntryName,
  categoryUI,
  formatMetric,
} from '@/lib/gameCategoryBoards';
import {
  getAdminGameRecords,
  getAdminGameStats,
  getAdminIndividualLeaderboard,
  getFeaturedGames,
  getGameConfigs,
  type FeaturedGame,
  type GameConfig,
  type GameRecordHighlight,
  type GameStatsSummary,
  type LeagueIndividualEntry,
} from '@/services/api';
import { featuredDateLongLabel } from '@/lib/gamesFeaturedSchedule';
import { cn } from '@/lib/utils';

/** One word per activity kind, for the little tag in front of each log line. */
const ACTIVITY_LABELS: Record<GamesActivityKind, string> = {
  featured: 'Featured',
  availability: 'Availability',
  settings: 'Settings',
  leaderboard: 'Leaderboard',
  puzzle: 'Puzzle',
  system: 'System',
};

type LoadState = 'loading' | 'ready' | 'error';

/** Today as UTC `YYYY-MM-DD`: the calendar the spotlight and the stats use. */
const todayIso = (): string => new Date().toISOString().slice(0, 10);

/**
 * The section's front page: availability, today's spotlight, this week's play and a way into
 * the two screens that do the actual work.
 *
 * Every figure is the server's: availability from the per-game configuration the manage
 * screens edit, the spotlight from the featured schedule, and play counts from the same
 * finished-round collectors behind a player's own summary. The local catalogue rotation is
 * deliberately absent -- naming a game the server never featured is a mock, and showing zero
 * while play exists is worse than showing that the request has not answered yet.
 */
export const AdminGamesDashboard = () => {
  const { activity } = useGamesConfig();

  // Availability, today's pin and play telemetry are three independent server
  // reads; each keeps its own state so one failure cannot blank the others.
  const [configs, setConfigs] = useState<GameConfig[] | null>(null);
  const [configsState, setConfigsState] = useState<LoadState>('loading');
  const [stats, setStats] = useState<GameStatsSummary | null>(null);
  const [statsState, setStatsState] = useState<LoadState>('loading');
  const [featured, setFeatured] = useState<FeaturedGame | null>(null);
  const [featuredState, setFeaturedState] = useState<LoadState>('loading');

  // The weekly individual board, top of the list only: enough to tell
  // whether play is happening and who is leading.
  const [snapshot, setSnapshot] = useState<LeagueIndividualEntry[]>([]);
  // Each game's headline record (fastest time, fewest guesses, ...) so the
  // overview shows the expanded rankings' headline figures without opening
  // every board. Null while the request is in flight.
  const [records, setRecords] = useState<GameRecordHighlight[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    getGameConfigs()
      .then((rows) => {
        if (cancelled) return;
        setConfigs(rows);
        setConfigsState('ready');
      })
      .catch(() => {
        if (cancelled) return;
        setConfigs(null);
        setConfigsState('error');
      });
    getAdminGameStats()
      .then((rows) => {
        if (cancelled) return;
        setStats(rows);
        setStatsState('ready');
      })
      .catch(() => {
        if (cancelled) return;
        setStats(null);
        setStatsState('error');
      });
    const today = todayIso();
    getFeaturedGames()
      .then((rows) => {
        if (cancelled) return;
        setFeatured(rows.find((row) => row.game_date === today) ?? null);
        setFeaturedState('ready');
      })
      .catch(() => {
        if (cancelled) return;
        setFeatured(null);
        setFeaturedState('error');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    getAdminIndividualLeaderboard({ period: 'weekly', limit: 5 })
      .then((board) => {
        if (!cancelled) setSnapshot(board.entries);
      })
      .catch(() => {
        if (!cancelled) setSnapshot([]);
      });
    getAdminGameRecords()
      .then((answer) => {
        if (!cancelled) setRecords(answer.highlights);
      })
      .catch(() => {
        if (!cancelled) setRecords([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const analytics = useMemo(
    () => (configs ? gamesAnalytics(configs, stats) : null),
    [configs, stats],
  );

  // Every title players cannot see or open: hidden ones are absent from the
  // selection entirely, disabled ones are listed as temporarily unavailable.
  const unavailableGames = useMemo(() => {
    if (!configs) return [];
    const configById = new Map(configs.map((entry) => [entry.game_id, entry]));
    return AVAILABLE_GAMES.flatMap((game) => {
      const config = configById.get(game.id);
      if (!config) return [];
      if (config.is_hidden) return [{ game, label: 'Hidden' }];
      if (!config.enabled) return [{ game, label: 'Off' }];
      return [];
    });
  }, [configs]);

  const statsReady = statsState === 'ready' && stats !== null;
  const featuredGame = featured ? getGameById(featured.game_id) : undefined;
  const FeaturedIcon = featuredGame?.icon;

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          <h1 className="text-3xl font-semibold">Overview</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Availability, today's spotlight and how much is being played.
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <Button asChild variant="outline">
            <NavLink to="/admin/games/manage">Manage games</NavLink>
          </Button>
          <Button asChild>
            <NavLink to="/admin/games/leaderboard">
              Leaderboard
              <ArrowRight />
            </NavLink>
          </Button>
        </div>
      </header>

      <section className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="Games live"
          value={analytics ? `${analytics.enabledGames}/${analytics.totalGames}` : '—'}
          hint={
            configsState === 'loading'
              ? 'Loading game configuration…'
              : configsState === 'error'
                ? 'Availability is unavailable'
                : analytics && analytics.disabledGames > 0
                  ? `${analytics.disabledGames} not live`
                  : 'Every title switched on'
          }
          icon={Gamepad2}
          accent="bg-gradient-romantic"
        />
        <StatCard
          label="Players today"
          value={statsReady ? stats.players_today.toLocaleString() : '—'}
          hint={
            statsState === 'loading'
              ? 'Loading play activity…'
              : statsState === 'error'
                ? 'Play stats are unavailable'
                : `${stats.players_this_week.toLocaleString()} this week`
          }
          icon={Users}
          accent="bg-gradient-love"
        />
        <StatCard
          label="Plays today"
          value={statsReady ? stats.plays_today.toLocaleString() : '—'}
          hint={
            statsState === 'loading'
              ? 'Loading play activity…'
              : statsState === 'error'
                ? 'Play stats are unavailable'
                : `${stats.plays_this_week.toLocaleString()} this week`
          }
          icon={Activity}
          accent="bg-gradient-romantic"
        />
        <StatCard
          label="Plays all time"
          value={statsReady ? stats.plays_all_time.toLocaleString() : '—'}
          hint={
            statsState === 'loading'
              ? 'Loading play activity…'
              : statsState === 'error'
                ? 'Play stats are unavailable'
                : analytics
                  ? `${analytics.activeGameTypes} game types in play`
                  : 'Loading game configuration…'
          }
          icon={Star}
          accent="bg-gradient-love"
        />
      </section>

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
            <CardHeader>
              <CardTitle className="text-xl">Most played this week</CardTitle>
              <CardDescription>
                Titles ranked by finished rounds, as a share of the week's play.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              {statsState === 'loading' ? (
                <p className="py-6 text-center text-sm text-muted-foreground">
                  Loading this week's play…
                </p>
              ) : statsState === 'error' ? (
                <p className="py-6 text-center text-sm text-muted-foreground">
                  Play activity is unavailable right now.
                </p>
              ) : !analytics || analytics.topGames.length === 0 ? (
                <p className="py-6 text-center text-sm text-muted-foreground">
                  No finished rounds this week yet.
                </p>
              ) : (
                analytics.topGames.map(({ game, playsWeek, share }) => {
                  const Icon = game.icon;
                  return (
                    <div key={game.id} className="flex items-center gap-3">
                      <span className={cn('shrink-0 rounded-xl p-2 text-white', game.accent)}>
                        <Icon className="h-4 w-4" />
                      </span>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-baseline justify-between gap-2">
                          <p className="truncate text-sm font-semibold">{game.name}</p>
                          <p className="shrink-0 text-xs tabular-nums text-muted-foreground">
                            {playsWeek.toLocaleString()} plays · {share}%
                          </p>
                        </div>
                        <Progress value={share} className="mt-1.5 h-1.5" />
                      </div>
                    </div>
                  );
                })
              )}
            </CardContent>
          </Card>

          <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
            <CardHeader>
              <CardTitle className="text-xl">Recent activity</CardTitle>
              <CardDescription>
                Featured picks and configuration changes made in this session.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              {activity.length === 0 ? (
                <p className="py-4 text-center text-sm text-muted-foreground">
                  No activity yet. Featured picks and admin actions appear here.
                </p>
              ) : (
                activity.slice(0, 6).map((entry) => (
                  <div
                    key={entry.id}
                    className="flex items-start justify-between gap-3 border-b border-border/40 pb-3 last:border-0 last:pb-0"
                  >
                    <div className="min-w-0">
                      <Badge variant="outline" className="mb-1.5 text-xs uppercase tracking-wider">
                        {ACTIVITY_LABELS[entry.kind]}
                      </Badge>
                      <p className="truncate text-sm">{entry.message}</p>
                    </div>
                    <span className="shrink-0 text-xs text-muted-foreground">
                      {formatActivityTime(entry.at)}
                    </span>
                  </div>
                ))
              )}
            </CardContent>
          </Card>
        </div>

        <div className="space-y-6">
          <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
            <CardHeader>
              <CardTitle className="text-xl">Featured today</CardTitle>
              <CardDescription>What the player-facing spotlight is showing.</CardDescription>
            </CardHeader>
            <CardContent>
              {featuredState === 'loading' ? (
                <p className="text-sm text-muted-foreground">Checking today's spotlight…</p>
              ) : featuredState === 'error' ? (
                <>
                  <p className="text-sm font-semibold">The spotlight could not be loaded</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Refresh the page to try again.
                  </p>
                </>
              ) : featured && featuredGame ? (
                <div className="flex items-center gap-3">
                  <span
                    className={cn(
                      'shrink-0 rounded-xl p-2.5 text-white',
                      featuredGame.accent,
                    )}
                  >
                    {FeaturedIcon ? (
                      <FeaturedIcon className="h-5 w-5" />
                    ) : (
                      <Star className="h-5 w-5" />
                    )}
                  </span>
                  <div className="min-w-0">
                    <p className="truncate text-sm font-semibold">{featuredGame.name}</p>
                    <p className="truncate text-xs text-muted-foreground">
                      {featuredDateLongLabel(featured.game_date)}
                    </p>
                  </div>
                  <Badge variant="default" className="ml-auto shrink-0">
                    Pinned
                  </Badge>
                </div>
              ) : (
                <>
                  <p className="text-sm font-semibold">No featured game today</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Nothing is pinned for today, so the player-facing spotlight is empty.
                  </p>
                </>
              )}
              <Button asChild variant="outline" size="sm" className="mt-4 w-full">
                <NavLink to="/admin/games/manage">
                  <CalendarClock />
                  Change the pick
                </NavLink>
              </Button>
            </CardContent>
          </Card>

          <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
            <CardHeader>
              <CardTitle className="text-xl">Single vs Couple</CardTitle>
              <CardDescription>This week's play split across the two types.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              {statsState === 'loading' ? (
                <p className="text-sm text-muted-foreground">Loading this week's play…</p>
              ) : statsState === 'error' ? (
                <p className="text-sm text-muted-foreground">
                  Play activity is unavailable right now.
                </p>
              ) : !analytics || analytics.playsThisWeek === 0 ? (
                <p className="text-sm text-muted-foreground">
                  No finished rounds this week yet.
                </p>
              ) : (
                analytics.typeBreakdown.map((entry) => (
                  <div key={entry.type}>
                    <div className="flex items-baseline justify-between gap-2">
                      <p className="text-sm font-semibold">{entry.label}</p>
                      <p className="text-xs tabular-nums text-muted-foreground">
                        {entry.share}% · {entry.gameCount} {entry.gameCount === 1 ? 'game' : 'games'}
                      </p>
                    </div>
                    <Progress value={entry.share} className="mt-1.5 h-1.5" />
                  </div>
                ))
              )}
            </CardContent>
          </Card>

          <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
            <CardHeader>
              <CardTitle className="text-xl">Availability</CardTitle>
              <CardDescription>
                {configsState === 'ready' && unavailableGames.length > 0
                  ? `Not live for players right now: ${unavailableGames.length}.`
                  : 'What players can see and open right now.'}
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-2">
              {configsState === 'loading' ? (
                <p className="text-sm text-muted-foreground">Loading availability…</p>
              ) : configsState === 'error' ? (
                <p className="text-sm text-muted-foreground">
                  Availability is unavailable right now.
                </p>
              ) : unavailableGames.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  Nothing is switched off or hidden.
                </p>
              ) : (
                unavailableGames.map(({ game, label }) => (
                  <div key={game.id} className="flex items-center justify-between gap-2">
                    <span className="truncate text-sm font-medium">{game.name}</span>
                    <Badge variant="outline">{label}</Badge>
                  </div>
                ))
              )}
              <Button asChild variant="outline" size="sm" className="mt-2 w-full">
                <NavLink to="/admin/games/manage">
                  <SlidersHorizontal />
                  Review availability
                </NavLink>
              </Button>
            </CardContent>
          </Card>
        </div>
      </div>

      <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-0">
              <CardTitle className="text-xl">Leaderboard snapshot</CardTitle>
              <CardDescription>Weekly individual standings across every game.</CardDescription>
            </div>
            <Button asChild variant="outline" size="sm">
              <NavLink to="/admin/games/leaderboard">
                Open board
                <ArrowRight />
              </NavLink>
            </Button>
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          {snapshot.length === 0 ? (
            <p className="py-4 text-center text-sm text-muted-foreground">
              No standings yet. Events appear here once they close and score.
            </p>
          ) : (
            snapshot.map((row) => (
            <div key={row.user_id} className="flex items-center gap-3">
              <span className="w-6 shrink-0 text-center text-sm font-bold tabular-nums">
                {row.rank}
              </span>
              <span className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-secondary text-xs font-bold">
                {row.display_name
                  .replace(/[^a-zA-Z]/g, '')
                  .slice(0, 2)
                  .toUpperCase() || '??'}
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-semibold">{row.display_name}</p>
                <p className="truncate text-xs text-muted-foreground">
                  {row.opportunities_played} {row.opportunities_played === 1 ? 'event' : 'events'} this week
                </p>
              </div>
              <span className="shrink-0 text-sm font-bold tabular-nums">
                {row.score.toLocaleString(undefined, { maximumFractionDigits: 2 })}
              </span>
            </div>
            ))
          )}
        </CardContent>
      </Card>

      {/* Headline records: each game's best finalized result, all-time.
          The same category builders behind the expanded rankings answer
          these, so the overview can never disagree with the board. */}
      <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-0">
              <CardTitle className="text-xl">Top records</CardTitle>
              <CardDescription>
                Each game's best finalized result, all-time — fastest solves and
                reactions, fewest guesses and the rest.
              </CardDescription>
            </div>
            <Button asChild variant="outline" size="sm">
              <NavLink to="/admin/games/leaderboard">
                Open expanded rankings
                <ArrowRight />
              </NavLink>
            </Button>
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          {records === null ? (
            <p className="py-4 text-center text-sm text-muted-foreground">Loading records…</p>
          ) : records.length === 0 ? (
            <p className="py-4 text-center text-sm text-muted-foreground">
              No finalized results yet. Records appear once events close.
            </p>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2">
              {records.map((highlight) => {
                const game = getGameById(highlight.game_id);
                const Icon = game?.icon ?? Star;
                const ui = categoryUI(highlight.game_id, highlight.category);
                const entry = highlight.entry;
                const value =
                  entry && ui.primary.key
                    ? formatMetric(entry.metrics[ui.primary.key], ui.primary.format)
                    : null;
                return (
                  <div
                    key={highlight.game_id}
                    className="flex items-center gap-3 rounded-xl border border-border/50 bg-background/40 p-3"
                  >
                    <span
                      className={cn(
                        'shrink-0 rounded-xl p-2 text-white',
                        game?.accent ?? 'bg-gradient-romantic',
                      )}
                    >
                      <Icon className="h-4 w-4" />
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-semibold">
                        {game?.name ?? highlight.game_id}
                      </p>
                      <p className="truncate text-xs text-muted-foreground">
                        {highlight.label}
                        {entry ? ` · ${categoryEntryName(entry)}` : ''}
                      </p>
                    </div>
                    {value !== null ? (
                      <span className="shrink-0 text-sm font-bold tabular-nums">{value}</span>
                    ) : (
                      <span className="shrink-0 text-xs font-medium text-muted-foreground">
                        No results yet
                      </span>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
};

export default AdminGamesDashboard;
