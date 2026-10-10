import { fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext, type User } from '@/contexts/AuthContext';
import { GamesDashboardPage } from '@/pages/GamesDashboardPage';
import { GamesLeaderboardPage } from '@/pages/GamesLeaderboardPage';
import {
  getCouplesLeaderboard,
  getFeaturedToday,
  getIndividualLeaderboard,
  getMyLeagueStanding,
  getPlayerGameSummary,
} from '@/services/api';

vi.mock('@/services/api', () => ({
  getIndividualLeaderboard: vi.fn(),
  getCouplesLeaderboard: vi.fn(),
  getGameLeaderboard: vi.fn(),
  getMyLeagueStanding: vi.fn(),
  getPlayerGameSummary: vi.fn(),
  getFeaturedToday: vi.fn(),
}));

vi.mock('sonner', () => ({
  toast: Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
  }),
  Toaster: () => null,
}));

const individualBoard = () => ({
  competition_type: 'individual' as const,
  period: 'weekly',
  game_id: null,
  opportunity_count: 4,
  total: 4,
  entries: [
    {
      rank: 1,
      user_id: 'user-1',
      display_name: 'Ada',
      score: 150,
      native: { solved: true, attempts: 3 },
      is_featured: true,
      opportunities_played: 4,
      per_game: { wordle: 100, anagram: 50 },
    },
    {
      rank: 2,
      user_id: 'user-2',
      display_name: 'Bo',
      score: 75,
      native: { solved: true, attempts: 4 },
      is_featured: false,
      opportunities_played: 3,
    },
    {
      rank: 3,
      user_id: 'user-3',
      display_name: 'Cy',
      score: 50,
      native: { solved: false, attempts: 6 },
      is_featured: false,
      opportunities_played: 2,
    },
    {
      rank: 4,
      user_id: 'user-4',
      display_name: 'Di',
      score: 12.5,
      native: { solved: false, attempts: 6 },
      is_featured: true,
      opportunities_played: 1,
    },
  ],
});

const authValue = {
  user: { id: 'user-1', name: 'Ada Lovelace', regno: 'CS-9' } as User,
  logout: vi.fn(),
  isAuthenticated: true,
  isLoading: false,
  token: 't' as string | null,
  verifyMagicToken: vi.fn(),
  fetchAndSetUser: vi.fn(),
  can: () => false,
  signInPath: '/login',
};

const renderLeaderboard = () =>
  render(
    <MemoryRouter initialEntries={['/games/leaderboard']}>
      <Routes>
        <Route path="/games/leaderboard" element={<GamesLeaderboardPage />} />
      </Routes>
    </MemoryRouter>,
  );

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getIndividualLeaderboard).mockResolvedValue(individualBoard());
  vi.mocked(getCouplesLeaderboard).mockResolvedValue({
    competition_type: 'couple',
    period: 'weekly',
    game_id: null,
    opportunity_count: 0,
    total: 0,
    entries: [],
  });
  vi.mocked(getMyLeagueStanding).mockResolvedValue({
    competition_type: 'individual',
    period: 'weekly',
    entries: [individualBoard().entries[0]],
  });
  vi.mocked(getPlayerGameSummary).mockResolvedValue({
    games_played: 0,
    current_streak_days: 0,
    longest_streak_days: 0,
    plays_this_week: 0,
    games_this_week: [],
    recent_plays: [],
  });
  vi.mocked(getFeaturedToday).mockResolvedValue({
    game_id: null,
    game_date: null,
    server_date: new Date().toISOString(),
  });
});

describe('GamesLeaderboardPage: the server-backed board', () => {
  it('renders the standings the league board answers', async () => {
    renderLeaderboard();

    expect(await screen.findByText('Ada')).toBeInTheDocument();
    expect(screen.getByText('Bo')).toBeInTheDocument();
    // The fourth row sits below the podium, where the featured badge lives.
    expect(screen.getByText('Di')).toBeInTheDocument();
    expect(screen.getByText('2× featured')).toBeInTheDocument();
    expect(screen.getByText('#1')).toBeInTheDocument();
    expect(getIndividualLeaderboard).toHaveBeenCalledWith(
      expect.objectContaining({ period: 'weekly', skip: 0 }),
    );
  });

  it('shows the empty state when nothing has scored', async () => {
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
    renderLeaderboard();

    expect(await screen.findByText('No standings yet')).toBeInTheDocument();
  });

  it('switches competitions and re-queries the server', async () => {
    vi.mocked(getCouplesLeaderboard).mockResolvedValue({
      competition_type: 'couple',
      period: 'weekly',
      game_id: null,
      opportunity_count: 1,
      total: 1,
      entries: [
        {
          rank: 1,
          member_a_id: 'a',
          member_b_id: 'b',
          member_a_name: 'Al',
          member_b_name: 'Bo',
          score: 100,
          native: {},
          is_featured: false,
          opportunities_played: 1,
        },
      ],
    });
    renderLeaderboard();
    await screen.findByText('Ada');

    fireEvent.click(screen.getByRole('button', { name: 'Couples' }));

    expect(await screen.findByText('Al & Bo')).toBeInTheDocument();
    expect(getCouplesLeaderboard).toHaveBeenCalledWith(
      expect.objectContaining({ period: 'weekly' }),
    );
  });

  it('keeps a failed fetch on screen with a retry', async () => {
    vi.mocked(getIndividualLeaderboard).mockRejectedValue(new Error('network'));
    renderLeaderboard();

    expect(await screen.findByText('Standings are unavailable')).toBeInTheDocument();
    vi.mocked(getIndividualLeaderboard).mockResolvedValue(individualBoard());
    fireEvent.click(screen.getByRole('button', { name: /try again/i }));
    expect(await screen.findByText('Ada')).toBeInTheDocument();
  });
});

describe('GamesDashboardPage: the league teaser', () => {
  it('shows weekly points, rank and leaders from the server', async () => {
    render(
      <AuthContext.Provider value={authValue}>
        <MemoryRouter initialEntries={['/games']}>
          <Routes>
            <Route path="/games" element={<GamesDashboardPage />} />
          </Routes>
        </MemoryRouter>
      </AuthContext.Provider>,
    );

    expect(await screen.findByText('Ada')).toBeInTheDocument();
    expect(screen.getByText('#1')).toBeInTheDocument();
    const board = within(screen.getByText('Top players').closest('[data-slot="card"]')!);
    expect(board.getByText('Bo')).toBeInTheDocument();
  });
});
