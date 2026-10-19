import { describe, expect, it } from 'vitest';

import { appendUnseen, rereadWindow, windowHasDrifted } from '@/lib/pagedWindow';

/**
 * The two ways a paged window goes wrong, and the repair.
 *
 * Paging is by offset, and an offset only means something against a list that has
 * not moved. These are the pieces that notice the list moved and put the rows
 * back.
 */

type Row = { id: string };

const row = (id: string): Row => ({ id });

describe('windowHasDrifted', () => {
  it('is false when the page brings rows that are new', () => {
    expect(windowHasDrifted([row('a'), row('b')], [row('c'), row('d')], 4, 4)).toBe(false);
  });

  it('is true when the page brings a row that is already on screen', () => {
    // A post inserted above the window shifted it: the page starts one row too
    // early and hands back a row the reader already has.
    expect(windowHasDrifted([row('a'), row('b')], [row('b'), row('c')], 2, 3)).toBe(true);
  });

  it('is true when the corpus shrank', () => {
    // A post removed from above the window shifted it the other way: the page
    // starts one row too late, and a row the reader has not seen is stepped over.
    // Nothing in the page shows it -- the row that is missing is the one that is
    // not there -- so the total is what gives it away.
    expect(windowHasDrifted([row('a'), row('b')], [row('c')], 3, 2)).toBe(true);
  });

  it('is false when the corpus grew and the page is all new rows', () => {
    // A post inserted *below* the window does not move it, and the corpus grew.
    expect(windowHasDrifted([row('a'), row('b')], [row('c'), row('d')], 2, 4)).toBe(false);
  });
});

describe('appendUnseen', () => {
  it('appends the rows that are new', () => {
    expect(appendUnseen([row('a')], [row('b'), row('c')])).toEqual([row('a'), row('b'), row('c')]);
  });

  it('drops the rows that are already on screen', () => {
    expect(appendUnseen([row('a'), row('b')], [row('b'), row('c')])).toEqual([
      row('a'),
      row('b'),
      row('c'),
    ]);
  });

  it('leaves the list alone when nothing is new', () => {
    const held = [row('a'), row('b')];
    expect(appendUnseen(held, [row('a'), row('b')])).toBe(held);
  });
});

describe('rereadWindow', () => {
  it('reads the window back from the top', async () => {
    const seen: number[] = [];
    const answer = await rereadWindow<Row>(4, 2, async (skip, limit) => {
      seen.push(skip);
      const rows = ['a', 'b', 'c', 'd'].slice(skip, skip + limit).map(row);
      return { rows, total: 4 };
    });

    expect(answer.rows.map((r) => r.id)).toEqual(['a', 'b', 'c', 'd']);
    // The server caps a page, and the reader may have paged past the cap, so
    // the window is walked rather than asked for in one request.
    expect(seen).toEqual([0, 2]);
  });

  it('stops when the server runs out of rows', async () => {
    const answer = await rereadWindow<Row>(10, 2, async (skip, limit) => {
      const rows = ['a', 'b'].slice(skip, skip + limit).map(row);
      return { rows, total: 2 };
    });

    expect(answer.rows.map((r) => r.id)).toEqual(['a', 'b']);
    expect(answer.total).toBe(2);
  });

  it('keeps the reader in the same place', async () => {
    // The window the reader holds is read back in the server's order, so the
    // rows they were looking at are still there -- in the right order.
    const answer = await rereadWindow<Row>(3, 10, async () => ({
      rows: [row('new'), row('a'), row('b')],
      total: 4,
    }));

    expect(answer.rows.map((r) => r.id)).toEqual(['new', 'a', 'b']);
  });
});
