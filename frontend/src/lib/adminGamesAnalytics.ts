/**
 * Games analytics for the admin dashboard: the numbers behind the section.
 *
 * Catalogue-derived counts (how many games, how many are on) are computed
 * from the live configuration, so they cannot disagree with what the manage
 * screen shows. Engagement counts are zero until play telemetry exists on
 * the backend -- the dashboard renders empty states rather than mock
 * figures.
 *
 * The activity log holds entries appended by real admin actions in the
 * context, plus one seeded row describing today's resolved pick.
 */

import { AVAILABLE_GAMES, type GameDefinition, type GameTypeId, gameTypeLabel } from './gamesCatalog';
import {
  isGameEnabled,
  resolveFeaturedGame,
  type GamesConfigState,
} from './gamesConfig';

/** Play totals for one title. All zero until play telemetry exists. */
export interface GamePlayTotals {
  gameId: string;
  playsToday: number;
  playsWeek: number;
  playsAllTime: number;
}

const MOCK_PLAYS: Record<string, Omit<GamePlayTotals, 'gameId'>> = {};

/** Players, not sessions. Zero until telemetry exists. */
export const MOCK_PLAYERS_TODAY = 0;
export const MOCK_PLAYERS_THIS_WEEK = 0;

const playTotalsFor = (game: GameDefinition): GamePlayTotals => ({
  gameId: game.id,
  ...(MOCK_PLAYS[game.id] ?? { playsToday: 0, playsWeek: 0, playsAllTime: 0 }),
});

export interface TopGame {
  game: GameDefinition;
  playsWeek: number;
  /** Share of the week's plays, 0–100, rounded. */
  share: number;
}

export interface GameTypeBreakdown {
  type: GameTypeId;
  label: string;
  gameCount: number;
  playsWeek: number;
  share: number;
}

export type GamesActivityKind =
  | 'featured'
  | 'availability'
  | 'settings'
  | 'leaderboard'
  | 'puzzle'
  | 'system';

export interface GamesActivityEntry {
  id: string;
  /** Epoch milliseconds. */
  at: number;
  kind: GamesActivityKind;
  message: string;
  /** The game this entry is about, when there is one. */
  gameId?: string;
}

export interface GamesAnalytics {
  totalGames: number;
  enabledGames: number;
  disabledGames: number;
  comingSoonGames: number;
  playersToday: number;
  playersThisWeek: number;
  playsToday: number;
  playsThisWeek: number;
  playsAllTime: number;
  /** Titles with at least one play this week, most played first. */
  topGames: TopGame[];
  /** Single vs Couple, by this week's plays. */
  typeBreakdown: GameTypeBreakdown[];
  activeGameTypes: number;
}

const percentOf = (part: number, whole: number): number =>
  whole === 0 ? 0 : Math.round((part / whole) * 100);

/**
 * Every figure the dashboard shows, computed from the live configuration.
 * Play-derived lists stay empty until telemetry exists.
 */
export const gamesAnalytics = (config: GamesConfigState): GamesAnalytics => {
  const totals = AVAILABLE_GAMES.map((game) => ({ game, totals: playTotalsFor(game) }));

  const playsToday = totals.reduce((sum, entry) => sum + entry.totals.playsToday, 0);
  const playsThisWeek = totals.reduce((sum, entry) => sum + entry.totals.playsWeek, 0);
  const playsAllTime = totals.reduce((sum, entry) => sum + entry.totals.playsAllTime, 0);

  const enabledGames = AVAILABLE_GAMES.filter((game) => isGameEnabled(config, game.id)).length;

  const topGames: TopGame[] = totals
    .filter((entry) => entry.totals.playsWeek > 0)
    .sort((a, b) => b.totals.playsWeek - a.totals.playsWeek)
    .slice(0, 5)
    .map((entry) => ({
      game: entry.game,
      playsWeek: entry.totals.playsWeek,
      share: percentOf(entry.totals.playsWeek, playsThisWeek),
    }));

  const typeBreakdown: GameTypeBreakdown[] = (['single', 'couple'] as GameTypeId[])
    .map((type) => {
      const inType = totals.filter((entry) => entry.game.type === type);
      const playsWeek = inType.reduce((sum, entry) => sum + entry.totals.playsWeek, 0);
      return {
        type,
        label: gameTypeLabel(type),
        gameCount: inType.length,
        playsWeek,
        share: percentOf(playsWeek, playsThisWeek),
      };
    })
    .filter((entry) => entry.gameCount > 0);

  return {
    totalGames: AVAILABLE_GAMES.length,
    enabledGames,
    disabledGames: AVAILABLE_GAMES.length - enabledGames,
    comingSoonGames: AVAILABLE_GAMES.filter((game) => game.status !== 'available').length,
    playersToday: MOCK_PLAYERS_TODAY,
    playersThisWeek: MOCK_PLAYERS_THIS_WEEK,
    playsToday,
    playsThisWeek,
    playsAllTime,
    topGames,
    typeBreakdown,
    activeGameTypes: typeBreakdown.filter((entry) => entry.playsWeek > 0).length,
  };
};

/** Today's headline pick, for the dashboard tile. Null when nothing is eligible at all. */
export const featuredSnapshot = (config: GamesConfigState) => resolveFeaturedGame(config);

/**
 * The activity log as the dashboard first sees it: today's resolved pick,
 * when there is one. Real admin actions prepend their own entries through
 * the context.
 */
export const seedGamesActivity = (config: GamesConfigState, now: number = Date.now()): GamesActivityEntry[] => {
  const todayStart = new Date(now);
  todayStart.setHours(0, 0, 0, 0);

  const featured = resolveFeaturedGame(config, new Date(now));
  const entries: GamesActivityEntry[] = [];

  if (featured) {
    entries.push({
      id: 'seed-featured',
      at: todayStart.getTime() + 60_000,
      kind: 'featured',
      message: `${featured.game.name} set as today's featured game`,
      gameId: featured?.game.id,
    });
  }

  return entries.sort((a, b) => b.at - a.at);
};

/** "just now" / "12m ago" / "3h ago" / "2d ago", falling back to a date past that. */
export const formatActivityTime = (at: number, now: number = Date.now()): string => {
  const elapsed = Math.max(0, now - at);
  const minutes = Math.floor(elapsed / 60_000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
};
