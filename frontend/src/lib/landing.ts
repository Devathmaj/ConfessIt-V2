// src/lib/landing.ts

/**
 * Where a signed-in account belongs after signing in or hitting the root path.
 *
 * There are three destinations, and which one an account gets is decided by the capability
 * list the server sent rather than by the table the account lives in. A moderator and an admin
 * are both staff, so both land in the moderation area -- but not on the same page, because the
 * two hold different capabilities and the pages show different things:
 *
 * * `/admin` is the administration console, and it is gated on `stats:read`, the same
 *   capability `GET /admin/stats` requires. What that endpoint returns is a census of the
 *   platform -- how many accounts exist, how many are blocked, who is signed in -- so the
 *   console is only for an account that may read those figures.
 * * `/moderation` is the moderation hub: the backlog of the queues a moderator can rule on.
 *   A moderator lands here, and it is the only staff landing page that is theirs -- the console
 *   already lists every queue, so the hub is not also offered to an admin.
 * * `/dashboard` is the participant's own.
 *
 * It takes `can` rather than the user object so the whole app asks the capabilities question
 * the same way.
 *
 * Lives in its own module because the route guards and the staff navigation both need this
 * answer, and one of them is reachable from the pages the other imports.
 */

import { CAPABILITIES, type Capability } from './capabilities';

/**
 * What the area calls itself, for the wordmark on the staff navigation bar.
 *
 * Derived from `landingPathFor` rather than asked separately, so the name of the area and the
 * page the wordmark points at cannot disagree. That disagreement is the bug this exists to
 * prevent: a moderator who opens `/admin/reports` from a link, a bookmark or a redirect is
 * *inside* the moderation area, and a bar reading "Admin" there tells them they are somewhere
 * they are not allowed to be.
 */
const AREA_LABELS: Record<string, string> = {
  '/admin': 'Admin',
  '/moderation': 'Moderation',
  '/dashboard': 'Dashboard',
};

export const landingPathFor = (can: (capability: Capability) => boolean): string => {
  if (can(CAPABILITIES.STATS_READ)) {
    return '/admin';
  }

  // The queue is what the hub is built around, and it is the same capability the hub's own
  // route is gated on. Asking the identical question in both places is deliberate: a guard
  // that redirected somewhere this function had just declined to send the account would send
  // it straight back here, and the two would trade the account between them.
  if (can(CAPABILITIES.REPORTS_REVIEW)) {
    return '/moderation';
  }

  return '/dashboard';
};

export const landingAreaLabel = (can: (capability: Capability) => boolean): string =>
  AREA_LABELS[landingPathFor(can)] ?? 'ConfessIt';