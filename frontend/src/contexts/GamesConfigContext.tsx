import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { getGameById, type GameDefinition } from '@/lib/gamesCatalog';
import {
  eligibleFeaturedGames,
  isGameEnabled,
  randomPickCandidates,
  resolveFeaturedGame,
  settingsForGame,
  initialGamesConfig,
  type GameSettingValue,
  type GamesConfigState,
  type ResolvedFeaturedGame,
} from '@/lib/gamesConfig';
import {
  featuredDateKey,
  randomGameIdFrom,
  withFeaturedGame,
  withoutFeaturedGame,
  type FeaturedDateKey,
} from '@/lib/gamesFeaturedSchedule';
import type {
  GamesActivityEntry,
  GamesActivityKind,
} from '@/lib/adminGamesAnalytics';

/**
 * The one instance of Games configuration the whole app reads.
 *
 * `/games` and `/admin/games` are two views of the same catalogue, so they must also be two
 * views of the same configuration: an administrator switching a game off, pinning a featured
 * title or changing a setting is editing the state this provider owns, and the player-facing
 * pages see the result on their next render. There is deliberately no second copy anywhere --
 * no admin-only store that the user pages would have to be *taught* to read later.
 *
 * Prototype state held in memory, but shaped as a future API would shape it: one `config`
 * object of id-keyed maps that could be persisted as one record, plus an append-only activity
 * log. Replacing the setters with requests and `initialGamesConfig` with a fetch is the whole
 * migration; nothing in the pages changes, because they only ever read through this hook.
 *
 * The context has a built-in default rather than requiring a provider, so a component that
 * only reads (the player's featured page, a unit test rendering one screen) works standalone
 * against a freshly seeded configuration. Anything that *writes* renders inside the provider,
 * which `App` mounts around the entire router.
 */

interface FeaturedSlot {
  /** The game for that day, or null when nothing eligible could be chosen. */
  game: GameDefinition | null;
  /** True when an administrator pinned the day; false when it falls back to the rotation. */
  explicit: boolean;
}

interface GamesConfigContextValue {
  config: GamesConfigState;
  /** Newest first. Starts empty; real admin actions append to it. */
  activity: GamesActivityEntry[];
  /** Today's spotlight, honouring the schedule and eligibility. */
  featured: ResolvedFeaturedGame | null;
  /** What may be chosen as featured: enabled and shipped. */
  eligibleGames: GameDefinition[];
  /** Whether a game is part of the player-facing experience right now. */
  isEnabled: (gameId: string) => boolean;
  /** Any day's slot, for the schedule rows. */
  featuredOn: (dateKey: FeaturedDateKey) => FeaturedSlot;
  setGameEnabled: (gameId: string, enabled: boolean) => void;
  setGameSetting: (gameId: string, key: string, value: GameSettingValue) => void;
  setFeatured: (dateKey: FeaturedDateKey, gameId: string) => void;
  clearFeatured: (dateKey: FeaturedDateKey) => void;
  /**
   * Pins a random eligible game on `dateKey` (today unless said otherwise) and returns it,
   * so the caller can name the pick it just made. Null when nothing is eligible.
   */
  setRandomFeatured: (dateKey?: FeaturedDateKey) => string | null;
  /** Appends an entry to the activity log; used by screens the context does not own. */
  logActivity: (entry: { kind: GamesActivityKind; message: string; gameId?: string }) => void;
  /**
   * Whether the Anagram page was left mid-game during this app session.
   *
   * Session UI state rather than configuration, kept here for one reason: this provider
   * wraps the whole router, so the flag survives every in-site navigation and dies on a
   * refresh -- exactly the split the Anagram resume gate needs. A refresh restores the
   * board outright; navigating away and back hides the word until the first click.
   */
  anagramLeftMidGame: boolean;
  /** Records leaving (or returning to) the Anagram page mid-game. */
  setAnagramLeftMidGame: (left: boolean) => void;
}

export const GamesConfigContext = createContext<GamesConfigContextValue | undefined>(undefined);

const gameName = (gameId: string): string => getGameById(gameId)?.name ?? gameId;

/** Built once and reused: the read-only default for renders outside the provider. */
let defaultContext: GamesConfigContextValue | undefined;

const defaultGamesConfigValue = (): GamesConfigContextValue => {
  if (!defaultContext) {
    const config = initialGamesConfig();
    defaultContext = {
      config,
      activity: [],
      featured: resolveFeaturedGame(config),
      eligibleGames: [],
      isEnabled: (gameId) => isGameEnabled(config, gameId),
      featuredOn: (dateKey) => {
        const explicitId = config.featuredSchedule[dateKey];
        return { game: explicitId ? getGameById(explicitId) ?? null : null, explicit: Boolean(explicitId) };
      },
      setGameEnabled: () => undefined,
      setGameSetting: () => undefined,
      setFeatured: () => undefined,
      clearFeatured: () => undefined,
      setRandomFeatured: () => null,
      logActivity: () => undefined,
      anagramLeftMidGame: false,
      setAnagramLeftMidGame: () => undefined,
    };
  }
  return defaultContext;
};

