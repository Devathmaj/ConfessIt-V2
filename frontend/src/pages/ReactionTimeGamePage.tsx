import { useCallback, useEffect, useRef, useState } from 'react';
import { NavLink } from 'react-router-dom';
import { Loader2, RotateCcw, Timer } from 'lucide-react';
import { toast } from 'sonner';
import { GamesPageHeader } from '@/components/games/GamesPageHeader';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { describeApiError } from '@/lib/apiError';
import { cn } from '@/lib/utils';
import {
  completeReactionTimeAttempt,
  getReactionTimeState,
  startReactionTimeAttempt,
  type ReactionTimeAttempt,
  type ReactionTimeState,
} from '@/services/api';

/**
 * The Reaction Time game: five submissions per session.
 *
 * The screen reads the caller's state once on mount and never invents
 * submissions itself: the server counts every blue-screen click as a
 * consumed submission, so a refresh, a navigation away and back, or a
 * logout and return all resume from the stored rows rather than from
 * frontend memory.
 *
 * One round is blue (click to start, consuming a submission) → red (wait for
 * green) → green (click fast) → result. A click while red is a false start:
 * it still spent its submission but carries no score. The reaction itself is
 * measured here with `performance.now()`; the backend validates the bounds
 * and persists.
 *
 * The press is the whole game, so feedback is synchronous and optimistic:
 * timestamps are captured as the first statement of the press handler and
 * the screen flips immediately -- the server round trips reconcile
 * afterwards and never gate what the player sees. Presses register on
 * pointer-down (not release), so the measured instant is the press itself.
 */

/** The random red-wait window: long enough that green cannot be memorised. */
const MIN_WAIT_MS = 1500;
const MAX_WAIT_MS = 4500;

const randomWaitMs = () =>
  MIN_WAIT_MS + Math.random() * (MAX_WAIT_MS - MIN_WAIT_MS);

type Phase = 'ready' | 'waiting' | 'green' | 'result';

const initialState: ReactionTimeState = {
  game_id: null,
  game_date: null,
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
};

const formatMs = (value: number | null): string =>
  value === null || value === undefined ? '—' : `${Math.round(value)} ms`;

/**
 * Instant tactile feedback shared by every pressable game surface: no tap
 * highlight, no text selection, no double-tap zoom delay, and a press scale
 * applied by CSS so it lands without waiting for a re-render.
 */
const PRESSABLE_SURFACE =
  'select-none [-webkit-tap-highlight-color:transparent] [touch-action:manipulation] transition-transform duration-75 active:scale-[0.99] cursor-pointer';

