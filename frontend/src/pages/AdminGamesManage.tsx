import { useMemo } from 'react';
import { CalendarClock, Dices, RotateCcw } from 'lucide-react';
import { toast } from 'sonner';
import { AdminWordleConfig } from '@/components/AdminWordleConfig';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useAuth } from '@/contexts/AuthContext';
import { useGamesConfig } from '@/contexts/GamesConfigContext';
import { CAPABILITIES } from '@/lib/capabilities';
import { getGameById } from '@/lib/gamesCatalog';
import {
  addDays,
  featuredDateKey,
  featuredDateKeysFrom,
  featuredDateLabel,
} from '@/lib/gamesFeaturedSchedule';
import { cn } from '@/lib/utils';

const AUTO = 'auto';

/**
 * The spotlight, and the one puzzle this screen publishes.
 *
 * The two cards are written through `useGamesConfig`, so a pick made here is the pick
 * `/games/featured` resolves against on the next render with no refresh step. Everything else
 * about the catalogue -- availability, per-game settings -- is not this screen's concern.
 *
 * Below them sits the Wordle answer, which is a different kind of control: it is the only
 * thing on this page that goes to the server, so it renders only for an account holding
 * `games:manage`, and the server refuses the same writes again regardless of what this gate
 * believes.
 */
export const AdminGamesManage = () => {
  const { featured, eligibleGames, isEnabled, featuredOn, setFeatured, clearFeatured, setRandomFeatured } =
    useGamesConfig();
  const { can } = useAuth();

  const todayKey = featuredDateKey(new Date());
  const upcomingKeys = useMemo(() => featuredDateKeysFrom(addDays(new Date(), 1), 7), []);

  const handleRandom = () => {
    const pick = setRandomFeatured();
    if (pick) {
      toast.success(`${getGameById(pick)?.name ?? pick} picked for today.`);
    } else {
      toast.error('No eligible game can take the spotlight right now.');
    }
  };

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-col gap-2">
        <h1 className="text-3xl font-semibold">Manage games</h1>
        <p className="text-sm text-muted-foreground">
          Today's spotlight, the week ahead, and the answer today's Wordle runs on. Changes
          here show up on the player-facing section straight away.
        </p>
      </header>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
          <CardHeader>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="min-w-0">
                <CardTitle className="text-xl">Today's featured</CardTitle>
                <CardDescription>
                  {featured
                    ? featured.explicit
                      ? 'Pinned by an administrator for this day.'
                      : 'Following the daily rotation until someone pins a title.'
                    : 'Nothing eligible can take the spotlight.'}
                </CardDescription>
              </div>
              {featured && (
                <Badge variant={featured.explicit ? 'default' : 'secondary'}>
                  {featured.explicit ? 'Pinned' : 'Rotation'}
                </Badge>
              )}
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            {featured ? (
              <div className="flex items-center gap-3">
                <span className={cn('shrink-0 rounded-xl p-2.5 text-white', featured.game.accent)}>
                  <featured.game.icon className="h-5 w-5" />
                </span>
                <div className="min-w-0">
                  <p className="truncate font-semibold">{featured.game.name}</p>
                  <p className="truncate text-xs text-muted-foreground">
                    {featured.game.tagline}
                  </p>
                </div>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">
                Switch a game back on and the spotlight returns.
              </p>
            )}

            <Select
              value={featured?.explicit ? featured.game.id : AUTO}
              onValueChange={(next) => {
                if (next === AUTO) {
                  clearFeatured(todayKey);
                  toast.info('Today follows the daily rotation again.');
                  return;
                }
                setFeatured(todayKey, next);
                toast.success(`${getGameById(next)?.name ?? next} featured for today.`);
              }}
            >
              <SelectTrigger className="w-full">
                <SelectValue placeholder="Choose the featured game" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={AUTO}>Automatic rotation</SelectItem>
                {eligibleGames.map((game) => (
                  <SelectItem key={game.id} value={game.id}>
                    {game.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>

            <div className="flex flex-wrap gap-2">
              <Button type="button" variant="outline" size="sm" onClick={handleRandom}>
                <Dices />
                Pick at random
              </Button>
              {featured?.explicit && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    clearFeatured(todayKey);
                    toast.info('Today follows the daily rotation again.');
                  }}
                >
                  <RotateCcw />
                  Back to rotation
                </Button>
              )}
            </div>
          </CardContent>
        </Card>

        <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
          <CardHeader>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="min-w-0">
                <CardTitle className="text-xl">Upcoming schedule</CardTitle>
                <CardDescription>
                  The next seven days. A day on automatic follows the daily rotation.
                </CardDescription>
              </div>
              <CalendarClock className="h-5 w-5 shrink-0 text-muted-foreground" />
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            {upcomingKeys.map((key) => {
              const slot = featuredOn(key);
              const pinnedGame = slot.explicit ? slot.game : null;
              const options =
                pinnedGame && !eligibleGames.some((game) => game.id === pinnedGame.id)
                  ? [...eligibleGames, pinnedGame]
                  : eligibleGames;

              return (
                <div
                  key={key}
                  className="flex flex-wrap items-center justify-between gap-3 border-b border-border/40 pb-3 last:border-0 last:pb-0"
                >
                  <div className="min-w-0">
                    <p className="text-sm font-semibold">{featuredDateLabel(key)}</p>
                    <p className="truncate text-xs text-muted-foreground">
                      {slot.explicit
                        ? `Pinned · ${slot.game?.name ?? 'unknown game'}`
                        : 'Automatic rotation'}
                    </p>
                  </div>
                  <Select
                    value={slot.explicit && pinnedGame ? pinnedGame.id : AUTO}
                    onValueChange={(next) => {
                      if (next === AUTO) {
                        clearFeatured(key);
                        toast.info(`${featuredDateLabel(key)} follows the rotation.`);
                        return;
                      }
                      setFeatured(key, next);
                      toast.success(
                        `${getGameById(next)?.name ?? next} scheduled for ${featuredDateLabel(key)}.`,
                      );
                    }}
                  >
                    <SelectTrigger className="w-44">
                      <SelectValue placeholder="Automatic" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={AUTO}>Automatic</SelectItem>
                      {options.map((game) => (
                        <SelectItem key={game.id} value={game.id}>
                          {game.name}
                          {game.status !== 'available' || !isEnabled(game.id) ? ' (off)' : ''}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              );
            })}
          </CardContent>
        </Card>
      </div>

      {can(CAPABILITIES.GAMES_MANAGE) && <AdminWordleConfig />}
    </div>
  );
};

export default AdminGamesManage;
