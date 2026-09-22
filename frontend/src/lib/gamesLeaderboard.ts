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
 */

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
