import { cohortKey, isSameCohort } from './accountFields';

/**
 * Turning a screen of slot boxes into rows, and the rows back into boxes.
 *
 * A box is the unit the operator builds and reasons about: dropping A, B and C into one box says
 * "these three may match each other, and each may match within itself". The database stores pairs,
 * because the matchmaking rule is stated on pairs and an indexable pair of columns is the only
 * shape that rule can be asked against cheaply. So a box is *expanded* into the pairs it permits on
 * save, and *reconstructed* from those pairs on load.
 *
 * That round trip is lossy unless the rows remember which box they came from, and for a while they
 * did not. `group_key` is the fix: every pair written from one box carries that box's key, so the
 * grouping is stored rather than guessed. It used to be guessed by keying on each pair's *first*
 * cohort, which is wrong in both directions at once -- it split one box apart, because B-C's first
 * cohort is B and so started a box of its own, and it merged unrelated boxes that happened to share
 * a first cohort. A box of three came back as a box of three plus a stray box of two.
 *
 * The pairing rule never noticed, because it reads pairs and not boxes: the edges were always
 * right. What broke was the save that follows a load. The editor deletes the old configuration
 * before writing the new one, so the duplicate pairs the mangled boxes produced were answered with
 * a 409 *after* the delete had already happened, leaving the operator with a truncated
 * configuration and an error.
 *
 * `group_key` changes nothing about what may match, and nothing in this module should imply
 * otherwise. Two boxes naming the same pair permit exactly what one of them would, which is why
 * `expandSlotBoxes` emits both and the server accepts both.
 */

/** A `(year, department)` pair, as the palette holds it and the API accepts it. */
export interface Cohort {
  year: number;
  department: string;
}

/**
 * One editor slot box.
 *
 * `groupKey` is always a string. Rows written before the column existed arrive without one and are
 * given a fresh key here, so that saving a legacy configuration upgrades it to grouped rows rather
 * than leaving a mixture the next load has to special-case again.
 */
export interface SlotBox {
  groupKey: string;
  cohorts: Cohort[];
}

/** A pair row as `GET /admin/config` returns it. Only the fields this module reads are named. */
export interface SlotPairRow {
  year_a: number;
  department_a: string;
  year_b: number;
  department_b: string;
  group_key?: string | null;
}

/** A pair to POST, with the box it belongs to. */
export interface ExpandedSlotPair {
  year_a: number;
  department_a: string;
  year_b: number;
  department_b: string;
  group_key: string;
}

/**
 * A key for a new box.
 *
 * `randomUUID` needs a secure context, which a plain-HTTP admin page may not be, so the fallback
 * builds a v4-shaped key from `getRandomValues` instead. A key only has to be unique among the boxes
 * on one screen and stable across a save, so this is not security-sensitive -- but it should not be
 * a counter, because a counter restarts at one on every reload and would collide with a key the
 * server has already issued.
 */
export function newSlotGroupKey(): string {
  const cryptoApi = globalThis.crypto;
  if (typeof cryptoApi?.randomUUID === 'function') return cryptoApi.randomUUID();
  const bytes = new Uint8Array(16);
  if (typeof cryptoApi?.getRandomValues === 'function') {
    cryptoApi.getRandomValues(bytes);
  } else {
    for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function dedupeCohorts(cohorts: Cohort[]): Cohort[] {
  const seen: Cohort[] = [];
  for (const cohort of cohorts) {
    if (!seen.some((existing) => isSameCohort(existing, cohort))) seen.push(cohort);
  }
  return seen;
}

/**
 * Every pair the given boxes permit, each tagged with the box it came from.
 *
 * A box of N cohorts emits the N(N-1)/2 distinct pairs. A box of one emits the single self-pair,
 * because a box holding one cohort has to be *stored* as something or it would not survive the
 * save -- there is no pair inside one cohort that is not that cohort with itself. That row is not
 * what permits self-matching (any row naming the cohort does, see
 * `matchmaking_slot_service.candidate_shares_a_slot_predicate`); it is here so the box can be read
 * back.
 *
 * Empty boxes emit nothing rather than being skipped silently elsewhere. A box with nothing in it
 * describes no pairing, so dropping it loses no configuration -- but it does mean the operator's
 * in-progress box disappears on reload, which is why "+" boxes start empty and are expected to be
 * filled before saving.
 */
export function expandSlotBoxes(boxes: SlotBox[]): ExpandedSlotPair[] {
  const pairs: ExpandedSlotPair[] = [];

  for (const box of boxes) {
    const cohorts = dedupeCohorts(box.cohorts);
    if (cohorts.length === 0) continue;

    if (cohorts.length === 1) {
      const only = cohorts[0];
      pairs.push({
        year_a: only.year,
        department_a: only.department,
        year_b: only.year,
        department_b: only.department,
        group_key: box.groupKey,
      });
      continue;
    }

    for (let i = 0; i < cohorts.length; i++) {
      for (let j = i + 1; j < cohorts.length; j++) {
        pairs.push({
          year_a: cohorts[i].year,
          department_a: cohorts[i].department,
          year_b: cohorts[j].year,
          department_b: cohorts[j].department,
          group_key: box.groupKey,
        });
      }
    }
  }

  return pairs;
}

/**
 * Put the boxes back together from the rows they were expanded into.
 *
 * Rows are grouped by `group_key`, so one box stays one box however many pairs it holds. Rows
 * without a key are the ones written before the column existed, and they fall back to grouping by
 * each pair's first cohort -- the old guess. It is still wrong for a box of three or more, but
 * changing it would mean inventing a grouping for rows whose real box is not recorded anywhere, and
 * the wrong answer here is the one the operator is already looking at. Saving upgrades them.
 *
 * Box order is first appearance in `rows`, so this is only as stable as the server's ordering. The
 * slot query sorts, deliberately: a drag-and-drop screen whose boxes move between saves is one the
 * operator cannot reason about, and an unordered `SELECT` reshuffles whenever PostgreSQL picks a
 * different plan.
 */
export function reconstructSlotBoxes(rows: SlotPairRow[]): SlotBox[] {
  const boxes = new Map<string, SlotBox>();
  const order: string[] = [];

  for (const row of rows) {
    const first: Cohort = { year: row.year_a, department: row.department_a };
    const second: Cohort = { year: row.year_b, department: row.department_b };

    // Prefixed so a legacy fallback bucket can never collide with a real group key.
    const groupKey = row.group_key ?? `legacy:${cohortKey(first)}`;
    let box = boxes.get(groupKey);
    if (!box) {
      box = { groupKey: row.group_key ?? newSlotGroupKey(), cohorts: [] };
      boxes.set(groupKey, box);
      order.push(groupKey);
    }
    for (const cohort of [first, second]) {
      if (!box.cohorts.some((existing) => isSameCohort(existing, cohort))) box.cohorts.push(cohort);
    }
  }

  return order.map((key) => boxes.get(key)!);
}
