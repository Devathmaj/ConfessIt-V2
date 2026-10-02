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
import { GameCard } from '@/components/games/GameCard';
import { GamesPageHeader } from '@/components/games/GamesPageHeader';
import { StatCard } from '@/components/games/StatCard';
import { AVAILABLE_GAMES, getGameById, type GameDefinition } from '@/lib/gamesCatalog';
import {
  getIndividualLeaderboard,
  getMyLeagueStanding,
  type LeagueIndividualEntry,
  type LeagueMyStanding,
} from '@/services/api';
import { MOCK_RECENTLY_PLAYED } from '@/lib/gamesStats';

/**
 * The Games dashboard: the landing screen of the section.
 *
 * Quick access and the featured spotlight read the real catalogue and the
 * shared configuration. Points, rank and the top-players teaser read the
 * weekly individual league board; streaks, challenges and recent plays stay
 * empty until their backends exist.
 */

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

  const summary = standing?.entries[0] ?? null;
  // The administrator's pick, resolved through the shared configuration -- the same source the
  // featured page and the manage screen read, so all three can never disagree.
  const { featured, isEnabled } = useGamesConfig();
  const game = featured?.game;
  const FeaturedIcon = game?.icon;
  const quickAccessGames = AVAILABLE_GAMES.filter(
    (game) => game.status === 'available' && isEnabled(game.id),
  ).slice(0, 4);
  const firstName = user?.name?.trim().split(' ')[0];

  return (
    <div>
      <GamesPageHeader
        eyebrow="Games"
        title={firstName ? `Welcome back, ${firstName}` : 'Welcome back'}
        description="Your streak, points and playground — pick up where you left off or jump into something new."
        actions={
          <>
            <Button asChild variant="outline" className="rounded-full font-semibold">
              <NavLink to="/games/leaderboard">
                <Trophy />
                Leaderboard
              </NavLink>
            </Button>
            <Button asChild className="rounded-full font-semibold">
              <NavLink to="/games/select">
                <Dices />
                Play now
              </NavLink>
            </Button>
          </>
        }
      />

      {/* Stat strip: weekly league figures; the rest is empty until telemetry exists. */}
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
          value="—"
          hint="No streak data yet"
          icon={Flame}
          accent="bg-gradient-passion"
        />
        <StatCard
          label="Games played"
          value="—"
          hint="No play history yet"
          icon={Gamepad2}
          accent="bg-gradient-love"
        />
      </div>

      <div className="mt-6 grid gap-6 lg:mt-8 lg:grid-cols-3">
        {/* Main column */}
        <div className="space-y-6 lg:col-span-2">
          <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
            <CardHeader className="pb-3">
              <CardTitle className="text-lg font-bold tracking-tight">Recently played</CardTitle>
              <CardDescription>Your last few rounds, newest first.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-2">
              {MOCK_RECENTLY_PLAYED.length === 0 ? (
                <div className="rounded-xl border border-dashed border-border/70 px-4 py-8 text-center">
                  <p className="text-sm font-semibold">No games played yet</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Your history will show up here after your first round.
                  </p>
                  <Button asChild size="sm" variant="outline" className="mt-4 rounded-full">
                    <NavLink to="/games/select">Browse games</NavLink>
                  </Button>
                </div>
              ) : (
                MOCK_RECENTLY_PLAYED.map((play) => {
                  const game = getGameById(play.gameId);
                  if (!game) return null;
                  const Icon = game.icon;

                  return (
                    <div
                      key={`${play.gameId}-${play.playedAt}`}
                      className="flex items-center gap-3 rounded-xl border border-border/50 bg-background/40 p-3 transition-colors hover:border-romantic/40"
                    >
                      <span
                        className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg text-white ${game.accent}`}
                      >
                        <Icon className="h-5 w-5" />
                      </span>
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm font-semibold">{game.name}</p>
                        <p className="truncate text-xs text-muted-foreground">
                          {play.playedAt}
                          {play.personalBest ? ' · Personal best' : ''}
                        </p>
                      </div>
                      <div className="hidden text-right sm:block">
                        <p className="text-sm font-bold tabular-nums">{play.score}</p>
                        <p className="text-xs text-muted-foreground">points</p>
                      </div>
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        className="shrink-0 rounded-full text-romantic hover:text-romantic"
                        onClick={() => notifyPlay(game)}
                      >
                        <RotateCcw />
                        <span className="hidden sm:inline">Play again</span>
                      </Button>
                    </div>
                  );
                })
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
          {/* Today's featured */}
          <Card className="rounded-2xl border-transparent bg-[var(--action)] text-white shadow-[var(--shadow-romantic)] dark:bg-gradient-romantic">
            <CardContent className="p-5">
              <div className="flex items-center justify-between gap-2">
                <p className="text-xs font-bold uppercase tracking-[0.18em] text-white/90">
                  Today&apos;s featured
                </p>
                <Sparkles className="h-4 w-4 text-white/90" />
              </div>
              <div className="mt-4 flex items-center gap-3">
                <span className="flex h-12 w-12 shrink-0 items-center justify-center rounded-xl bg-white/20">
                  {FeaturedIcon ? (
                    <FeaturedIcon className="h-6 w-6 text-white" />
                  ) : (
                    <Sparkles className="h-6 w-6 text-white" />
                  )}
                </span>
                <div className="min-w-0">
                  <p className="truncate font-bold">{game?.name ?? 'Spotlight paused'}</p>
                  <p className="truncate text-xs text-white/80">
                    {game ? `${game.players} · ${game.duration}` : 'No game in the spotlight'}
                  </p>
                </div>
              </div>
              <Button
                asChild
                variant="ghost"
                size="sm"
                className="mt-4 w-full rounded-full font-semibold text-white hover:bg-white/20 hover:text-white"
              >
                <NavLink to="/games/featured">
                  View featured
                  <ArrowRight />
                </NavLink>
              </Button>
            </CardContent>
          </Card>

          {/* Weekly challenge: empty until challenges exist on the backend. */}
          <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
            <CardHeader className="pb-3">
              <CardTitle className="flex items-center gap-2 text-base font-bold">
                <Target className="h-4 w-4 text-romantic" />
                Weekly challenge
              </CardTitle>
              <CardDescription>No weekly challenge right now</CardDescription>
            </CardHeader>
            <CardContent>
              <Progress value={0} className="h-2.5 rounded-full" />
              <div className="mt-2 flex items-center justify-between gap-2 text-xs text-muted-foreground">
                <span>Check back once challenges go live</span>
              </div>
            </CardContent>
          </Card>

          {/* Top players teaser */}
          <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
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
                    className="flex items-center gap-3 rounded-xl border border-border/50 bg-background/40 p-2.5"
                  >
                    <span className="w-5 text-center text-xs font-extrabold tabular-nums text-muted-foreground">
                      {entry.rank}
                    </span>
                    <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-gradient-hero text-xs font-bold text-white">
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
