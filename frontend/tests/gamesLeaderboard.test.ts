import { describe, it, expect, vi } from 'vitest';

import {
  ALL_GAMES_FILTER,
  LEAGUE_COMPETITIONS,
  LEAGUE_PERIODS,
  entryDisplayName,
  gameCompetitionType,
  gamesForCompetition,
  lastCompletedDayAnchor,
  previousPeriodAnchor,
  previousPeriodRangeLabel,
  todayAnchor,
} from '@/lib/gamesLeaderboard';

/**
 * The leaderboard vocabulary the filters and the API client speak.
 *
 * Standings themselves are server-calculated; what matters here is that
 * every catalogue game feeds exactly one competition, unavailable
 * combinations cannot be selected, and entry names resolve for both
 * subject types.
 */

describe('gamesLeaderboard: the competition vocabulary', () => {
  it('offers exactly two competitions with four UTC periods', () => {
    expect(LEAGUE_COMPETITIONS.map((option) => option.id).sort()).toEqual([
      'couple',
      'individual',
    ]);
    expect(LEAGUE_PERIODS.map((option) => option.id)).toEqual([
      'daily',
      'weekly',
      'monthly',
      'all_time',
    ]);
  });

  it('maps every catalogue game to exactly one competition', () => {
    expect(gameCompetitionType('wordle')).toBe('individual');
    expect(gameCompetitionType('anagram')).toBe('individual');
    expect(gameCompetitionType('connections')).toBe('individual');
    expect(gameCompetitionType('trivia-drop')).toBe('individual');
    expect(gameCompetitionType('reaction-time')).toBe('individual');
    expect(gameCompetitionType('couple-questionnaire')).toBe('couple');
    expect(gameCompetitionType('two-key-escape')).toBe('couple');
    expect(gameCompetitionType('no-such-game')).toBeNull();
  });

  it('splits the catalogue between the two boards without overlap', () => {
    const individual = gamesForCompetition('individual').map((game) => game.id);
    const couples = gamesForCompetition('couple').map((game) => game.id);

    expect(individual).toContain('wordle');
    expect(couples).toContain('couple-questionnaire');
    expect(couples).toContain('two-key-escape');
    expect(individual.filter((id) => couples.includes(id))).toEqual([]);
    // The combined filter is distinct from every game id.
    expect([...individual, ...couples]).not.toContain(ALL_GAMES_FILTER);
  });

  it('names individual and couple entries alike', () => {
    expect(
      entryDisplayName({
        rank: 1,
        user_id: 'u1',
        display_name: 'Ada',
        score: 87.5,
        native: {},
        is_featured: false,
        opportunities_played: 2,
      }),
    ).toBe('Ada');
    expect(
      entryDisplayName({
        rank: 1,
        member_a_id: 'a',
        member_b_id: 'b',
        member_a_name: 'Al',
        member_b_name: 'Bo',
        score: 100,
        native: {},
        is_featured: false,
        opportunities_played: 1,
      }),
    ).toBe('Al & Bo');
  });

  it('anchors boards to a UTC calendar day', () => {
    expect(todayAnchor()).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it('anchors the previous window without overflowing month ends', () => {
    expect(previousPeriodAnchor('weekly', '2026-10-08')).toBe('2026-10-01');
    expect(previousPeriodAnchor('monthly', '2026-03-31')).toBe('2026-02-01');
    expect(previousPeriodAnchor('monthly', '2026-01-15')).toBe('2025-12-01');
    // Daily already shows yesterday's result; no previous window is derived.
    expect(previousPeriodAnchor('daily', '2026-10-08')).toBe('2026-10-08');
  });

  it('labels the previous window as a closed UTC range', () => {
    expect(previousPeriodRangeLabel('weekly', '2026-10-08')).toContain('Sep');
    expect(previousPeriodRangeLabel('weekly', '2026-10-08')).toContain('Oct');
    expect(previousPeriodRangeLabel('monthly', '2026-03-31')).toContain('Feb');
    expect(previousPeriodRangeLabel('daily', '2026-10-08')).toBeNull();
  });

  it('opens daily boards on the last completed UTC day', () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date('2026-10-08T23:30:00Z'));
      expect(todayAnchor()).toBe('2026-10-08');
      expect(lastCompletedDayAnchor()).toBe('2026-10-07');

      vi.setSystemTime(new Date('2026-01-01T00:10:00Z'));
      expect(lastCompletedDayAnchor()).toBe('2025-12-31');
    } finally {
      vi.useRealTimers();
    }
  });
});
