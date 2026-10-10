/**
 * Navigation memory for the Games section's escape hatches.
 *
 * Two questions need answering once a person is inside Games:
 *
 * - "Back to ConfessIt" should return them to the last main-app page they had
 *   open, not a hard-coded dashboard.
 * - A game screen's own back control should return them to the last Games page
 *   they had open (the selection grid, the spotlight, the dashboard) -- never to
 *   another game.
 *
 * Both are recorded in `sessionStorage` as the person navigates: per tab, able to
 * survive a refresh, never shared with another tab and never outliving the
 * browsing session. Auth/door screens and the root redirect are ignored, as are
 * game screens for the "last Games page" half (going "back" into a game would be
 * a loop). With nothing remembered, the fallbacks are the dashboard and the
 * Games dashboard respectively, so a deep link still has a way home.
 */

import { AVAILABLE_GAMES } from './gamesCatalog';

const CONFESSIT_KEY = 'confessit:last-page';
const GAMES_KEY = 'confessit:last-games-page';

/** Where "Back to ConfessIt" goes when this tab has no remembered page yet. */
export const DEFAULT_CONFESSIT_PAGE = '/dashboard';

/** Where a game's back control goes when this tab has no remembered Games page. */
export const DEFAULT_GAMES_PAGE = '/games';

const PLAY_ROUTES = new Set(AVAILABLE_GAMES.map((game) => game.route));

/** Whether a path belongs to the Games section (including its nested screens). */
const isGamesPath = (pathname: string): boolean =>
  pathname === '/games' || pathname.startsWith('/games/');

/** Whether a path is a playable game's screen rather than a section page. */
export const isPlayRoute = (pathname: string): boolean => PLAY_ROUTES.has(pathname);

/**
 * Whether a path is a transient auth/door screen that should never be remembered:
 * those routes either replace themselves on sign-in or bounce an authenticated
 * visitor straight out, so they are not a page anyone can be sent "back" to.
 */
const isTransientPath = (pathname: string): boolean =>
  pathname === '/' ||
  pathname === '/verify-login' ||
  pathname === '/mini-games' ||
  pathname.startsWith('/login');

const store = (key: string, value: string): void => {
  try {
    window.sessionStorage.setItem(key, value);
  } catch {
    // Storage can be blocked (private mode, policy): the fallback still applies.
  }
};

/**
 * A stored in-app path, or the fallback.
 *
 * Query strings and hashes are kept, so a remembered filtered page (for example
 * `/confessions?sort=recent`) comes back as it was. Only same-origin, in-app
 * paths are accepted: the value is used as a router destination, so a tampered
 * storage entry must never become an off-site link.
 */
const read = (key: string, fallback: string): string => {
  try {
    const stored = window.sessionStorage.getItem(key);
    if (stored && stored.startsWith('/') && !stored.startsWith('//')) return stored;
  } catch {
    // Fall through to the fallback when storage is unavailable.
  }
  return fallback;
};

/**
 * Record a visited page into whichever memory it belongs to. Games screens are
 * skipped by the "Back to ConfessIt" memory; playable game screens are skipped by
 * the Games memory as well. Called on every navigation by the app shell.
 */
export const rememberVisitedPage = (
  pathname: string,
  search: string = '',
  hash: string = '',
): void => {
  const target = `${pathname}${search}${hash}`;
  if (isGamesPath(pathname)) {
    if (!isPlayRoute(pathname)) store(GAMES_KEY, target);
    return;
  }
  if (isTransientPath(pathname)) return;
  store(CONFESSIT_KEY, target);
};

/** The main-app page "Back to ConfessIt" returns to. */
export const lastConfessItPage = (): string => read(CONFESSIT_KEY, DEFAULT_CONFESSIT_PAGE);

/** The Games page a game screen's back control returns to. */
export const lastGamesPage = (): string => read(GAMES_KEY, DEFAULT_GAMES_PAGE);
