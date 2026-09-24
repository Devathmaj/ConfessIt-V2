import { describe, it, expect } from 'vitest';

import {
  assignWord,
  emptyAssignment,
  isComplete,
  submissionGroups,
  unassignedCount,
  type ConnectionsAssignment,
} from '@/lib/connectionsGroups';

/**
 * The Connections assignment rules, without rendering anything.
 *
 * Every interaction on the board -- pointer drag-and-drop and the tap-to-select
 * fallback -- funnels through `assignWord`, so these pin the one capacity rule
 * the spec calls mandatory: dropping a fifth word into a full group accepts
 * the incoming word and returns the earliest-placed word of that group to the
 * available area, keeping the group at four. Around it, the invariants that
 * keep a submission valid: a word never appears twice, moving between groups
 * removes it from the previous one first, and only a full 4x4 board is
 * submittable.
 */

const WORDS = [
  'Apple', 'River', 'Chair', 'Blue',
  'Cloud', 'Drum', 'Elm', 'Flute',
  'Grape', 'Harp', 'Ivory', 'Jade',
  'Kite', 'Lemon', 'Maple', 'North',
];

const stateWith = (available: string[], groups: string[][]): ConnectionsAssignment => ({
  available: [...available],
  groups: groups.map((group) => [...group]) as ConnectionsAssignment['groups'],
});

describe('connections assignment', () => {
  it('starts with every word available and four empty groups', () => {
    const state = emptyAssignment(WORDS);
    expect(state.available).toEqual(WORDS);
    expect(state.groups).toEqual([[], [], [], []]);
    expect(unassignedCount(state)).toBe(16);
    expect(isComplete(state)).toBe(false);
  });

  it('assigns a word into an empty group', () => {
    const state = assignWord(emptyAssignment(WORDS), 'Apple', 0);
    expect(state.groups[0]).toEqual(['Apple']);
    expect(state.available).not.toContain('Apple');
    expect(state.available).toHaveLength(15);
  });

  it('moves a word between groups without duplicating it', () => {
    let state = emptyAssignment(WORDS);
    state = assignWord(state, 'Apple', 0);
    state = assignWord(state, 'Apple', 2);
    expect(state.groups[0]).toEqual([]);
    expect(state.groups[2]).toEqual(['Apple']);
    const everywhere = [...state.available, ...state.groups.flat()];
    expect(everywhere.filter((word) => word === 'Apple')).toHaveLength(1);
  });

  it('returns a word to the available area', () => {
    let state = emptyAssignment(WORDS);
    state = assignWord(state, 'Apple', 1);
    state = assignWord(state, 'Apple', -1);
    expect(state.groups[1]).toEqual([]);
    expect(state.available).toContain('Apple');
  });

  it('dropping a fifth word into a full group returns the oldest to the available area', () => {
    // Group holds, in placement order: Apple -> River -> Chair -> Blue.
    let state = emptyAssignment(WORDS);
    state = assignWord(state, 'Apple', 0);
    state = assignWord(state, 'River', 0);
    state = assignWord(state, 'Chair', 0);
    state = assignWord(state, 'Blue', 0);
    // The user drops Cloud into that group.
    state = assignWord(state, 'Cloud', 0);
    expect(state.groups[0]).toEqual(['River', 'Chair', 'Blue', 'Cloud']);
    expect(state.available).toContain('Apple');
    expect(state.available).not.toContain('Cloud');
  });

  it('moving a word from another group into a full group applies the same capacity rule', () => {
    let state = emptyAssignment(WORDS);
    for (const word of ['Apple', 'River', 'Chair', 'Blue']) {
      state = assignWord(state, word, 0);
    }
    state = assignWord(state, 'Cloud', 1);
    // Cloud leaves group 1 first, then displaces the oldest of group 0.
    state = assignWord(state, 'Cloud', 0);
    expect(state.groups[0]).toEqual(['River', 'Chair', 'Blue', 'Cloud']);
    expect(state.groups[1]).toEqual([]);
    expect(state.available).toContain('Apple');
  });

  it('dropping a tile into its existing group changes nothing', () => {
    let state = emptyAssignment(WORDS);
    state = assignWord(state, 'Apple', 0);
    state = assignWord(state, 'River', 0);
    const before = state;
    state = assignWord(state, 'Apple', 0);
    expect(state.groups[0]).toEqual(before.groups[0]);
    expect(state.available).toEqual(before.available);
  });

  it('ignores unknown words and out-of-range destinations', () => {
    const start = emptyAssignment(WORDS);
    expect(assignWord(start, 'Nope', 0)).toEqual(start);
    expect(assignWord(start, 'Apple', 7)).toEqual(start);
  });

  it('an incomplete arrangement cannot be submitted', () => {
    let state = emptyAssignment(WORDS);
    for (const word of WORDS.slice(0, 15)) {
      state = assignWord(state, word, 0);
    }
    // Group 0 capped at four; the rest spilled back -- never complete.
    expect(isComplete(state)).toBe(false);
    expect(unassignedCount(state)).toBeGreaterThan(0);
  });

  it('a full 4x4 board is complete and submits four groups of four', () => {
    let state = emptyAssignment(WORDS);
    WORDS.forEach((word, index) => {
      state = assignWord(state, word, Math.floor(index / 4));
    });
    expect(isComplete(state)).toBe(true);
    const groups = submissionGroups(state);
    expect(groups).toHaveLength(4);
    for (const group of groups) expect(group).toHaveLength(4);
    expect(groups.flat().sort()).toEqual([...WORDS].sort());
  });

  it('no word ever appears in two places at once', () => {
    let state = stateWith([], [[...WORDS.slice(0, 4)], [...WORDS.slice(4, 8)], [...WORDS.slice(8, 12)], [...WORDS.slice(12)]]);
    for (const word of WORDS) {
      state = assignWord(state, word, (WORDS.indexOf(word) + 1) % 4);
    }
    const everywhere = [...state.available, ...state.groups.flat()];
    expect(everywhere.sort()).toEqual([...WORDS].sort());
  });
});
