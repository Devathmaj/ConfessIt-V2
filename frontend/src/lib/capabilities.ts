// src/lib/capabilities.ts

/**
 * The client's view of what the signed-in account may reach.
 *
 * The server sends the caller's capabilities with `/auth/me` rather than making the browser
 * re-derive them from a role name. Two reasons: the privilege table then exists in exactly
 * one place, on the server, instead of being restated here and drifting; and a role added
 * or moved server-side starts working in the UI without a matching release of the client.
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
  CONVERSATIONS_REVIEW: 'conversations:review',
  CONVERSATIONS_READ_MESSAGES: 'conversations:read_messages',
  CONVERSATIONS_TERMINATE: 'conversations:terminate',
  MATCHMAKING_REVIEW: 'matchmaking:review',
  MATCHMAKING_MODERATE: 'matchmaking:moderate',
  REPORTS_REVIEW: 'reports:review',
  REPORTS_MODERATE: 'reports:moderate',
  STATS_READ: 'stats:read',
  USERS_REVIEW: 'users:review',
  USERS_CREATE: 'users:create',
  USERS_UPDATE: 'users:update',
  USERS_BLOCK: 'users:block',
  SESSIONS_READ: 'sessions:read',
  MESSAGES_ARCHIVE: 'messages:archive',
} as const;

export type Capability = (typeof CAPABILITIES)[keyof typeof CAPABILITIES];

export const ALL_ROLES = ['user', 'admin', 'moderator'] as const;
export type Role = (typeof ALL_ROLES)[number];

/**
 * Narrow an arbitrary value to a role, defaulting to `user`.
 *
 * Mirrors the server: anything unrecognised loses every privilege rather than gaining one.
 */
export const asRole = (value: unknown): Role =>
  (ALL_ROLES as readonly string[]).includes(value as string) ? (value as Role) : 'user';

/** Whether `capabilities` carries `required`. An absent list carries nothing. */
export const can = (capabilities: readonly string[] | undefined, required: Capability): boolean =>
  Array.isArray(capabilities) && capabilities.includes(required);
