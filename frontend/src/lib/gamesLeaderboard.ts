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
