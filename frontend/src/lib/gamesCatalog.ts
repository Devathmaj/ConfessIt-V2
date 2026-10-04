import type { LucideIcon } from 'lucide-react';
import {
  CircleHelp,
  Grid3x3,
  HeartHandshake,
  Shuffle,
  Timer,
} from 'lucide-react';

/**
 * The catalogue of games the Games section can show.
 *
 * Only games with a real backend session lifecycle and a real game screen
 * are listed here. There is no placeholder or coming-soon data: a game is
 * either playable through its route or it is absent from this list.
 */

export type GameCategoryId =
  | 'icebreaker'
  | 'party'
  | 'wordplay'
  | 'memory'
  | 'trivia';

export type GameDifficulty = 'easy' | 'medium' | 'hard';

/**
 * Which of the two game categories a title belongs to.
 *
 * This is a separate axis from `category`: `category` is the genre a player browses by
 * (wordplay, trivia, ...), while `type` is whether the game is played alone or with a
 * partner. The administration area filters and groups on it, and the leaderboard uses it as
 * a dimension of its own, so it lives on the definition rather than being re-derived from the
 * free-form `players` string in two places.
 */
export type GameTypeId = 'single' | 'couple';

export interface GameType {
  id: GameTypeId;
  label: string;
}

export const GAME_TYPES: GameType[] = [
  { id: 'single', label: 'Single' },
  { id: 'couple', label: 'Couple' },
];

/**
 * Playable now through a real game screen and a real session lifecycle.
 * Games without an implementation are absent rather than listed as
 * coming soon.
 */
export type GameStatus = 'available';

export interface GameDefinition {
  id: string;
  name: string;
  tagline: string;
  category: GameCategoryId;
  type: GameTypeId;
  difficulty: GameDifficulty;
  /** Free-form because lobby sizes are a product decision, not a number the UI math needs. */
  players: string;
  /** Free-form average run time, e.g. "~3 min". */
  duration: string;
  icon: LucideIcon;
  /** Tailwind gradient class used for the game's icon tile. */
  accent: string;
  status: GameStatus;
  /** The app route a Play action navigates to. Every catalogued game has one. */
  route: string;
}

export interface GameCategory {
  id: GameCategoryId;
  label: string;
}

/**
 * Genre buckets (Icebreakers, Party, ...). No longer surfaced in the UI: the
 * selection page groups by `GAME_TYPES` (Single vs Couple) and cards badge the
 * type. Kept on the definition for backward compatibility only.
 *
 * @deprecated Use `GAME_TYPES` / `game.type` for all UI grouping and labels.
 */
export const GAME_CATEGORIES: GameCategory[] = [
  { id: 'icebreaker', label: 'Icebreakers' },
  { id: 'party', label: 'Party' },
  { id: 'wordplay', label: 'Wordplay' },
  { id: 'memory', label: 'Memory' },
  { id: 'trivia', label: 'Trivia' },
];

