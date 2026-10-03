import type { GameCategoryEntry } from '@/services/api';

/**
 * Presentation config for per-game leaderboard categories.
 *
 * The backend ranks by metric and returns documented `metrics` keys plus
 * detail `records` per entry; this map decides which keys become columns,
 * how they format, and which one headlines the viewer's own standing.
 * Unknown categories fall back to rendering every metric key as-is, so a
 * new backend category degrades to a plain table rather than a blank page.
 */

export type MetricFormat = 'int' | 'num' | 'ms' | 's' | 'pct' | 'date' | 'text' | 'bool';

export interface CategoryColumn {
  key: string;
  label: string;
  format: MetricFormat;
}

export interface CategoryUI {
  columns: CategoryColumn[];
  /** Headline metric for the viewer's standing line. */
  primary: { key: string; label: string; format: MetricFormat };
}

const SOLVED_COLS: CategoryColumn[] = [
  { key: 'solved', label: 'Solved', format: 'int' },
  { key: 'opportunities', label: 'Played', format: 'int' },
  { key: 'solve_rate', label: 'Solve rate', format: 'pct' },
];

export const CATEGORY_UI: Record<string, CategoryUI> = {
  performance: {
    columns: [
      ...SOLVED_COLS,
      { key: 'avg_guesses', label: 'Avg guesses', format: 'num' },
      { key: 'best_guesses', label: 'Best', format: 'int' },
    ],
    primary: { key: 'solved', label: 'solved', format: 'int' },
  },
  most_solved: {
    columns: [...SOLVED_COLS, { key: 'avg_guesses', label: 'Avg guesses', format: 'num' }],
    primary: { key: 'solved', label: 'solved', format: 'int' },
  },
  fewest_guesses: {
    columns: [
      { key: 'best_guesses', label: 'Best guesses', format: 'int' },
      { key: 'best_duration_s', label: 'Solve time', format: 's' },
      { key: 'solved', label: 'Solved', format: 'int' },
    ],
    primary: { key: 'best_guesses', label: 'best guesses', format: 'int' },
  },
  fastest_solve: {
    columns: [
      { key: 'fastest_s', label: 'Fastest', format: 's' },
      { key: 'guesses', label: 'Guesses', format: 'int' },
      { key: 'solved', label: 'Solved', format: 'int' },
    ],
    primary: { key: 'fastest_s', label: 'fastest solve', format: 's' },
  },
  fewest_attempts: {
    columns: [
      { key: 'best_attempts', label: 'Best attempts', format: 'int' },
      { key: 'best_active_s', label: 'Active time', format: 's' },
      { key: 'solved', label: 'Solved', format: 'int' },
    ],
    primary: { key: 'best_attempts', label: 'best attempts', format: 'int' },
  },
  average: {
    columns: [
      { key: 'avg_ms', label: 'Average', format: 'ms' },
      { key: 'attempts', label: 'Attempts', format: 'int' },
    ],
    primary: { key: 'avg_ms', label: 'average', format: 'ms' },
  },
  fastest: {
    columns: [
      { key: 'fastest_ms', label: 'Fastest', format: 'ms' },
      { key: 'attempts', label: 'Attempts', format: 'int' },
      { key: 'date', label: 'Date', format: 'date' },
    ],
    primary: { key: 'fastest_ms', label: 'fastest', format: 'ms' },
  },
  best_quality: {
    columns: [
      { key: 'best_quality', label: 'Best quality', format: 'num' },
      { key: 'groups', label: 'Groups', format: 'int' },
      { key: 'words', label: 'Words', format: 'int' },
      { key: 'date', label: 'Date', format: 'date' },
    ],
    primary: { key: 'best_quality', label: 'best quality', format: 'num' },
  },
  answer_performance: {
    columns: [
      { key: 'total_score', label: 'Total', format: 'num' },
      { key: 'answered', label: 'Answered', format: 'int' },
      { key: 'correct', label: 'Correct', format: 'int' },
    ],
    primary: { key: 'total_score', label: 'total', format: 'num' },
  },
  most_answered: {
    columns: [
      { key: 'answered', label: 'Answered', format: 'int' },
      { key: 'correct', label: 'Correct', format: 'int' },
      { key: 'avg_response_ms', label: 'Avg response', format: 'ms' },
    ],
    primary: { key: 'answered', label: 'answered', format: 'int' },
  },
  fastest_answer: {
    columns: [
      { key: 'fastest_ms', label: 'Fastest', format: 'ms' },
      { key: 'correct', label: 'Correct', format: 'bool' },
      { key: 'answered', label: 'Answered', format: 'int' },
    ],
    primary: { key: 'fastest_ms', label: 'fastest answer', format: 'ms' },
  },
  score: {
    columns: [
      { key: 'score', label: 'Score', format: 'num' },
      { key: 'correct', label: 'Correct', format: 'int' },
      { key: 'total', label: 'Questions', format: 'int' },
    ],
    primary: { key: 'score', label: 'score', format: 'num' },
  },
  escape_performance: {
    columns: [
      { key: 'escapes', label: 'Escapes', format: 'int' },
      { key: 'fastest_s', label: 'Fastest', format: 's' },
      { key: 'avg_s', label: 'Average', format: 's' },
    ],
    primary: { key: 'escapes', label: 'escapes', format: 'int' },
  },
  fastest_escape: {
    columns: [
      { key: 'fastest_s', label: 'Fastest', format: 's' },
      { key: 'escapes', label: 'Escapes', format: 'int' },
      { key: 'moves', label: 'Moves', format: 'int' },
      { key: 'date', label: 'Date', format: 'date' },
    ],
    primary: { key: 'fastest_s', label: 'fastest escape', format: 's' },
  },
};

