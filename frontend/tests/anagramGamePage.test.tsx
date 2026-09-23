import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within, act } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useNavigate, type NavigateFunction } from 'react-router-dom';
import type { ContextType } from 'react';
import { toast } from 'sonner';

import { AnagramGamePage } from '@/pages/AnagramGamePage';
import { AuthContext } from '@/contexts/AuthContext';
import { GamesConfigProvider } from '@/contexts/GamesConfigContext';
import type { Capability } from '@/lib/capabilities';
import type { AnagramCurrentGame, AnagramParticipation } from '@/services/api';

/**
 * The daily Anagram screen against its API boundary.
 *
 * The contract pinned here is the one the player experiences: the scramble the server
 * deals is the tiles on screen, rearranging never phones home until SUBMIT, a guess goes
 * to the server as one uppercase arrangement, and a word the server refuses is refused
 * *visibly* -- the tiles stay where the player left them. The screen keeps the full local
 * state from `GET /games/anagram` (participation may be null for a player who has not
 * pressed REVEAL WORD), and a successful start or guess replaces that state with the
 * POST's response, so the board a returning player opens is always the board they left.
 *
 * The clock is the other half: it opens on reveal, banks on pause, and never runs while
 * the page is hidden or the player is signed out -- all asserted through the API calls
 * the screen does and does not make, because the server owns every persisted number.
 */

