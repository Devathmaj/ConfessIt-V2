import { fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { GamesLeaderboardPage } from '@/pages/GamesLeaderboardPage';
import { GameCategorySection } from '@/components/games/GameCategorySection';
import {
  getGameCategories,
  getGameCategoryBoard,
  getGameCategoryMe,
  getIndividualLeaderboard,
  getCouplesLeaderboard,
  getMyLeagueStanding,
} from '@/services/api';

vi.mock('@/services/api', () => ({
  getIndividualLeaderboard: vi.fn(),
  getCouplesLeaderboard: vi.fn(),
  getGameLeaderboard: vi.fn(),
  getMyLeagueStanding: vi.fn(),
  getGameCategories: vi.fn(),
  getGameCategoryBoard: vi.fn(),
  getGameCategoryMe: vi.fn(),
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

const categories = [
  { id: 'performance', label: 'Performance', description: 'Cross-session.', scope: 'period', is_default: true },
  { id: 'most_solved', label: 'Most Solved', description: 'Most solves.', scope: 'period', is_default: false },
  { id: 'fastest_solve', label: 'Fastest Solve', description: 'All-time record.', scope: 'record', is_default: false },
];

const mostSolvedBoard = {
  game_id: 'wordle',
  category: 'most_solved',
  scope: 'period',
  period: 'weekly',
  anchor: null,
  total: 2,
  entries: [
    {
      rank: 1,
      user_id: 'u1',
      display_name: 'Ada',
      member_a_id: null,
      member_b_id: null,
      member_a_name: null,
      member_b_name: null,
      metrics: { solved: 3, opportunities: 4, solve_rate: 0.75, avg_guesses: 2.5 },
      records: [],
    },
    {
      rank: 2,
      user_id: 'u2',
      display_name: 'Bo',
      member_a_id: null,
      member_b_id: null,
      member_a_name: null,
      member_b_name: null,
      metrics: { solved: 1, opportunities: 1, solve_rate: 1, avg_guesses: 2 },
      records: [{ word: 'cigar', guesses: 2, duration_s: 45, date: '2026-10-01', opportunity_id: 'o1' }],
    },
  ],
};

const renderSection = () =>
  render(
    <MemoryRouter>
      <GameCategorySection gameId="wordle" gameName="Wordle" period="weekly" anchor="2026-10-12" />
    </MemoryRouter>,
  );

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getGameCategories).mockResolvedValue(categories);
  vi.mocked(getGameCategoryBoard).mockResolvedValue(mostSolvedBoard);
  vi.mocked(getGameCategoryMe).mockResolvedValue({
    game_id: 'wordle',
    category: 'most_solved',
    scope: 'period',
    entries: [mostSolvedBoard.entries[0]],
  });
  vi.mocked(getIndividualLeaderboard).mockResolvedValue({
    competition_type: 'individual',
    period: 'weekly',
    game_id: null,
    opportunity_count: 0,
    total: 0,
    entries: [],
  });
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
    entries: [],
  });
});

