/**
 * Leaderboard data and the query that selects from it.
 *
 * The Games section is built around the assumption that leaderboards will have several
 * dimensions at once -- a time scope (global / daily / weekly / monthly) and a game filter
 * (every game, or one specific title). `getLeaderboardEntries` takes both and returns rows in
 * display order, which is the entire contract the leaderboard UI depends on.
 *
 * Rows are synthesised deterministically from the query: the same scope + game always yields
 * the same roster and scores, so switching filters feels stable instead of reshuffling on
 * every render. Replacing this one function with an API call is the intended upgrade path;
 * no component needs to change as long as the returned shape stays as it is.
 *
 * The admin board lives here too, deliberately. It ranks the same roster over the same
 * synthesis -- the two screens cannot drift apart, because there is only one of each -- and
 * adds the columns an administrator needs: which game a standing belongs to, how often it was
 * played, how the sessions ended and when. `getAdminLeaderboardRows` is its own query rather
 * than a flag on the player's, because its row shape is genuinely different.
 */

import { AVAILABLE_GAMES, getGameById, type GameTypeId } from './gamesCatalog';

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
 * Anonymous-style handles, matching the tone of the rest of ConfessIt. The signed-in player
 * is a roster entry like any other so their row can appear wherever it ranks rather than
 * being pinned to the top of the board.
 */
const ROSTER: Array<{ id: string; name: string }> = [
  { id: 'stargazer', name: 'StarGazer' },
  { id: 'moonlight-rose', name: 'MoonlightRose' },
  { id: 'silent-crush', name: 'SilentCrush' },
  { id: 'paper-hearts', name: 'PaperHearts' },
  { id: 'neon-dove', name: 'NeonDove' },
  { id: 'velvet-sky', name: 'VelvetSky' },
  { id: 'ink-stain', name: 'InkStain' },
  { id: 'night-owl', name: 'NightOwl_21' },
  { id: 'echoes', name: 'Echoes' },
  { id: 'coffee-bean', name: 'CoffeeBean' },
  { id: 'sunny-side', name: 'SunnySideUp' },
  { id: 'you', name: 'You' },
];

/** Score ranges per scope for the combined board. Per-game boards use a fraction of these. */
const SCOPE_RANGES: Record<LeaderboardScope, { min: number; max: number }> = {
  daily: { min: 60, max: 480 },
  weekly: { min: 400, max: 3_600 },
  monthly: { min: 1_500, max: 12_000 },
  global: { min: 5_000, max: 48_000 },
};

/** A single-game board is a smaller pool of points than the combined board. */
const PER_GAME_FACTOR = 0.4;

/**
 * A dependency-free string hash in [0, 1). No bitwise operators so the intent stays obvious
 * and the linter has nothing to complain about; stability across renders is all this needs.
 */
const unitHash = (input: string): number => {
  let hash = 0;
  for (let index = 0; index < input.length; index += 1) {
    hash = (hash * 31 + input.charCodeAt(index)) % 1_000_000_007;
  }
  return hash / 1_000_000_007;
};

const monogramFor = (name: string): string => {
  const letters = name.replace(/[^a-zA-Z]/g, '');
  if (letters.length === 0) return '??';
  return (letters[0] + (letters[1] ?? letters[0])).toUpperCase();
};

/**
 * Builds the board for one scope + game combination.
 *
 * Sorted by score descending, so callers can slice a teaser (top 3) or render the full list
 * without re-sorting. The current user is flagged rather than extracted, letting the UI both
 * highlight their row wherever it falls and summarise their rank from the same array.
 */