vi.mock('@/services/api', () => ({
  getCurrentAnagram: vi.fn(),
  startAnagram: vi.fn(),
  pauseAnagram: vi.fn(),
  heartbeatAnagram: vi.fn(),
  submitAnagramGuess: vi.fn(),
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

const {
  getCurrentAnagram,
  startAnagram,
  pauseAnagram,
  heartbeatAnagram,
  submitAnagramGuess,
} = await import('@/services/api');

type AuthContextValue = ContextType<typeof AuthContext>;

const authValue = (isAuthenticated: boolean): AuthContextValue => ({
  user: isAuthenticated
    ? {
        id: 'player-1',
        name: 'Test Player',
        regno: 'TEST001',
        email: 'test@example.com',
      }
    : null,
  isAuthenticated,
  isLoading: false,
  token: isAuthenticated ? 'token' : null,
  logout: vi.fn(),
  verifyMagicToken: vi.fn(),
  fetchAndSetUser: vi.fn(),
  can: (_capability: Capability) => false,
  signInPath: '/login',
} as AuthContextValue);

const makeParticipation = (
  overrides: Partial<AnagramParticipation> = {},
): AnagramParticipation => ({
  anagram_game_id: 'game-1',
  game_date: '2026-10-07',
  status: 'in_progress',
  success: false,
  attempt_count: 0,
  max_attempts: 5,
  guesses: [],
  scrambled_word: 'HEPLTANE',
  active_time_seconds: 0,
  timer_started_at: null,
  timer_running: false,
  started_at: '2026-10-07T00:00:00Z',
  last_guess_at: null,
  completed_at: null,
  solved_at: null,
  answer: null,
  ...overrides,
});

const makeCurrentGame = (
  overrides: Partial<AnagramCurrentGame> = {},
): AnagramCurrentGame => ({
  game_id: 'game-1',
  game_date: '2026-10-07',
  participation: null,
  max_attempts: 5,
  ...overrides,
});

const renderGame = (isAuthenticated = true) =>
  render(
    <AuthContext.Provider value={authValue(isAuthenticated)}>
      <MemoryRouter initialEntries={['/games/anagram']}>
        <Routes>
          <Route path="/games/anagram" element={<AnagramGamePage />} />
          <Route path="/games" element={<div>games home</div>} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );

/** Set by `NavProbe` on every render, so a test can leave the page the way a link does. */
let navigateTo: NavigateFunction | null = null;

const NavProbe = () => {
  navigateTo = useNavigate();
  return null;
};

/** The letters currently on the board, left to right. */
const boardLetters = () =>
  within(screen.getByRole('grid', { name: 'Anagram tiles' }))
    .getAllByRole('gridcell')
    .map((cell) => cell.textContent);

/** Lay the tiles out left to right so pointer drags have somewhere to land. */
const mockTileGeometry = () => {
  const cells = within(screen.getByRole('grid', { name: 'Anagram tiles' })).getAllByRole(
    'gridcell',
  );
  cells.forEach((cell, index) => {
    cell.getBoundingClientRect = () =>
      ({
        left: index * 52,
        right: index * 52 + 48,
        top: 0,
        bottom: 56,
        width: 48,
        height: 56,
        x: index * 52,
        y: 0,
        toJSON: () => {},
      }) as DOMRect;
  });
  return cells;
};

const realVisibilityState = Object.getOwnPropertyDescriptor(document, 'visibilityState');

const setVisibility = (state: 'visible' | 'hidden') => {
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => state,
  });
};

describe('opening the game', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useRealTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    if (realVisibilityState) {
      Object.defineProperty(document, 'visibilityState', realVisibilityState);
    }
  });

  it('shows the puzzle is missing rather than a broken board', async () => {
    vi.mocked(getCurrentAnagram).mockResolvedValue(
      makeCurrentGame({ game_id: null, game_date: null, participation: null }),
    );

    renderGame();

    expect(await screen.findByText('No puzzle available today.')).toBeInTheDocument();
    expect(screen.queryByRole('grid', { name: 'Anagram tiles' })).not.toBeInTheDocument();
  });

  it('offers REVEAL WORD to a fresh player, with the clock at zero', async () => {
    vi.mocked(getCurrentAnagram).mockResolvedValue(makeCurrentGame());

    renderGame();

    expect(await screen.findByRole('button', { name: /reveal word/i })).toBeInTheDocument();
    expect(screen.queryByRole('grid', { name: 'Anagram tiles' })).not.toBeInTheDocument();
    expect(screen.getByTestId('play-clock')).toHaveTextContent('00:00');
    expect(screen.queryByTestId('attempt-label')).not.toBeInTheDocument();
  });

  it('reveals the dealt scramble and starts the clock on press', async () => {
    vi.mocked(getCurrentAnagram).mockResolvedValue(makeCurrentGame());
    const started = makeParticipation({
      timer_started_at: new Date().toISOString(),
      timer_running: true,
    });
    vi.mocked(startAnagram).mockResolvedValue(started);

    renderGame();
    fireEvent.click(await screen.findByRole('button', { name: /reveal word/i }));

    await waitFor(() => expect(startAnagram).toHaveBeenCalledWith('game-1'));
    expect(await screen.findByRole('grid', { name: 'Anagram tiles' })).toBeInTheDocument();
    // The server's scramble, uppercase, one tile per letter -- duplicates included.
    expect(boardLetters()).toEqual(['H', 'E', 'P', 'L', 'T', 'A', 'N', 'E']);
    expect(screen.getByTestId('attempt-label')).toHaveTextContent('Attempts remaining: 5 / 5');
  });

  it('restores the exact board a returning player left behind', async () => {
    vi.mocked(getCurrentAnagram).mockResolvedValue(
      makeCurrentGame({
        participation: makeParticipation({
          attempt_count: 2,
          guesses: ['PELHANTE', 'HLEPANTE'],
          active_time_seconds: 87,
          timer_started_at: null,
          timer_running: false,
        }),
      }),
    );

    renderGame();
    await screen.findByRole('grid', { name: 'Anagram tiles' });

    expect(boardLetters()).toEqual(['H', 'E', 'P', 'L', 'T', 'A', 'N', 'E']);
    expect(screen.getByTestId('attempt-label')).toHaveTextContent('Attempts remaining: 3 / 5');
    expect(screen.getByText('PELHANTE')).toBeInTheDocument();
    expect(screen.getByText('HLEPANTE')).toBeInTheDocument();
    expect(screen.getByTestId('play-clock')).toHaveTextContent('01:27');
    // Paused, so the control offers to resume rather than to submit.
    expect(screen.getByRole('button', { name: /resume/i })).toBeInTheDocument();
    expect(startAnagram).not.toHaveBeenCalled();
  });
});

