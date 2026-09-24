import { useCallback, useEffect, useRef, useState } from 'react';
import { NavLink } from 'react-router-dom';
import { Eye, Loader2, Play, RotateCcw } from 'lucide-react';
import { toast } from 'sonner';
import { GamesPageHeader } from '@/components/games/GamesPageHeader';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { useAuth } from '@/contexts/AuthContext';
import { useGamesConfig } from '@/contexts/GamesConfigContext';
import { describeApiError } from '@/lib/apiError';
import { cn } from '@/lib/utils';
import {
  getCurrentAnagram,
  heartbeatAnagram,
  pauseAnagram,
  pauseAnagramBeacon,
  startAnagram,
  submitAnagramGuess,
  type AnagramCurrentGame,
  type AnagramParticipation,
} from '@/services/api';

/**
 * The daily Anagram: one scramble per player, five guesses, an honest clock.
 *
 * The screen reads today's puzzle once on mount and never invents state itself: the
 * scramble is dealt by the server at reveal and the same tiles come back on every read,
 * so a refresh, a navigation away and back, or a logout and return all show the same
 * board. Committing a guess replaces the local participation with the response, which
 * doubles as the server's receipt for the attempt.
 *
 * The answer is never visible here. ``participation.answer`` stays null until the game is
 * over and the server decides it has nothing left to hide; the tiles only ever hold the
 * player's own scramble and their guesses.
 *
 * The clock counts active play, not wall time. It opens on REVEAL WORD, banks on pause,
 * heartbeat and settling guess, and stops -- banked server-side -- when the page hides,
 * the window loses focus, the route changes, the component unmounts, or the game ends.
 * Losing focus also hides the tiles behind a resume gate: the word is not on screen while
 * the player is elsewhere, and the first click back both reveals it and reopens the
 * clock. Navigating away mid-game and returning does the same -- a session flag owned
 * app-wide remembers the departure, so the word returns gated until that click, while a
 * refresh (which carries no such memory) restores the board outright. The visible tick is
 * local; every persisted number comes from the server, so the display is recomputed from
 * the participation on each tick rather than accumulated in state.
 */

/** How often a visibly running game checkpoints its clock without stopping it. */
const HEARTBEAT_MS = 30_000;

/** One tile: the letter, plus the stable identity that tells duplicate letters apart. */
interface Tile {
  id: number;
  letter: string;
}

const tilesFromScramble = (scrambled: string): Tile[] =>
  [...scrambled].map((letter, index) => ({ id: index, letter }));

const formatClock = (totalSeconds: number): string => {
  const clamped = Math.max(0, Math.floor(totalSeconds));
  const minutes = Math.floor(clamped / 60);
  const seconds = clamped % 60;
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
};

const initialGame: AnagramCurrentGame = {
  game_id: null,
  game_date: null,
  participation: null,
  max_attempts: 5,
};

