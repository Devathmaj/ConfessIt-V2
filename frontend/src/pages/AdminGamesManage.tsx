import { useMemo, useState } from 'react';
import { CalendarClock, Dices, RotateCcw, Sparkles } from 'lucide-react';
import { toast } from 'sonner';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { useGamesConfig } from '@/contexts/GamesConfigContext';
import {
  AVAILABLE_GAMES,
  categoryLabel,
  gameTypeLabel,
  getGameById,
  type GameDefinition,
} from '@/lib/gamesCatalog';
import {
  settingValueFor,
  settingsForGame,
  type GameSettingDefinition,
} from '@/lib/gamesConfig';
import {
  addDays,
  featuredDateKey,
  featuredDateKeysFrom,
  featuredDateLabel,
} from '@/lib/gamesFeaturedSchedule';
import { cn } from '@/lib/utils';

const AUTO = 'auto';

/**
 * The generic editor for one declared game setting.
 *
 * Branches on the setting's `kind` rather than each game building its own controls, which is
 * the arrangement `GAME_SETTING_DEFINITIONS` assumes: a new kind of knob means one branch
 * here, a new game means data only. The number field commits on blur (or Enter) instead of on
 * every keystroke -- clamping while typing makes a bound like 3–12 impossible to type through.
 */
const GameSettingControl = ({
  gameId,
  definition,
}: {
  gameId: string;
  definition: GameSettingDefinition;
}) => {
  const { config, setGameSetting } = useGamesConfig();
  const [draft, setDraft] = useState<string | null>(null);
  const value = settingValueFor(config, gameId, definition);
  const controlId = `${gameId}-${definition.key}`;

  const commitDraft = () => {
    if (draft === null) return;
    const parsed = Number(draft);
    setDraft(null);
    if (draft.trim() === '' || Number.isNaN(parsed)) return;
    const upperBound = definition.max ?? parsed;
    const lowerBound = definition.min ?? parsed;
    setGameSetting(gameId, definition.key, Math.min(upperBound, Math.max(lowerBound, parsed)));
  };

  if (definition.kind === 'toggle') {
    return (
      <Switch
        id={controlId}
        checked={value === true}
        onCheckedChange={(checked) => setGameSetting(gameId, definition.key, checked)}
      />
    );
  }

  if (definition.kind === 'select') {
    return (
      <Select
        value={String(value)}
        onValueChange={(next) => setGameSetting(gameId, definition.key, next)}
      >
        <SelectTrigger id={controlId} className="w-44">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {(definition.options ?? []).map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    );
  }

  return (
    <Input
      id={controlId}
      type="number"
      className="w-24"
      min={definition.min}
      max={definition.max}
      step={definition.step ?? 1}
      value={draft ?? String(value)}
      onChange={(event) => setDraft(event.target.value)}
      onBlur={commitDraft}
      onKeyDown={(event) => {
        if (event.key === 'Enter') commitDraft();
      }}
    />
  );
};

/** One catalogue title: identity, its own settings, and the switch that takes it in or out. */
const GameManageCard = ({ game }: { game: GameDefinition }) => {
  const { isEnabled, setGameEnabled } = useGamesConfig();
  const definitions = settingsForGame(game.id);
  const enabled = isEnabled(game.id);
  const switchId = `${game.id}-availability`;
  const Icon = game.icon;

  return (
    <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
      <CardContent className="p-4 sm:p-5">
        <div className="flex flex-wrap items-start gap-4">
          <span className={cn('shrink-0 rounded-xl p-2.5 text-white', game.accent)}>
            <Icon className="h-5 w-5" />
          </span>

          <div className="min-w-0 flex-1 basis-64">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-base font-semibold">{game.name}</h3>
              <Badge variant="secondary">{categoryLabel(game.category)}</Badge>
              <Badge variant="outline">{gameTypeLabel(game.type)}</Badge>
              {game.status !== 'available' && <Badge variant="outline">Coming soon</Badge>}
            </div>
            <p className="mt-1 text-sm text-muted-foreground">{game.tagline}</p>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {game.players} · {game.duration} · {game.difficulty}
            </p>

            {definitions.length > 0 ? (
              <div className="mt-3 space-y-3 border-t border-border/40 pt-3">
                {definitions.map((definition) => (
                  <div
                    key={definition.key}
                    className="flex flex-wrap items-center justify-between gap-3"
                  >
                    <div className="min-w-0">
                      <label htmlFor={`${game.id}-${definition.key}`} className="text-sm font-medium">
                        {definition.label}
                      </label>
                      {definition.help && (
                        <p className="text-xs text-muted-foreground">{definition.help}</p>
                      )}
                    </div>
                    <GameSettingControl gameId={game.id} definition={definition} />
                  </div>
                ))}
              </div>
            ) : (
              <p className="mt-3 text-xs text-muted-foreground">No game-specific settings.</p>
            )}
          </div>

          <div className="flex shrink-0 items-center gap-2">
            <label htmlFor={switchId} className="text-sm font-medium text-muted-foreground">
              Live
            </label>
            <Switch
              id={switchId}
              checked={enabled}
              onCheckedChange={(next) => {
                setGameEnabled(game.id, next);
                toast.success(
                  next ? `${game.name} is live for players.` : `${game.name} switched off.`,
                );
              }}
            />
          </div>
        </div>
      </CardContent>
    </Card>
  );
};

/**
 * The screens where the catalogue is actually changed: which games exist for players, what
 * each one's own settings are set to, and who owns the spotlight -- today and for the week
 * ahead.
 *
 * Three sections, three kinds of state, all read from and written through `useGamesConfig`,
 * so everything here is immediately visible on `/games` with no refresh step: the switch
 * governs appearance in the selection grid, and the schedule is what the player's featured
 * page resolves against.
 */
export const AdminGamesManage = () => {
  const { config, featured, eligibleGames, isEnabled, featuredOn, setGameEnabled, setFeatured, clearFeatured, setRandomFeatured } =
    useGamesConfig();

  const todayKey = featuredDateKey(new Date());
  const upcomingKeys = useMemo(() => featuredDateKeysFrom(addDays(new Date(), 1), 7), []);
  const enabledCount = AVAILABLE_GAMES.filter((game) => isEnabled(game.id)).length;

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
          {enabledCount} of {AVAILABLE_GAMES.length} titles live, {eligibleGames.length} eligible
          for the spotlight. Changes here show up on the player-facing section straight away.
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

      <section className="flex flex-col gap-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 className="text-2xl font-semibold">Catalogue</h2>
            <p className="text-sm text-muted-foreground">
              Availability, genre, type and each game's own settings.
            </p>
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => {
              AVAILABLE_GAMES.forEach((game) => {
                if (!isEnabled(game.id)) setGameEnabled(game.id, true);
              });
              toast.success('Every title is live for players.');
            }}
            disabled={enabledCount === AVAILABLE_GAMES.length}
          >
            <Sparkles />
            Enable everything
          </Button>
        </div>

        <div className="space-y-4">
          {AVAILABLE_GAMES.map((game) => (
            <GameManageCard key={game.id} game={game} />
          ))}
        </div>
      </section>
    </div>
  );
};

export default AdminGamesManage;