describe('GameCategorySection', () => {
  it('shows the category tabs and the default board with metric columns', async () => {
    renderSection();

    expect(await screen.findByRole('tab', { name: /performance/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /most solved/i })).toBeInTheDocument();
    // Metric columns come from the category config, not from points.
    // (Labels render in both desktop and mobile layouts, so expect several.)
    expect(await screen.findAllByText('Solved')).not.toHaveLength(0);
    expect(screen.getByText('Ada')).toBeInTheDocument();
    expect(getGameCategoryBoard).toHaveBeenCalledWith(
      'wordle',
      'performance',
      expect.objectContaining({ period: 'weekly' }),
    );
  });

  it('switching tabs refetches with the new category', async () => {
    renderSection();
    await screen.findByText('Ada');

    fireEvent.click(screen.getByRole('tab', { name: /most solved/i }));

    expect(await screen.findByText('Bo')).toBeInTheDocument();
    expect(getGameCategoryBoard).toHaveBeenCalledWith(
      'wordle',
      'most_solved',
      expect.objectContaining({ period: 'weekly', anchor: '2026-10-12' }),
    );
  });

  it('record scope drops the rolling window and says all-time', async () => {
    vi.mocked(getGameCategoryBoard).mockResolvedValue({
      ...mostSolvedBoard,
      category: 'fastest_solve',
      scope: 'record',
      period: 'all_time',
    });
    renderSection();
    await screen.findByText('Ada');

    fireEvent.click(screen.getByRole('tab', { name: /fastest solve/i }));

    expect(await screen.findByText(/all-time record/i)).toBeInTheDocument();
    expect(getGameCategoryBoard).toHaveBeenCalledWith('wordle', 'fastest_solve', { skip: 0, limit: 25 });
  });

  it('shows the viewer standing and expandable records', async () => {
    renderSection();
    await screen.findByText('Ada');

    expect(screen.getByText(/you rank #1/i)).toBeInTheDocument();

    const expanders = screen.getAllByRole('button', { name: /records for/i });
    expect(expanders).toHaveLength(1);
    fireEvent.click(expanders[0]);
    expect(screen.getByText('cigar')).toBeInTheDocument();
    // Internal identifiers stay off screen.
    expect(screen.queryByText('o1')).not.toBeInTheDocument();
    expect(screen.queryByText(/opportunity id/i)).not.toBeInTheDocument();
  });

  it('redacts words that match a live puzzle', async () => {
    vi.mocked(getGameCategoryBoard).mockResolvedValue({
      ...mostSolvedBoard,
      entries: [
        {
          ...mostSolvedBoard.entries[1],
          records: [{ word: null, guesses: 2, duration_s: 45, date: '2026-10-01', opportunity_id: 'o1' }],
        },
      ],
    });
    renderSection();
    await screen.findByText('Bo');

    fireEvent.click(screen.getByRole('button', { name: /records for/i }));
    expect(screen.getByText(/hidden \(live puzzle\)/i)).toBeInTheDocument();
  });

  it('renders couple identities as pairs', async () => {
    vi.mocked(getGameCategories).mockResolvedValue([
      { id: 'score', label: 'Score', description: 'Accuracy.', scope: 'period', is_default: true },
    ]);
    vi.mocked(getGameCategoryBoard).mockResolvedValue({
      game_id: 'couple-questionnaire',
      category: 'score',
      scope: 'period',
      period: 'weekly',
      anchor: null,
      total: 1,
      entries: [
        {
          rank: 1,
          user_id: null,
          display_name: null,
          member_a_id: 'a',
          member_b_id: 'b',
          member_a_name: 'Al',
          member_b_name: 'Bo',
          metrics: { score: 90, correct: 9, total: 10 },
          records: [],
        },
      ],
    });
    render(
      <MemoryRouter>
        <GameCategorySection gameId="couple-questionnaire" gameName="Couple Questionnaire" period="weekly" anchor="2026-10-12" />
      </MemoryRouter>,
    );

    expect(await screen.findByText('Al & Bo')).toBeInTheDocument();
    expect(screen.getAllByText('90').length).toBeGreaterThan(0);
  });
});

describe('leaderboard page integration', () => {
  it('selecting a game swaps the points board for category views', async () => {
    render(
      <MemoryRouter initialEntries={['/games/leaderboard']}>
        <Routes>
          <Route path="/games/leaderboard" element={<GamesLeaderboardPage />} />
        </Routes>
      </MemoryRouter>,
    );

    const trigger = await screen.findByLabelText(/filter by game/i);
    fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false, pointerType: 'mouse' });
    const option = await screen.findByRole('option', { name: 'Wordle' });
    fireEvent.click(option);

    expect(await screen.findByRole('tablist', { name: /wordle leaderboard views/i })).toBeInTheDocument();
    expect(getGameCategories).toHaveBeenCalledWith('wordle');
  });
});