export const AVAILABLE_GAMES: GameDefinition[] = [
  {
    id: 'wordle',
    name: 'Wordle',
    tagline: 'Five letters, six guesses, one word a day.',
    category: 'wordplay',
    type: 'single',
    difficulty: 'medium',
    players: 'Solo',
    duration: '~3 min',
    icon: Grid3x3,
    accent: 'bg-gradient-hero',
    status: 'available',
    route: '/games/wordle',
  },
  {
    id: 'anagram',
    name: 'Anagram',
    tagline: 'Unscramble the letters in five guesses or fewer.',
    category: 'wordplay',
    type: 'single',
    difficulty: 'medium',
    players: 'Solo',
    duration: '~3 min',
    icon: Shuffle,
    accent: 'bg-gradient-passion',
    status: 'available',
    route: '/games/anagram',
  },
  {
    id: 'couple-questionnaire',
    name: 'Couple Questionnaire',
    tagline: 'Answer for yourself, then predict your partner — 15 and 15.',
    category: 'trivia',
    type: 'couple',
    difficulty: 'medium',
    players: '2 players',
    duration: '~5 min',
    icon: HeartHandshake,
    accent: 'bg-gradient-love',
    status: 'available',
    route: '/games/couple-questionnaire',
  },
  {
    id: 'two-key-escape',
    name: 'Two-Key Escape',
    tagline: 'One maze, two roles — navigate together, asynchronously.',
    category: 'memory',
    type: 'couple',
    difficulty: 'medium',
    players: '2 players',
    duration: '~5 min',
    icon: HeartHandshake,
    accent: 'bg-gradient-love',
    status: 'available',
    route: '/games/two-key-escape',
  },
  {
    id: 'reaction-time',
    name: 'Reaction Time',
    tagline: 'Wait for green, then click as fast as you can — five tries.',
    category: 'icebreaker',
    type: 'single',
    difficulty: 'easy',
    players: 'Solo',
    duration: '~2 min',
    icon: Timer,
    accent: 'bg-gradient-hero',
    status: 'available',
    route: '/games/reaction-time',
  },
  {
    id: 'connections',
    name: 'Connections',
    tagline: 'Find the four hidden groups of four — one submission.',
    category: 'wordplay',
    type: 'single',
    difficulty: 'medium',
    players: 'Solo',
    duration: '~4 min',
    icon: Grid3x3,
    accent: 'bg-gradient-passion',
    status: 'available',
    route: '/games/connections',
  },
  {
    id: 'trivia-drop',
    name: 'Trivia Drop',
    tagline: 'One live question at a time — answer fast, answer once.',
    category: 'trivia',
    type: 'single',
    difficulty: 'medium',
    players: 'Solo',
    duration: '~1 min',
    icon: CircleHelp,
    accent: 'bg-gradient-love',
    status: 'available',
    route: '/games/trivia-drop',
  },
  {
    id: 'plot-twist',
    name: 'Plot Twist',
    tagline: 'A branching story you and your partner write together.',
    category: 'party',
    type: 'couple',
    difficulty: 'medium',
    players: '2 players',
    duration: '~5 min',
    icon: HeartHandshake,
    accent: 'bg-gradient-love',
    status: 'available',
    route: '/games/plot-twist',
  },
  {
    id: 'case-closed',
    name: 'Case Closed',
    tagline: 'A couple game — gameplay lands soon.',
    category: 'party',
    type: 'couple',
    difficulty: 'medium',
    players: '2 players',
    duration: '~5 min',
    icon: HeartHandshake,
    accent: 'bg-gradient-love',
    status: 'available',
    route: '/games/case-closed',
  },
];

export const getGameById = (gameId: string): GameDefinition | undefined =>
  AVAILABLE_GAMES.find((game) => game.id === gameId);

export const categoryLabel = (category: GameCategoryId): string =>
  GAME_CATEGORIES.find((entry) => entry.id === category)?.label ?? category;

export const gameTypeLabel = (type: GameTypeId): string =>
  GAME_TYPES.find((entry) => entry.id === type)?.label ?? type;

/**
 * The day's pick, rotated by day number over `games`.
 *
 * The same rule as `getFeaturedGame`, but over the caller's list rather than the whole
 * catalogue: the admin side rotates only over the games that are actually eligible for the
 * spotlight, and both pick the same title when given the same list. Null for an empty list --
 * "no eligible games" is a state the caller has to render, not an exception.
 */
export const featuredGameFrom = (
  games: readonly GameDefinition[],
  now: Date = new Date(),
): GameDefinition | null => {
  if (games.length === 0) return null;
  const startOfYear = new Date(now.getFullYear(), 0, 0);
  const dayOfYear = Math.floor((now.getTime() - startOfYear.getTime()) / 86_400_000);
  return games[dayOfYear % games.length];
};

/**
 * Picks "today's featured game" the same way every time the page is opened on the same day:
 * the day number rotates through the catalogue. A server-backed spotlight replaces this;
 * it is kept only as a fallback for renders without server data.
 */
export const getFeaturedGame = (now: Date = new Date()): GameDefinition | undefined =>
  featuredGameFrom(AVAILABLE_GAMES, now);
