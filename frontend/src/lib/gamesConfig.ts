/**
 * The configuration layer that sits between the game catalogue and everything that shows it.
 *
 * `gamesCatalog` says what a game *is*; this module says what an administrator has *done* to
 * it -- which titles are currently available and which game owns which day's spotlight.
 * The server (`GET /games/select`, `GET /games/featured`, `/admin/featured`) is the source
 * of truth; this in-memory state only seeds local fallbacks so pages render before the
 * fetch resolves. There is no mock schedule content beyond the catalogue rotation.
 */

import {
  AVAILABLE_GAMES,
  getGameById,
  featuredGameFrom,
  type GameDefinition,
} from './gamesCatalog';
import {
  featuredDateKey,
  featuredGameIdFor,
  type FeaturedDateKey,
  type FeaturedSchedule,
} from './gamesFeaturedSchedule';

export type GameSettingValue = string | number | boolean;

/**
 * How a game's setting is edited, and therefore how the admin UI draws it.
 *
 * A game declares its own settings in `GAME_SETTING_DEFINITIONS` and the manage screen renders
 * whichever kinds it finds -- so a new game with a new kind of knob needs a definition here and
 * a renderer for that kind, never a bespoke settings screen. Only kinds that exist today are
 * declared; adding one later is additive.
 */
export type GameSettingKind = 'toggle' | 'select' | 'number';

export interface GameSettingOption {
  value: string;
  label: string;
}

export interface GameSettingDefinition {
  /** Stable identifier; the value this writes to. */
  key: string;
  label: string;
  kind: GameSettingKind;
  /** One line of guidance shown under the control. */
  help?: string;
  /** Required for `select`. */
  options?: GameSettingOption[];
  /** Bounds for `number`. */
  min?: number;
  max?: number;
  step?: number;
}

/**
 * Per-game administrative settings, by game id.
 *
 * No game declares local-only settings: availability, content mode, reset
 * cadence and grid size live on the server (`GET /admin/games/config`).
 * A game opts in by adding its own list here; a game with no entry renders
 * as "no game-specific settings".
 */
export const GAME_SETTING_DEFINITIONS: Record<string, GameSettingDefinition[]> = {};

export const settingsForGame = (gameId: string): GameSettingDefinition[] =>
  GAME_SETTING_DEFINITIONS[gameId] ?? [];

const defaultValueFor = (definition: GameSettingDefinition): GameSettingValue => {
  if (definition.kind === 'toggle') return false;
  if (definition.kind === 'number') return definition.min ?? 0;
  return definition.options?.[0]?.value ?? '';
};

/**
 * A declared setting's current value, falling back to its declared default.
 *
 * The one place that knows the default lives here rather than in each renderer, so a control
 * that has never been touched and a control whose stored value went missing render the same
 * thing instead of one showing blank.
 */
export const settingValueFor = (
  config: GamesConfigState,
  gameId: string,
  definition: GameSettingDefinition,
): GameSettingValue => {
  const stored = config.settings[gameId]?.[definition.key];
  return stored === undefined ? defaultValueFor(definition) : stored;
};

/** The whole configuration surface, as three id-keyed maps. */
export interface GamesConfigState {
  /** gameId -> whether the game is part of the user-facing experience right now. */
  availability: Record<string, boolean>;
  /** gameId -> setting key -> value. Only games with declared settings ever have an entry. */
  settings: Record<string, Record<string, GameSettingValue>>;
  /** dateKey -> gameId. See `gamesFeaturedSchedule` for the one-day-per-pick rule. */
  featuredSchedule: FeaturedSchedule;
}

/** How many days of the schedule are seeded so the admin's calendar starts populated. */
const SCHEDULE_SEED_DAYS = 14;

/**
 * A fresh configuration for a deployment with no stored state.
 *
 * Every catalogue game starts enabled (availability is an opt-out, so a game nobody has
 * configured still appears), settings start at their declared defaults, and the schedule is
 * seeded for the next two weeks starting from the pick the catalogue's own day rotation would
 * show today -- the admin screen and the player's spotlight therefore agree before anybody
 * touches anything.
 */
