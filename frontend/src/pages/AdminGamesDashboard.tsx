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
  getAdminIndividualLeaderboard,
  type GameRecordHighlight,
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

/**
 * The section's front page: availability, today's spotlight, this week's play and a way into
 * the two screens that do the actual work.
 *
 * Catalogue counts come from the live configuration. Play-derived figures
 * stay empty until telemetry exists on the backend.
 */
export const AdminGamesDashboard = () => {
  const { config, activity, featured, isEnabled } = useGamesConfig();

  const analytics = useMemo(() => gamesAnalytics(config), [config]);

  // The weekly individual board, top of the list only: enough to tell
  // whether play is happening and who is leading.
  const [snapshot, setSnapshot] = useState<LeagueIndividualEntry[]>([]);
  // Each game's headline record (fastest time, fewest guesses, ...) so the
  // overview shows the expanded rankings' headline figures without opening
  // every board. Null while the request is in flight.
  const [records, setRecords] = useState<GameRecordHighlight[] | null>(null);
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

  const FeaturedIcon = featured?.game.icon;

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
          value={`${analytics.enabledGames}/${analytics.totalGames}`}
          hint={
            analytics.disabledGames > 0
              ? `${analytics.disabledGames} switched off`
              : 'Every title switched on'
          }
          icon={Gamepad2}
          accent="bg-gradient-romantic"
        />
        <StatCard
          label="Players today"
          value={analytics.playersToday.toLocaleString()}
          hint={`${analytics.playersThisWeek.toLocaleString()} this week`}
          icon={Users}
          accent="bg-gradient-love"
        />
        <StatCard
          label="Plays today"
          value={analytics.playsToday.toLocaleString()}
          hint={`${analytics.playsThisWeek.toLocaleString()} this week`}
          icon={Activity}
          accent="bg-gradient-romantic"
        />
        <StatCard
          label="Plays all time"
          value={analytics.playsAllTime.toLocaleString()}
          hint={`${analytics.activeGameTypes} game types in play`}
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
                Titles ranked by sessions started, as a share of the week's play.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              {analytics.topGames.length === 0 ? (
                <p className="py-6 text-center text-sm text-muted-foreground">
                  No sessions recorded this week yet.
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
              {featured && FeaturedIcon ? (
                <div className="flex items-center gap-3">
                  <span
                    className={cn('shrink-0 rounded-xl p-2.5 text-white', featured.game.accent)}
                  >
                    <FeaturedIcon className="h-5 w-5" />
                  </span>
                  <div className="min-w-0">
                    <p className="truncate text-sm font-semibold">{featured.game.name}</p>
                    <p className="truncate text-xs text-muted-foreground">
                      {featuredDateLongLabel(featured.dateKey)}
                    </p>
                  </div>
                  <Badge
                    variant={featured.explicit ? 'default' : 'secondary'}
                    className="ml-auto shrink-0"
                  >
                    {featured.explicit ? 'Pinned' : 'Rotation'}
                  </Badge>
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">
                  Nothing can take the spotlight while every title is switched off.
                </p>
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
              {analytics.typeBreakdown.map((entry) => (
                <div key={entry.type}>
                  <div className="flex items-baseline justify-between gap-2">
                    <p className="text-sm font-semibold">{entry.label}</p>
                    <p className="text-xs tabular-nums text-muted-foreground">
                      {entry.share}% · {entry.gameCount} {entry.gameCount === 1 ? 'game' : 'games'}
                    </p>
                  </div>
                  <Progress value={entry.share} className="mt-1.5 h-1.5" />
                </div>
              ))}
            </CardContent>
          </Card>

          <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
            <CardHeader>
              <CardTitle className="text-xl">Availability</CardTitle>
              <CardDescription>
                {analytics.disabledGames > 0
                  ? `Hidden from players right now: ${analytics.disabledGames}.`
                  : 'Every title is visible to players.'}
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-2">
              {analytics.disabledGames === 0 ? (
                <p className="text-sm text-muted-foreground">
                  Nothing has been switched off in this configuration.
                </p>
              ) : (
                AVAILABLE_GAMES.filter((game) => !isEnabled(game.id)).map((game) => (
                  <div key={game.id} className="flex items-center justify-between gap-2">
                    <span className="truncate text-sm font-medium">{game.name}</span>
                    <Badge variant="outline">Off</Badge>
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
