/**
 * Where a notification sends you when you click it.
 *
 * The server decides *whether* a notification is clickable by sending a `target_kind` /
 * `target_id` pair, or by sending neither. This module is the other half: it turns that pair
 * into the in-app path that lands on the item. Nothing here decides clickability -- it only
 * translates, and returns null when there is nothing to translate.
 *
 * Both halves live apart from the page for the same reason `lib/reportAuthors.ts` does: a
 * module exporting both a component and a plain function cannot Fast Refresh, and keeping the
 * rules here means editing InboxPage during development does not reload the browser and lose
 * the tab you were on. It also makes the rules unit-testable without rendering a page.
 *
 * An unknown kind returns null rather than a guessed URL. A notification must never look
 * clickable and then take the reader somewhere that does not contain what it announced, and
 * the server refusing to send an unknown kind (`NOTIFICATION_TARGETS`) plus this returning
 * null for one means a kind added on one side without the other degrades to "not clickable"
 * instead of "clickable and wrong".
 */

/**
 * Every target kind this client knows how to open.
 *
 * Kept as a map rather than a switch so the kind -> path pairing is one line per kind and a
 * new one cannot be half-added: the kind is either here and has a path, or is absent and is
 * treated as unknown.
 *
 * `conversation` takes a **match** id, not a conversation id. Every conversation route in
 * the API is keyed by match, so that is the id the target pages need.
 *
 * The two routes carry confusingly similar names and are not interchangeable: `/inbox` is the
 * Message Box (conversations) and `/notifications` is this notifications list.
 */
const TARGET_PATHS: Record<string, (targetId: string) => string> = {
  conversation: (matchId) => `/inbox?match=${encodeURIComponent(matchId)}`,
  love_note: (noteId) => `/love-notes?tab=inbox&note=${encodeURIComponent(noteId)}`,
};

/**
 * The in-app path a notification opens, or null when it opens nothing.
 *
 * Null is the normal case for a large share of notifications -- a match that was drawn, a
 * report that was reviewed, content that was taken down -- and each of those has no item on any
 * page to point at. `is_read` is unused here and deliberately not part of the signature: a
 * read notification is still clickable, and unread-ness is a rendering concern, not a routing
 * one.
 */
export const notificationTargetPath = (
  targetKind: string | null | undefined,
  targetId: string | null | undefined
): string | null => {
  const build = targetKind ? TARGET_PATHS[targetKind] : undefined;
  if (!build || !targetId) return null;
  return build(targetId);
};

/**
 * Whether a notification with this target should be rendered as clickable.
 *
 * Separate from `notificationTargetPath` so the cursor, the focus ring and the accessible name
 * are all decided by the same question the click handler asks -- one predicate, rather than a
 * `if (path)` repeated in three places that can drift.
 */
export const notificationIsClickable = (
  targetKind: string | null | undefined,
  targetId: string | null | undefined
): boolean => notificationTargetPath(targetKind, targetId) !== null;