describe('rearranging and guessing', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useRealTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    if (realVisibilityState) {
      Object.defineProperty(document, 'visibilityState', realVisibilityState);
    }
  });

  const renderStarted = async () => {
    vi.mocked(getCurrentAnagram).mockResolvedValue(makeCurrentGame());
    vi.mocked(startAnagram).mockResolvedValue(
      makeParticipation({ timer_started_at: new Date().toISOString(), timer_running: true }),
    );
    renderGame();
    fireEvent.click(await screen.findByRole('button', { name: /reveal word/i }));
    await screen.findByRole('grid', { name: 'Anagram tiles' });
  };

  it('swaps two tiles on tap-tap and submits the new order', async () => {
    await renderStarted();
    vi.mocked(submitAnagramGuess).mockResolvedValue({
      ...makeParticipation({ attempt_count: 1, guesses: ['EHPLTANE'] }),
    });

    const cells = within(screen.getByRole('grid', { name: 'Anagram tiles' })).getAllByRole(
      'gridcell',
    );
    fireEvent.click(cells[0]);
    fireEvent.click(cells[1]);

    expect(boardLetters()).toEqual(['E', 'H', 'P', 'L', 'T', 'A', 'N', 'E']);
    fireEvent.click(screen.getByRole('button', { name: /^submit$/i }));

    await waitFor(() =>
      expect(submitAnagramGuess).toHaveBeenCalledWith('game-1', 'EHPLTANE'),
    );
  });

  it('moves a tile by drag and submits where it landed', async () => {
    await renderStarted();
    const cells = mockTileGeometry();

    fireEvent.pointerDown(cells[0], { pointerId: 1, clientX: 10, clientY: 20 });
    fireEvent.pointerMove(cells[0], { pointerId: 1, clientX: 115, clientY: 20 });
    fireEvent.pointerUp(cells[0], { pointerId: 1, clientX: 115, clientY: 20 });

    expect(boardLetters()).toEqual(['E', 'P', 'H', 'L', 'T', 'A', 'N', 'E']);

    vi.mocked(submitAnagramGuess).mockResolvedValue({
      ...makeParticipation({ attempt_count: 1, guesses: ['EPHLTANE'] }),
    });
    fireEvent.click(screen.getByRole('button', { name: /^submit$/i }));
    await waitFor(() =>
      expect(submitAnagramGuess).toHaveBeenCalledWith('game-1', 'EPHLTANE'),
    );
  });

  it('restores the dealt scramble on reset', async () => {
    await renderStarted();

    const cells = within(screen.getByRole('grid', { name: 'Anagram tiles' })).getAllByRole(
      'gridcell',
    );
    fireEvent.click(cells[0]);
    fireEvent.click(cells[3]);
    expect(boardLetters()).toEqual(['E', 'P', 'L', 'H', 'T', 'A', 'N', 'E']);

    fireEvent.click(screen.getByRole('button', { name: /reset tiles/i }));
    expect(boardLetters()).toEqual(['H', 'E', 'P', 'L', 'T', 'A', 'N', 'E']);
  });

  it('celebrates a solve with the guess count and the banked time', async () => {
    await renderStarted();
    vi.mocked(submitAnagramGuess).mockResolvedValue({
      ...makeParticipation({
        status: 'won',
        success: true,
        attempt_count: 3,
        guesses: ['PELHANTE', 'HLEPANTE', 'ELEPHANT'],
        active_time_seconds: 102,
        timer_started_at: null,
        timer_running: false,
        completed_at: new Date().toISOString(),
        solved_at: new Date().toISOString(),
        answer: 'ELEPHANT',
      }),
      guess: 'ELEPHANT',
      correct: true,
    });

    fireEvent.click(screen.getByRole('button', { name: /^submit$/i }));

    expect(await screen.findByText('Solved!')).toBeInTheDocument();
    expect(screen.getByText(/You solved it in 3 guesses/)).toBeInTheDocument();
    expect(screen.getByText('01:42')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^submit$/i })).toBeDisabled();
  });

  it('keeps a wrong guess on the board with fewer attempts left', async () => {
    await renderStarted();
    vi.mocked(submitAnagramGuess).mockResolvedValue({
      ...makeParticipation({ attempt_count: 1, guesses: ['EHPLTANE'] }),
      guess: 'EHPLTANE',
      correct: false,
    });

    fireEvent.click(screen.getByRole('button', { name: /^submit$/i }));

    await waitFor(() =>
      expect(screen.getByTestId('attempt-label')).toHaveTextContent(
        'Attempts remaining: 4 / 5',
      ),
    );
    expect(screen.getByText('EHPLTANE')).toBeInTheDocument();
    // The tiles stay where the player left them: no re-deal after a miss.
    expect(boardLetters()).toEqual(['H', 'E', 'P', 'L', 'T', 'A', 'N', 'E']);
  });

  it('reveals the answer after the fifth miss and locks the board', async () => {
    vi.mocked(getCurrentAnagram).mockResolvedValue(
      makeCurrentGame({
        participation: makeParticipation({
          attempt_count: 4,
          guesses: ['PELHANTE', 'HLEPANTE', 'EPHLTANE', 'LPHETANE'],
          timer_started_at: new Date().toISOString(),
          timer_running: true,
        }),
      }),
    );
    renderGame();
    await screen.findByRole('grid', { name: 'Anagram tiles' });

    vi.mocked(submitAnagramGuess).mockResolvedValue({
      ...makeParticipation({
        status: 'lost',
        success: false,
        attempt_count: 5,
        guesses: ['PELHANTE', 'HLEPANTE', 'EPHLTANE', 'LPHETANE', 'TNEHPLAE'],
        active_time_seconds: 64,
        timer_started_at: null,
        timer_running: false,
        completed_at: new Date().toISOString(),
        answer: 'ELEPHANT',
      }),
      guess: 'TNEHPLAE',
      correct: false,
    });
    fireEvent.click(screen.getByRole('button', { name: /^submit$/i }));

    expect(await screen.findByText('Out of guesses')).toBeInTheDocument();
    expect(screen.getByText('Correct answer')).toBeInTheDocument();
    expect(screen.getAllByText('ELEPHANT').length).toBeGreaterThan(0);
    expect(screen.getByTestId('attempt-label')).toHaveTextContent(
      'Attempts remaining: 0 / 5',
    );
    expect(screen.getByRole('button', { name: /^submit$/i })).toBeDisabled();
  });

  it('leaves refused guesses on the board with a reason', async () => {
    await renderStarted();
    vi.mocked(submitAnagramGuess).mockRejectedValue({
      response: { status: 400, data: { detail: 'A guess uses exactly the letters you were dealt.' } },
    });

    fireEvent.click(screen.getByRole('button', { name: /^submit$/i }));

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(boardLetters()).toEqual(['H', 'E', 'P', 'L', 'T', 'A', 'N', 'E']);
    expect(screen.getByTestId('attempt-label')).toHaveTextContent('Attempts remaining: 5 / 5');
  });
});

