/**
 * Games analytics for the admin dashboard: the numbers behind the section.
 *
 * Two kinds of figure live here and they are deliberately separated. Catalogue-derived counts
 * (how many games, how many are on) are computed from the live configuration, so they cannot
 * disagree with what the manage screen shows. Engagement counts (plays, players) are mock
 * totals in `MOCK_PLAYS` -- there is no play telemetry to read -- and they sit in one table
 * keyed by catalogue id rather than being scattered through the dashboard's JSX, so replacing
 * them with an endpoint is one import to change.
 *
 * The activity log is the exception that proves the rule: entries are appended by the real
 * admin actions in the context, so "recent activity" reports what actually happened in this
 * session and only the seeded rows are fiction.
 */

import { AVAILABLE_GAMES, type GameDefinition, type GameTypeId, gameTypeLabel } from './gamesCatalog';
import {
  isGameEnabled,
  resolveFeaturedGame,
  type GamesConfigState,
} from './gamesConfig';

/** Play totals for one title. All three are mock figures until a play event exists. */
export interface GamePlayTotals {
  gameId: string;
  playsToday: number;
  playsWeek: number;
  playsAllTime: number;
}

const MOCK_PLAYS: Record<string, Omit<GamePlayTotals, 'gameId'>> = {
  'truth-dare': { playsToday: 148, playsWeek: 903, playsAllTime: 8_640 },
  'compatibility-quiz': { playsToday: 121, playsWeek: 774, playsAllTime: 7_120 },
  'love-letter-builder': { playsToday: 64, playsWeek: 402, playsAllTime: 3_980 },
  'hearts-memory-match': { playsToday: 96, playsWeek: 631, playsAllTime: 5_410 },
  'would-you-rather': { playsToday: 87, playsWeek: 545, playsAllTime: 4_760 },
  'emoji-decoded': { playsToday: 0, playsWeek: 0, playsAllTime: 0 },
  'sweet-nothings-sprint': { playsToday: 0, playsWeek: 0, playsAllTime: 0 },
};

/** Players, not sessions: one account that played five games counts once. */
export const MOCK_PLAYERS_TODAY = 312;
export const MOCK_PLAYERS_THIS_WEEK = 847;

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

export type GamesActivityKind = 'featured' | 'availability' | 'settings' | 'leaderboard' | 'system';

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
 * Every figure the dashboard shows, computed from the live configuration plus the mock play
 * table. Called on render rather than memoised anywhere: the list is seven games long.
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
 * The activity log as the dashboard first sees it.
 *
 * Seeded with entries that are true of a fresh configuration -- today's pick, the nightly
 * recalculation, the catalogue sync -- rather than with invented incidents, because a row
 * that claims a game was switched off while the manage screen says it is on is worse than no
 * row at all. Real actions prepend their own entries through the context.
 */
export const seedGamesActivity = (config: GamesConfigState, now: number = Date.now()): GamesActivityEntry[] => {
  const todayStart = new Date(now);
  todayStart.setHours(0, 0, 0, 0);

  const featured = resolveFeaturedGame(config, new Date(now));
  const entries: GamesActivityEntry[] = [
    {
      id: 'seed-featured',
      at: todayStart.getTime() + 60_000,
      kind: 'featured',
      message: featured
        ? `${featured.game.name} set as today's featured game`
        : 'No featured game could be selected',
      gameId: featured?.game.id,
    },
    {
      id: 'seed-leaderboard',
      at: now - 14 * 3_600_000,
      kind: 'leaderboard',
      message: 'Weekly leaderboard standings recalculated',
    },
    {
      id: 'seed-catalogue',
      at: now - 30 * 3_600_000,
      kind: 'system',
      message: 'Games catalogue synchronised with the player-facing section',
    },
  ];

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
