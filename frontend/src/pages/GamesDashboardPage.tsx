import { useEffect, useState } from 'react';
import { NavLink, useNavigate } from 'react-router-dom';
import {
  ArrowRight,
  Dices,
  Flame,
  Gamepad2,
  RotateCcw,
  Sparkles,
  Star,
  Target,
  Trophy,
} from 'lucide-react';
import { useAuth } from '@/contexts/AuthContext';
import { useGamesConfig } from '@/contexts/GamesConfigContext';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Progress } from '@/components/ui/progress';
import { Skeleton } from '@/components/ui/skeleton';
import { GameCard } from '@/components/games/GameCard';
import { GamesPageHeader } from '@/components/games/GamesPageHeader';
import { FloralDecor } from '@/components/FloralDecor';
import { StatCard } from '@/components/games/StatCard';
import { AVAILABLE_GAMES, getGameById, type GameDefinition } from '@/lib/gamesCatalog';
import { formatPlayedAt, weeklyChallengeProgress } from '@/lib/gamesStats';
import {
  getFeaturedToday,
  getIndividualLeaderboard,
  getMyLeagueStanding,
  getPlayerGameSummary,
  type LeagueIndividualEntry,
  type LeagueMyStanding,
  type PlayerGameSummary,
} from '@/services/api';

/**
 * The Games dashboard: the landing screen of the section.
 *
 * The stat strip and "recently played" read the caller's own finished rounds
 * (`GET /games/summary`), the points/rank tiles and the top-players teaser
 * read the weekly individual league board, and today's spotlight is the
 * server's pick. The dashboard deliberately never falls back to the local
 * rotation: naming a game the server never featured is a mock, so a day with
 * nothing in the spotlight says so instead.
 */

type LoadState = 'loading' | 'ready' | 'error';

/** Plays a game: every catalogued game has a real screen behind its route. */
const useLaunchPlay = () => {
  const navigate = useNavigate();
  return (game: GameDefinition) => {
    navigate(game.route);
  };
};