describe('the clock', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useRealTimers();
    setVisibility('visible');
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    if (realVisibilityState) {
      Object.defineProperty(document, 'visibilityState', realVisibilityState);
    }
  });

  it('pauses when the page hides and resumes only on press', async () => {
    const paused = makeParticipation({ active_time_seconds: 45 });
    vi.mocked(getCurrentAnagram).mockResolvedValue(
      makeCurrentGame({
        participation: makeParticipation({
          active_time_seconds: 40,
          timer_started_at: new Date().toISOString(),
          timer_running: true,
        }),
      }),
    );
    vi.mocked(pauseAnagram).mockResolvedValue(paused);
    renderGame();
    await screen.findByRole('grid', { name: 'Anagram tiles' });

    setVisibility('hidden');
    fireEvent(document, new Event('visibilitychange'));

    await waitFor(() => expect(pauseAnagram).toHaveBeenCalledWith('game-1'));
    // Paused: the control offers RESUME, and showing the page again starts nothing.
    expect(await screen.findByRole('button', { name: /resume/i })).toBeInTheDocument();
    setVisibility('visible');
    fireEvent(document, new Event('visibilitychange'));
    expect(startAnagram).not.toHaveBeenCalled();

    vi.mocked(startAnagram).mockResolvedValue(
      makeParticipation({
        active_time_seconds: 45,
        timer_started_at: new Date().toISOString(),
        timer_running: true,
      }),
    );
    fireEvent.click(screen.getByRole('button', { name: /resume/i }));
    await waitFor(() => expect(startAnagram).toHaveBeenCalledWith('game-1'));
  });

  it('checkpoints a visibly running game on an interval', async () => {
    vi.useFakeTimers();
    vi.mocked(getCurrentAnagram).mockResolvedValue(
      makeCurrentGame({
        participation: makeParticipation({
          active_time_seconds: 40,
          timer_started_at: new Date().toISOString(),
          timer_running: true,
        }),
      }),
    );
    vi.mocked(heartbeatAnagram).mockResolvedValue(makeParticipation({ active_time_seconds: 70 }));
    renderGame();
    await act(async () => {});

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(heartbeatAnagram).toHaveBeenCalledWith('game-1');
  });

  it('does not checkpoint a paused game, and stops ticking on logout', async () => {
    const startedAt = new Date('2026-10-08T12:00:00Z').getTime();
    vi.spyOn(Date, 'now').mockReturnValue(startedAt + 65_000);
    vi.mocked(getCurrentAnagram).mockResolvedValue(
      makeCurrentGame({
        participation: makeParticipation({
          active_time_seconds: 87,
          timer_started_at: new Date(startedAt).toISOString(),
          timer_running: true,
        }),
      }),
    );
    const { rerender } = renderGame(true);
    // 87 banked plus 65 on the open segment.
    expect(await screen.findByText('02:32')).toBeInTheDocument();

    rerender(
      <AuthContext.Provider value={authValue(false)}>
        <MemoryRouter initialEntries={['/games/anagram']}>
          <Routes>
            <Route path="/games/anagram" element={<AnagramGamePage />} />
            <Route path="/games" element={<div>games home</div>} />
          </Routes>
        </MemoryRouter>
      </AuthContext.Provider>,
    );

    // Signed out: the tick freezes at the banked total, and no heartbeat goes out.
    await waitFor(
      () => expect(screen.getByTestId('play-clock')).toHaveTextContent('01:27'),
      { timeout: 3000 },
    );
    expect(heartbeatAnagram).not.toHaveBeenCalled();
  });
});

