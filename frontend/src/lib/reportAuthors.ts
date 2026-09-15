// src/lib/reportAuthors.ts
/**
 * How a reported post's author is named on a staff screen.
 *
 * This lives apart from `ReportsReview` for a reason that is not tidiness: a module that
 * exports both a component and a plain function cannot Fast Refresh, so every edit to the page
 * during development reloads the browser and loses the tab you were on. `authorLabel` is
 * exported precisely so it can be unit-tested, and keeping it here gets both — a testable pure
 * function and a page that hot-reloads.
 *
 * The rule it encodes is the second line of defence, not the mechanism. The server never sends
 * an anonymous author at all, so there is normally no name to withhold; this matters for the
 * case where a future response field or a shape change puts one there, which is not something
 * anyone would notice by looking at the screen.
 */

/** One person, as the server names them. Fields may be null: the row is gone, or the name is
 * withheld. */
export interface Person {
  id?: string | null;
  regno?: string | null;
  name?: string | null;
}

/** The name to show where an author would go.
 *
 * `author_hidden` wins over any name in the payload, so an anonymous post stays anonymous on
 * every screen even if something upstream starts including the author.
 */
export function authorLabel(report: { author: Person | null; author_hidden: boolean }): string {
  if (report.author_hidden) return 'Anonymous to everyone, including staff';
  return partyName(report.author);
}

/** The name of anyone else in a report: the reporter, the sender of a note, the author of a
 * message. Those are all named to staff — a report is only actionable once staff can see who
 * wrote the thing being reported. */
export function partyName(person: Person | null | undefined): string {
  if (person?.name) return person.name;
  if (person?.regno) return person.regno;
  return 'Unknown';
}

/** The identifier to print beside a name, for a reviewer who needs to pull the account up.
 * A blank line in a moderation queue reads as a rendering bug. */
export function partyRef(person: Person | null | undefined): string {
  return person?.regno ?? person?.id ?? 'no identifier';
}
