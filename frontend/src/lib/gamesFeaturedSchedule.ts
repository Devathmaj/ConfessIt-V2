/**
 * The featured-game schedule: which title owns the spotlight on which day.
 *
 * One day, one game. A date's entry is the whole of that day's configuration -- resolving
 * "who is featured today" reads only today's key, so yesterday's pick expires by construction
 * rather than by an expiry check somewhere, and tomorrow's pick cannot leak backwards. A day
 * with no entry of its own is left to the caller's fallback (the catalogue's day rotation),
 * which is what "not scheduled" means.
 *
 * Pure date and schedule arithmetic, deliberately: no catalogue lookups, no availability rules
 * and no React. Callers decide which games may be scheduled (`gamesConfig` composes this with
 * the catalogue) and the context decides when a change was made. That keeps this module
 * testable as plain data and keeps a future backend swap to one place -- persist this map,
 * keep every function signature.
 *
 * Dates are the reader's local calendar day, keyed `YYYY-MM-DD`. The prototype runs in one
 * browser; a server-backed version would store the same key and agree with the client on the
 * day the pick applies to.
 */

/** A local calendar day as `YYYY-MM-DD`, e.g. `"2026-10-07"`. */
export type FeaturedDateKey = string;

/** dateKey -> gameId. An absent key means that day has no explicit pick. */
export interface FeaturedSchedule {
  [dateKey: FeaturedDateKey]: string;
}

const pad2 = (value: number): string => String(value).padStart(2, '0');

export const featuredDateKey = (date: Date): FeaturedDateKey =>
  `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;

/**
 * Parses a key back into a local Date at midnight.
 *
 * Built from the parts rather than `new Date('YYYY-MM-DD')`, which the spec reads as UTC and
 * would land on the previous local day west of Greenwich -- quietly mislabelling every row in
 * the schedule.
 */
export const featuredDateFromKey = (key: FeaturedDateKey): Date => {
  const [year, month, day] = key.split('-').map(Number);
  return new Date(year, (month ?? 1) - 1, day ?? 1);
};

export const addDays = (date: Date, days: number): Date => {
  const next = new Date(date);
  next.setDate(next.getDate() + days);
  return next;
};

/** The keys for `count` days starting at `from`, inclusive. */
export const featuredDateKeysFrom = (from: Date, count: number): FeaturedDateKey[] =>
  Array.from({ length: count }, (_, index) => featuredDateKey(addDays(from, index)));

/** "Oct 8" / "8 Oct" style label for a schedule row. Local day, no time involved. */
export const featuredDateLabel = (key: FeaturedDateKey): string => {
  const date = featuredDateFromKey(key);
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
};

/** Full label for a schedule row, e.g. "Wednesday, October 8". */
export const featuredDateLongLabel = (key: FeaturedDateKey): string => {
  const date = featuredDateFromKey(key);
  return date.toLocaleDateString(undefined, {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
  });
};

export const featuredGameIdFor = (
  schedule: FeaturedSchedule,
  dateKey: FeaturedDateKey,
): string | null => schedule[dateKey] ?? null;

export const withFeaturedGame = (
  schedule: FeaturedSchedule,
  dateKey: FeaturedDateKey,
  gameId: string,
): FeaturedSchedule => ({ ...schedule, [dateKey]: gameId });

export const withoutFeaturedGame = (
  schedule: FeaturedSchedule,
  dateKey: FeaturedDateKey,
): FeaturedSchedule => {
  if (!(dateKey in schedule)) return schedule;
  const next = { ...schedule };
  delete next[dateKey];
  return next;
};

/**
 * Seeds `days` of picks by rotating through `gameIds` from `from`.
 *
 * The seed exists so a fresh install has something on every row of the schedule instead of a
 * page of "auto rotation", and because the catalogue's own day rotation is the behaviour the
 * prototype shipped with -- starting today's slot at the same pick keeps the admin view and
 * the player's `/games/featured` agreeing from the first frame.
 */
export const rotateFeaturedSchedule = (
  gameIds: readonly string[],
  from: Date,
  days: number,
  offset = 0,
): FeaturedSchedule => {
  if (gameIds.length === 0) return {};

  const schedule: FeaturedSchedule = {};
  for (let index = 0; index < days; index += 1) {
    const dateKey = featuredDateKey(addDays(from, index));
    schedule[dateKey] = gameIds[(index + offset) % gameIds.length];
  }
  return schedule;
};

/**
 * A uniformly random member of `gameIds`, or null when there is nothing to choose from.
 *
 * Takes the list rather than reaching for the catalogue so eligibility stays the caller's
 * decision: "random featured game" must never mean "random game, including the ones nobody
 * can play today".
 */
export const randomGameIdFrom = (
  gameIds: readonly string[],
  random: () => number = Math.random,
): string | null => {
  if (gameIds.length === 0) return null;
  const index = Math.min(gameIds.length - 1, Math.floor(random() * gameIds.length));
  return gameIds[index];
};