describe('losing and regaining focus', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useRealTimers();
    setVisibility('visible');
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    if (realVisibilityState) {
      Object.defineProperty(document, 'visibilityState', realVisibilityState);
    }
  });

  const renderRunning = async () => {
    vi.mocked(getCurrentAnagram).mockResolvedValue(
      makeCurrentGame({
        participation: makeParticipation({
          active_time_seconds: 40,
          timer_started_at: new Date().toISOString(),
          timer_running: true,
        }),
      }),
    );
    vi.mocked(pauseAnagram).mockResolvedValue(makeParticipation({ active_time_seconds: 45 }));
    renderGame();
    await screen.findByRole('grid', { name: 'Anagram tiles' });
  };

  it('hides the word and banks the clock when the window blurs', async () => {
    await renderRunning();

    fireEvent.blur(window);

    await waitFor(() => expect(pauseAnagram).toHaveBeenCalledWith('game-1'));
    expect(screen.queryByRole('grid', { name: 'Anagram tiles' })).not.toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Resume game' }),
    ).toBeInTheDocument();
    expect(screen.getByText(/click anywhere to resume/i)).toBeInTheDocument();
  });

  it('resumes on the first click back, keeping the board as it was left', async () => {
    await renderRunning();

    // Rearrange first, so the resume has a board worth remembering.
    const cells = within(screen.getByRole('grid', { name: 'Anagram tiles' })).getAllByRole(
      'gridcell',
    );
    fireEvent.click(cells[0]);
    fireEvent.click(cells[1]);
    expect(boardLetters()).toEqual(['E', 'H', 'P', 'L', 'T', 'A', 'N', 'E']);

    fireEvent.blur(window);
    await screen.findByRole('button', { name: 'Resume game' });

    // Refocusing alone resumes nothing: the word stays hidden until a click.
    fireEvent.focus(window);
    expect(screen.queryByRole('grid', { name: 'Anagram tiles' })).not.toBeInTheDocument();
    expect(startAnagram).not.toHaveBeenCalled();

    vi.mocked(startAnagram).mockResolvedValue(
      makeParticipation({
        active_time_seconds: 45,
        timer_started_at: new Date().toISOString(),
        timer_running: true,
      }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Resume game' }));

    await waitFor(() => expect(startAnagram).toHaveBeenCalledWith('game-1'));
    expect(await screen.findByRole('grid', { name: 'Anagram tiles' })).toBeInTheDocument();
    expect(boardLetters()).toEqual(['E', 'H', 'P', 'L', 'T', 'A', 'N', 'E']);
  });

  it('hides a paused game without banking anything extra', async () => {
    vi.mocked(getCurrentAnagram).mockResolvedValue(
      makeCurrentGame({
        participation: makeParticipation({ active_time_seconds: 45 }),
      }),
    );
    renderGame();
    await screen.findByRole('grid', { name: 'Anagram tiles' });

    fireEvent.blur(window);

    expect(await screen.findByRole('button', { name: 'Resume game' })).toBeInTheDocument();
    expect(pauseAnagram).not.toHaveBeenCalled();
  });

  it('leaves fresh and finished games alone when focus moves', async () => {
    vi.mocked(getCurrentAnagram).mockResolvedValue(makeCurrentGame());
    renderGame();
    expect(await screen.findByRole('button', { name: /reveal word/i })).toBeInTheDocument();

    // Nothing to hide before the reveal: no gate, no pause.
    fireEvent.blur(window);
    expect(screen.queryByRole('button', { name: 'Resume game' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /reveal word/i })).toBeInTheDocument();
    expect(pauseAnagram).not.toHaveBeenCalled();
  });

  it('never gates a completed game', async () => {
    vi.mocked(getCurrentAnagram).mockResolvedValue(
      makeCurrentGame({
        participation: makeParticipation({
          status: 'won',
          success: true,
          attempt_count: 2,
          active_time_seconds: 60,
          completed_at: new Date().toISOString(),
          solved_at: new Date().toISOString(),
          answer: 'ELEPHANT',
        }),
      }),
    );
    renderGame();
    expect(await screen.findByText('Solved!')).toBeInTheDocument();

    fireEvent.blur(window);

    expect(screen.queryByRole('button', { name: 'Resume game' })).not.toBeInTheDocument();
    expect(screen.getByText('Solved!')).toBeInTheDocument();
    expect(pauseAnagram).not.toHaveBeenCalled();
  });

  it('stops checkpointing while blurred', async () => {
    vi.useFakeTimers();
    vi.mocked(getCurrentAnagram).mockResolvedValue(
      makeCurrentGame({
        participation: makeParticipation({
          active_time_seconds: 40,
          timer_started_at: new Date().toISOString(),
          timer_running: true,
        }),
      }),
    );
    vi.mocked(pauseAnagram).mockResolvedValue(makeParticipation({ active_time_seconds: 45 }));
    renderGame();
    await act(async () => {});

    fireEvent.blur(window);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });

    expect(pauseAnagram).toHaveBeenCalledWith('game-1');
    expect(heartbeatAnagram).not.toHaveBeenCalled();
  });
});

