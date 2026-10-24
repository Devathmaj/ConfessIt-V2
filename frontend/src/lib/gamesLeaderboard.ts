/**
 * League leaderboard vocabulary shared by the Games section.
 *
 * Standings themselves live on the server (`GET /leaderboards/...`), which
 * reads the persisted award ledger: this module only names the competition
 * types, periods and game mapping the filters and the API client speak.
 * Individual games rank users; couple games rank canonical pairs; the two
 * never mix.
 */

import { AVAILABLE_GAMES } from './gamesCatalog';
import type {
  LeagueCompetitionType,
  LeagueCoupleEntry,
  LeagueIndividualEntry,
  LeaguePeriod,
} from '@/services/api';

export type { LeagueCompetitionType, LeagueCoupleEntry, LeagueIndividualEntry, LeaguePeriod };

/** The combined board across every game of one competition type. */
export const ALL_GAMES_FILTER = 'all';

export interface LeaguePeriodOption {
  id: LeaguePeriod;
  label: string;
  /** Short hint shown under the scope title or in a tooltip-style subtitle. */
  hint: string;
}

export const LEAGUE_PERIODS: LeaguePeriodOption[] = [
  { id: 'daily', label: 'Daily', hint: 'Latest completed UTC day' },
  { id: 'weekly', label: 'Weekly', hint: 'Monday to Sunday, UTC' },
  { id: 'monthly', label: 'Monthly', hint: 'One UTC month' },
  { id: 'all_time', label: 'All time', hint: 'Accumulated weekly scores' },
];

export interface LeagueCompetitionOption {
  id: LeagueCompetitionType;
  label: string;
  hint: string;
}

export const LEAGUE_COMPETITIONS: LeagueCompetitionOption[] = [
  { id: 'individual', label: 'Individual', hint: 'One user per entry' },
  { id: 'couple', label: 'Couples', hint: 'One pair per entry' },
];

/** Which leaderboard a game feeds, or null for an uncatalogued id. */
export const gameCompetitionType = (gameId: string): LeagueCompetitionType | null => {
  const game = AVAILABLE_GAMES.find((entry) => entry.id === gameId);
  if (!game) return null;
  return game.type === 'couple' ? 'couple' : 'individual';
};

/** Catalogue games feeding one competition type's boards. */
export const gamesForCompetition = (
  competitionType: LeagueCompetitionType,
): typeof AVAILABLE_GAMES =>
  AVAILABLE_GAMES.filter(
    (game) => game.status === 'available' && gameCompetitionType(game.id) === competitionType,
  );

/** Display names for one board entry, whatever its subject type. */
export const entryDisplayName = (
  entry: LeagueIndividualEntry | LeagueCoupleEntry,
): string =>
  'display_name' in entry ? entry.display_name : `${entry.member_a_name} & ${entry.member_b_name}`;

/** Today as UTC `YYYY-MM-DD`, the only calendar the boards speak. */
export const todayAnchor = (): string => new Date().toISOString().slice(0, 10);

const anchorFromKey = (anchor: string): Date => {
  const [year, month, day] = anchor.split('-').map(Number);
  return new Date(Date.UTC(year, (month ?? 1) - 1, day ?? 1));
};

const anchorFromDate = (date: Date): string => date.toISOString().slice(0, 10);

const utcDateLabel = (date: Date): string =>
  date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' });

/**
 * An anchor inside the calendar window immediately before the one holding
 * `anchor`: last week (-7 days) for weekly, last month (first day) for monthly.
 *
 * Weekly and monthly boards are windows, so the previous window is a settled
 * final table once the next one opens. Returning an anchor (not bounds) keeps
 * the request on the same calendar rules the server applies.
 */
export const previousPeriodAnchor = (period: LeaguePeriod, anchor: string): string => {
  const day = anchorFromKey(anchor);
  if (period === 'weekly') {
    day.setUTCDate(day.getUTCDate() - 7);
  } else if (period === 'monthly') {
    // Move to the 1st first: subtracting a month from the 31st would overflow
    // into the current month in the Date API.
    day.setUTCDate(1);
    day.setUTCMonth(day.getUTCMonth() - 1);
  }
  return anchorFromDate(day);
};

/** Human-readable UTC range of the window before `anchor`, e.g. "Oct 12 – Oct 18". */
export const previousPeriodRangeLabel = (
  period: LeaguePeriod,
  anchor: string,
): string | null => {
  const day = anchorFromKey(anchor);
  if (period === 'weekly') {
    const monday = new Date(day);
    monday.setUTCDate(monday.getUTCDate() - ((monday.getUTCDay() + 6) % 7));
    const start = new Date(monday);
    start.setUTCDate(start.getUTCDate() - 7);
    const end = new Date(start);
    end.setUTCDate(end.getUTCDate() + 6);
    return `${utcDateLabel(start)} – ${utcDateLabel(end)}`;
  }
  if (period === 'monthly') {
    const start = new Date(Date.UTC(day.getUTCFullYear(), day.getUTCMonth() - 1, 1));
    const end = new Date(Date.UTC(day.getUTCFullYear(), day.getUTCMonth(), 0));
    return `${utcDateLabel(start)} – ${utcDateLabel(end)}`;
  }
  return null;
};

/**
 * Yesterday as UTC `YYYY-MM-DD`: the newest day a daily board can score.
 *
 * A session's awards are written when it closes, at the reset boundary that
 * starts the next day. A daily board anchored to today is therefore empty by
 * construction, so the UI opens daily boards on the last completed day.
 */
export const lastCompletedDayAnchor = (): string => {
  const day = new Date();
  day.setUTCDate(day.getUTCDate() - 1);
  return day.toISOString().slice(0, 10);
};
