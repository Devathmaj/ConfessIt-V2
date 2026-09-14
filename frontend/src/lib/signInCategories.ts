// src/lib/signInCategories.ts

/**
 * The three sign-in doors, one per account table.
 *
 * These exist because a regno is unique only *within* an account table. The same registration
 * number can exist as a participant and as staff at the same time, and when it does there is
 * nothing in the regno itself that says which one a person meant. The page they came in
 * through is that answer, so each category posts to the endpoint for its own table and the
 * server never has to guess: `/auth/login/<table>/magic` reads exactly one table, and a regno
 * that is not in that table is simply "no account here" rather than somebody else's account.
 *
 * Enforced on the server, not here. Nothing in this file grants access -- the endpoint does
 * the lookup and the token pins the row it found. These pages are the honest expression of
 * that, and their presence is not what makes the categories separate.
 *
 * The doors do not link to each other. A participant page that advertised the staff doors
 * would be telling every student where the moderation console is, for no benefit: staff are
 * given the URL directly, and hiding it costs nothing because the endpoint never searched
 * across tables in the first place.
 */

import { ACCOUNT_TYPES, type AccountType } from './capabilities';

const API_URL = 'http://localhost:8001';

export interface SignInCategory {
  /** The account table this door signs in to. Also the path segment the API takes. */
  readonly accountType: AccountType;
  /** Where this door lives, so the router can mount it. */
  readonly path: string;
  /** Heading, so a person can tell at a glance which door they are at. */
  readonly title: string;
  /** One line on what this account is. */
  readonly tagline: string;
  /** Submit button label. */
  readonly cta: string;
  /** Submit button label while the request is in flight. */
  readonly pendingCta: string;
  /** Placeholder on the regno field. */
  readonly regnoPlaceholder: string;
  /**
   * Whether this door gets the participant treatment: the looping background video, the
   * floating hearts, the accent-coloured button. Staff sign-in is deliberately plain, so that
   * landing on a moderation door looks nothing like landing on the participant one.
   */
  readonly themed: boolean;
}

export const SIGN_IN_CATEGORIES = {
  users: {
    accountType: 'users',
    path: '/login',
    title: 'ConfessIt',
    tagline: "Share your heart in our Valentine's Day celebration 💕",
    cta: 'Enter ConfessIt 💕',
    pendingCta: 'Generating Link...',
    regnoPlaceholder: 'Registration Number',
    themed: true,
  },
  admin: {
    accountType: 'admin',
    path: '/login/admin',
    title: 'Admin sign-in',
    tagline: 'Administrator accounts. Moderation and platform administration.',
    cta: 'Sign in as administrator',
    pendingCta: 'Generating Link...',
    regnoPlaceholder: 'Administrator registration number',
    themed: false,
  },
  moderator: {
    accountType: 'moderator',
    path: '/login/moderator',
    title: 'Moderator sign-in',
    tagline: 'Moderator accounts. Content and conversation review.',
    cta: 'Sign in as moderator',
    pendingCta: 'Generating Link...',
    regnoPlaceholder: 'Moderator registration number',
    themed: false,
  },
} as const satisfies Record<AccountType, SignInCategory>;

export type SignInCategoryKey = keyof typeof SIGN_IN_CATEGORIES;

/**
 * The endpoint for one door. The account table is in the path rather than the body so that
 * which table is being asked about is decided by the URL the browser called, not by a field
 * the page could get wrong.
 */
export const magicLinkEndpoint = (accountType: AccountType): string =>
  `${API_URL}/auth/login/${accountType}/magic`;

/**
 * The door an account should be sent back to when its session ends.
 *
 * A staff account that has just been logged out -- deliberately, or by expiry -- needs to come
 * back to the door it came in through. Sending a moderator to the participant form would
 * answer a valid moderator regno with "no participant account with that registration number",
 * which is both wrong and confusing: the account is real, it is simply not a participant.
 *
 * An unrecognised or absent value returns the participant door. That is a routing decision,
 * not a privilege one: the worst a wrong guess does is show the wrong form, and the endpoint
 * behind it still reads only its own table, so the guess cannot become access.
 */
export const signInPathFor = (accountType: unknown): string =>
  (ACCOUNT_TYPES as readonly string[]).includes(accountType as string)
    ? SIGN_IN_CATEGORIES[accountType as AccountType].path
    : SIGN_IN_CATEGORIES.users.path;
