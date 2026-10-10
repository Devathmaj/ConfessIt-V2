/**
 * Player-facing statistics for the Games dashboard.
 *
 * Every figure comes from `GET /games/summary`, composed server-side from the
 * games' native participation tables so a finished round shows up immediately
 * rather than when its league event finalizes. Nothing here invents numbers or
 * schedules: an empty history renders as an empty state, never as mock data.
 */

import type { PlayerGameSummary } from '@/services/api';

/**
 * How many different games the weekly challenge asks for.
 *
 * The challenge is derived from real activity rather than configured content:
 * variety is the point of the section, and "play three different games" is a
 * goal the server's own weekly counts can always answer.
 */
export const WEEKLY_CHALLENGE_TARGET = 3;

export interface WeeklyChallengeProgress {
  label: string;
  current: number;
  target: number;
  complete: boolean;
}

/**
 * This week's challenge, read from the player's own weekly activity.
 *
 * `null` while no summary is available (still loading, or the request
 * failed), so the caller can render an honest placeholder instead of a
 * progress bar that looks real.
 */
export const weeklyChallengeProgress = (
  stats: PlayerGameSummary | null,
): WeeklyChallengeProgress | null => {
  if (!stats) return null;
  const current = Math.min(stats.games_this_week.length, WEEKLY_CHALLENGE_TARGET);
  return {
    label: `Play ${WEEKLY_CHALLENGE_TARGET} different games this week`,
    current,
    target: WEEKLY_CHALLENGE_TARGET,
    complete: current >= WEEKLY_CHALLENGE_TARGET,
  };
};

/** "just now" / "12m ago" / "3h ago" / "2d ago", a date past a week. */
export const formatPlayedAt = (playedAt: string, now: number = Date.now()): string => {
  const at = Date.parse(playedAt);
  if (Number.isNaN(at)) return '';
  const minutes = Math.floor(Math.max(0, now - at) / 60_000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
};