export const getLeaderboardEntries = ({
  scope,
  gameId,
}: LeaderboardQuery): LeaderboardEntry[] => {
  const { min, max } = SCOPE_RANGES[scope];
  const upperBound = gameId === ALL_GAMES_FILTER ? max : min + (max - min) * PER_GAME_FACTOR;
  const floor = gameId === ALL_GAMES_FILTER ? min : min * PER_GAME_FACTOR;

  const scored = ROSTER.map((player) => {
    const roll = unitHash(`${scope}:${gameId}:${player.id}`);
    const trendRoll = unitHash(`trend:${scope}:${gameId}:${player.id}`);
    const trendDirection = Math.floor(trendRoll * 3); // 0 = held, 1 = climbed, 2 = slipped
    const trendMagnitude = 1 + Math.floor(trendRoll * 4);
    const trendDelta =
      trendDirection === 0 ? 0 : trendDirection === 1 ? trendMagnitude : -trendMagnitude;

    return {
      playerId: player.id,
      displayName: player.name,
      monogram: monogramFor(player.name),
      score: Math.round(floor + roll * (upperBound - floor)),
      trendDelta,
      isCurrentUser: player.id === 'you',
    };
  });

  scored.sort((a, b) => b.score - a.score || a.displayName.localeCompare(b.displayName));

  return scored.map((entry, index) => ({ ...entry, rank: index + 1 }));
};

/**
 * How many players the boards claim to rank. The visible roster only holds the top of the
 * field, but "Top N%" is measured against the whole population the board represents, so the
 * percentage still reads like a real standing.
 */
export const MOCK_RANKED_PLAYERS = 1_240;

export interface LeaderboardUserSummary {
  rank: number;
  score: number;
  /** Percentage of the ranked population the player outranks, e.g. 8 for "Top 8%". */
  topPercent: number;
  totalPlayers: number;
}

/** Summarises the current user's position on a board for the "your rank" card. */
export const summarizeCurrentUser = (
  entries: LeaderboardEntry[],
): LeaderboardUserSummary | null => {
  const entry = entries.find((row) => row.isCurrentUser);
  if (!entry) return null;

  return {
    rank: entry.rank,
    score: entry.score,
    topPercent: Math.max(1, Math.ceil((entry.rank / MOCK_RANKED_PLAYERS) * 100)),
    totalPlayers: MOCK_RANKED_PLAYERS,
  };
};

// ---------------------------------------------------------------------------
// The administrative board
// ---------------------------------------------------------------------------

/**
 * Whether the board covers every game or one title, and over what window.
 *
 * Two dimensions rather than the player board's single scope selector, because the admin
 * filter set is bigger and a combined list of six overlapping options is unreadable: scope
 * and period answer different questions and are shown as two short segmented controls.
 * `all_time` maps onto the player board's `global` scope -- same roster, same scores, same
 * synthesis -- so the two surfaces agree on who is leading.
 */
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

/**
 * How a session ended, for the result column.
 *
 * Mock, and stated as an outcome rather than a score breakdown: there is no match record to
 * read yet, and "completed / forfeited / abandoned" is the vocabulary a moderator actually
 * acts on -- an abandoned session at the top of a board is the thing worth investigating.
 */
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

/** The player-board scope each admin period reads from. */
const PERIOD_SCOPES: Record<AdminBoardPeriod, LeaderboardScope> = {
  daily: 'daily',
  weekly: 'weekly',
  all_time: 'global',
};

const DAY_MS = 86_400_000;

/** Play counts per period, so a daily board does not claim a month of sessions. */
const PERIOD_PLAY_RANGES: Record<AdminBoardPeriod, { min: number; max: number }> = {
  daily: { min: 1, max: 6 },
  weekly: { min: 2, max: 26 },
  all_time: { min: 5, max: 140 },
};

/** The instant the selected period opens. Local midnight, Monday, or 90 days back. */
const periodStart = (period: AdminBoardPeriod, now: number): number => {
  const date = new Date(now);
  if (period === 'daily') {
    date.setHours(0, 0, 0, 0);
    return date.getTime();
  }
  if (period === 'weekly') {
    date.setHours(0, 0, 0, 0);
    // Monday as the week's first day, matching the player board's "resets every Monday".
    date.setDate(date.getDate() - ((date.getDay() + 6) % 7));
    return date.getTime();
  }
  return now - 90 * DAY_MS;
};

