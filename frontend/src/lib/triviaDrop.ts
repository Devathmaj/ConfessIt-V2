/**
 * The Trivia Drop selection rules, as pure functions over plain data.
 *
 * One question is live at a time and each player answers it at most once, so
 * the only state the board owns is the current selection: a set of option
 * indexes. Single-answer questions behave as radios (choosing replaces),
 * multiple-answer questions as checkboxes (toggling adds or removes), and
 * both submit the whole set in one request. The mode itself is derived from
 * the question the server serves -- never configured anywhere here.
 *
 * Pure so the rules are unit-testable without rendering anything: the page
 * owns the state, this module owns the transitions.
 */

export type TriviaAnswerMode = 'single' | 'multiple';

/** The instruction the screen shows for one answer mode -- never a count. */
export const instructionForMode = (mode: TriviaAnswerMode): string =>
  mode === 'single' ? 'Select one answer' : 'Select all that apply';

/**
 * The next selection after choosing `index` under `mode`.
 *
 * Single: choosing replaces the previous choice outright. Multiple: choosing
 * toggles membership, so options can be selected and deselected freely
 * before submission. Out-of-range indexes leave the selection unchanged.
 */
export const toggleSelection = (
  selection: readonly number[],
  index: number,
  mode: TriviaAnswerMode,
  optionCount = 4,
): number[] => {
  if (!Number.isInteger(index) || index < 0 || index >= optionCount) return [...selection];
  if (mode === 'single') return [index];
  return selection.includes(index)
    ? selection.filter((entry) => entry !== index)
    : [...selection, index];
};

/** Whether the selection is submittable: at least one option chosen. */
export const canSubmitSelection = (selection: readonly number[]): boolean =>
  selection.length > 0;

/**
 * A response time in milliseconds as a person reads it.
 *
 * Rounds to whole seconds: `59:57` for fifty-nine minutes and fifty-seven
 * seconds, `0:05` for five seconds. The authoritative number stays an integer
 * count of milliseconds on the server; this is display only.
 */
export const formatResponseTime = (responseTimeMs: number): string => {
  const clamped = Math.max(0, Math.floor(responseTimeMs / 1000));
  const minutes = Math.floor(clamped / 60);
  const seconds = clamped % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
};
