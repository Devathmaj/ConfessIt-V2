import { describe, it, expect } from 'vitest';

import {
  ALL_GAMES_FILTER,
  getLeaderboardEntries,
  LEADERBOARD_SCOPES,
  summarizeCurrentUser,
} from '@/lib/gamesLeaderboard';

/**
 * The leaderboard query is the seam this whole feature hangs on: the page asks one question
 * (scope + game) and renders whatever comes back, so a backend later has to answer exactly
 * this question to drop in without a UI rewrite.
 *
 * What matters here is therefore the *contract*, not the specific mock scores -- that the
 * board is deterministic for a given query (filters must not reshuffle rows between renders),
 * that it is sorted and ranked for display, that each filter dimension actually changes the
 * answer, and that the current user can be summarised out of it for the "your rank" card.
 */

describe('gamesLeaderboard: the query the UI depends on', () => {
  it('returns the same board for the same query, so filters never reshuffle between renders', () => {
    const first = getLeaderboardEntries({ scope: 'weekly', gameId: ALL_GAMES_FILTER });
    const second = getLeaderboardEntries({ scope: 'weekly', gameId: ALL_GAMES_FILTER });

    expect(first).toEqual(second);
  });

  it('ranks the board from the top down', () => {
    const entries = getLeaderboardEntries({ scope: 'global', gameId: ALL_GAMES_FILTER });

    expect(entries.length).toBeGreaterThan(3);
    for (let index = 1; index < entries.length; index += 1) {
      expect(entries[index - 1].score).toBeGreaterThanOrEqual(entries[index].score);
    }
    expect(entries.map((entry) => entry.rank)).toEqual(
      entries.map((_, index) => index + 1),
    );
  });

  it('answers every scope, and a longer scope outscores a shorter one', () => {
    const boards = LEADERBOARD_SCOPES.map((scope) => ({
      scope: scope.id,
      entries: getLeaderboardEntries({ scope: scope.id, gameId: ALL_GAMES_FILTER }),
    }));

    boards.forEach(({ entries }) => expect(entries.length).toBeGreaterThan(0));

    const dailyTop = boards.find((board) => board.scope === 'daily')!.entries[0].score;
    const globalTop = boards.find((board) => board.scope === 'global')!.entries[0].score;
    expect(globalTop).toBeGreaterThan(dailyTop);
  });

  it('answers per-game boards differently from the combined board', () => {
    const combined = getLeaderboardEntries({ scope: 'global', gameId: ALL_GAMES_FILTER });
    const singleGame = getLeaderboardEntries({ scope: 'global', gameId: 'truth-dare' });

    expect(singleGame.length).toBe(combined.length);
    expect(singleGame.map((entry) => entry.score)).not.toEqual(
      combined.map((entry) => entry.score),
    );
    // Filtering by a game is a different board, not a slice of the combined one: the same
    // roster is re-ranked on its own scores, so the order can differ.
    expect([...singleGame.map((entry) => entry.playerId)].sort()).toEqual(
      [...combined.map((entry) => entry.playerId)].sort(),
    );
  });

  it('summarises the current user out of any board for the "your rank" card', () => {
    const entries = getLeaderboardEntries({ scope: 'daily', gameId: ALL_GAMES_FILTER });
    const summary = summarizeCurrentUser(entries);

    expect(summary).not.toBeNull();
    const row = entries.find((entry) => entry.isCurrentUser)!;
    expect(summary!.rank).toBe(row.rank);
    expect(summary!.score).toBe(row.score);
    expect(summary!.topPercent).toBeGreaterThanOrEqual(1);
    expect(summary!.topPercent).toBeLessThanOrEqual(100);
    expect(summary!.totalPlayers).toBeGreaterThanOrEqual(summary!.rank);
  });
});
