import { fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { GamesConfigProvider } from '@/contexts/GamesConfigContext';
import { AdminWordleConfig } from '@/components/AdminWordleConfig';
import { AdminConnectionsManage } from '@/pages/AdminConnectionsManage';
import { AdminTriviaDropManage } from '@/pages/AdminTriviaDropManage';

/**
 * Paging the session-history lists behind the games manage screens.
 *
 * Three properties are pinned here, each failing in a way the old fetch-everything
 * lists could not have caught:
 *
 * * **History grows downwards.** Each list holds one contiguous window with the server
 *   total beside it, and "Show more" appends the next page beneath the sessions already
 *   on screen -- the request carries the offset of what is shown.
 * * **An update re-reads from the top.** Ending the current session opens a brand-new
 *   one, so the refresh that follows an Update answers the first page again, where the
 *   new session belongs.
 * * **Trivia reads working set and history apart.** Drafts and scheduled rows are a small
 *   working read; ended and cancelled rows page as history. One unbounded read split by
 *   hand would put the working set behind however many ended questions exist.
 */

const api = vi.hoisted(() => ({
  getAdminWordleGames: vi.fn(),
  getAdminWordleGame: vi.fn(),
  getCurrentAdminWordleSession: vi.fn(),
  getScheduledWordleWords: vi.fn(),
  createWordleGame: vi.fn(),
  getAdminConnectionsGames: vi.fn(),
  getCurrentAdminConnectionsSession: vi.fn(),
  getScheduledConnectionsPuzzles: vi.fn(),
  createConnectionsGame: vi.fn(),
  getAdminTriviaQuestions: vi.fn(),
  getCurrentAdminTriviaQuestion: vi.fn(),
  createTriviaQuestion: vi.fn(),
  getGameConfigs: vi.fn(),
  updateGameConfig: vi.fn(),
  getFeaturedGames: vi.fn(),
  replaceFeaturedGame: vi.fn(),
}));

const toast = vi.hoisted(() => ({
  info: vi.fn(),
  error: vi.fn(),
  success: vi.fn(),
  warning: vi.fn(),
}));

vi.mock('@/services/api', () => api);
vi.mock('sonner', () => ({ toast, Toaster: () => null }));

const wordleSession = (id: string, gameDate: string) => ({
  id,
  game_date: gameDate,
  mode: 'custom',
  answer: 'APPLE',
  status: 'ended',
  created_by: 'adminuser',
});

const connectionsSession = (id: string, gameDate: string) => ({
  id,
  game_date: gameDate,
  status: 'ended',
  groups: [
    ['a1', 'a2', 'a3', 'a4'],
    ['b1', 'b2', 'b3', 'b4'],
    ['c1', 'c2', 'c3', 'c4'],
    ['d1', 'd2', 'd3', 'd4'],
  ],
  created_by: 'adminuser',
});

const triviaQuestion = (id: string, text: string, status: string) => ({
  id,
  question_text: text,
  status,
  answer_mode: 'single',
  options: [
    { option_index: 0, option_text: 'Mars', is_correct: false },
    { option_index: 1, option_text: 'Venus', is_correct: true },
  ],
  correct_indexes: [1],
});

function renderWithProviders(ui: React.ReactElement) {
  return render(
    <GamesConfigProvider>
      <MemoryRouter>{ui}</MemoryRouter>
    </GamesConfigProvider>,
  );
}

/** Opens the collapsed session history both config cards hide it behind. */
async function showHistory() {
  fireEvent.click(screen.getByRole('button', { name: /show session history/i }));
}

describe('AdminGameHistory: paged session history', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getCurrentAdminWordleSession.mockRejectedValue({ response: { status: 404 } });
    api.getScheduledWordleWords.mockResolvedValue([]);
    api.getGameConfigs.mockResolvedValue([]);
    api.getFeaturedGames.mockResolvedValue([]);
    api.getCurrentAdminConnectionsSession.mockRejectedValue({ response: { status: 404 } });
    api.getScheduledConnectionsPuzzles.mockResolvedValue([]);
    api.getCurrentAdminTriviaQuestion.mockRejectedValue({ response: { status: 404 } });
  });

  it('grows the wordle history downwards with the server total beside it', async () => {
    const sessions = Array.from({ length: 21 }, (_, index) =>
      wordleSession(`w${index}`, `2026-01-${String(index + 1).padStart(2, '0')}`),
    );
    api.getAdminWordleGames.mockImplementation(async (params: Record<string, unknown> = {}) => {
      const skip = (params.skip as number | undefined) ?? 0;
      const limit = (params.limit as number | undefined) ?? 20;
      return {
        games: sessions.slice(skip, skip + limit),
        total: sessions.length,
        skip,
        limit,
      };
    });
    renderWithProviders(<AdminWordleConfig />);
    await screen.findByText(/21 sessions/);
    await showHistory();

    expect(api.getAdminWordleGames).toHaveBeenCalledWith(
      expect.objectContaining({ skip: 0, limit: 20 }),
    );
    expect(screen.getByRole('button', { name: 'Show more sessions' })).toHaveTextContent(
      'Show more (1 remaining)',
    );

    fireEvent.click(screen.getByRole('button', { name: 'Show more sessions' }));
    await screen.findByText(/2026-01-21/);

    const skips = api.getAdminWordleGames.mock.calls.map((call) => call[0]?.skip ?? 0);
    expect(skips).toContain(20);
    expect(
      screen.queryByRole('button', { name: 'Show more sessions' }),
    ).not.toBeInTheDocument();
  });

  it('re-reads from the top after a wordle update so the new session leads', async () => {
    let sessions = [wordleSession('w1', '2026-01-01')];
    api.getAdminWordleGames.mockImplementation(async (params: Record<string, unknown> = {}) => {
      const skip = (params.skip as number | undefined) ?? 0;
      const limit = (params.limit as number | undefined) ?? 20;
      return { games: sessions.slice(skip, skip + limit), total: sessions.length, skip, limit };
    });
    api.createWordleGame.mockImplementation(async () => {
      sessions = [wordleSession('w2', '2026-01-02'), ...sessions];
      return sessions[0];
    });
    renderWithProviders(<AdminWordleConfig />);
    await screen.findByText(/1 sessions/);

    fireEvent.change(screen.getByLabelText('Answer'), { target: { value: 'APPLE' } });
    fireEvent.click(screen.getByRole('button', { name: 'Update session' }));
    await screen.findByText(/2 sessions/);

    // The refresh answered page one again, where the brand-new session belongs.
    const skips = api.getAdminWordleGames.mock.calls.map((call) => call[0]?.skip ?? 0);
    expect(skips[skips.length - 1]).toBe(0);
    await showHistory();
    expect(screen.getByText('2026-01-02')).toBeInTheDocument();
  });

  it('grows the connections history downwards the same way', async () => {
    const sessions = Array.from({ length: 21 }, (_, index) =>
      connectionsSession(`c${index}`, `2026-02-${String(index + 1).padStart(2, '0')}`),
    );
    api.getAdminConnectionsGames.mockImplementation(
      async (params: Record<string, unknown> = {}) => {
        const skip = (params.skip as number | undefined) ?? 0;
        const limit = (params.limit as number | undefined) ?? 20;
        return {
          games: sessions.slice(skip, skip + limit),
          total: sessions.length,
          skip,
          limit,
        };
      },
    );
    renderWithProviders(<AdminConnectionsManage />);
    await screen.findByText(/21 sessions/);
    await showHistory();

    fireEvent.click(screen.getByRole('button', { name: 'Show more sessions' }));
    await screen.findByText(/2026-02-21/);

    const skips = api.getAdminConnectionsGames.mock.calls.map((call) => call[0]?.skip ?? 0);
    expect(skips).toContain(20);
    expect(
      screen.queryByRole('button', { name: 'Show more sessions' }),
    ).not.toBeInTheDocument();
  });

  it('reads the trivia working set apart from its paged history', async () => {
    const working = [
      triviaQuestion('d1', 'draft question', 'draft'),
      triviaQuestion('s1', 'scheduled question', 'scheduled'),
    ];
    const history = Array.from({ length: 21 }, (_, index) =>
      triviaQuestion(`h${index}`, `ended question ${index}`, 'ended'),
    );
    api.getAdminTriviaQuestions.mockImplementation(
      async (params: Record<string, unknown> = {}) => {
        const status = params.status as string | undefined;
        const skip = (params.skip as number | undefined) ?? 0;
        const limit = (params.limit as number | undefined) ?? 20;
        // The working read names its statuses; the history read names ended and cancelled.
        const rows = status?.includes('draft') ? working : history;
        return { questions: rows.slice(skip, skip + limit), total: rows.length, skip, limit };
      },
    );
    renderWithProviders(<AdminTriviaDropManage />);
    await screen.findByText('draft question');

    // Drafts arrive whole on the working read, whatever the history holds.
    expect(api.getAdminTriviaQuestions).toHaveBeenCalledWith(
      expect.objectContaining({ status: expect.stringContaining('draft') }),
    );
    expect(screen.getByText('scheduled question')).toBeInTheDocument();

    // History pages on its own read, newest first, twenty at a time.
    expect(api.getAdminTriviaQuestions).toHaveBeenCalledWith(
      expect.objectContaining({ status: expect.stringContaining('ended') }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Show history' }));
    expect(screen.getByText('ended question 0')).toBeInTheDocument();
    expect(screen.queryByText('ended question 20')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Show more history' }));
    await screen.findByText('ended question 20');

    const skips = api.getAdminTriviaQuestions.mock.calls.map((call) => call[0]?.skip ?? 0);
    expect(skips).toContain(20);
    expect(
      screen.queryByRole('button', { name: 'Show more history' }),
    ).not.toBeInTheDocument();
  });

  it('still renders legacy bare-array answers with no pager', async () => {
    api.getAdminWordleGames.mockResolvedValue([wordleSession('w1', '2026-01-01')]);
    renderWithProviders(<AdminWordleConfig />);
    await screen.findByText(/1 sessions/);
    await showHistory();

    expect(screen.getByText('2026-01-01')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Show more sessions' })).not.toBeInTheDocument();
  });
});
