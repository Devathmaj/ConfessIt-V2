/**
 * The Connections assignment rules, as pure functions over plain data.
 *
 * The board is one assignment state: `available` holds the unassigned words
 * in tile order, and `groups` holds the four destination areas in placement
 * order (index 0 is the word placed earliest in that group). Every
 * interaction -- pointer drag-and-drop and the tap-to-select fallback --
 * funnels through `assignWord`, so both paths enforce exactly the same rules:
 *
 * * a word lives in exactly one place at a time and is never duplicated;
 * * a destination group holds at most four words;
 * * dropping a word into a full group accepts the incoming word and returns
 *   the earliest-placed word of that group to the available area;
 * * dropping a tile into the group it already occupies changes nothing.
 *
 * Pure so the capacity rule is unit-testable without rendering anything: the
 * page owns the state, this module owns the transitions.
 */

export interface ConnectionsAssignment {
  /** Words not currently assigned to any group, in tile order. */
  available: string[];
  /** The four destination groups; each entry is placement-ordered. */
  groups: [string[], string[], string[], string[]];
}

export const emptyAssignment = (words: readonly string[]): ConnectionsAssignment => ({
  available: [...words],
  groups: [[], [], [], []],
});

/** Where `word` currently lives: a group index, or -1 when available, or null when absent. */
const locate = (state: ConnectionsAssignment, word: string): number | null => {
  if (state.available.includes(word)) return -1;
  for (let index = 0; index < state.groups.length; index += 1) {
    if (state.groups[index].includes(word)) return index;
  }
  return null;
};

/**
 * Move `word` to `destination` (-1 for the available area, 0-3 for a group).
 *
 * Unknown words and out-of-range destinations leave the state unchanged.
 * Moving within the same place is a no-op: dragging a tile into its existing
 * group neither displaces another tile nor duplicates the assignment.
 */
export const assignWord = (
  state: ConnectionsAssignment,
  word: string,
  destination: number,
): ConnectionsAssignment => {
  if (destination < -1 || destination > 3) return state;
  const current = locate(state, word);
  if (current === null || current === destination) return state;

  const available = state.available.filter((entry) => entry !== word);
  const groups = state.groups.map((group) =>
    group.filter((entry) => entry !== word),
  ) as [string[], string[], string[], string[]];

  if (destination === -1) {
    return { available: [...available, word], groups };
  }

  const target = groups[destination];
  if (target.length >= 4) {
    // Full group: accept the incoming word, return the earliest-placed word
    // of that group to the available area. The group stays at four words.
    const [oldest, ...rest] = target;
    return {
      available: [...available, oldest],
      groups: groups.map((group, index) =>
        index === destination ? [...rest, word] : group,
      ) as [string[], string[], string[], string[]],
    };
  }
  return {
    available,
    groups: groups.map((group, index) =>
      index === destination ? [...group, word] : group,
    ) as [string[], string[], string[], string[]],
  };
};

/** How many words are still unassigned. */
export const unassignedCount = (state: ConnectionsAssignment): number =>
  state.available.length;

/** Whether the arrangement is submittable: all 16 assigned, four per group. */
export const isComplete = (state: ConnectionsAssignment): boolean =>
  state.available.length === 0 &&
  state.groups.every((group) => group.length === 4);

/** The submission payload: the four groups in their current placement order. */
export const submissionGroups = (state: ConnectionsAssignment): string[][] =>
  state.groups.map((group) => [...group]);