export const GamesDashboardPage = () => {
  const { user } = useAuth();
  const notifyPlay = useLaunchPlay();

  const [topPlayers, setTopPlayers] = useState<LeagueIndividualEntry[]>([]);
  const [standing, setStanding] = useState<LeagueMyStanding | null>(null);
  // The caller's own history. `stats === null` covers both "no rounds yet"
  // and "could not load"; `statsState` keeps those two apart for the copy.
  const [stats, setStats] = useState<PlayerGameSummary | null>(null);
  const [statsState, setStatsState] = useState<LoadState>('loading');
  // Today's spotlight, resolved by the server exactly like /games/featured
  // does. `undefined` while in flight, the game id -- or null when the day
  // has no pick -- once the server answers; the failure flag keeps "nothing
  // is featured" distinct from "could not ask".
  const [spotlightId, setSpotlightId] = useState<string | null | undefined>(undefined);
  const [spotlightFailed, setSpotlightFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getIndividualLeaderboard({ period: 'weekly', limit: 3 })
      .then((board) => {
        if (!cancelled) setTopPlayers(board.entries);
      })
      .catch(() => {
        if (!cancelled) setTopPlayers([]);
      });
    getMyLeagueStanding('individual', { period: 'weekly' })
      .then((mine) => {
        if (!cancelled) setStanding(mine);
      })
      .catch(() => {
        if (!cancelled) setStanding(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    getPlayerGameSummary()
      .then((response) => {
        if (cancelled) return;
        setStats(response);
        setStatsState('ready');
      })
      .catch(() => {
        if (cancelled) return;
        setStats(null);
        setStatsState('error');
      });
    getFeaturedToday()
      .then((response) => {
        if (!cancelled) setSpotlightId(response.game_id);
      })
      .catch(() => {
        if (cancelled) return;
        setSpotlightId(null);
        setSpotlightFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const summary = standing?.entries[0] ?? null;
  // The administrator's configuration still gates the quick-access grid; the
  // spotlight itself comes from the server above, never from the local seed.
  const { isEnabled } = useGamesConfig();
  const quickAccessGames = AVAILABLE_GAMES.filter(
    (game) => game.status === 'available' && isEnabled(game.id),
  ).slice(0, 4);
  const firstName = user?.name?.trim().split(' ')[0];

  const statsReady = statsState === 'ready';
  const streakDays = stats?.current_streak_days ?? 0;
  const longestStreakDays = stats?.longest_streak_days ?? 0;
  const challenge = weeklyChallengeProgress(stats);
  const spotlightGame = spotlightId ? getGameById(spotlightId) : undefined;
  const spotlightLoading = spotlightId === undefined && !spotlightFailed;
  const FeaturedIcon = spotlightGame?.icon;

  return (
    <div className="relative">
      {/* Floral preview: 1706715 top half, top-right, extra large. */}
      <FloralDecor pair={6} className="right-0 top-0 w-[36rem] opacity-90 sm:w-[44rem]" />
      {/* Floral edge: cropped blossom top-left.
          The section frame (GamesLayout) covers the remaining corners. */}
      <FloralDecor pair={5} className="left-0 top-0 w-[32rem] opacity-90 sm:w-[38rem]" />
      <GamesPageHeader
        eyebrow="Games"
        title={firstName ? `Welcome back, ${firstName}` : 'Welcome back'}
        description="Your streak, points and playground — pick up where you left off or jump into something new."
        actions={
          <>
            <Button asChild variant="outline" className="[border-radius:var(--edge-pill)] font-semibold">
              <NavLink to="/games/leaderboard">
                <Trophy />
                Leaderboard
              </NavLink>
            </Button>
            <Button asChild className="[border-radius:var(--edge-pill)] font-semibold">
              <NavLink to="/games/select">
                <Dices />
                Play now
              </NavLink>
            </Button>
          </>
        }
      />

      {/* Stat strip: league figures plus the caller's own play history. */}
      <div className="grid grid-cols-2 gap-3 sm:gap-4 xl:grid-cols-4">
        <StatCard
          label="Points"
          value={summary ? summary.score.toLocaleString(undefined, { maximumFractionDigits: 2 }) : '—'}
          hint={summary ? 'Weekly league points' : 'No standings yet'}
          icon={Star}
          accent="bg-gradient-romantic"
        />
        <StatCard
          label="Weekly rank"
          value={summary ? `#${summary.rank}` : '—'}
          hint={summary ? `${summary.opportunities_played} ${summary.opportunities_played === 1 ? 'event' : 'events'} played` : 'Unranked'}
          icon={Trophy}
          accent="bg-gradient-hero"
        />
        <StatCard
          label="Current streak"
          value={streakDays > 0 ? `${streakDays} ${streakDays === 1 ? 'day' : 'days'}` : '—'}
          hint={
            statsState === 'loading'
              ? 'Loading your streak…'
              : statsState === 'error'
                ? 'Stats are unavailable'
                : streakDays > 0
                  ? `Longest: ${longestStreakDays} ${longestStreakDays === 1 ? 'day' : 'days'}`
                  : 'Play a round to start one'
          }
          icon={Flame}
          accent="bg-gradient-passion"
        />
        <StatCard
          label="Games played"
          value={statsReady && stats ? stats.games_played.toLocaleString() : '—'}
          hint={
            statsState === 'loading'
              ? 'Loading your history…'
              : statsState === 'error'
                ? 'Stats are unavailable'
                : stats && stats.plays_this_week > 0
                  ? `${stats.plays_this_week} ${stats.plays_this_week === 1 ? 'round' : 'rounds'} this week`
                  : 'No rounds yet — jump into a game'
          }
          icon={Gamepad2}
          accent="bg-gradient-love"
        />
      </div>

      <div className="mt-6 grid gap-6 lg:mt-8 lg:grid-cols-3">
        {/* Main column */}
        <div className="space-y-6 lg:col-span-2">
          <Card className="[border-radius:var(--edge-card)] border-border/60 bg-card/70 backdrop-blur">
            <CardHeader className="pb-3">
              <CardTitle className="text-lg font-bold tracking-tight">Recently played</CardTitle>
              <CardDescription>Your last few rounds, newest first.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-2">
              {statsState === 'loading' ? (
                <div className="space-y-2" role="status" aria-label="Loading your recent games">
                  {[0, 1, 2].map((row) => (
                    <div
                      key={row}
                      className="flex items-center gap-3 [border-radius:var(--edge-card-sm)] border border-border/50 bg-background/40 p-3"
                    >
                      <Skeleton className="h-10 w-10 shrink-0 [border-radius:var(--edge-icon)]" />
                      <div className="min-w-0 flex-1 space-y-1.5">
                        <Skeleton className="h-4 w-1/3" />
                        <Skeleton className="h-3 w-1/2" />
                      </div>
                    </div>
                  ))}
                </div>
              ) : statsState === 'error' ? (
                <div className="[border-radius:var(--edge-card-sm)] border border-dashed border-border/70 px-4 py-8 text-center">
                  <p className="text-sm font-semibold">Play history is unavailable right now</p>
                  <p className="mt-1 text-xs text-muted-foreground">Refresh the page to try again.</p>
                </div>
              ) : stats && stats.recent_plays.length > 0 ? (
                stats.recent_plays.map((play) => {
                  const game = getGameById(play.game_id);
                  if (!game) return null;
                  const Icon = game.icon;

                  return (
                    <div
                      key={`${play.game_id}-${play.played_at}`}
                      className="flex items-center gap-3 [border-radius:var(--edge-card-sm)] border border-border/50 bg-background/40 p-3 transition-colors hover:border-romantic/40"
                    >
                      <span
                        className={`flex h-10 w-10 shrink-0 items-center justify-center [border-radius:var(--edge-icon)] text-white ${game.accent}`}
                      >
                        <Icon className="h-5 w-5" />
                      </span>
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm font-semibold">{game.name}</p>
                        <p className="truncate text-xs text-muted-foreground">
                          {formatPlayedAt(play.played_at)} · {play.summary}
                        </p>
                      </div>
                      {play.score !== null && play.score_label && (
                        <div className="hidden text-right sm:block">
                          <p className="text-sm font-bold tabular-nums">
                            {play.score.toLocaleString(undefined, { maximumFractionDigits: 1 })}
                          </p>
                          <p className="text-xs text-muted-foreground">{play.score_label}</p>
                        </div>
                      )}
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        className="shrink-0 [border-radius:var(--edge-pill)] text-romantic hover:text-romantic"
                        onClick={() => notifyPlay(game)}
                      >
                        <RotateCcw />
                        <span className="hidden sm:inline">Play again</span>
                      </Button>
                    </div>
                  );
                })
              ) : (
                <div className="[border-radius:var(--edge-card-sm)] border border-dashed border-border/70 px-4 py-8 text-center">
                  <p className="text-sm font-semibold">No games played yet</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Your history will show up here after your first round.
                  </p>
                  <Button asChild size="sm" variant="outline" className="mt-4 [border-radius:var(--edge-pill)]">
                    <NavLink to="/games/select">Browse games</NavLink>
                  </Button>
                </div>
              )}
            </CardContent>
          </Card>

          {/* Quick access */}
          <div>
            <div className="mb-3 flex items-center justify-between gap-3">
              <h2 className="text-lg font-bold tracking-tight sm:text-xl">Quick access</h2>
              <NavLink
                to="/games/select"
                className="inline-flex items-center gap-1 text-sm font-semibold text-romantic hover:underline"
              >
                Browse all
                <ArrowRight className="h-4 w-4" />
              </NavLink>
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              {quickAccessGames.map((game) => (
                <GameCard key={game.id} game={game} onPlay={notifyPlay} />
              ))}
            </div>
          </div>
        </div>

        {/* Sidebar */}
        <div className="space-y-6">
          {/* Today's featured: the server's pick, or an honest empty state. */}
          {spotlightLoading ? (
            <Card className="[border-radius:var(--edge-card)] border-transparent bg-[var(--action)] text-white shadow-[var(--shadow-romantic)] dark:bg-gradient-romantic">
              <CardContent className="p-5" role="status" aria-label="Loading today's featured game">
                <div className="flex items-center justify-between gap-2">
                  <p className="text-xs font-bold uppercase tracking-[0.18em] text-white/90">
                    Today&apos;s featured
                  </p>
                  <Sparkles className="h-4 w-4 text-white/90" />
                </div>
                <div className="mt-4 flex items-center gap-3">
                  <Skeleton className="h-12 w-12 shrink-0 bg-white/20 [border-radius:var(--edge-icon)]" />
                  <div className="min-w-0 flex-1 space-y-2">
                    <Skeleton className="h-4 w-2/3 bg-white/20" />
                    <Skeleton className="h-3 w-1/2 bg-white/20" />
                  </div>
                </div>
              </CardContent>
            </Card>
          ) : spotlightGame ? (
            <Card className="[border-radius:var(--edge-card)] border-transparent bg-[var(--action)] text-white shadow-[var(--shadow-romantic)] dark:bg-gradient-romantic">
              <CardContent className="p-5">
                <div className="flex items-center justify-between gap-2">
                  <p className="text-xs font-bold uppercase tracking-[0.18em] text-white/90">
                    Today&apos;s featured
                  </p>
                  <Sparkles className="h-4 w-4 text-white/90" />
                </div>
                <div className="mt-4 flex items-center gap-3">
                  <span className="flex h-12 w-12 shrink-0 items-center justify-center [border-radius:var(--edge-icon)] bg-white/20">
                    {FeaturedIcon ? (
                      <FeaturedIcon className="h-6 w-6 text-white" />
                    ) : (
                      <Sparkles className="h-6 w-6 text-white" />
                    )}
                  </span>
                  <div className="min-w-0">
                    <p className="truncate font-bold">{spotlightGame.name}</p>
                    <p className="truncate text-xs text-white/80">
                      {spotlightGame.players} · {spotlightGame.duration}
                    </p>
                  </div>
                </div>
                <Button
                  asChild
                  variant="ghost"
                  size="sm"
                  className="mt-4 w-full [border-radius:var(--edge-pill)] font-semibold text-white hover:bg-white/20 hover:text-white"
                >
                  <NavLink to="/games/featured">
                    View featured
                    <ArrowRight />
                  </NavLink>
                </Button>
              </CardContent>
            </Card>
          ) : (
            <Card className="[border-radius:var(--edge-card)] border-border/60 bg-card/70 backdrop-blur">
              <CardContent className="p-5">
                <div className="flex items-center justify-between gap-2">
                  <p className="text-xs font-bold uppercase tracking-[0.18em] text-muted-foreground">
                    Today&apos;s featured
                  </p>
                  <Sparkles className="h-4 w-4 text-muted-foreground" />
                </div>
                <p className="mt-4 text-sm font-semibold">
                  {spotlightFailed ? 'The spotlight could not be loaded' : 'No featured game today'}
                </p>
                <p className="mt-1 text-xs text-muted-foreground">
                  {spotlightFailed
                    ? 'Refresh the page to try again, or browse the full library.'
                    : 'The spotlight rotates daily — check back tomorrow or browse the full library.'}
                </p>
                <Button
                  asChild
                  variant="outline"
                  size="sm"
                  className="mt-4 w-full [border-radius:var(--edge-pill)] font-semibold"
                >
                  <NavLink to="/games/select">Browse games</NavLink>
                </Button>
              </CardContent>
            </Card>
          )}

          {/* Weekly challenge: progress read from the caller's own week. */}
          <Card className="[border-radius:var(--edge-card)] border-border/60 bg-card/70 backdrop-blur">
            <CardHeader className="pb-3">
              <CardTitle className="flex items-center gap-2 text-base font-bold">
                <Target className="h-4 w-4 text-romantic" />
                Weekly challenge
              </CardTitle>
              <CardDescription>
                {challenge?.label ?? 'Play 3 different games this week'}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <Progress
                value={challenge ? Math.round((challenge.current / challenge.target) * 100) : 0}
                className="h-2.5 [border-radius:var(--edge-pill)]"
              />
              <div className="mt-2 flex items-center justify-between gap-2 text-xs text-muted-foreground">
                {statsState === 'loading' ? (
                  <span>Tracking your week…</span>
                ) : statsState === 'error' ? (
                  <span>Progress is unavailable right now</span>
                ) : challenge ? (
                  <>
                    <span>
                      {challenge.current} of {challenge.target} games
                    </span>
                    <span>
                      {challenge.complete
                        ? 'Challenge complete'
                        : `${challenge.target - challenge.current} to go`}
                    </span>
                  </>
                ) : null}
              </div>
            </CardContent>
          </Card>

          {/* Top players teaser */}
          <Card className="[border-radius:var(--edge-card)] border-border/60 bg-card/70 backdrop-blur">
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between gap-2">
                <CardTitle className="text-base font-bold">Top players</CardTitle>
                <NavLink
                  to="/games/leaderboard"
                  className="text-xs font-semibold text-romantic hover:underline"
                >
                  View all
                </NavLink>
              </div>
              <CardDescription>Weekly individual standings.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-2">
              {topPlayers.length === 0 ? (
                <p className="py-4 text-center text-sm text-muted-foreground">
                  No standings yet. Be the first to play.
                </p>
              ) : (
                topPlayers.map((entry) => (
                  <div
                    key={entry.user_id}
                    className="flex items-center gap-3 [border-radius:var(--edge-card-sm)] border border-border/50 bg-background/40 p-2.5"
                  >
                    <span className="w-5 text-center text-xs font-extrabold tabular-nums text-muted-foreground">
                      {entry.rank}
                    </span>
                    <span className="flex h-8 w-8 shrink-0 items-center justify-center [border-radius:var(--edge-pill)] bg-gradient-hero text-xs font-bold text-white">
                      {entry.display_name
                        .replace(/[^a-zA-Z]/g, '')
                        .slice(0, 2)
                        .toUpperCase() || '??'}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-sm font-semibold">
                      {entry.display_name}
                    </span>
                    <span className="text-sm font-bold tabular-nums">
                      {entry.score.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                    </span>
                  </div>
                ))
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
};

export default GamesDashboardPage;
