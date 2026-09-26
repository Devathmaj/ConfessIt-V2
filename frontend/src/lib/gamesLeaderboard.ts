/**
 * Leaderboard query shapes for the Games section.
 *
 * There is no global leaderboard backend, so every query below answers
 * empty until one exists: the pages render their empty states rather than
 * synthesised rosters or scores. Per-game leaderboards that do exist on the
 * server (Two-Key Escape, Trivia Drop history) are read from their own game
 * endpoints, not from here.
 */

import type { GameTypeId } from './gamesCatalog';

export type LeaderboardScope = 'global' | 'daily' | 'weekly' | 'monthly';

export interface LeaderboardScopeOption {
  id: LeaderboardScope;
  label: string;
  /** Short hint shown under the scope title or in a tooltip-style subtitle. */
  hint: string;
}

export interface LeaderboardEntry {
  /** 1-based position after sorting by score. */
  rank: number;
  playerId: string;
  displayName: string;
  /** Two-letter monogram for the avatar circle. */
  monogram: string;
  score: number;
  /** Positions gained/lost since the previous update; 0 renders as "no change". */
  trendDelta: number;
  isCurrentUser: boolean;
}

export interface LeaderboardQuery {
  scope: LeaderboardScope;
  /** A catalogue game id, or 'all' for the combined board. */
  gameId: string;
}

export const ALL_GAMES_FILTER = 'all';

export const LEADERBOARD_SCOPES: LeaderboardScopeOption[] = [
  { id: 'global', label: 'Global', hint: 'All-time standings' },
  { id: 'daily', label: 'Daily', hint: 'Resets every midnight' },
  { id: 'weekly', label: 'Weekly', hint: 'Resets every Monday' },
  { id: 'monthly', label: 'Monthly', hint: 'Resets on the 1st' },
];

/**
 * The board for one scope + game combination. Empty until a leaderboard
 * endpoint exists.
 */
export const getLeaderboardEntries = (_query: LeaderboardQuery): LeaderboardEntry[] => [];

/** Ranked population the boards represent. Zero while boards are empty. */
export const MOCK_RANKED_PLAYERS = 0;

export interface LeaderboardUserSummary {
  rank: number;
  score: number;
  /** Percentage of the ranked population the player outranks, e.g. 8 for "Top 8%". */
  topPercent: number;
  totalPlayers: number;
}

/** The current user's position on a board. Null while boards are empty. */
export const summarizeCurrentUser = (
  _entries: LeaderboardEntry[],
): LeaderboardUserSummary | null => null;

// ---------------------------------------------------------------------------
// The administrative board
// ---------------------------------------------------------------------------

export type AdminBoardScope = 'global' | 'game';
export type AdminBoardPeriod = 'daily' | 'weekly' | 'all_time';
/** Single/Couple narrows the global board; a single game already knows which it is. */
export type AdminBoardGameTypeFilter = 'all' | GameTypeId;

export interface AdminBoardOption {
  id: string;
  label: string;
  hint: string;
}

export const ADMIN_BOARD_SCOPES: AdminBoardOption[] = [
  { id: 'global', label: 'Global', hint: 'Every game on one board' },
  { id: 'game', label: 'Game', hint: 'Standings for a single title' },
];

export const ADMIN_BOARD_PERIODS: AdminBoardOption[] = [
  { id: 'daily', label: 'Daily', hint: 'Since midnight' },
  { id: 'weekly', label: 'Weekly', hint: 'Since Monday' },
  { id: 'all_time', label: 'All time', hint: 'Everything on record' },
];

export interface AdminLeaderboardQuery {
  scope: AdminBoardScope;
  period: AdminBoardPeriod;
  /** A catalogue game id. Meaningful only when `scope` is `game`. */
  gameId: string;
  /** Meaningful only when `scope` is `global`; ignored for a single game. */
  gameType: AdminBoardGameTypeFilter;
}

export type AdminLeaderboardResult = 'completed' | 'forfeited' | 'abandoned';

export const ADMIN_LEADERBOARD_RESULTS: Array<{ id: AdminLeaderboardResult; label: string }> = [
  { id: 'completed', label: 'Completed' },
  { id: 'forfeited', label: 'Forfeited' },
  { id: 'abandoned', label: 'Abandoned' },
];

export const adminResultLabel = (result: AdminLeaderboardResult): string =>
  ADMIN_LEADERBOARD_RESULTS.find((entry) => entry.id === result)?.label ?? result;

export interface AdminLeaderboardRow {
  rank: number;
  playerId: string;
  displayName: string;
  monogram: string;
  /** The title this standing belongs to: the selected game, or the player's leading title. */
  gameId: string;
  gameType: GameTypeId;
  score: number;
  plays: number;
  result: AdminLeaderboardResult;
  /** Epoch milliseconds of the most recent session inside the selected period. */
  lastPlayedAt: number;
  trendDelta: number;
}

/**
 * The admin board for one filter combination. Empty until a leaderboard
 * endpoint exists.
 */
export const getAdminLeaderboardRows = (
  _query: AdminLeaderboardQuery,
  _now: number = Date.now(),
): AdminLeaderboardRow[] => [];

export interface AdminBoardSummary {
  rows: number;
  totalPlays: number;
  topScore: number;
  averageScore: number;
  /** Sessions that did not complete, across every row on the board. */
  incompleteSessions: number;
}

export const summarizeAdminBoard = (_rows: AdminLeaderboardRow[]): AdminBoardSummary => ({
  rows: 0,
  totalPlays: 0,
  topScore: 0,
  averageScore: 0,
  incompleteSessions: 0,
});
