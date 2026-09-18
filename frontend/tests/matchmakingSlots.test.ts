import { describe, it, expect } from 'vitest';

import {
  expandSlotBoxes,
  newSlotGroupKey,
  reconstructSlotBoxes,
  type SlotBox,
} from '@/lib/matchmakingSlots';

/**
 * The save -> load round trip, which is the whole reason `group_key` exists.
 *
 * A slot box is expanded into the pairs it permits on save and put back together from those pairs on
 * load. Before `group_key`, the pairing was reconstructed by guessing -- grouping on each pair's
 * *first* cohort -- and the guess was wrong in both directions at once. One box of A, B and C came
 * back as a box of A, B, C *plus a stray box of B, C*, because B-C's first cohort is B rather than
 * A. Nothing about matchmaking noticed, because the rule reads pairs and the pairs were always
 * right; what broke was the save that follows a load. The stray box duplicated pairs the first box
 * already wrote, the server answered 409, and because the editor deletes the old configuration
 * before writing the new one, the operator was left with a truncated configuration and an error.
 *
 * So these tests assert the *round trip*, not the expansion: a configuration must survive
 * save -> load -> save with the same boxes and the same pairs. Asserting only that the right pairs
 * come out would have passed against the broken version, which is what makes the first test here
 * the one that matters.
 */

const A = { year: 2, department: 'CSE' };
const B = { year: 2, department: 'ECE' };
const C = { year: 3, department: 'CSE' };
const D = { year: 3, department: 'ECE' };
const E = { year: 4, department: 'CSE' };

const box = (cohorts: { year: number; department: string }[]): SlotBox => ({
  groupKey: newSlotGroupKey(),
  cohorts,
});

/** The pairs a configuration means, as an order-independent set of canonical strings. */
function pairSet(boxes: SlotBox[]): string[] {
  return expandSlotBoxes(boxes)
    .map((pair) => {
      const first = `${pair.year_a}/${pair.department_a}`;
      const second = `${pair.year_b}/${pair.department_b}`;
      return [first, second].sort().join(' <-> ');
    })
    .sort();
}

/** The cohorts of each box, as an order-independent shape that survives grouping. */
function shapeOf(boxes: SlotBox[]): string[] {
  return boxes
    .map((slot) => slot.cohorts.map((c) => `${c.year}/${c.department}`).sort().join('+'))
    .sort();
}

describe('slot box round trip', () => {
  it('returns a box of three as a box of three', () => {
    // The regression, stated as the operator sees it. The broken reconstruction returned two boxes
    // here -- the real one and a spurious [B, C] -- and every test below this one would still have
    // passed, because the pairs were correct either way.
    const before = [box([A, B, C])];

    const boxes = reconstructSlotBoxes(expandSlotBoxes(before));

    expect(boxes).toHaveLength(1);
    expect(shapeOf(boxes)).toEqual(['2/CSE+2/ECE+3/CSE']);
  });

  it.each([
    ['two cohorts', [A, B]],
    ['three cohorts', [A, B, C]],
    ['four cohorts', [A, B, C, D]],
    ['five cohorts', [A, B, C, D, E]],
  ])('survives save -> load -> save unchanged: %s', (_label, cohorts) => {
    const before = [box(cohorts)];

    const first = expandSlotBoxes(before);
    const loaded = reconstructSlotBoxes(first);
    const second = expandSlotBoxes(loaded);

    expect(shapeOf(loaded)).toEqual(shapeOf(before));
    // The pair set is compared as a set, and the row count separately: the pairs are what
    // matchmaking obeys and must be identical, while the count is what proves the reload did not
    // quietly lose or duplicate a row on the way through.
    expect(pairSet(loaded)).toEqual(pairSet(before));
    expect(second).toHaveLength(first.length);
  });

  it('keeps a cohort in several boxes, which is a supported configuration', () => {
    // The user-facing rule: A in [A, B, C] and [D, E, A] means A pairs with itself, B, C, D and E.
    // Both boxes write pairs naming A, and the rule reads pairs, so both apply.
    const before = [box([A, B, C]), box([D, E, A])];

    const boxes = reconstructSlotBoxes(expandSlotBoxes(before));

    expect(shapeOf(boxes)).toEqual(['2/CSE+2/ECE+3/CSE', '2/CSE+3/ECE+4/CSE']);
  });

  it('keeps two boxes apart even when one contains the other', () => {
    // [A, B] and [A, B, C] both write A-B. The server accepts a pair that repeats across different
    // boxes precisely so this can be saved, and each row remembers which box sent it. Grouping on
    // anything derived from the pair would merge these into one box of three and lose the operator's
    // smaller box.
    const before = [box([A, B]), box([A, B, C])];

    const boxes = reconstructSlotBoxes(expandSlotBoxes(before));

    expect(shapeOf(boxes)).toEqual(['2/CSE+2/ECE', '2/CSE+2/ECE+3/CSE'].sort());
  });

  it('keeps boxes apart when they share their first cohort', () => {
    // [A, B] and [A, C]. Sharing a first cohort is what the old grouping keyed on, so these were the
    // boxes it merged. They are two boxes.
    const before = [box([A, B]), box([A, C])];

    const boxes = reconstructSlotBoxes(expandSlotBoxes(before));

    expect(shapeOf(boxes)).toEqual(['2/CSE+2/ECE', '2/CSE+3/CSE']);
  });

  it('does not merge an unrelated box into one it happens to sit beside', () => {
    // [A, B] and [B, C]. One shared cohort, in opposite positions: A-B has A first, B-C has B first.
    // Ordering the pairs by their first cohort is the only thing that used to separate them.
    const before = [box([A, B]), box([B, C])];

    const boxes = reconstructSlotBoxes(expandSlotBoxes(before));

    expect(shapeOf(boxes)).toEqual(['2/CSE+2/ECE', '2/ECE+3/CSE']);
  });

  it('preserves an empty box as nothing rather than as a box of one cohort', () => {
    // A "+" box the operator has not filled in yet writes no pairs, so it cannot be read back. That is
    // the one thing a box cannot round-trip, and it is why an unsaved empty box disappears on reload
    // rather than being an error.
    expect(expandSlotBoxes([box([])])).toEqual([]);
    expect(reconstructSlotBoxes([])).toEqual([]);
  });
});

