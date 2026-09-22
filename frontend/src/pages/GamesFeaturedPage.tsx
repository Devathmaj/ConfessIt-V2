import { useEffect, useState } from 'react';
import { NavLink } from 'react-router-dom';
import { Gamepad2, Sparkles } from 'lucide-react';
import { toast } from 'sonner';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { GamesPageHeader } from '@/components/games/GamesPageHeader';
import { useGamesConfig } from '@/contexts/GamesConfigContext';
import { categoryLabel, type GameDefinition } from '@/lib/gamesCatalog';

/**
 * Today's featured game.
 *
 * Deliberately lightweight: one mini-game in the spotlight for the day, in the spirit of a
 * daily puzzle rotation. There is no artwork or marketing copy to lean on yet, so the page is
 * built around the pick itself, a reset countdown and a single obvious way to play.
 */

/** Mock engagement numbers until play events exist on the backend. */
const FEATURED_TODAY_STATS = {
  playsToday: 1204,
  yourBest: 480,
  averageScore: 312,
};

const pad = (value: number) => String(value).padStart(2, '0');

/** Counts down to local midnight, when the featured pick rotates. */
const useResetCountdown = () => {
  const [label, setLabel] = useState('00:00:00');

  useEffect(() => {
    const tick = () => {
      const now = new Date();
      const end = new Date(now);
      end.setHours(24, 0, 0, 0);
      const distance = Math.max(0, end.getTime() - now.getTime());
      const hours = Math.floor(distance / 3_600_000);
      const minutes = Math.floor((distance % 3_600_000) / 60_000);
      const seconds = Math.floor((distance % 60_000) / 1000);
      setLabel(`${pad(hours)}:${pad(minutes)}:${pad(seconds)}`);
    };

    tick();
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, []);

  return label;
};

const notifyPlay = (game: GameDefinition) => {
  toast(`${game.name} opens here once its game screen lands 🎮`);
};

export const GamesFeaturedPage = () => {
  const countdown = useResetCountdown();
  // The shared configuration, not the catalogue's own rotation: the pick an administrator
  // pinned on the manage screen is the pick shown here. Falls back to a freshly seeded
  // configuration when the provider is absent, so the page also works standalone.
  const { featured } = useGamesConfig();
  const game = featured?.game;

  if (!game) {
    return (
      <div>
        <GamesPageHeader
          eyebrow="Today's featured"
          title="Game of the Day"
          description="One mini-game takes the spotlight every day. The pick rotates at midnight."
        />
        <div className="mx-auto max-w-2xl rounded-3xl border border-dashed border-border/70 px-6 py-14 text-center">
          <Sparkles className="mx-auto h-8 w-8 text-muted-foreground" />
          <p className="mt-3 text-base font-bold">No featured game right now</p>
          <p className="mx-auto mt-1 max-w-sm text-sm text-muted-foreground">
            Every title is taking a break from the spotlight. The rest of the library is still
            open.
          </p>
          <Button asChild variant="outline" size="sm" className="mt-5 rounded-full">
            <NavLink to="/games/select">
              <Gamepad2 />
              Browse all games
            </NavLink>
          </Button>
        </div>
      </div>
    );
  }

  const FeaturedIcon = game.icon;

  return (
    <div>
      <GamesPageHeader
        eyebrow="Today's featured"
        title="Game of the Day"
        description="One mini-game takes the spotlight every day. The pick rotates at midnight."
      />

      <Card className="mx-auto max-w-2xl overflow-hidden rounded-3xl border-border/60 bg-card/80 shadow-romantic backdrop-blur">
        {/* Spotlight banner */}
        <div className="relative overflow-hidden bg-gradient-hero px-5 py-6 text-white sm:px-8 sm:py-7">
          <div className="relative z-10 flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="flex items-center gap-2 text-xs font-bold uppercase tracking-[0.2em] text-white/85">
                <Sparkles className="h-3.5 w-3.5" />
                Game of the day
              </p>
              <h2 className="mt-2 break-words text-2xl font-extrabold tracking-tight sm:text-3xl">
                {game.name}
              </h2>
              <div className="mt-2 flex flex-wrap gap-2">
                <Badge className="border-transparent bg-white/20 text-white hover:bg-white/30">
                  {categoryLabel(game.category)}
                </Badge>
                <Badge className="border-transparent bg-white/20 text-white hover:bg-white/30">
                  {game.difficulty}
                </Badge>
              </div>
            </div>
            <span className="shrink-0 rounded-full bg-black/20 px-3 py-1.5 text-xs font-bold tabular-nums backdrop-blur">
              Resets in {countdown}
            </span>
          </div>
          <span
            aria-hidden
            className="absolute -bottom-10 -right-8 rounded-full bg-white/10 p-7 sm:-right-6 sm:p-9"
          >
            <FeaturedIcon className="h-14 w-14 text-white/70 sm:h-20 sm:w-20" />
          </span>
        </div>

        <CardContent className="space-y-5 p-5 sm:p-8">
          <p className="text-sm leading-relaxed text-muted-foreground sm:text-base">
            {game.tagline} Play it today before the spotlight moves on to another game.
          </p>

          <div className="grid grid-cols-3 overflow-hidden rounded-2xl border border-border/60 bg-background/40 text-center">
            <div className="p-3 sm:p-4">
              <p className="text-lg font-extrabold tabular-nums sm:text-xl">
                {FEATURED_TODAY_STATS.playsToday.toLocaleString()}
              </p>
              <p className="mt-0.5 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                Plays today
              </p>
            </div>
            <div className="border-x border-border/60 p-3 sm:p-4">
              <p className="text-lg font-extrabold tabular-nums sm:text-xl">
                {FEATURED_TODAY_STATS.yourBest}
              </p>
              <p className="mt-0.5 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                Your best
              </p>
            </div>
            <div className="p-3 sm:p-4">
              <p className="text-lg font-extrabold tabular-nums sm:text-xl">
                {FEATURED_TODAY_STATS.averageScore}
              </p>
              <p className="mt-0.5 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                Avg. score
              </p>
            </div>
          </div>

          <Button
            type="button"
            size="lg"
            className="w-full rounded-full font-bold"
            onClick={() => notifyPlay(game)}
          >
            <Gamepad2 />
            Play today&apos;s featured game
          </Button>

          <div className="flex flex-wrap items-center justify-center gap-x-5 gap-y-2 text-sm">
            <NavLink
              to="/games/select"
              className="font-semibold text-romantic hover:underline"
            >
              Choose another game
            </NavLink>
            <NavLink
              to="/games/leaderboard"
              className="font-semibold text-romantic hover:underline"
            >
              See today&apos;s standings
            </NavLink>
          </div>
        </CardContent>
      </Card>
    </div>
  );
};

export default GamesFeaturedPage;