const resultFor = (roll: number): AdminLeaderboardResult => {
  if (roll < 0.7) return 'completed';
  if (roll < 0.87) return 'forfeited';
  return 'abandoned';
};

/**
 * The admin board for one filter combination, ranked by score.
 *
 * Scores, roster and trend come from the very same `getLeaderboardEntries` the player board
 * uses -- this module synthesises nothing twice -- and the extra columns are derived from the
 * same deterministic hash, keyed on the full query, so re-rendering or re-opening a filter
 * never reshuffles the metadata underneath the scores.
 *
 * Returns an empty list for a combination the UI does not offer (a game scope with no game
 * chosen, a type filter that excludes the chosen game) rather than quietly widening the
 * query: an impossible filter showing a full board would look like data nobody asked for.
 */
export const getAdminLeaderboardRows = (
  query: AdminLeaderboardQuery,
  now: number = Date.now(),
): AdminLeaderboardRow[] => {
  const { scope, period, gameType } = query;

  let gameIdForBoard: string = ALL_GAMES_FILTER;
  if (scope === 'game') {
    const game = getGameById(query.gameId);
    if (!game) return [];
    if (gameType !== 'all' && game.type !== gameType) return [];
    gameIdForBoard = game.id;
  }

  const baseEntries = getLeaderboardEntries({
    scope: PERIOD_SCOPES[period],
    gameId: gameIdForBoard,
  });

  const pool =
    scope === 'game'
      ? [getGameById(gameIdForBoard)!]
      : gameType === 'all'
        ? AVAILABLE_GAMES
        : AVAILABLE_GAMES.filter((game) => game.type === gameType);

  if (pool.length === 0) return [];

  const playsRange = PERIOD_PLAY_RANGES[period];
  const start = periodStart(period, now);
  const span = Math.max(1, now - start);

  const rows = baseEntries.map((entry) => {
    const game =
      scope === 'game'
        ? pool[0]
        : pool[Math.min(pool.length - 1, Math.floor(unitHash(`game:${period}:${gameType}:${entry.playerId}`) * pool.length))];
    const playsRoll = unitHash(`plays:${scope}:${period}:${gameIdForBoard}:${entry.playerId}`);
    const lastPlayedRoll = unitHash(`last:${scope}:${period}:${gameIdForBoard}:${entry.playerId}`);
    const resultRoll = unitHash(`result:${scope}:${period}:${gameIdForBoard}:${entry.playerId}`);

    return {
      playerId: entry.playerId,
      displayName: entry.displayName,
      monogram: entry.monogram,
      gameId: game.id,
      gameType: game.type,
      score: entry.score,
      plays:
        playsRange.min +
        Math.floor(playsRoll * (playsRange.max - playsRange.min + 1)),
      result: resultFor(resultRoll),
      lastPlayedAt: now - Math.floor(lastPlayedRoll * span),
      trendDelta: entry.trendDelta,
    };
  });

  // Ranked after any type filter has been applied, so a board narrowed to one category still
  // counts 1, 2, 3... instead of starting at whatever the combined board's first row was.
  rows.sort((a, b) => b.score - a.score || a.displayName.localeCompare(b.displayName));
  return rows.map((row, index) => ({ ...row, rank: index + 1 }));
};

export interface AdminBoardSummary {
  rows: number;
  totalPlays: number;
  topScore: number;
  averageScore: number;
  /** Sessions that did not complete, across every row on the board. */
  incompleteSessions: number;
}

export const summarizeAdminBoard = (rows: AdminLeaderboardRow[]): AdminBoardSummary => {
  if (rows.length === 0) {
    return { rows: 0, totalPlays: 0, topScore: 0, averageScore: 0, incompleteSessions: 0 };
  }

  const totalPlays = rows.reduce((sum, row) => sum + row.plays, 0);
  const totalScore = rows.reduce((sum, row) => sum + row.score, 0);

  return {
    rows: rows.length,
    totalPlays,
    topScore: rows[0].score,
    averageScore: Math.round(totalScore / rows.length),
    incompleteSessions: rows.filter((row) => row.result !== 'completed').length,
  };
};
