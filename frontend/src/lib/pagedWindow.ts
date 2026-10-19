/**
 * A window of rows read by offset, and the two ways that goes wrong.
 *
 * Paging is by offset, and an offset only means something against a list that has
 * not moved. A list that has moved shifts the window under the reader:
 *
 * * a row inserted above the window shifts it down, so the next page starts one
 *   row too early and hands back a row that is already on screen -- the same id
 *   twice, which React objects to as a duplicate key;
 * * a row removed above the window shifts it up, so the next page starts one row
 *   too late, and a row the reader has not seen is never handed to them at all.
 *
 * The first is visible and the second is not, which is why the total is watched
 * as well as the ids: a corpus that shrank lost a row from somewhere, and if it
 * lost one from above the window then a row below it has just been stepped over.
 *
 * Neither is fixed by de-duplication alone. Dropping the row that arrived twice
 * stops it being shown twice, but the offset is still counting a list that has
 * moved, so every page after it reads one row too early again. The window is
 * therefore re-read when it has drifted, which keeps the reader's place and puts
 * the rows back in the order the server ranks them.
 */

/**
 * Whether the page just read lines up with the window already on screen.
 *
 * A row already on screen is proof the window moved: an offset means nothing
 * against a list that shifted under it. A total that shrank is proof of the
 * other direction -- a row was removed from somewhere, and if it was removed from
 * above the window then a row below it has just been stepped over.
 */
export const windowHasDrifted = <T extends { id: string }>(
  held: T[],
  incoming: T[],
  totalBefore: number,
  totalAfter: number
): boolean => {
  if (totalAfter < totalBefore) return true;
  const known = new Set(held.map((row) => row.id));
  return incoming.some((row) => known.has(row.id));
};

/**
 * The rows of a page that are not on screen already.
 *
 * The list is state and a state update only becomes visible after the next
 * render, so the ids are read from the list as it stands rather than from a copy
 * the render will refresh later.
 */
export const appendUnseen = <T extends { id: string }>(held: T[], incoming: T[]): T[] => {
  const known = new Set(held.map((row) => row.id));
  const fresh = incoming.filter((row) => !known.has(row.id));
  return fresh.length ? [...held, ...fresh] : held;
};

/**
 * Re-read a window the server has moved under the reader.
 *
 * The rows come back in the order the server ranks them, so the reader's place is
 * kept rather than dropped back at the top. The server caps a page, and the
 * reader may have paged past that cap, so the window is walked in page-sized
 * reads rather than asked for in one.
 */
export const rereadWindow = async <T>(
  rows: number,
  pageSize: number,
  fetchPage: (skip: number, limit: number) => Promise<{ rows: T[]; total: number }>
): Promise<{ rows: T[]; total: number }> => {
  const collected: T[] = [];
  let total = 0;
  for (let skip = 0; skip < rows; skip += pageSize) {
    const page = await fetchPage(skip, pageSize);
    total = page.total;
    collected.push(...page.rows);
    if (collected.length >= rows || page.rows.length < pageSize) break;
  }
  return { rows: collected.slice(0, rows), total };
};