describe('navigating away and back', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useRealTimers();
    setVisibility('visible');
    navigateTo = null;
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    navigateTo = null;
    if (realVisibilityState) {
      Object.defineProperty(document, 'visibilityState', realVisibilityState);
    }
  });

  /** The page inside the real section provider, with somewhere else to go. */
  const renderSection = () =>
    render(
      <GamesConfigProvider>
        <AuthContext.Provider value={authValue(true)}>
          <MemoryRouter initialEntries={['/games/anagram']}>
            <NavProbe />
            <Routes>
              <Route path="/games/anagram" element={<AnagramGamePage />} />
              <Route path="/games" element={<div>games home</div>} />
            </Routes>
          </MemoryRouter>
        </AuthContext.Provider>
      </GamesConfigProvider>,
    );

  const goTo = async (path: string) => {
    await act(async () => {
      navigateTo?.(path);
    });
  };

  it('gates the word on return, and the first click resumes the same board', async () => {
    vi.mocked(getCurrentAnagram).mockResolvedValue(
      makeCurrentGame({
        participation: makeParticipation({
          active_time_seconds: 40,
          timer_started_at: new Date().toISOString(),
          timer_running: true,
        }),
      }),
    );
    vi.mocked(pauseAnagram).mockResolvedValue(makeParticipation({ active_time_seconds: 45 }));
    vi.mocked(startAnagram).mockResolvedValue(
      makeParticipation({
        active_time_seconds: 45,
        timer_started_at: new Date().toISOString(),
        timer_running: true,
      }),
    );
    renderSection();

    // First visit: the board is simply there.
    expect(await screen.findByRole('grid', { name: 'Anagram tiles' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Resume game' })).not.toBeInTheDocument();

    // Leaving banks the clock and remembers the mid-game departure.
    await goTo('/games');
    expect(await screen.findByText('games home')).toBeInTheDocument();
    await waitFor(() => expect(pauseAnagram).toHaveBeenCalledWith('game-1'));

    // Back again: the word returns hidden until the first click.
    await goTo('/games/anagram');
    expect(await screen.findByRole('button', { name: 'Resume game' })).toBeInTheDocument();
    expect(screen.queryByRole('grid', { name: 'Anagram tiles' })).not.toBeInTheDocument();
    expect(startAnagram).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Resume game' }));
    await waitFor(() => expect(startAnagram).toHaveBeenCalledWith('game-1'));
    expect(await screen.findByRole('grid', { name: 'Anagram tiles' })).toBeInTheDocument();
    expect(boardLetters()).toEqual(['H', 'E', 'P', 'L', 'T', 'A', 'N', 'E']);
  });

  it('never gates a completed game on return', async () => {
    vi.mocked(getCurrentAnagram).mockResolvedValue(
      makeCurrentGame({
        participation: makeParticipation({
          status: 'won',
          success: true,
          attempt_count: 2,
          active_time_seconds: 60,
          completed_at: new Date().toISOString(),
          solved_at: new Date().toISOString(),
          answer: 'ELEPHANT',
        }),
      }),
    );
    renderSection();
    expect(await screen.findByText('Solved!')).toBeInTheDocument();

    await goTo('/games');
    await goTo('/games/anagram');

    expect(await screen.findByText('Solved!')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Resume game' })).not.toBeInTheDocument();
    expect(pauseAnagram).not.toHaveBeenCalled();
    expect(startAnagram).not.toHaveBeenCalled();
  });
});
