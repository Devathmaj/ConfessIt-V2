import { render, screen, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext, type User } from '@/contexts/AuthContext';
import { GamesDashboardPage } from '@/pages/GamesDashboardPage';
import {
  getFeaturedToday,
  getIndividualLeaderboard,
  getMyLeagueStanding,
  getPlayerGameSummary,
  type PlayerGameSummary,
} from '@/services/api';

/**
 * The Games dashboard's personal half: stats, recent rounds, the weekly
 * challenge and today's spotlight all read real data.
 *
 * The dashboard used to render "—" streaks, an empty history regardless of
 * what was played, a weekly challenge that was permanently "coming soon",
 * and a locally rotated game as "today's featured" even when the server had
 * nothing in the spotlight. These tests pin the replacements: every figure
 * comes from `getPlayerGameSummary`, a day with no featured pick says so,
 * and a failed request reads as unavailable rather than as "nothing yet".
 */

vi.mock('@/services/api', () => ({
  getIndividualLeaderboard: vi.fn(),
  getCouplesLeaderboard: vi.fn(),
  getGameLeaderboard: vi.fn(),
  getMyLeagueStanding: vi.fn(),
  getPlayerGameSummary: vi.fn(),
  getFeaturedToday: vi.fn(),
}));

const authValue = {
  user: { id: 'me', name: 'Ada Lovelace', regno: 'CS-9' } as User,
  logout: vi.fn(),
  isAuthenticated: true,
  isLoading: false,
  token: 't' as string | null,
  verifyMagicToken: vi.fn(),
  fetchAndSetUser: vi.fn(),
  can: () => false,
  signInPath: '/login',
};

const renderDashboard = () =>
  render(
    <AuthContext.Provider value={authValue}>
      <MemoryRouter initialEntries={['/games']}>
        <Routes>
          <Route path="/games" element={<GamesDashboardPage />} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );

/** The stat tile carrying `label`, so "3 days" can be asserted in place. */
const statCard = (label: string) =>
  screen.getByText(label).closest('[data-slot="card"]') as HTMLElement;

const summaryWith = (overrides: Partial<PlayerGameSummary>): PlayerGameSummary => ({
  games_played: 0,
  current_streak_days: 0,
  longest_streak_days: 0,
  plays_this_week: 0,
  games_this_week: [],
  recent_plays: [],
  ...overrides,
});

const hoursAgo = (hours: number) => new Date(Date.now() - hours * 3_600_000).toISOString();

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getIndividualLeaderboard).mockResolvedValue({
    competition_type: 'individual',
    period: 'weekly',
    game_id: null,
    opportunity_count: 0,
    total: 0,
    entries: [],
  });
  vi.mocked(getMyLeagueStanding).mockResolvedValue({
    competition_type: 'individual',
    period: 'weekly',
    entries: [],
  });
  vi.mocked(getPlayerGameSummary).mockResolvedValue(summaryWith({}));
  vi.mocked(getFeaturedToday).mockResolvedValue({
    game_id: null,
    game_date: null,
    server_date: new Date().toISOString(),
  });
});