export const GamesConfigProvider = ({ children }: { children: ReactNode }) => {
  const [config, setConfig] = useState<GamesConfigState>(() => initialGamesConfig());
  const [activity, setActivity] = useState<GamesActivityEntry[]>([]);
  const [anagramLeftMidGame, setAnagramLeftMidGame] = useState(false);

  // Ids only have to be unique within the log, and a counter is stable across re-renders
  // where Date.now() would not be -- two entries written in the same millisecond would
  // otherwise share a React key.
  const entryCounter = useRef(0);

  const pushActivity = useCallback(
    (entry: { kind: GamesActivityKind; message: string; gameId?: string }) => {
      entryCounter.current += 1;
      const next: GamesActivityEntry = {
        id: `activity-${entryCounter.current}`,
        at: Date.now(),
        ...entry,
      };
      setActivity((prev) => [next, ...prev]);
    },
    [],
  );

  const isEnabled = useCallback(
    (gameId: string) => isGameEnabled(config, gameId),
    [config],
  );

  const featuredOn = useCallback(
    (dateKey: FeaturedDateKey): FeaturedSlot => {
      const explicitId = config.featuredSchedule[dateKey];
      if (!explicitId) return { game: null, explicit: false };
      return { game: getGameById(explicitId) ?? null, explicit: true };
    },
    [config],
  );

  const setGameEnabled = useCallback(
    (gameId: string, enabled: boolean) => {
      setConfig((prev) =>
        (prev.availability[gameId] ?? true) === enabled
          ? prev
          : { ...prev, availability: { ...prev.availability, [gameId]: enabled } },
      );
      pushActivity({
        kind: 'availability',
        gameId,
        message: `${gameName(gameId)} ${enabled ? 'switched on' : 'switched off'}`,
      });
    },
    [pushActivity],
  );

  const setGameSetting = useCallback(
    (gameId: string, key: string, value: GameSettingValue) => {
      setConfig((prev) => ({
        ...prev,
        settings: {
          ...prev.settings,
          [gameId]: { ...(prev.settings[gameId] ?? {}), [key]: value },
        },
      }));
      const label = settingsForGame(gameId).find((definition) => definition.key === key)?.label ?? key;
      pushActivity({
        kind: 'settings',
        gameId,
        message: `${gameName(gameId)} · ${label} set to ${String(value)}`,
      });
    },
    [pushActivity],
  );

  const setFeatured = useCallback(
    (dateKey: FeaturedDateKey, gameId: string) => {
      setConfig((prev) => ({
        ...prev,
        featuredSchedule: withFeaturedGame(prev.featuredSchedule, dateKey, gameId),
      }));
      const todayKey = featuredDateKey(new Date());
      pushActivity({
        kind: 'featured',
        gameId,
        message:
          dateKey === todayKey
            ? `${gameName(gameId)} set as today's featured game`
            : `${gameName(gameId)} scheduled as featured for ${dateKey}`,
      });
    },
    [pushActivity],
  );

  const clearFeatured = useCallback(
    (dateKey: FeaturedDateKey) => {
      setConfig((prev) => ({
        ...prev,
        featuredSchedule: withoutFeaturedGame(prev.featuredSchedule, dateKey),
      }));
      pushActivity({
        kind: 'featured',
        message: `Featured pick for ${dateKey} cleared — back to the daily rotation`,
      });
    },
    [pushActivity],
  );

  const setRandomFeatured = useCallback(
    (dateKey?: FeaturedDateKey): string | null => {
      const key = dateKey ?? featuredDateKey(new Date());
      const currentId =
        key === featuredDateKey(new Date())
          ? resolveFeaturedGame(config)?.game.id
          : config.featuredSchedule[key];

      const pick = randomGameIdFrom(randomPickCandidates(config, currentId));
      if (!pick) return null;

      setConfig((prev) => ({
        ...prev,
        featuredSchedule: withFeaturedGame(prev.featuredSchedule, key, pick),
      }));
      const todayKey = featuredDateKey(new Date());
      pushActivity({
        kind: 'featured',
        gameId: pick,
        message:
          key === todayKey
            ? `${gameName(pick)} picked at random as today's featured game`
            : `${gameName(pick)} picked at random as featured for ${key}`,
      });
      return pick;
    },
    [config, pushActivity],
  );

  const value = useMemo<GamesConfigContextValue>(
    () => ({
      config,
      activity,
      featured: resolveFeaturedGame(config),
      eligibleGames: eligibleFeaturedGames(config),
      isEnabled,
      featuredOn,
      setGameEnabled,
      setGameSetting,
      setFeatured,
      clearFeatured,
      setRandomFeatured,
      logActivity: pushActivity,
      anagramLeftMidGame,
      setAnagramLeftMidGame,
    }),
    [
      config,
      activity,
      isEnabled,
      featuredOn,
      setGameEnabled,
      setGameSetting,
      setFeatured,
      clearFeatured,
      setRandomFeatured,
      pushActivity,
      anagramLeftMidGame,
    ],
  );

  return <GamesConfigContext.Provider value={value}>{children}</GamesConfigContext.Provider>;
};

export const useGamesConfig = (): GamesConfigContextValue =>
  useContext(GamesConfigContext) ?? defaultGamesConfigValue();
