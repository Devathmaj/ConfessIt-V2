/**
 * Games analytics for the admin overview: the numbers behind the section.
 *
 * Availability comes from the server's per-game configuration -- the same
 * rows the manage screens edit -- and play figures from the server's
 * finished-round telemetry, built by the same collectors the player
 * dashboard reads. Nothing here invents a number: a request that has not
 * answered is the page's loading state, never a zero.
 *
 * The activity log holds entries appended by real admin actions in the
 * context. It is deliberately session-local and starts empty: a fabricated
 * "featured" line would name a pick the server never made.
 */

import type { GameConfig, GameStatsSummary } from '@/services/api';
import { AVAILABLE_GAMES, type GameDefinition, type GameTypeId, gameTypeLabel } from './gamesCatalog';

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
 * Every figure the overview shows, from the server's configuration and play
 * telemetry. Counts are joined to the catalogue by game id, so the dashboard
 * can never report a title players cannot open. ``stats`` is null while the
 * telemetry request is in flight or has failed; availability is still
 * answered from the configuration then, and play fields read zero only for
 * that transitional state -- the page shows the request's own loading/error
 * copy instead of presenting the zeros as fact.
 */
export const gamesAnalytics = (
  configs: readonly GameConfig[],
  stats: GameStatsSummary | null,
): GamesAnalytics => {
  const configById = new Map(configs.map((config) => [config.game_id, config]));
  // A game with no config row reads as its server default: live until an
  // administrator says otherwise.
  const enabledGames = AVAILABLE_GAMES.filter((game) => {
    const config = configById.get(game.id);
    return config === undefined || (config.enabled && !config.is_hidden);
  }).length;

  const weekPlays = stats?.plays_this_week ?? 0;
  const totalsByGame = new Map(
    stats?.games.map((totals) => [totals.game_id, totals]) ?? [],
  );
  const playsWeekFor = (game: GameDefinition): number =>
    totalsByGame.get(game.id)?.plays_this_week ?? 0;

  const topGames: TopGame[] = AVAILABLE_GAMES.map((game) => ({
    game,
    playsWeek: playsWeekFor(game),
  }))
    .filter((entry) => entry.playsWeek > 0)
    .sort((a, b) => b.playsWeek - a.playsWeek)
    .slice(0, 5)
    .map((entry) => ({
      game: entry.game,
      playsWeek: entry.playsWeek,
      share: percentOf(entry.playsWeek, weekPlays),
    }));

  const typeBreakdown: GameTypeBreakdown[] = (['single', 'couple'] as GameTypeId[])
    .map((type) => {
      const inType = AVAILABLE_GAMES.filter((game) => game.type === type);
      const playsWeek = inType.reduce((sum, game) => sum + playsWeekFor(game), 0);
      return {
        type,
        label: gameTypeLabel(type),
        gameCount: inType.length,
        playsWeek,
        share: percentOf(playsWeek, weekPlays),
      };
    })
    .filter((entry) => entry.gameCount > 0);

  return {
    totalGames: AVAILABLE_GAMES.length,
    enabledGames,
    disabledGames: AVAILABLE_GAMES.length - enabledGames,
    comingSoonGames: AVAILABLE_GAMES.filter((game) => game.status !== 'available').length,
    playersToday: stats?.players_today ?? 0,
    playersThisWeek: stats?.players_this_week ?? 0,
    playsToday: stats?.plays_today ?? 0,
    playsThisWeek: weekPlays,
    playsAllTime: stats?.plays_all_time ?? 0,
    topGames,
    typeBreakdown,
    activeGameTypes: typeBreakdown.filter((entry) => entry.playsWeek > 0).length,
  };
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
