import { describe, it, expect } from 'vitest';

import {
  ALL_GAMES_FILTER,
  getAdminLeaderboardRows,
  getLeaderboardEntries,
  summarizeAdminBoard,
  summarizeCurrentUser,
} from '@/lib/gamesLeaderboard';

/**
 * The leaderboard queries are empty until a leaderboard backend exists:
 * the pages render their empty states rather than synthesised scores.
 */

describe('gamesLeaderboard: empty until the backend exists', () => {
  it('answers every scope with an empty board', async () => {
    const { LEADERBOARD_SCOPES } = await import('@/lib/gamesLeaderboard');
    LEADERBOARD_SCOPES.forEach((scope) => {
      expect(
        getLeaderboardEntries({ scope: scope.id, gameId: ALL_GAMES_FILTER }),
      ).toEqual([]);
    });
  });

  it('answers per-game boards empty as well', () => {
    expect(getLeaderboardEntries({ scope: 'global', gameId: 'wordle' })).toEqual([]);
  });

  it('has no current user to summarise on an empty board', () => {
    expect(summarizeCurrentUser([])).toBeNull();
  });

  it('answers the admin board empty with a zero summary', () => {
    const rows = getAdminLeaderboardRows({
      scope: 'global',
      period: 'weekly',
      gameId: ALL_GAMES_FILTER,
      gameType: 'all',
    });
    expect(rows).toEqual([]);
    expect(summarizeAdminBoard(rows)).toEqual({
      rows: 0,
      totalPlays: 0,
      topScore: 0,
      averageScore: 0,
      incompleteSessions: 0,
    });
  });
});
