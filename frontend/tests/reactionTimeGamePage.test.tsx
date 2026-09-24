import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { toast } from 'sonner';

import { ReactionTimeGamePage } from '@/pages/ReactionTimeGamePage';
import type { ReactionTimeAttempt, ReactionTimeState } from '@/services/api';

/**
 * The Reaction Time screen against its API boundary.
 *
 * The press is the whole game, so the contract pinned here is timing: the
 * screen flips on the press itself -- blue to red, red to "Too soon!", green
 * to the measured milliseconds -- while the server round trips reconcile
 * afterwards. Every "instant" test below holds the POST promise open and
 * asserts the new screen is already showing, which is what makes a ~100 ms
 * round trip invisible to the player without ever trusting the client for
 * the authoritative count: the start still consumes the submission
 * server-side, and the result card is replaced by the server's answer when
 * it lands.
 *
 * Presses go through `pointerdown` (the press, not the release), with `click`
 * kept as the keyboard fallback -- both are exercised below.
 */

vi.mock('@/services/api', () => ({
  getReactionTimeState: vi.fn(),
  startReactionTimeAttempt: vi.fn(),
  completeReactionTimeAttempt: vi.fn(),
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
  getReactionTimeState,
  startReactionTimeAttempt,
  completeReactionTimeAttempt,
} = await import('@/services/api');

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

const makeAttempt = (overrides: Partial<ReactionTimeAttempt> = {}): ReactionTimeAttempt => ({
  submission_id: 'sub-1',
  attempt_number: 1,
  status: 'valid',
  reaction_time_ms: 243,
  elapsed_ms: null,
  started_at: '2026-10-09T00:00:00Z',
  green_at: '2026-10-09T00:00:01Z',
  clicked_at: '2026-10-09T00:00:01Z',
  created_at: '2026-10-09T00:00:01Z',
  ...overrides,
});

const makeState = (overrides: Partial<ReactionTimeState> = {}): ReactionTimeState => ({
  game_id: 'game-1',
  game_date: '2026-10-09',
  max_submissions: 5,
  submissions_used: 0,
  submissions_remaining: 5,
  is_finished: false,
  attempts: [],
  valid_count: 0,
  false_start_count: 0,
  best_ms: null,
  average_ms: null,
  pending_submission: null,
  ...overrides,
});

const renderGame = () =>
  render(
    <MemoryRouter initialEntries={['/games/reaction-time']}>
      <Routes>
        <Route path="/games/reaction-time" element={<ReactionTimeGamePage />} />
        <Route path="/games" element={<div>games home</div>} />
      </Routes>
    </MemoryRouter>,
  );

const pressBlue = () =>
  fireEvent.pointerDown(screen.getByRole('button', { name: /click to start/i }));

describe('opening the game', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useRealTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('shows the blue start screen with the server counts', async () => {
    vi.mocked(getReactionTimeState).mockResolvedValue(makeState());

    renderGame();

    expect(await screen.findByRole('button', { name: /click to start/i })).toBeInTheDocument();
    expect(screen.getByText(/Attempts: 0 \/ 5/)).toBeInTheDocument();
  });

  it('shows the empty state when there is no session', async () => {
    vi.mocked(getReactionTimeState).mockResolvedValue(makeState({ game_id: null, game_date: null }));

    renderGame();

    expect(await screen.findByText(/No game available today/)).toBeInTheDocument();
  });

  it('shows the finished state with final statistics and no retry', async () => {
    vi.mocked(getReactionTimeState).mockResolvedValue(
      makeState({
        submissions_used: 5,
        submissions_remaining: 0,
        is_finished: true,
        valid_count: 4,
        false_start_count: 1,
        best_ms: 227,
        average_ms: 252.75,
      }),
    );

    renderGame();

    expect(await screen.findByText(/All 5 attempts used/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument();
  });
});

describe('instant presses, reconciled afterwards', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useRealTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('turns red the moment blue is pressed, before the start round trip lands', async () => {
    vi.mocked(getReactionTimeState).mockResolvedValue(makeState());
    const startGate = deferred<ReactionTimeState>();
    vi.mocked(startReactionTimeAttempt).mockReturnValue(startGate.promise);

    renderGame();
    expect(await screen.findByRole('button', { name: /click to start/i })).toBeInTheDocument();
    pressBlue();

    // The POST is still in flight -- and the screen is already red.
    expect(startReactionTimeAttempt).toHaveBeenCalledWith('game-1');
    expect(screen.getByRole('button', { name: /wait for green/i })).toBeInTheDocument();

    startGate.resolve(
      makeState({
        submissions_used: 1,
        submissions_remaining: 4,
        pending_submission: makeAttempt({ status: 'pending', reaction_time_ms: null }),
      }),
    );
    await waitFor(() => expect(screen.getByText(/Attempts: 1 \/ 5/)).toBeInTheDocument());
  });

  it('shows "Too soon!" the moment red is pressed, scoring nothing until the server answers', async () => {
    vi.mocked(getReactionTimeState).mockResolvedValue(makeState());
    const startGate = deferred<ReactionTimeState>();
    const completeGate = deferred<ReactionTimeState>();
    vi.mocked(startReactionTimeAttempt).mockReturnValue(startGate.promise);
    vi.mocked(completeReactionTimeAttempt).mockReturnValue(completeGate.promise);

    renderGame();
    expect(await screen.findByRole('button', { name: /click to start/i })).toBeInTheDocument();
    pressBlue();
    fireEvent.pointerDown(screen.getByRole('button', { name: /wait for green/i }));

    // Both round trips are still in flight -- and the verdict is already up.
    expect(screen.getByText('Too soon!')).toBeInTheDocument();
    expect(completeReactionTimeAttempt).not.toHaveBeenCalled();

    startGate.resolve(
      makeState({
        submissions_used: 1,
        submissions_remaining: 4,
        pending_submission: makeAttempt({ submission_id: 'sub-9', status: 'pending', reaction_time_ms: null }),
      }),
    );
    await waitFor(() => expect(completeReactionTimeAttempt).toHaveBeenCalled());
    const payload = vi.mocked(completeReactionTimeAttempt).mock.calls[0][0];
    expect(payload).toMatchObject({ submission_id: 'sub-9', status: 'too_early' });
    expect(typeof payload.elapsed_ms).toBe('number');

    completeGate.resolve(
      makeState({
        submissions_used: 1,
        submissions_remaining: 4,
        false_start_count: 1,
        attempts: [makeAttempt({ submission_id: 'sub-9', status: 'too_early', reaction_time_ms: null, elapsed_ms: 384 })],
      }),
    );
    await waitFor(() => expect(screen.getByText(/Attempts: 1 \/ 5/)).toBeInTheDocument());
    // Best/average untouched by the false start.
    expect(screen.getByText('Best: —')).toBeInTheDocument();
  });

  it('shows the measured milliseconds the moment green is pressed', async () => {
    vi.mocked(getReactionTimeState).mockResolvedValue(makeState());
    // Shortest possible wait so the test runs in ~1.5 s of real time.
    vi.spyOn(Math, 'random').mockReturnValue(0);
    const pending = makeAttempt({ status: 'pending', reaction_time_ms: null });
    vi.mocked(startReactionTimeAttempt).mockResolvedValue(
      makeState({ submissions_used: 1, submissions_remaining: 4, pending_submission: pending }),
    );
    const completeGate = deferred<ReactionTimeState>();
    vi.mocked(completeReactionTimeAttempt).mockReturnValue(completeGate.promise);

    renderGame();
    expect(await screen.findByRole('button', { name: /click to start/i })).toBeInTheDocument();
    pressBlue();
    // Green arrives on its timer; the press itself is what is measured.
    expect(await screen.findByRole('button', { name: /click now/i }, { timeout: 5000 })).toBeInTheDocument();
    fireEvent.pointerDown(screen.getByRole('button', { name: /click now/i }));

    expect(await screen.findByText(/Your reaction time/)).toBeInTheDocument();
    expect(completeReactionTimeAttempt).toHaveBeenCalledTimes(1);
    const payload = vi.mocked(completeReactionTimeAttempt).mock.calls[0][0];
    expect(payload.status).toBe('valid');
    expect(typeof payload.reaction_time_ms).toBe('number');

    completeGate.resolve(
      makeState({
        submissions_used: 1,
        submissions_remaining: 4,
        valid_count: 1,
        best_ms: payload.reaction_time_ms,
        average_ms: payload.reaction_time_ms,
        attempts: [makeAttempt({ reaction_time_ms: payload.reaction_time_ms })],
      }),
    );
    await waitFor(() =>
      expect(screen.getByText(`Best: ${payload.reaction_time_ms} ms`)).toBeInTheDocument(),
    );
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument();
  });

  it('rolls the screen back when the start is refused', async () => {
    vi.mocked(getReactionTimeState).mockResolvedValue(makeState());
    vi.mocked(startReactionTimeAttempt).mockRejectedValue({
      response: { status: 409, data: { detail: "No attempts left for this game." } },
    });

    renderGame();
    // Keyboard path: click without a preceding pointer-down still starts.
    fireEvent.click(await screen.findByRole('button', { name: /click to start/i }));

    expect(await screen.findByRole('button', { name: /click to start/i })).toBeInTheDocument();
    expect(toast.info).toHaveBeenCalled();
  });

  it('records an interrupted pending attempt as missed without scoring it', async () => {
    const pending = makeAttempt({ submission_id: 'sub-3', attempt_number: 2, status: 'pending', reaction_time_ms: null });
    vi.mocked(getReactionTimeState).mockResolvedValue(
      makeState({ submissions_used: 2, submissions_remaining: 3, pending_submission: pending }),
    );
    vi.mocked(completeReactionTimeAttempt).mockResolvedValue(
      makeState({ submissions_used: 2, submissions_remaining: 3, false_start_count: 1 }),
    );

    renderGame();

    expect(await screen.findByText(/Attempt 2 was interrupted/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /record as missed/i }));

    await waitFor(() => expect(completeReactionTimeAttempt).toHaveBeenCalledWith({
      submission_id: 'sub-3',
      status: 'too_early',
      clicked_at: expect.any(String),
    }));
    expect(await screen.findByRole('button', { name: /click to start/i })).toBeInTheDocument();
  });
});
