import { describe, it, expect } from 'vitest';

import {
  addDays,
  featuredDateFromKey,
  featuredDateKey,
  featuredDateKeysFrom,
  featuredGameIdFor,
  randomGameIdFrom,
  withFeaturedGame,
  withoutFeaturedGame,
} from '@/lib/gamesFeaturedSchedule';
import { AVAILABLE_GAMES, featuredGameFrom, getFeaturedGame } from '@/lib/gamesCatalog';
import {
  eligibleFeaturedGames,
  initialGamesConfig,
  isGameEnabled,
  randomPickCandidates,
  resolveFeaturedGame,
  settingValueFor,
  settingsForGame,
} from '@/lib/gamesConfig';
import {
  formatActivityTime,
  gamesAnalytics,
  seedGamesActivity,
} from '@/lib/adminGamesAnalytics';

/**
 * The configuration layer under the whole section, as plain data.
 *
 * The two properties everything else leans on: a day's pick belongs to that day alone (keyed by
 * the reader's local calendar day, resolved from one key, so yesterday expires by construction
 * rather than by an expiry check), and a fresh configuration is one nobody has touched -- every
 * title live, declared settings at their declared defaults, a schedule that already agrees with
 * the player's rotation. On top of those, the rules the admin screens promise: an explicit pin
 * beats the rotation, a pin on something switched off falls back instead of showing a title
 * players cannot open, and "every title off" answers null rather than pretending.
 *
 * Dates are passed in rather than read from the clock so none of this moves with the day the
 * suite happens to run.
 */

const AT_NOON = new Date(2026, 9, 8, 12);

describe('gamesFeaturedSchedule: the day a pick belongs to', () => {
  it('keys the local calendar day, not the UTC one', () => {
    expect(featuredDateKey(new Date(2026, 9, 8, 23, 59))).toBe('2026-10-08');

    // Built from the parts, so parsing lands on the reader's midnight rather than shifting a
    // day west of Greenwich.
    const midnight = featuredDateFromKey('2026-10-08');
    expect(midnight.getFullYear()).toBe(2026);
    expect(midnight.getMonth()).toBe(9);
    expect(midnight.getDate()).toBe(8);
    expect(midnight.getHours()).toBe(0);
  });

  it('walks forward across a month and a year boundary', () => {
    expect(featuredDateKey(addDays(new Date(2026, 9, 31), 1))).toBe('2026-11-01');
    expect(featuredDateKey(addDays(new Date(2026, 0, 1), -1))).toBe('2025-12-31');
  });

  it('lists a run of days from wherever it is asked to start', () => {
    expect(featuredDateKeysFrom(new Date(2026, 9, 8), 3)).toEqual([
      '2026-10-08',
      '2026-10-09',
      '2026-10-10',
    ]);
  });
});

describe('gamesFeaturedSchedule: the schedule as data', () => {
  it('pins a day without mutating the schedule it was given', () => {
    const schedule = withFeaturedGame({}, '2026-10-08', 'wordle');
    const next = withFeaturedGame(schedule, '2026-10-09', 'anagram');

    expect(schedule).toEqual({ '2026-10-08': 'wordle' });
    expect(featuredGameIdFor(next, '2026-10-09')).toBe('anagram');
    expect(featuredGameIdFor(schedule, '2026-10-09')).toBeNull();
    expect(featuredGameIdFor({}, '2026-10-08')).toBeNull();
  });

  it('clears exactly the day it is asked about', () => {
    const next = {
      '2026-10-08': 'wordle',
      '2026-10-09': 'anagram',
    };

    expect(Object.keys(withoutFeaturedGame(next, '2026-10-08'))).toEqual(['2026-10-09']);
    expect(next).toEqual({
      '2026-10-08': 'wordle',
      '2026-10-09': 'anagram',
    });
    // A day with no pick is already clear, so the same object comes back rather than a copy
    // that merely looks equal.
    expect(withoutFeaturedGame(next, '2026-01-01')).toBe(next);
  });

  it('draws only from the list it is given', () => {
    expect(randomGameIdFrom([])).toBeNull();
    expect(randomGameIdFrom(['a', 'b', 'c'], () => 0)).toBe('a');
    expect(randomGameIdFrom(['a', 'b', 'c'], () => 0.99)).toBe('c');
    // A single candidate clamps rather than running off the end of the list.
    expect(randomGameIdFrom(['a'], () => 0.99)).toBe('a');
  });
});