describe('what a box permits', () => {
  it('permits a single cohort to match only with itself', () => {
    const pairs = expandSlotBoxes([box([A])]);

    expect(pairs).toHaveLength(1);
    expect(pairs[0]).toMatchObject({
      year_a: A.year, department_a: A.department,
      year_b: A.year, department_b: A.department,
    });
  });

  it('permits every pair of a box of N, and self-matching for each cohort in it', () => {
    // [A, B, C] permits A-A, A-B, A-C, B-B, B-C and C-C. Six pairs stored, nine permitted matchings
    // -- the three self-pairings come from any row naming that cohort, which is why the stored pair
    // count is not the permitted count and why the backend rule asks "does some row name both?"
    // rather than looking for a row equal to the pair.
    const cohorts = [A, B, C];

    expect(expandSlotBoxes([box(cohorts)])).toHaveLength(3);

    const named = new Set<string>();
    for (const pair of expandSlotBoxes([box(cohorts)])) {
      named.add(`${pair.year_a}/${pair.department_a}`);
      named.add(`${pair.year_b}/${pair.department_b}`);
    }
    for (const cohort of cohorts) {
      expect(named).toContain(`${cohort.year}/${cohort.department}`);
    }
  });

  it('emits no pair for an empty box', () => {
    expect(expandSlotBoxes([box([]), box([A]), box([B])])).toHaveLength(2);
  });

  it('does not emit the same pair twice from one box', () => {
    // The drag handler refuses to add a cohort twice, but the expansion is where that would turn
    // into a duplicate row, so it is guarded here as well rather than trusting the caller.
    const duplicated = box([A, B, A]);

    expect(expandSlotBoxes([duplicated])).toHaveLength(1);
  });
});

describe('rows without a group key', () => {
  it('groups them by first cohort, which is the old guess', () => {
    // Rows written before the column existed carry no key and their real box is recorded nowhere.
    // Keeping the old guess means the operator is shown the same grouping they were shown before,
    // and saving upgrades the rows to keyed ones. A box of two survives unchanged.
    const rows = [
      { year_a: A.year, department_a: A.department, year_b: B.year, department_b: B.department, group_key: null },
    ];

    const boxes = reconstructSlotBoxes(rows);

    expect(boxes).toHaveLength(1);
    expect(shapeOf(boxes)).toEqual(['2/CSE+2/ECE']);
  });

  it('gives every reconstructed box a key, so the next save groups them', () => {
    const rows = [
      { year_a: A.year, department_a: A.department, year_b: B.year, department_b: B.department, group_key: null },
    ];

    const boxes = reconstructSlotBoxes(rows);

    expect(boxes[0].groupKey).toBeTruthy();
    expect(expandSlotBoxes(boxes)[0].group_key).toBe(boxes[0].groupKey);
  });

  it('never mixes a keyed row into a legacy bucket', () => {
    // The fallback bucket is keyed by the pair's first cohort, so a real group key that happened to
    // equal that string would collide. The `legacy:` prefix is what stops it.
    const rows = [
      { year_a: A.year, department_a: A.department, year_b: B.year, department_b: B.department, group_key: null },
      { year_a: A.year, department_a: A.department, year_b: C.year, department_b: C.department, group_key: 'some-real-key' },
    ];

    expect(reconstructSlotBoxes(rows)).toHaveLength(2);
  });
});

describe('group keys', () => {
  it('are distinct', () => {
    const keys = new Set(Array.from({ length: 200 }, () => newSlotGroupKey()));
    expect(keys.size).toBe(200);
  });

  it('are uuid-shaped, because the server refuses anything else', () => {
    expect(newSlotGroupKey()).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
    );
  });
});
