import type { LucideIcon } from 'lucide-react';
import {
  Gift,
  Heart,
  MessageCircle,
  Scale,
  Smile,
  Star,
  Timer,
} from 'lucide-react';

/**
 * The catalogue of games the Games section can show.
 *
 * Everything the Games UI knows about a game lives here so that adding a title later is a
 * data change rather than a UI change: the dashboard quick-access grid, the selection page
 * grid, the featured picker and the leaderboard's game filter all read from the same list.
 *
 * This is prototype data. Nothing here is fetched, so the shapes are deliberately small and
 * close to what a future API would return -- swapping the constants for a request should not
 * require touching any component that renders them.
 */

export type GameCategoryId =
  | 'icebreaker'
  | 'party'
  | 'wordplay'
  | 'memory'
  | 'trivia';

export type GameDifficulty = 'easy' | 'medium' | 'hard';

/**
 * `available` games are selectable today (the game screen itself does not exist yet, so the
 * Play action is still a placeholder toast). `coming-soon` games render in a muted state so
 * the catalogue can advertise titles that are not ready without the page looking broken.
 */
export type GameStatus = 'available' | 'coming-soon';

export interface GameDefinition {
  id: string;
  name: string;
  tagline: string;
  category: GameCategoryId;
  difficulty: GameDifficulty;
  /** Free-form because lobby sizes are a product decision, not a number the UI math needs. */
  players: string;
  /** Free-form average run time, e.g. "~3 min". */
  duration: string;
  icon: LucideIcon;
  /** Tailwind gradient class used for the game's icon tile. */
  accent: string;
  status: GameStatus;
}

export interface GameCategory {
  id: GameCategoryId;
  label: string;
}

export const GAME_CATEGORIES: GameCategory[] = [
  { id: 'icebreaker', label: 'Icebreakers' },
  { id: 'party', label: 'Party' },
  { id: 'wordplay', label: 'Wordplay' },
  { id: 'memory', label: 'Memory' },
  { id: 'trivia', label: 'Trivia' },
];

export const AVAILABLE_GAMES: GameDefinition[] = [
  {
    id: 'truth-dare',
    name: 'Truth or Dare',
    tagline: 'The classic, with a romantic twist every round.',
    category: 'party',
    difficulty: 'easy',
    players: '2–6 players',
    duration: '~5 min',
    icon: Heart,
    accent: 'bg-gradient-romantic',
    status: 'available',
  },
  {
    id: 'compatibility-quiz',
    name: 'Compatibility Quiz',
    tagline: 'Answer together and see how well you match.',
    category: 'trivia',
    difficulty: 'medium',
    players: '2 players',
    duration: '~4 min',
    icon: Star,
    accent: 'bg-gradient-love',
    status: 'available',
  },
  {
    id: 'love-letter-builder',
    name: 'Love Letter Builder',
    tagline: 'Take turns building one letter, line by line.',
    category: 'wordplay',
    difficulty: 'easy',
    players: '2 players',
    duration: '~6 min',
    icon: MessageCircle,
    accent: 'bg-gradient-passion',
    status: 'available',
  },
  {
    id: 'hearts-memory-match',
    name: 'Hearts Memory Match',
    tagline: 'Flip and find the pairs before the timer runs out.',
    category: 'memory',
    difficulty: 'hard',
    players: 'Solo',
    duration: '~3 min',
    icon: Gift,
    accent: 'bg-gradient-hero',
    status: 'available',
  },
  {
    id: 'would-you-rather',
    name: 'Would You Rather?',
    tagline: 'Two awkward choices, one winner per round.',
    category: 'icebreaker',
    difficulty: 'easy',
    players: '2–4 players',
    duration: '~4 min',
    icon: Scale,
    accent: 'bg-gradient-love',
    status: 'available',
  },
  {
    id: 'emoji-decoded',
    name: 'Emoji Decoded',
    tagline: 'Read the emoji chain, guess the phrase.',
    category: 'trivia',
    difficulty: 'medium',
    players: 'Solo',
    duration: '~2 min',
    icon: Smile,
    accent: 'bg-gradient-romantic',
    status: 'coming-soon',
  },
  {
    id: 'sweet-nothings-sprint',
    name: 'Sweet Nothings Sprint',
    tagline: 'Type the most compliments before the clock hits zero.',
    category: 'wordplay',
    difficulty: 'hard',
    players: 'Solo',
    duration: '~90 sec',
    icon: Timer,
    accent: 'bg-gradient-passion',
    status: 'coming-soon',
  },
];

export const getGameById = (gameId: string): GameDefinition | undefined =>
  AVAILABLE_GAMES.find((game) => game.id === gameId);

export const categoryLabel = (category: GameCategoryId): string =>
  GAME_CATEGORIES.find((entry) => entry.id === category)?.label ?? category;

/**
 * Picks "today's featured game" the same way every time the page is opened on the same day:
 * the day number rotates through the catalogue. A future backend can replace this with a
 * stored daily pick and callers will not notice, because the return shape is unchanged.
 */
export const getFeaturedGame = (now: Date = new Date()): GameDefinition => {
  const startOfYear = new Date(now.getFullYear(), 0, 0);
  const dayOfYear = Math.floor((now.getTime() - startOfYear.getTime()) / 86_400_000);
  const index = dayOfYear % AVAILABLE_GAMES.length;
  return AVAILABLE_GAMES[index];
};