export const AnagramGamePage = () => {
  const { isAuthenticated } = useAuth();
  // Session memory of leaving mid-game, owned app-wide so it survives in-site
  // navigation and dies on refresh. Captured once below: this mount only needs the
  // value the previous page left behind.
  const { anagramLeftMidGame, setAnagramLeftMidGame } = useGamesConfig();
  const leftMidGameAtMount = useRef(anagramLeftMidGame);
  // The setter is referentially stable (a useState setter inside the provider, a module
  // singleton outside it), so capturing it once keeps the mount-once load effect below
  // honest without re-running the fetch if the flag ever flips mid-mount.
  const setLeftMidGameRef = useRef(setAnagramLeftMidGame);

  const [game, setGame] = useState<AnagramCurrentGame>(initialGame);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [tiles, setTiles] = useState<Tile[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [draggingId, setDraggingId] = useState<number | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [isVisible, setIsVisible] = useState(
    typeof document === 'undefined' ? true : document.visibilityState === 'visible',
  );
  const [displaySeconds, setDisplaySeconds] = useState(0);
  // Whether this window currently holds focus. A tab switch fires `visibilitychange`;
  // an alt-tab to another window fires `blur` while the tab stays "visible", so both
  // are tracked -- a visible-but-unfocused page is not being played.
  const [hasFocus, setHasFocus] = useState(true);
  // The resume gate: the tiles hide when focus is lost mid-game and return on the first
  // click back, which is also the press that reopens the clock. Cleared only by that
  // explicit click -- refocusing the window alone never resumes anything.
  const [gateActive, setGateActive] = useState(false);

  // Refs mirror the values the intervals and teardown handlers need, so a pause fired
  // from `pagehide` or unmount never reads a stale closure.
  const participationRef = useRef<AnagramParticipation | null>(null);
  const gameIdRef = useRef<string | null>(null);
  const authRef = useRef(isAuthenticated);
  const visibleRef = useRef(isVisible);
  const focusRef = useRef(hasFocus);
  const gateRef = useRef(gateActive);
  const tileElements = useRef(new Map<number, HTMLElement>());
  const dragRef = useRef<{ id: number; pointerId: number } | null>(null);
  // Set when a drag reseated a tile: the click that lands after pointer-up belongs to
  // the finished drag, not to tap-to-swap, and is consumed rather than selecting.
  const suppressClickRef = useRef(false);
  const appliedScrambleRef = useRef<string | null>(null);

  const participation = game.participation;
  const gameId = game.game_id;
  const maxAttempts = game.max_attempts;
  const isComplete = participation !== null && participation.status !== 'in_progress';
  const isStarted = participation !== null;
  const isPaused = isStarted && !isComplete && !participation.timer_running;
  const canPlay = isStarted && !isComplete && !submitting && !gateActive;
  const attemptsLeft = maxAttempts - (participation?.attempt_count ?? 0);

  /** Install a participation, rebuilding the tiles only when the scramble changed. */
  const applyParticipation = useCallback((next: AnagramParticipation) => {
    // A resolved undefined (a mocked or failed call the caller did not guard) must not
    // wipe the board: ignore anything without the one field every participation carries.
    if (!next || typeof next.scrambled_word !== 'string') return;
    participationRef.current = next;
    setGame((previous) => ({ ...previous, participation: next }));
    if (appliedScrambleRef.current !== next.scrambled_word) {
      appliedScrambleRef.current = next.scrambled_word;
      setTiles(tilesFromScramble(next.scrambled_word));
      setSelectedId(null);
    }
  }, []);

  useEffect(() => {
    participationRef.current = participation;
    gameIdRef.current = gameId;
  }, [participation, gameId]);
  useEffect(() => {
    authRef.current = isAuthenticated;
  }, [isAuthenticated]);
  useEffect(() => {
    visibleRef.current = isVisible;
  }, [isVisible]);
  useEffect(() => {
    focusRef.current = hasFocus;
  }, [hasFocus]);
  useEffect(() => {
    gateRef.current = gateActive;
  }, [gateActive]);

  useEffect(() => {
    let cancelled = false;
    getCurrentAnagram()
      .then((data) => {
        if (cancelled) return;
        gameIdRef.current = data.game_id;
        participationRef.current = data.participation;
        setGame(data);
        if (data.participation) {
          appliedScrambleRef.current = data.participation.scrambled_word;
          setTiles(tilesFromScramble(data.participation.scrambled_word));
        }
        setDisplaySeconds(data.participation?.active_time_seconds ?? 0);
        // Opened already hidden (a restored background tab), or back from elsewhere in
        // the site mid-game: the word starts gated, so nothing is on screen before the
        // first explicit click. A refresh carries no such memory and restores the board
        // outright. The flag is consumed here so the next mount starts clean.
        if (data.participation?.status === 'in_progress') {
          if (
            (typeof document !== 'undefined' && document.visibilityState === 'hidden') ||
            leftMidGameAtMount.current
          ) {
            setGateActive(true);
          }
          setLeftMidGameRef.current(false);
        }
      })
      .catch((error) => {
        if (!cancelled) setLoadError(describeApiError(error, "Could not load today's puzzle."));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  /** Bank the open segment if this player is clocked in. Best-effort on the way out. */
  const pauseIfRunning = useCallback(async () => {
    const current = participationRef.current;
    const id = gameIdRef.current;
    if (!id || !current || current.status !== 'in_progress' || !current.timer_running) return;
    try {
      applyParticipation(await pauseAnagram(id));
    } catch {
      // The close that never lands is bounded server-side by the stale-segment rule
      // rather than by a retry this context cannot run.
    }
  }, [applyParticipation]);

  // Pause when the page hides or the window loses focus; never auto-resume when
  // either returns -- resuming is the gate's click, so coming back waits on the player.
  // Focus alone does not clear the gate either: refocusing without clicking leaves the
  // word hidden, because the click is the resume.
  useEffect(() => {
    const onVisibilityChange = () => {
      const visible = document.visibilityState === 'visible';
      setIsVisible(visible);
      if (!visible) {
        const current = participationRef.current;
        if (current && current.status === 'in_progress') setGateActive(true);
        void pauseIfRunning();
      }
    };
    const onBlur = () => {
      setHasFocus(false);
      const current = participationRef.current;
      if (current && current.status === 'in_progress') setGateActive(true);
      void pauseIfRunning();
    };
    const onFocus = () => {
      setHasFocus(true);
    };
    const onPageHide = () => {
      const id = gameIdRef.current;
      const current = participationRef.current;
      if (id && current?.status === 'in_progress' && current.timer_running) {
        pauseAnagramBeacon(id);
      }
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    window.addEventListener('blur', onBlur);
    window.addEventListener('focus', onFocus);
    window.addEventListener('pagehide', onPageHide);
    return () => {
      document.removeEventListener('visibilitychange', onVisibilityChange);
      window.removeEventListener('blur', onBlur);
      window.removeEventListener('focus', onFocus);
      window.removeEventListener('pagehide', onPageHide);
    };
  }, [pauseIfRunning]);

  // Pause on unmount (route change) and checkpoint on an interval while visibly running.
  // Leaving mid-game also raises the session flag, so coming back hides the word behind
  // the resume gate. A refresh never runs this cleanup -- the document is discarded, not
  // unmounted -- which is exactly why a refresh restores the board ungated.
  useEffect(() => {
    return () => {
      const current = participationRef.current;
      if (current && current.status === 'in_progress') {
        setAnagramLeftMidGame(true);
      }
      void pauseIfRunning();
    };
  }, [pauseIfRunning, setAnagramLeftMidGame]);

  useEffect(() => {
    const id = window.setInterval(() => {
      const current = participationRef.current;
      const gid = gameIdRef.current;
      if (!gid || !current || current.status !== 'in_progress' || !current.timer_running) return;
      if (!authRef.current || !visibleRef.current || !focusRef.current || gateRef.current) return;
      heartbeatAnagram(gid).then(applyParticipation).catch(() => {});
    }, HEARTBEAT_MS);
    return () => window.clearInterval(id);
  }, [applyParticipation]);

  // The visible tick: recomputed from the server's numbers every second, never
  // accumulated locally, so a re-render cannot invent time the server did not bank.
  useEffect(() => {
    const id = window.setInterval(() => {
      const current = participationRef.current;
      if (!current) {
        setDisplaySeconds(0);
        return;
      }
      let total = current.active_time_seconds;
      if (
        current.timer_running &&
        current.timer_started_at &&
        current.status === 'in_progress' &&
        authRef.current &&
        visibleRef.current &&
        focusRef.current &&
        !gateRef.current
      ) {
        total += Math.max(
          0,
          Math.floor((Date.now() - new Date(current.timer_started_at).getTime()) / 1000),
        );
      }
      setDisplaySeconds(total);
    }, 1000);
    return () => window.clearInterval(id);
  }, []);

  /** Reveal a fresh game, or resume a paused one: one control, one endpoint.
   *
   * Also the gate's click: the first press back clears the hiding and reopens the
   * clock together, so returning is a single gesture. The gate lifts only on success --
   * a refused resume keeps the word hidden rather than showing a dead board.
   */
  const revealOrResume = async () => {
    if (!gameId || actionBusy) return;
    setActionBusy(true);
    try {
      const next = await startAnagram(gameId);
      setGateActive(false);
      setHasFocus(true);
      applyParticipation(next);
    } catch (error) {
      toast.error(describeApiError(error, 'Could not start the game.'));
    } finally {
      setActionBusy(false);
    }
  };

  /** Score the tile order currently on the board. A refusal leaves the tiles alone. */
  const submitGuess = async () => {
    if (!canPlay || !gameId || !participation) return;
    const guess = tiles.map((tile) => tile.letter).join('');
    if (guess.length === 0) {
      toast.info('Arrange the tiles, then submit.');
      return;
    }
    setSubmitting(true);
    try {
      const response = await submitAnagramGuess(gameId, guess);
      applyParticipation(response);
      if (response.correct) {
        toast.success(`Solved in ${response.attempt_count} ${response.attempt_count === 1 ? 'guess' : 'guesses'}!`);
      } else if (response.status === 'lost') {
        toast.error('Out of guesses.');
      } else {
        const left = response.max_attempts - response.attempt_count;
        toast.info(`Not quite -- ${left} ${left === 1 ? 'guess' : 'guesses'} left.`);
      }
    } catch (error) {
      toast.error(describeApiError(error, 'That guess was rejected.'));
    } finally {
      setSubmitting(false);
    }
  };

  const resetTiles = () => {
    if (!participation) return;
    appliedScrambleRef.current = participation.scrambled_word;
    setTiles(tilesFromScramble(participation.scrambled_word));
    setSelectedId(null);
  };

  // --- drag and drop ----------------------------------------------------------
  //
  // Pointer events carry mouse, pen and touch through one path, which is why this is a
  // hand-rolled pointer drag rather than the HTML5 drag API: HTML5 dragging does not
  // exist on touch screens. Press a tile and move across its neighbours to reseat it
  // live; release drops it. Tiles are also buttons, so a tap selects and a second tap
  // swaps -- the same outcome for a precise screen, a keyboard, and a test.
  // `touch-action: none` on the tiles keeps a vertical page scroll from stealing a drag.

  const moveTileTo = useCallback((dragId: number, targetId: number) => {
    setTiles((previous) => {
      const from = previous.findIndex((tile) => tile.id === dragId);
      const to = previous.findIndex((tile) => tile.id === targetId);
      if (from < 0 || to < 0 || from === to) return previous;
      const next = previous.filter((tile) => tile.id !== dragId);
      next.splice(to, 0, previous[from]);
      return next;
    });
  }, []);

  const tileAtPoint = (x: number, y: number, excludeId: number): number | null => {
    for (const [id, element] of tileElements.current) {
      if (id === excludeId) continue;
      const rect = element.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) continue;
      if (x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom) {
        return id;
      }
    }
    return null;
  };

  const onTilePointerDown = (id: number, event: React.PointerEvent<HTMLButtonElement>) => {
    if (!canPlay) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture?.(event.pointerId);
    dragRef.current = { id, pointerId: event.pointerId };
    setDraggingId(id);
  };

  const onTilePointerMove = (event: React.PointerEvent<HTMLButtonElement>) => {
    const drag = dragRef.current;
    if (!drag || event.pointerId !== drag.pointerId) return;
    const target = tileAtPoint(event.clientX, event.clientY, drag.id);
    if (target !== null) {
      suppressClickRef.current = true;
      moveTileTo(drag.id, target);
    }
  };

  const endTileDrag = (event: React.PointerEvent<HTMLButtonElement>) => {
    const drag = dragRef.current;
    if (!drag || event.pointerId !== drag.pointerId) return;
    dragRef.current = null;
    setDraggingId(null);
  };

  /** Tap to select, tap another to swap. A drag that moved consumes its own click. */
  const onTileClick = (id: number) => {
    if (!canPlay) return;
    if (suppressClickRef.current) {
      suppressClickRef.current = false;
      return;
    }
    if (selectedId === null) {
      setSelectedId(id);
      return;
    }
    if (selectedId === id) {
      setSelectedId(null);
      return;
    }
    moveTileTo(selectedId, id);
    setSelectedId(null);
  };

  if (loading) {
    return (
      <div>
        <GamesPageHeader eyebrow="Word play" title="Anagram" />
        <div className="flex justify-center py-16 text-muted-foreground">
          <Loader2 className="h-6 w-6 animate-spin" />
        </div>
      </div>
    );
  }

  if (loadError) {
    return (
      <div>
        <GamesPageHeader eyebrow="Word play" title="Anagram" />
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

  if (game.session_status === 'pending' && gameId !== null) {
    return (
      <div>
        <GamesPageHeader
          eyebrow="Word play"
          title="Anagram"
          description="Unscramble the letters in five guesses or fewer."
        />
        <Card>
          <CardContent className="flex flex-col items-center gap-4 py-12 text-center">
            <p className="text-lg font-semibold">Today&apos;s word has not been set yet.</p>
            <p className="max-w-sm text-sm text-muted-foreground">
              {game.message ??
                'The session is waiting for its word. Check back soon -- or look at what else the Games section has to offer.'}
            </p>
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
          eyebrow="Word play"
          title="Anagram"
          description="Unscramble the letters in five guesses or fewer."
        />
        <Card>
          <CardContent className="flex flex-col items-center gap-4 py-12 text-center">
            <p className="text-lg font-semibold">No puzzle available today.</p>
            <p className="max-w-sm text-sm text-muted-foreground">
              An administrator has not published today's word yet. Check back later -- or look
              at what else the Games section has to offer.
            </p>
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
        eyebrow="Word play"
        title="Anagram"
        description="Unscramble the letters in five guesses or fewer."
      />

      <div className="flex flex-col items-center gap-6">
        <div className="flex flex-wrap items-center justify-center gap-4 text-sm">
          <p className="font-mono text-2xl font-bold tabular-nums" data-testid="play-clock">
            {formatClock(displaySeconds)}
          </p>
          {participation && (
            <p className="text-muted-foreground" data-testid="attempt-label">
              Attempts remaining: {attemptsLeft} / {maxAttempts}
            </p>
          )}
        </div>

        {!isStarted || !participation ? (
          <Card className="w-full max-w-md">
            <CardContent className="flex flex-col items-center gap-4 py-10 text-center">
              <p className="text-lg font-semibold">Today's scramble is waiting.</p>
              <p className="max-w-sm text-sm text-muted-foreground">
                Revealing shows your personal scramble -- dealt just for you -- and starts the
                clock. It never shows the answer.
              </p>
              <Button
                type="button"
                size="lg"
                onClick={revealOrResume}
                disabled={actionBusy}
              >
                <Eye />
                {actionBusy ? 'Revealing...' : 'REVEAL WORD'}
              </Button>
            </CardContent>
          </Card>
        ) : (
          <>
            {gateActive ? (
              // The resume gate: the word hides the moment focus leaves, and the first
              // click back lifts it and reopens the clock together. The tiles keep
              // their order in state -- only their letters are withheld from the DOM --
              // so resuming shows exactly the board the player left. A whole-button
              // panel rather than a small control, because the click is the resume: any
              // press on it continues the game.
              <button
                type="button"
                onClick={revealOrResume}
                disabled={actionBusy}
                aria-label="Resume game"
                className="flex w-full max-w-xl flex-col items-center gap-4 rounded-2xl border border-dashed border-border/70 bg-card/50 px-6 py-10 text-center transition-colors hover:border-romantic/60"
              >
                <div aria-hidden="true" className="flex flex-wrap justify-center gap-1.5">
                  {tiles.map((tile) => (
                    <span
                      key={tile.id}
                      className="flex h-12 w-12 items-center justify-center rounded-md border-2 border-border bg-secondary/40 text-xl font-extrabold text-muted-foreground/50 blur-[2px] sm:h-14 sm:w-14"
                    >
                      •
                    </span>
                  ))}
                </div>
                <span className="flex flex-col items-center gap-1">
                  <span className="text-lg font-semibold">
                    {actionBusy ? 'Resuming...' : 'Paused — click anywhere to resume'}
                  </span>
                  <span className="max-w-sm text-sm text-muted-foreground">
                    Your tiles are hidden while you&apos;re away. They return exactly as
                    you left them, and the clock restarts on this click.
                  </span>
                </span>
              </button>
            ) : (
              <>
                <div
                  role="grid"
                  aria-label="Anagram tiles"
                  className="flex max-w-xl flex-wrap justify-center gap-1.5"
                >
                  {tiles.map((tile) => {
                    const isSelected = tile.id === selectedId;
                    const isDragging = tile.id === draggingId;
                    return (
                      <button
                        key={tile.id}
                        type="button"
                        role="gridcell"
                        aria-label={`Tile ${tile.letter}`}
                        data-tile-id={tile.id}
                        disabled={!canPlay}
                        ref={(element) => {
                          if (element) tileElements.current.set(tile.id, element);
                          else tileElements.current.delete(tile.id);
                        }}
                        onPointerDown={(event) => onTilePointerDown(tile.id, event)}
                        onPointerMove={onTilePointerMove}
                        onPointerUp={endTileDrag}
                        onPointerCancel={endTileDrag}
                        onClick={() => onTileClick(tile.id)}
                        className={cn(
                          'flex h-12 w-12 touch-none items-center justify-center rounded-md border-2 text-xl font-extrabold uppercase transition-colors sm:h-14 sm:w-14',
                          'border-border bg-secondary/40',
                          canPlay && 'cursor-grab hover:border-romantic/60 active:cursor-grabbing',
                          isSelected && 'border-romantic ring-2 ring-romantic/60',
                          isDragging && 'border-romantic bg-secondary opacity-90 shadow-lg',
                          'disabled:cursor-not-allowed',
                        )}
                      >
                        {tile.letter}
                      </button>
                    );
                  })}
                </div>

                <p className="max-w-md text-center text-xs text-muted-foreground">
                  Drag a tile to another spot -- or tap one tile, then tap another to swap them.
                </p>

                <div className="flex flex-wrap items-center justify-center gap-3">
                  {isPaused ? (
                    <Button type="button" onClick={revealOrResume} disabled={actionBusy}>
                      <Play />
                      {actionBusy ? 'Resuming...' : 'RESUME'}
                    </Button>
                  ) : (
                    <Button type="button" onClick={submitGuess} disabled={!canPlay}>
                      {submitting ? 'Submitting...' : 'SUBMIT'}
                    </Button>
                  )}
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={resetTiles}
                    disabled={!canPlay}
                  >
                    <RotateCcw />
                    Reset tiles
                  </Button>
                </div>
              </>
            )}

            {participation.guesses.length > 0 && (
              <div className="flex flex-col items-center gap-2">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  Previous guesses
                </h3>
                <ol className="flex flex-col items-center gap-1">
                  {participation.guesses.map((guess, index) => (
                    <li
                      key={`${index}-${guess}`}
                      className="font-mono text-sm uppercase tracking-widest"
                    >
                      <span className="mr-2 text-muted-foreground">{index + 1}.</span>
                      {guess}
                    </li>
                  ))}
                </ol>
              </div>
            )}
          </>
        )}
      </div>

      {isComplete && participation !== null && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 p-4 backdrop-blur-sm">
          <Card className="w-full max-w-sm">
            <CardContent className="flex flex-col items-center gap-4 py-8 text-center">
              <p className="text-xs font-bold uppercase tracking-[0.2em] text-romantic">
                {participation.success ? 'Solved!' : 'Out of guesses'}
              </p>
              {participation.success ? (
                <p className="text-sm text-muted-foreground">
                  You solved it in {participation.attempt_count}{' '}
                  {participation.attempt_count === 1 ? 'guess' : 'guesses'}.
                </p>
              ) : (
                <>
                  <p className="text-xs uppercase tracking-wide text-muted-foreground">
                    Correct answer
                  </p>
                  <p className="text-3xl font-extrabold uppercase tracking-tight">
                    {participation.answer ?? '?????'}
                  </p>
                </>
              )}
              <p className="text-sm text-muted-foreground">
                Time: <span className="font-mono font-bold">{formatClock(participation.active_time_seconds)}</span>
              </p>
              <NavLink to="/games">
                <Button>Back to Games</Button>
              </NavLink>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
};

export default AnagramGamePage;