describe('gamesCatalog: the rotation a day without a pick falls back to', () => {
  it('picks the same title for the same day, from the catalogue it was handed', () => {
    const morning = featuredGameFrom(AVAILABLE_GAMES, new Date(2026, 9, 8, 6));
    const evening = featuredGameFrom(AVAILABLE_GAMES, new Date(2026, 9, 8, 23));

    expect(morning?.id).toBe(evening?.id);
    expect(AVAILABLE_GAMES.map((game) => game.id)).toContain(morning?.id);
    expect(getFeaturedGame(new Date(2026, 9, 8, 12))?.id).toBe(morning?.id);
  });

  it('lists only games with a real implementation', () => {
    expect(AVAILABLE_GAMES.map((game) => game.id).sort()).toEqual(
      [
        'anagram',
        'connections',
        'couple-questionnaire',
        'reaction-time',
        'trivia-drop',
        'two-key-escape',
        'wordle',
      ].sort(),
    );
    AVAILABLE_GAMES.forEach((game) => {
      expect(game.route).toMatch(/^\/games\//);
    });
  });
});

describe('gamesConfig: a configuration nobody has touched', () => {
  it('starts every catalogue title live, with no local-only settings', () => {
    const config = initialGamesConfig(AT_NOON);

    AVAILABLE_GAMES.forEach((game) => {
      expect(isGameEnabled(config, game.id)).toBe(true);
    });
    // No game declares local-only settings: availability and content live on the server.
    AVAILABLE_GAMES.forEach((game) => {
      expect(settingsForGame(game.id)).toEqual([]);
      expect(config.settings[game.id]).toBeUndefined();
    });

    // Two weeks of schedule, today's row included, starting from the rotation's own pick.
    const keys = Object.keys(config.featuredSchedule);
    expect(keys).toHaveLength(14);
    expect(keys).toContain(featuredDateKey(AT_NOON));
  });

  it('serves an explicit pin ahead of the rotation', () => {
    const config = initialGamesConfig(AT_NOON);
    const pinned = {
      ...config,
      featuredSchedule: withFeaturedGame(
        config.featuredSchedule,
        featuredDateKey(AT_NOON),
        'anagram',
      ),
    };

    const resolved = resolveFeaturedGame(pinned, AT_NOON);
    expect(resolved).toMatchObject({ explicit: true, dateKey: '2026-10-08' });
    expect(resolved?.game.id).toBe('anagram');
  });

  it('falls back to the rotation when the pinned title has been switched off', () => {
    const config = initialGamesConfig(AT_NOON);
    const pinned = {
      ...config,
      featuredSchedule: withFeaturedGame(
        config.featuredSchedule,
        featuredDateKey(AT_NOON),
        'anagram',
      ),
      availability: { ...config.availability, 'anagram': false },
    };

    const resolved = resolveFeaturedGame(pinned, AT_NOON);
    expect(resolved?.explicit).toBe(false);
    expect(resolved?.game.id).not.toBe('anagram');
    expect(isGameEnabled(pinned, resolved!.game.id)).toBe(true);
    expect(resolved?.game.status).toBe('available');
  });

  it('answers null when nothing at all can take the spotlight', () => {
    const config = initialGamesConfig(AT_NOON);
    const allOff = {
      ...config,
      availability: Object.fromEntries(AVAILABLE_GAMES.map((game) => [game.id, false])),
    };

    expect(resolveFeaturedGame(allOff, AT_NOON)).toBeNull();
    expect(eligibleFeaturedGames(allOff, AT_NOON)).toHaveLength(0);
    expect(randomPickCandidates(allOff)).toHaveLength(0);
  });

  it('leaves the current pick out of a random draw when there is anything else', () => {
    const config = initialGamesConfig(AT_NOON);
    const eligible = eligibleFeaturedGames(config, AT_NOON);
    const current = resolveFeaturedGame(config, AT_NOON)!.game.id;

    const others = randomPickCandidates(config, current);
    expect(others).not.toContain(current);
    expect(others).toHaveLength(eligible.length - 1);
    expect(randomPickCandidates(config)).toHaveLength(eligible.length);
  });

  it('reads a stored setting value when one exists', () => {
    const config = initialGamesConfig(AT_NOON);
    const definition = {
      key: 'rounds',
      label: 'Rounds per game',
      kind: 'number' as const,
      min: 3,
      max: 12,
      step: 1,
    };

    expect(settingValueFor(config, 'wordle', definition)).toBe(3);
    expect(
      settingValueFor(
        { ...config, settings: { ...config.settings, 'wordle': { rounds: 8 } } },
        'wordle',
        definition,
      ),
    ).toBe(8);
  });
});

describe('adminGamesAnalytics: the numbers the overview reports', () => {
  it('counts the catalogue from the configuration with empty play figures', () => {
    const config = initialGamesConfig(AT_NOON);
    const analytics = gamesAnalytics(config);

    expect(analytics.totalGames).toBe(AVAILABLE_GAMES.length);
    expect(analytics.enabledGames).toBe(AVAILABLE_GAMES.length);
    expect(analytics.disabledGames).toBe(0);
    expect(analytics.playsAllTime).toBe(0);
    expect(analytics.playsThisWeek).toBe(0);
    expect(analytics.playsToday).toBe(0);
    expect(analytics.playersToday).toBe(0);
    expect(analytics.playersThisWeek).toBe(0);

    // No telemetry means no top games and no type play.
    expect(analytics.topGames).toEqual([]);
    expect(analytics.typeBreakdown).toHaveLength(2);
    expect(analytics.typeBreakdown.every((entry) => entry.playsWeek === 0)).toBe(true);
  });

  it('follows the availability switch rather than the size of the catalogue', () => {
    const config = initialGamesConfig(AT_NOON);
    const oneOff = {
      ...config,
      availability: { ...config.availability, 'wordle': false },
    };

    const analytics = gamesAnalytics(oneOff);
    expect(analytics.enabledGames).toBe(AVAILABLE_GAMES.length - 1);
    expect(analytics.disabledGames).toBe(1);
  });

  it('seeds an activity log that is newest first and true to today’s pick', () => {
    const now = AT_NOON.getTime();
    const config = initialGamesConfig(new Date(now));
    const entries = seedGamesActivity(config, now);

    expect(entries).toHaveLength(1);
    expect([...entries].sort((a, b) => b.at - a.at)).toEqual(entries);

    const featured = resolveFeaturedGame(config, new Date(now))!;
    expect(entries[0].id).toBe('seed-featured');
    expect(entries[0].message).toBe(`${featured.game.name} set as today's featured game`);
  });

  it('ages an entry the way the overview reads it', () => {
    const now = 1_700_000_000_000;

    expect(formatActivityTime(now - 30_000, now)).toBe('just now');
    expect(formatActivityTime(now - 5 * 60_000, now)).toBe('5m ago');
    expect(formatActivityTime(now - 2 * 3_600_000, now)).toBe('2h ago');
    expect(formatActivityTime(now - 3 * 86_400_000, now)).toBe('3d ago');
    // Future stamps (a clock that ran ahead) are treated as just now rather than as a countdown.
    expect(formatActivityTime(now + 60_000, now)).toBe('just now');
    // Past a week the label stops counting and names the date instead.
    expect(formatActivityTime(now - 10 * 86_400_000, now)).not.toMatch(/ago/);
  });
});
