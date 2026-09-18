import { describe, it, expect } from 'vitest';

import {
  cohortDragPayload,
  cohortFromDragPayload,
  cohortKey,
  cohortFromKey,
} from '@/lib/accountFields';

/**
 * The drag payload, and why it is JSON and not `cohortKey`.
 *
 * `cohortKey` flattens a cohort with a U+0000 separator, which is the right choice for a
 * `<select>` value that never leaves the page. The drag payload does leave the page -- it is handed
 * to the browser through `DataTransfer.setData` and read back with `getData` -- and a control
 * character is not safe to rely on across that boundary. When it did not survive, `cohortFromKey`
 * found no separator, returned null, and every drop was silently discarded: the chip snapped back
 * and the screen looked broken with nothing in the console.
 *
 * The first test is the one that pins the decision. The rest check that the parser validates,
 * because a drag can start outside this page.
 */
describe('cohort drag payload', () => {
  const cohort = { year: 2, department: 'CSE' };

  it('contains no control characters, unlike the select key', () => {
    const payload = cohortDragPayload(cohort);

    expect(payload).not.toContain('\u0000');
    expect(cohortKey(cohort)).toContain('\u0000');
    // The thing that actually broke: the old encoding is not safe to drag.
    expect(cohortFromKey(cohortDragPayload(cohort))).toBeNull();
  });

  it('round-trips a cohort', () => {
    expect(cohortFromDragPayload(cohortDragPayload(cohort))).toEqual(cohort);
  });

  it('round-trips two-digit years, where string and number orderings diverge', () => {
    const twoDigit = { year: 10, department: 'ECE' };

    expect(cohortFromDragPayload(cohortDragPayload(twoDigit))).toEqual(twoDigit);
  });

  it('keeps a department containing spaces intact', () => {
    const spaced = { year: 11, department: 'Computer Science' };

    expect(cohortFromDragPayload(cohortDragPayload(spaced))).toEqual(spaced);
  });

  it('rejects the old flattened key outright', () => {
    // Belt and braces for the first test: a key is JSON-parseable only by accident, and must not
    // be mistaken for one.
    expect(cohortFromDragPayload('2\u0000CSE')).toBeNull();
  });

  it('returns null for anything that is not a cohort', () => {
    for (const payload of ['', 'not json', '{', 'null', '42', '"CSE"', '[]', '{"year":2}']) {
      expect(cohortFromDragPayload(payload)).toBeNull();
    }
  });

  it('rejects a year outside the range', () => {
    for (const year of [0, 13, -1, 2.5, '2', null]) {
      expect(cohortFromDragPayload(JSON.stringify({ year, department: 'CSE' }))).toBeNull();
    }
  });

  it('rejects a non-string department', () => {
    expect(cohortFromDragPayload('{"year":2,"department":5}')).toBeNull();
  });
});