/** Anagram boards share shapes with Wordle's but different metric keys. */
CATEGORY_UI['performance_anagram'] = {
  columns: [
    ...SOLVED_COLS,
    { key: 'avg_attempts', label: 'Avg attempts', format: 'num' },
    { key: 'avg_active_s', label: 'Avg active', format: 's' },
  ],
  primary: { key: 'solved', label: 'solved', format: 'int' },
};
CATEGORY_UI['most_solved_anagram'] = {
  columns: [
    ...SOLVED_COLS,
    { key: 'avg_attempts', label: 'Avg attempts', format: 'num' },
  ],
  primary: { key: 'solved', label: 'solved', format: 'int' },
};
CATEGORY_UI['performance_connections'] = {
  columns: [
    { key: 'puzzles', label: 'Puzzles', format: 'int' },
    { key: 'total_quality', label: 'Total quality', format: 'num' },
    { key: 'avg_quality', label: 'Average', format: 'num' },
    { key: 'best_quality', label: 'Best', format: 'num' },
  ],
  primary: { key: 'total_quality', label: 'total quality', format: 'num' },
};

/** Resolve UI config: game-aware variant first, then the shared default. */
export const categoryUI = (gameId: string, categoryId: string): CategoryUI =>
  CATEGORY_UI[`${categoryId}_${gameId}`] ??
  CATEGORY_UI[categoryId] ?? {
    columns: [],
    primary: { key: '', label: '', format: 'text' as MetricFormat },
  };

export const formatMetric = (
  value: number | boolean | string | string[] | null | undefined,
  format: MetricFormat,
): string => {
  if (value === null || value === undefined || value === '') return '—';
  if (format === 'bool') return value === true || value === 'true' || value === 1 ? 'Yes' : 'No';
  if (format === 'pct' && typeof value === 'number') return `${Math.round(value * 100)}%`;
  if (format === 'ms' && typeof value === 'number') return `${value} ms`;
  if (format === 's' && typeof value === 'number') return `${value} s`;
  if (format === 'num' && typeof value === 'number') {
    return Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100);
  }
  if (format === 'int' && typeof value === 'number') return String(value);
  if (Array.isArray(value)) return value.join(', ');
  return String(value);
};

/** Display name for a category entry (individual or couple). */
export const categoryEntryName = (entry: GameCategoryEntry): string => {
  if (entry.display_name) return entry.display_name;
  const pair = [entry.member_a_name, entry.member_b_name].filter(Boolean);
  if (pair.length === 2) return `${pair[0]} & ${pair[1]}`;
  return 'Player';
};

/** Stable React key for a category entry. */
export const categoryEntryKey = (entry: GameCategoryEntry): string =>
  entry.user_id ?? `${entry.member_a_id}:${entry.member_b_id}`;

/** Human label for a detail-record field. */
export const recordFieldLabel = (key: string): string =>
  key
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase());

/** Guess a display format for record fields not covered by columns. */
export const recordFieldFormat = (key: string, value: unknown): MetricFormat => {
  const lower = key.toLowerCase();
  if (typeof value === 'boolean') return 'bool';
  if (lower.includes('correct') && typeof value === 'boolean') return 'bool';
  if (lower === 'correct' && (value === 0 || value === 1)) return 'bool';
  if (lower.includes('_ms') || lower === 'ms' || lower.includes('response')) return 'ms';
  if (lower.includes('duration') || lower.includes('elapsed') || lower === 'fastest_s' || lower.endsWith('_s')) return 's';
  if (lower.includes('date')) return 'date';
  if (typeof value === 'number') return Number.isInteger(value) ? 'int' : 'num';
  return 'text';
};
