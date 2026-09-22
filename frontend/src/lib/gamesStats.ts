/**
 * Player-facing statistics for the Games dashboard.
 *
 * Prototype data, kept in the shape a future endpoint would plausibly return so the dashboard
 * only needs its data source replaced, not its layout. Nothing here is derived from real
 * games -- there are no games to derive it from yet.
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
  /** Human relative time, e.g. "2h ago" -- mock data does not keep real timestamps. */
  playedAt: string;
  score: number;
  personalBest?: boolean;
}

export const MOCK_PLAYER_STATS: PlayerStats = {
  points: 12_480,
  rank: 42,
  topPercent: 8,
  currentStreakDays: 6,
  longestStreakDays: 14,
  gamesPlayed: 87,
  gamesPlayedThisWeek: 9,
  winRate: 64,
};

export const MOCK_WEEKLY_CHALLENGE: WeeklyChallenge = {
  label: 'Play 5 games this week',
  current: 3,
  target: 5,
  unit: 'games',
  reward: '+250 bonus points',
};

export const MOCK_RECENTLY_PLAYED: RecentPlay[] = [
  { gameId: 'hearts-memory-match', playedAt: '2h ago', score: 480, personalBest: true },
  { gameId: 'truth-dare', playedAt: 'Yesterday', score: 320 },
  { gameId: 'would-you-rather', playedAt: '2 days ago', score: 210 },
  { gameId: 'compatibility-quiz', playedAt: '4 days ago', score: 275 },
];