describe('GamesDashboardPage: personal stats and history', () => {
  it('renders the streak, rounds and history the server answers', async () => {
    vi.mocked(getPlayerGameSummary).mockResolvedValue(
      summaryWith({
        games_played: 7,
        current_streak_days: 3,
        longest_streak_days: 5,
        plays_this_week: 2,
        games_this_week: ['anagram', 'wordle'],
        recent_plays: [
          {
            game_id: 'wordle',
            played_at: hoursAgo(0),
            summary: 'Solved in 3',
            score: 3,
            score_label: 'guesses',
          },
          {
            game_id: 'anagram',
            played_at: hoursAgo(26),
            summary: 'Not solved',
            score: 5,
            score_label: 'guesses',
          },
        ],
      }),
    );
    renderDashboard();

    expect(await screen.findByText('3 days')).toBeInTheDocument();
    expect(within(statCard('Current streak')).getByText('Longest: 5 days')).toBeInTheDocument();

    const gamesCard = statCard('Games played');
    expect(within(gamesCard).getByText('7')).toBeInTheDocument();
    expect(within(gamesCard).getByText('2 rounds this week')).toBeInTheDocument();

    const history = screen.getByText('Recently played').closest('[data-slot="card"]') as HTMLElement;
    expect(within(history).getByText('Wordle')).toBeInTheDocument();
    expect(within(history).getByText(/Solved in 3/)).toBeInTheDocument();
    expect(within(history).getByText(/Not solved/)).toBeInTheDocument();
    expect(within(history).getByText('1d ago · Not solved')).toBeInTheDocument();
  });

  it('keeps the honest empty state when there is no history yet', async () => {
    renderDashboard();

    expect(await screen.findByText('No games played yet')).toBeInTheDocument();
    expect(within(statCard('Current streak')).getByText('—')).toBeInTheDocument();
    expect(within(statCard('Games played')).getByText('0')).toBeInTheDocument();
    expect(
      within(statCard('Games played')).getByText('No rounds yet — jump into a game'),
    ).toBeInTheDocument();
  });

  it('reads a failed history as unavailable, not as an empty history', async () => {
    vi.mocked(getPlayerGameSummary).mockRejectedValue(new Error('network'));
    renderDashboard();

    expect(
      await screen.findByText('Play history is unavailable right now'),
    ).toBeInTheDocument();
    expect(screen.queryByText('No games played yet')).not.toBeInTheDocument();
  });
});

describe('GamesDashboardPage: the weekly challenge', () => {
  it('tracks this week\'s distinct games against the target', async () => {
    vi.mocked(getPlayerGameSummary).mockResolvedValue(
      summaryWith({ plays_this_week: 4, games_this_week: ['anagram', 'wordle'] }),
    );
    renderDashboard();

    expect(
      await screen.findByText('Play 3 different games this week'),
    ).toBeInTheDocument();
    expect(await screen.findByText('2 of 3 games')).toBeInTheDocument();
    expect(screen.getByText('1 to go')).toBeInTheDocument();
  });

  it('marks the challenge complete once three games are played', async () => {
    vi.mocked(getPlayerGameSummary).mockResolvedValue(
      summaryWith({
        plays_this_week: 3,
        games_this_week: ['anagram', 'trivia-drop', 'wordle'],
      }),
    );
    renderDashboard();

    expect(await screen.findByText('Challenge complete')).toBeInTheDocument();
    expect(screen.getByText('3 of 3 games')).toBeInTheDocument();
  });
});

describe('GamesDashboardPage: today\'s featured', () => {
  it('shows the server\'s pick', async () => {
    vi.mocked(getFeaturedToday).mockResolvedValue({
      game_id: 'trivia-drop',
      game_date: new Date().toISOString().slice(0, 10),
      server_date: new Date().toISOString(),
    });
    renderDashboard();

    const spotlight = (await screen.findByText('Today\'s featured')).closest(
      '[data-slot="card"]',
    ) as HTMLElement;
    expect(await within(spotlight).findByText('Trivia Drop')).toBeInTheDocument();
    expect(within(spotlight).getByRole('link', { name: /view featured/i })).toHaveAttribute(
      'href',
      '/games/featured',
    );
  });

  it('says nothing is featured when the server has no pick -- no rotated stand-in', async () => {
    renderDashboard();

    expect(await screen.findByText('No featured game today')).toBeInTheDocument();
    // The spotlit card links to the library instead of naming a game.
    const spotlight = screen
      .getByText('No featured game today')
      .closest('[data-slot="card"]') as HTMLElement;
    expect(within(spotlight).getByRole('link', { name: /browse games/i })).toHaveAttribute(
      'href',
      '/games/select',
    );
  });

  it('tells the player the spotlight failed to load rather than claiming none exists', async () => {
    vi.mocked(getFeaturedToday).mockRejectedValue(new Error('offline'));
    renderDashboard();

    expect(
      await screen.findByText('The spotlight could not be loaded'),
    ).toBeInTheDocument();
    expect(screen.queryByText('No featured game today')).not.toBeInTheDocument();
  });
});
