// src/lib/capabilities.ts

/**
 * The client's view of what the signed-in account may reach.
 *
 * The server sends the caller's capabilities with `/auth/me` rather than making the browser
 * re-derive them from the account's table. Two reasons: the privilege table then exists in
 * exactly one place, on the server, instead of being restated here and drifting; and a
 * capability added or moved server-side starts working in the UI without a matching release
 * of the client.
 *
 * This is a rendering concern only. Every capability below guards a screen or a menu entry
 * that the server independently refuses, so getting one of these checks wrong shows a
 * button that fails rather than exposing anything.
 *
 * An absent or unrecognised capability list is treated as *no* capabilities rather than as
 * a staff default. A response that predates the field, or a session restored from a stale
 * cache, must not open the moderation area on the strength of a field that was never there.
 */

export const CAPABILITIES = {
  CONFESSIONS_REVIEW: 'confessions:review',
  CONFESSIONS_DELETE: 'confessions:delete',
  LOVE_NOTES_REVIEW: 'love_notes:review',
  LOVE_NOTES_MODERATE: 'love_notes:moderate',
  LOVE_NOTES_DELETE: 'love_notes:delete',
  REPORTS_REVIEW: 'reports:review',
  REPORTS_MODERATE: 'reports:moderate',
  STATS_READ: 'stats:read',
  USERS_REVIEW: 'users:review',
  USERS_CREATE: 'users:create',
  USERS_UPDATE: 'users:update',
  USERS_BLOCK: 'users:block',
  MESSAGES_ARCHIVE: 'messages:archive',
  APP_CONFIG_READ: 'app_config:read',
  GAMES_MANAGE: 'games:manage',
} as const;

export type Capability = (typeof CAPABILITIES)[keyof typeof CAPABILITIES];

/**
 * The account tables, as the server names them.
 *
 * The value *is* the table name, exactly as on the server, so the discriminator a component
 * carries, the row a lookup resolved and the table a write went to are the same string. Note
 * that a participant account is `"users"` and not `"user"`: the singular is the participant
 * side of the vocabulary and the plural is the table, and guessing wrong there fails a
 * create.
 */
export const ACCOUNT_TYPES = ['users', 'admin', 'moderator'] as const;
export type AccountType = (typeof ACCOUNT_TYPES)[number];

/**
 * Narrow an arbitrary value to an account table, defaulting to `users`.
 *
 * Mirrors the server's `normalize_account_type`, including its direction: anything
 * unrecognised loses every privilege rather than gaining one. This is for *reading* a value
 * the server sent. A value a user is choosing from a form is not narrowed this way -- that
 * one is rejected server-side, so a typo is an error the operator sees rather than an
 * account quietly created as a participant.
 */
export const asAccountType = (value: unknown): AccountType =>
  (ACCOUNT_TYPES as readonly string[]).includes(value as string) ? (value as AccountType) : 'users';

/** Whether `capabilities` carries `required`. An absent list carries nothing. */
export const can = (capabilities: readonly string[] | undefined, required: Capability): boolean =>
  Array.isArray(capabilities) && capabilities.includes(required);