export const initialGamesConfig = (now: Date = new Date()): GamesConfigState => {
  const availability: Record<string, boolean> = {};
  const settings: Record<string, Record<string, GameSettingValue>> = {};

  AVAILABLE_GAMES.forEach((game) => {
    availability[game.id] = true;
    const declared = settingsForGame(game.id);
    if (declared.length > 0) {
      settings[game.id] = Object.fromEntries(
        declared.map((definition) => [definition.key, defaultValueFor(definition)]),
      );
    }
  });

  return {
    availability,
    settings,
    featuredSchedule: seedFeaturedSchedule(now),
  };
};

/**
 * Two weeks of picks, rotated from today's catalogue pick.
 *
 * Built by finding today's rotation pick and walking forward rather than by indexing the day
 * of the year a second time, so the seed cannot drift from `featuredGameFrom` if either is
 * ever changed.
 */
const seedFeaturedSchedule = (now: Date): FeaturedSchedule => {
  if (AVAILABLE_GAMES.length === 0) return {};

  const todayPick = featuredGameFrom(AVAILABLE_GAMES, now);
  const startIndex = Math.max(
    0,
    AVAILABLE_GAMES.findIndex((game) => game.id === todayPick?.id),
  );

  const schedule: FeaturedSchedule = {};
  for (let index = 0; index < SCHEDULE_SEED_DAYS; index += 1) {
    const date = new Date(now);
    date.setDate(date.getDate() + index);
    schedule[featuredDateKey(date)] = AVAILABLE_GAMES[(startIndex + index) % AVAILABLE_GAMES.length].id;
  }
  return schedule;
};

/** Whether an administrator has this game switched on. Unknown games default to on. */
export const isGameEnabled = (config: GamesConfigState, gameId: string): boolean =>
  config.availability[gameId] ?? true;

/** Whether a game can be offered to players at all: enabled *and* shipped. */
export const isGamePlayable = (config: GamesConfigState, game: GameDefinition): boolean =>
  isGameEnabled(config, game.id) && game.status === 'available';

/**
 * The pool a featured pick may be drawn from: enabled, shipped titles.
 *
 * Both the "pick random" action and the schedule's own fallback resolve through this, so a
 * disabled or not-yet-released game can never end up in the spotlight -- not by a random draw
 * and not by being the day's rotation pick.
 */
export const eligibleFeaturedGames = (
  config: GamesConfigState,
  now: Date = new Date(),
): GameDefinition[] => AVAILABLE_GAMES.filter((game) => isGamePlayable(config, game));

/** What owns the spotlight on `now`'s day, and whether it was pinned or rotated into place. */
export interface ResolvedFeaturedGame {
  game: GameDefinition;
  /** True when an administrator pinned this day; false when it fell back to the rotation. */
  explicit: boolean;
  dateKey: FeaturedDateKey;
}

/**
 * Today's featured game, honouring the schedule and falling back to a rotation over the
 * eligible pool when the day has no pick of its own -- or when its pick has since been
 * switched off, which is the same thing as far as players are concerned.
 *
 * Returning `null` only when nothing is eligible at all: every game disabled is a state the
 * UI has to survive, and it should say so rather than crash on an empty list.
 */
export const resolveFeaturedGame = (
  config: GamesConfigState,
  now: Date = new Date(),
): ResolvedFeaturedGame | null => {
  const dateKey = featuredDateKey(now);
  const eligible = eligibleFeaturedGames(config, now);
  if (eligible.length === 0) return null;

  const pinnedId = featuredGameIdFor(config.featuredSchedule, dateKey);
  const pinned = pinnedId ? getGameById(pinnedId) : undefined;
  if (pinned && isGamePlayable(config, pinned)) {
    return { game: pinned, explicit: true, dateKey };
  }

  const rotated = featuredGameFrom(eligible, now);
  return rotated ? { game: rotated, explicit: false, dateKey } : null;
};

/**
 * The ids eligible for a random draw, with the excluded game left out when there is anything
 * else to choose. "Random" that lands on the game already featured tells the administrator
 * nothing, so the current pick is only considered when it is the sole option.
 */
export const randomPickCandidates = (
  config: GamesConfigState,
  excludeGameId?: string,
): string[] => {
  const eligible = eligibleFeaturedGames(config).map((game) => game.id);
  const others = excludeGameId ? eligible.filter((id) => id !== excludeGameId) : eligible;
  return others.length > 0 ? others : eligible;
};