export const ReactionTimeGamePage = () => {
  const [state, setState] = useState<ReactionTimeState>(initialState);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [phase, setPhase] = useState<Phase>('ready');
  const [busy, setBusy] = useState(false);
  const [lastAttempt, setLastAttempt] = useState<ReactionTimeAttempt | null>(null);

  // The live round, mirrored in refs so press handlers -- which must run
  // synchronously to measure honestly -- never read a stale closure. State
  // updates are async; these refs are the guards that make a second press
  // in the same tick land on the phase the first press just entered.
  const liveRef = useRef<Phase>('ready');
  const stateRef = useRef(state);
  stateRef.current = state;

  const timeoutRef = useRef<number | null>(null);
  const pendingIdRef = useRef<string | null>(null);
  const startPromiseRef = useRef<Promise<ReactionTimeState> | null>(null);
  const startingRef = useRef(false);
  // True while this round owns the pending submission (its settle is in
  // flight). The interrupted screen below must not hijack the render then:
  // the pending row is ours and its result card is already showing.
  const settlingRef = useRef(false);
  const redStartRef = useRef(0);
  const greenAtRef = useRef(0);
  const clickedRef = useRef(false);

  const enterPhase = useCallback((next: Phase) => {
    liveRef.current = next;
    setPhase(next);
  }, []);

  const clearTimer = useCallback(() => {
    if (timeoutRef.current !== null) {
      window.clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    getReactionTimeState()
      .then((data) => {
        if (!cancelled) setState(data);
      })
      .catch((error) => {
        if (!cancelled)
          setLoadError(describeApiError(error, "Could not load today's game."));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // A wait abandoned by unmount keeps its pending submission on the server --
  // the attempt was already consumed when it started, so there is nothing to
  // refund. The timer here is only the local green flip; clearing it stops a
  // state update on an unmounted screen.
  useEffect(() => clearTimer, [clearTimer]);

  const gameId = state.game_id;
  const pending = state.pending_submission;
  const finished = state.is_finished && pending === null;

  const fireGreen = useCallback(() => {
    timeoutRef.current = null;
    // Captured before the paint: the transition instant is the measurement.
    greenAtRef.current = performance.now();
    liveRef.current = 'green';
    setPhase('green');
  }, []);

  /** The blue-screen press: consumes one submission, then turns red at once. */
  const handleStart = useCallback(() => {
    if (gameId === null) return;
    // Synchronous guards: a pointer-down followed by its compatibility click
    // (or a keyboard press) in the same tick must not start twice.
    if (liveRef.current !== 'ready' || startingRef.current) return;
    startingRef.current = true;
    clearTimer();
    redStartRef.current = performance.now();
    clickedRef.current = false;
    pendingIdRef.current = null;
    setLastAttempt(null);
    liveRef.current = 'waiting';
    setPhase('waiting');
    timeoutRef.current = window.setTimeout(fireGreen, randomWaitMs());

    // The server receipt arrives in the background: the screen is already
    // red, measured from the press above. A refusal rolls the screen back
    // rather than leaving a wait nothing owns.
    const started = startReactionTimeAttempt(gameId);
    startPromiseRef.current = started;
    void started
      .then((next) => {
        setState(next);
        const receipt = next.pending_submission;
        if (receipt === null) {
          clearTimer();
          pendingIdRef.current = null;
          startPromiseRef.current = null;
          enterPhase(next.is_finished ? 'result' : 'ready');
          return;
        }
        pendingIdRef.current = receipt.submission_id;
      })
      .catch(async (error: unknown) => {
        clearTimer();
        pendingIdRef.current = null;
        startPromiseRef.current = null;
        try {
          setState(await getReactionTimeState());
        } catch {
          // The refresh is best-effort; the toast below still says what happened.
        }
        enterPhase('ready');
        toast.info(describeApiError(error, 'Could not start an attempt.'));
      })
      .finally(() => {
        startingRef.current = false;
      });
  }, [gameId, clearTimer, fireGreen, enterPhase]);

  /** Settle the live attempt: instant result card, server receipt after. */
  const settle = useCallback(
    (kind: 'valid' | 'too_early') => {
      const live = liveRef.current;
      if (kind === 'valid' ? live !== 'green' : live !== 'waiting') return;
      if (clickedRef.current) return;
      clickedRef.current = true;
      settlingRef.current = true;
      clearTimer();

      // Measured now, shown now: the card below renders from this object
      // before any network leaves the browser.
      const elapsed = kind === 'valid'
        ? performance.now() - greenAtRef.current
        : performance.now() - redStartRef.current;
      const nowWall = Date.now();
      const optimistic: ReactionTimeAttempt = {
        submission_id: pendingIdRef.current ?? 'pending',
        attempt_number: stateRef.current.submissions_used + 1,
        status: kind,
        reaction_time_ms: kind === 'valid' ? Math.max(1, Math.round(elapsed)) : null,
        elapsed_ms: kind === 'too_early' ? Math.max(0, Math.round(elapsed)) : null,
        started_at: new Date().toISOString(),
        green_at:
          kind === 'valid'
            ? new Date(nowWall - Math.round(elapsed)).toISOString()
            : null,
        clicked_at: new Date(nowWall).toISOString(),
        created_at: new Date().toISOString(),
      };
      setLastAttempt(optimistic);
      liveRef.current = 'result';
      setPhase('result');

      void (async () => {
        let submissionId: string | null = null;
        try {
          const started = await startPromiseRef.current;
          submissionId =
            pendingIdRef.current ??
            started?.pending_submission?.submission_id ??
            null;
          if (submissionId === null || submissionId === 'pending') {
            throw new Error('That attempt was never started.');
          }
          const next =
            kind === 'valid'
              ? await completeReactionTimeAttempt({
                  submission_id: submissionId,
                  status: 'valid',
                  reaction_time_ms: optimistic.reaction_time_ms ?? 1,
                  green_at: optimistic.green_at ?? undefined,
                  clicked_at: optimistic.clicked_at ?? undefined,
                })
              : await completeReactionTimeAttempt({
                  submission_id: submissionId,
                  status: 'too_early',
                  ...(optimistic.elapsed_ms !== null && optimistic.elapsed_ms !== undefined
                    ? { elapsed_ms: optimistic.elapsed_ms }
                    : {}),
                  clicked_at: optimistic.clicked_at ?? undefined,
                });
          pendingIdRef.current = null;
          startPromiseRef.current = null;
          settlingRef.current = false;
          setState(next);
          const settled =
            next.attempts.find((attempt) => attempt.submission_id === submissionId) ?? null;
          setLastAttempt(settled ?? optimistic);
        } catch (error: unknown) {
          // Reconcile: the write may have landed while its response was
          // lost, so look for the settled row before giving up the card.
          try {
            const fresh = await getReactionTimeState();
            pendingIdRef.current = null;
            startPromiseRef.current = null;
            settlingRef.current = false;
            setState(fresh);
            const found =
              submissionId !== null
                ? (fresh.attempts.find(
                    (attempt) =>
                      attempt.submission_id === submissionId && attempt.status !== 'pending',
                  ) ?? null)
                : null;
            if (found !== null) {
              setLastAttempt(found);
              liveRef.current = 'result';
              setPhase('result');
            } else {
              setLastAttempt(null);
              const done = fresh.is_finished && fresh.pending_submission === null;
              if (done) {
                liveRef.current = 'result';
                setPhase('result');
              } else {
                liveRef.current = 'ready';
                setPhase('ready');
              }
            }
          } catch {
            settlingRef.current = false;
            liveRef.current = 'ready';
            setPhase('ready');
          }
          // A null id means the start itself never produced a receipt --
          // its own failure was already reported, so stay quiet here.
          if (submissionId !== null) {
            toast.info(describeApiError(error, 'Could not record that attempt.'));
          }
        }
      })();
    },
    [clearTimer],
  );

  /** A press while red: too soon -- the submission is spent, but scoreless. */
  const handleEarlyClick = useCallback(() => {
    settle('too_early');
  }, [settle]);

  /** A press on green: the measured reaction. */
  const handleGreenClick = useCallback(() => {
    settle('valid');
  }, [settle]);

  /** Settle an interrupted attempt the caller left pending elsewhere. */
  const handleResolvePending = useCallback(async () => {
    if (pending === null || busy) return;
    setBusy(true);
    try {
      const next = await completeReactionTimeAttempt({
        submission_id: pending.submission_id,
        status: 'too_early',
        clicked_at: new Date().toISOString(),
      });
      setState(next);
      setLastAttempt(null);
      if (next.is_finished && next.pending_submission === null) {
        liveRef.current = 'result';
        setPhase('result');
      } else {
        liveRef.current = 'ready';
        setPhase('ready');
      }
    } catch (error: unknown) {
      toast.info(describeApiError(error, 'Could not resolve that attempt.'));
      try {
        setState(await getReactionTimeState());
      } catch {
        // Best-effort, as above.
      }
    } finally {
      setBusy(false);
    }
  }, [pending, busy]);

  const handleTryAgain = useCallback(() => {
    setLastAttempt(null);
    liveRef.current = 'ready';
    setPhase('ready');
  }, []);

  if (loading) {
    return (
      <div>
        <GamesPageHeader eyebrow="Reflex" title="Reaction Time" />
        <div className="flex justify-center py-16 text-muted-foreground">
          <Loader2 className="h-6 w-6 animate-spin" />
        </div>
      </div>
    );
  }

  if (loadError) {
    return (
      <div>
        <GamesPageHeader eyebrow="Reflex" title="Reaction Time" />
        <Card>
          <CardContent className="flex flex-col items-center gap-4 py-10 text-center">
            <p className="text-sm text-muted-foreground">{loadError}</p>
            <NavLink to="/games">
              <Button variant="outline">Back to Games</Button>
            </NavLink>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (gameId === null) {
    return (
      <div>
        <GamesPageHeader
          eyebrow="Reflex"
          title="Reaction Time"
          description="Wait for green, then click as fast as you can."
        />
        <Card>
          <CardContent className="flex flex-col items-center gap-4 py-12 text-center">
            <p className="text-lg font-semibold">No game available today.</p>
            <p className="max-w-sm text-sm text-muted-foreground">
              There is no active Reaction Time session right now. Sessions roll
              on the game's reset cadence -- check back later, or look at what
              else the Games section has to offer.
            </p>
            <NavLink to="/games">
              <Button variant="outline">Back to Games</Button>
            </NavLink>
          </CardContent>
        </Card>
      </div>
    );
  }

  // An attempt started elsewhere (another tab, a page left mid-wait) and never
  // settled. It already counts as consumed, so starting here is refused until
  // it is recorded -- as missed, with no score -- rather than silently
  // spending another submission. While this round's own settle is in flight
  // the pending row is ours, so the result card below keeps the screen.
  if (pending !== null && !settlingRef.current && phase !== 'waiting' && phase !== 'green') {
    return (
      <div>
        <GamesPageHeader
          eyebrow="Reflex"
          title="Reaction Time"
          description="Wait for green, then click as fast as you can."
        />
        <Card>
          <CardContent className="flex flex-col items-center gap-4 py-12 text-center">
            <p className="text-lg font-semibold">Attempt {pending.attempt_number} was interrupted.</p>
            <p className="max-w-sm text-sm text-muted-foreground">
              It started but was never finished, and it still counts as one of
              your {state.max_submissions} submissions. Record it as missed to
              keep playing -- it will not count toward your best or average.
            </p>
            <Button type="button" onClick={handleResolvePending} disabled={busy}>
              <RotateCcw />
              {busy ? 'Recording...' : 'Record as missed and continue'}
            </Button>
            <p className="text-xs text-muted-foreground" data-testid="attempt-label">
              {state.submissions_used} of {state.max_submissions} submissions used
            </p>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (finished) {
    return (
      <div>
        <GamesPageHeader
          eyebrow="Reflex"
          title="Reaction Time"
          description="Wait for green, then click as fast as you can."
        />
        <Card>
          <CardContent className="flex flex-col items-center gap-4 py-12 text-center">
            <Timer className="h-8 w-8 text-muted-foreground" />
            <p className="text-lg font-semibold">All {state.max_submissions} attempts used.</p>
            <div className="grid w-full max-w-sm grid-cols-2 gap-3 text-left">
              <div className="rounded-xl border border-border/60 p-3">
                <p className="text-xs uppercase tracking-wide text-muted-foreground">Best</p>
                <p className="text-xl font-extrabold tabular-nums">{formatMs(state.best_ms)}</p>
              </div>
              <div className="rounded-xl border border-border/60 p-3">
                <p className="text-xs uppercase tracking-wide text-muted-foreground">Average</p>
                <p className="text-xl font-extrabold tabular-nums">{formatMs(state.average_ms)}</p>
              </div>
              <div className="rounded-xl border border-border/60 p-3">
                <p className="text-xs uppercase tracking-wide text-muted-foreground">Valid</p>
                <p className="text-xl font-extrabold tabular-nums">{state.valid_count}</p>
              </div>
              <div className="rounded-xl border border-border/60 p-3">
                <p className="text-xs uppercase tracking-wide text-muted-foreground">Too soon</p>
                <p className="text-xl font-extrabold tabular-nums">{state.false_start_count}</p>
              </div>
            </div>
            <p className="text-sm text-muted-foreground" data-testid="attempt-label">
              {state.submissions_used} of {state.max_submissions} submissions used
            </p>
            <AttemptHistory attempts={state.attempts} />
            <NavLink to="/games">
              <Button variant="outline">Back to Games</Button>
            </NavLink>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div>
      <GamesPageHeader
        eyebrow="Reflex"
        title="Reaction Time"
        description="Wait for green, then click as fast as you can."
      />

      <div className="flex flex-col items-center gap-4">
        {phase === 'ready' && (
          <button
            type="button"
            onPointerDown={handleStart}
            onClick={handleStart}
            onContextMenu={(event) => event.preventDefault()}
            aria-label="Click to start a reaction attempt"
            className={cn(
              'flex min-h-[52vh] w-full max-w-4xl select-none flex-col items-center justify-center gap-3 rounded-3xl bg-blue-600 px-6 py-16 text-white shadow-lg transition-colors',
              'hover:bg-blue-500',
              PRESSABLE_SURFACE,
            )}
          >
            <Timer className="h-10 w-10 md:h-12 md:w-12" />
            <span className="text-4xl font-black tracking-tight md:text-6xl">Click to start</span>
            <span className="text-base text-white/80 md:text-xl">
              This press uses one of your {state.max_submissions} attempts
            </span>
          </button>
        )}

        {phase === 'waiting' && (
          <button
            type="button"
            onPointerDown={handleEarlyClick}
            onClick={handleEarlyClick}
            onContextMenu={(event) => event.preventDefault()}
            aria-label="Wait for green"
            className={cn(
              'flex min-h-[52vh] w-full max-w-4xl select-none flex-col items-center justify-center gap-3 rounded-3xl bg-red-600 px-6 py-16 text-white shadow-lg',
              PRESSABLE_SURFACE,
            )}
          >
            <span className="text-4xl font-black tracking-tight md:text-6xl">Wait for green...</span>
            <span className="text-base text-white/80 md:text-xl">Pressing now is too soon</span>
          </button>
        )}

        {phase === 'green' && (
          <button
            type="button"
            onPointerDown={handleGreenClick}
            onClick={handleGreenClick}
            onContextMenu={(event) => event.preventDefault()}
            aria-label="Click now"
            className={cn(
              'flex min-h-[52vh] w-full max-w-4xl select-none flex-col items-center justify-center gap-3 rounded-3xl bg-green-600 px-6 py-16 text-white shadow-lg',
              PRESSABLE_SURFACE,
            )}
          >
            <span className="text-6xl font-black tracking-tight md:text-8xl">Click!</span>
            <span className="text-base text-white/80 md:text-xl">As fast as you can</span>
          </button>
        )}

        {phase === 'result' && lastAttempt !== null && (
          <div className="flex w-full max-w-4xl flex-col items-center gap-4">
            <div
              className={cn(
                'flex min-h-[52vh] w-full select-none flex-col items-center justify-center gap-3 rounded-3xl px-6 py-16 text-white shadow-lg',
                lastAttempt.status === 'valid' ? 'bg-green-600' : 'bg-red-600',
              )}
            >
              {lastAttempt.status === 'valid' ? (
                <>
                  <span className="text-sm font-bold uppercase tracking-[0.2em] text-white/80 md:text-base">
                    Your reaction time
                  </span>
                  <span className="text-6xl font-black tabular-nums tracking-tight md:text-8xl">
                    {lastAttempt.reaction_time_ms} ms
                  </span>
                </>
              ) : (
                <>
                  <span className="text-4xl font-black tracking-tight md:text-6xl">Too soon!</span>
                  <span className="text-base text-white/80 md:text-xl">
                    You pressed before green. That attempt is spent but unscored.
                  </span>
                </>
              )}
            </div>
            {state.submissions_remaining > 0 && (
              <Button type="button" size="lg" onClick={handleTryAgain} className="rounded-full px-8">
                <RotateCcw />
                Try Again
              </Button>
            )}
          </div>
        )}

        <div className="flex flex-wrap items-center justify-center gap-x-6 gap-y-1 text-sm text-muted-foreground">
          <span data-testid="attempt-label">
            Attempts: {state.submissions_used} / {state.max_submissions}
          </span>
          <span>Best: {formatMs(state.best_ms)}</span>
          <span>Average: {formatMs(state.average_ms)}</span>
        </div>

        <AttemptHistory attempts={state.attempts} />
      </div>
    </div>
  );
};

const AttemptHistory = ({ attempts }: { attempts: ReactionTimeAttempt[] }) => {
  if (attempts.length === 0) return null;
  return (
    <ol className="grid w-full max-w-4xl grid-cols-1 gap-2 sm:grid-cols-2">
      {attempts.map((attempt) => (
        <li
          key={attempt.submission_id}
          className="flex items-center justify-between gap-3 rounded-xl border border-border/60 px-3 py-2 text-sm"
        >
          <span className="font-semibold">Attempt {attempt.attempt_number}</span>
          {attempt.status === 'valid' ? (
            <span className="font-mono tabular-nums">{attempt.reaction_time_ms} ms</span>
          ) : attempt.status === 'too_early' ? (
            <span className="text-muted-foreground">Too soon</span>
          ) : (
            <span className="text-muted-foreground">In progress</span>
          )}
        </li>
      ))}
    </ol>
  );
};

export default ReactionTimeGamePage;
