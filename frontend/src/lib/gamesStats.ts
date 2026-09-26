/**
 * Player-facing statistics for the Games dashboard.
 *
 * There is no stats endpoint, so every figure below is empty until one
 * exists: the dashboard renders its empty states rather than mock numbers.
 */

export interface PlayerStats {
  /** Lifetime points, the number the leaderboard ranks on. */
  points: number;
  /** 1-based position on the global leaderboard. */
  rank: number;
  /** Share of the leaderboard the player sits above, used for the "Top N%" label. */
  topPercent: number;
  /** Consecutive days with at least one game played. */
  currentStreakDays: number;
  longestStreakDays: number;
  gamesPlayed: number;
  gamesPlayedThisWeek: number;
  /** Percentage, 0–100. */
  winRate: number;
}

export interface WeeklyChallenge {
  label: string;
  current: number;
  target: number;
  unit: string;
  /** Shown once the challenge is finished; absent while it is still in progress. */
  reward?: string;
}

/** A single row in "recently played". The game itself is resolved from the catalogue. */
export interface RecentPlay {
  gameId: string;
  /** Human relative time, e.g. "2h ago". */
  playedAt: string;
  score: number;
  personalBest?: boolean;
}

/** Empty until a stats endpoint exists. */
export const MOCK_PLAYER_STATS: PlayerStats = {
  points: 0,
  rank: 0,
  topPercent: 0,
  currentStreakDays: 0,
  longestStreakDays: 0,
  gamesPlayed: 0,
  gamesPlayedThisWeek: 0,
  winRate: 0,
};

/** Empty until challenges exist on the backend. */
export const MOCK_WEEKLY_CHALLENGE: WeeklyChallenge = {
  label: 'No weekly challenge right now',
  current: 0,
  target: 0,
  unit: 'games',
};

/** Empty until play history exists on the backend. */
export const MOCK_RECENTLY_PLAYED: RecentPlay[] = [];
