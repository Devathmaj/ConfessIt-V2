import { describe, it, expect } from 'vitest';

import {
  canSubmitSelection,
  formatResponseTime,
  instructionForMode,
  toggleSelection,
} from '@/lib/triviaDrop';

/**
 * The Trivia Drop selection rules, without rendering anything.
 *
 * One question is live at a time and each player answers it at most once, so
 * the board's whole state is its current selection. Single-answer questions
 * behave as radios (choosing replaces), multiple-answer questions as
 * checkboxes (toggling adds or removes), and both submit the whole set in one
 * request. Around the transitions, the display rules that keep the key
 * secret: the multiple-answer instruction never names a count, and nothing
 * here carries correctness.
 */

describe('trivia selection', () => {
  it('single-answer choosing replaces the previous choice', () => {
    expect(toggleSelection([], 1, 'single')).toEqual([1]);
    expect(toggleSelection([1], 2, 'single')).toEqual([2]);
    expect(toggleSelection([2], 2, 'single')).toEqual([2]);
  });

  it('multiple-answer choosing toggles membership', () => {
    expect(toggleSelection([], 0, 'multiple')).toEqual([0]);
    expect(toggleSelection([0], 2, 'multiple')).toEqual([0, 2]);
    expect(toggleSelection([0, 2], 0, 'multiple')).toEqual([2]);
    expect(toggleSelection([0, 2], 0, 'multiple')).toEqual([2]);
  });

  it('ignores out-of-range option indexes', () => {
    expect(toggleSelection([1], 9, 'single')).toEqual([1]);
    expect(toggleSelection([1], -1, 'multiple')).toEqual([1]);
    expect(toggleSelection([], 4, 'multiple')).toEqual([]);
  });

  it('an empty selection cannot be submitted', () => {
    expect(canSubmitSelection([])).toBe(false);
    expect(canSubmitSelection([2])).toBe(true);
    expect(canSubmitSelection([0, 2])).toBe(true);
  });

  it('instructs single- and multiple-answer play distinctly', () => {
    expect(instructionForMode('single')).toMatch(/one answer/i);
    expect(instructionForMode('multiple')).toMatch(/all that apply/i);
  });

  it('the multiple-answer instruction reveals no correct count', () => {
    // Whatever the key holds -- two, three or four correct -- the screen says
    // the same sentence, so the wording cannot leak the answer's shape.
    expect(instructionForMode('multiple')).not.toMatch(/\d/);
  });

  it('formats response times for display without touching the stored value', () => {
    // 59 minutes 57 seconds from the spec's example stays 59:57.
    expect(formatResponseTime(59 * 60_000 + 57_000)).toBe('59:57');
    expect(formatResponseTime(5_000)).toBe('0:05');
    expect(formatResponseTime(65_000)).toBe('1:05');
    expect(formatResponseTime(-100)).toBe('0:00');
  });
});
