import { useCallback, useEffect, useMemo, useState } from 'react';
import { NavLink } from 'react-router-dom';
import { Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { GamesPageHeader } from '@/components/games/GamesPageHeader';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { describeApiError } from '@/lib/apiError';
import { cn } from '@/lib/utils';
import {
  getCurrentWordle,
  submitWordleGuess,
  type WordleCurrentGame,
  type WordleParticipation,
  type WordleTile,
} from '@/services/api';

/**
 * The daily Wordle, one player per participation.
 *
 * The screen reads today's puzzle once on mount and never re-derives anything itself: the
 * server re-scores the stored guesses on every read, so the board a returning player sees is
 * exactly the board they left. Committing a guess replaces the local participation with the
 * response, which doubles as the server's receipt for the attempt.
 *
 * The word is never visible here. ``participation.answer`` stays null until the game is over
 * and the server decides it has nothing left to hide; the board and keyboard only ever hold
 * guesses and their verdicts.
 */

const KEY_ROWS: string[][] = [
  ['Q', 'W', 'E', 'R', 'T', 'Y', 'U', 'I', 'O', 'P'],
  ['A', 'S', 'D', 'F', 'G', 'H', 'J', 'K', 'L'],
  ['ENTER', 'Z', 'X', 'C', 'V', 'B', 'N', 'M', 'BACKSPACE'],
];

/** A guessed tile: solved green, present amber, ruled out zinc. */
const TILE_STYLES: Record<WordleTile, string> = {
  correct: 'border-emerald-600 bg-emerald-600 text-white',
  present: 'border-amber-500 bg-amber-500 text-white',
  absent: 'border-zinc-600 bg-zinc-600 text-white',
};

const EMPTY_TILE = 'border-border bg-secondary/40';

const isLetter = (key: string) => /^[a-zA-Z]$/.test(key);

const initialGame: WordleCurrentGame = {
  game_id: null,
  game_date: null,
  participation: null,
  max_attempts: 6,
  word_length: 5,
};

export const WordleGamePage = () => {
  const [wordle, setWordle] = useState<WordleCurrentGame>(initialGame);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [message, setMessage] = useState('');
  const [submitting, setSubmitting] = useState(false);
  /** Bumped on every refused guess so the current row visibly shakes again. */
  const [shakeKey, setShakeKey] = useState(0);

  const gameId = wordle.game_id;
  const participation = wordle.participation;
  const wordLength = wordle.word_length;
  const maxAttempts = wordle.max_attempts;
  const isComplete = participation !== null && participation.status !== 'in_progress';
  const canType = gameId !== null && !isComplete;

  useEffect(() => {
    let cancelled = false;
    getCurrentWordle()
      .then((data) => {
        if (!cancelled) setWordle(data);
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

  const rejectMessage = useCallback((reason: string) => {
    setShakeKey((key) => key + 1);
    toast.info(reason);
  }, []);

  /** Score the word currently on the row. On a refusal the letters stay for the player. */
  const submitMessage = useCallback(async () => {
    if (!canType || submitting || gameId === null) return;
    const word = message.trim();
    if (word.length === 0) {
      rejectMessage('Type a word to guess.');
      return;
    }
    if (word.length !== wordLength) {
      rejectMessage('Not enough letters.');
      return;
    }
    setSubmitting(true);
    try {
      const response = await submitWordleGuess(gameId, word.toLowerCase());
      setWordle((previous) => ({ ...previous, participation: response }));
      setMessage('');
    } catch (error) {
      rejectMessage(describeApiError(error, 'That guess was rejected.'));
    } finally {
      setSubmitting(false);
    }
  }, [canType, submitting, gameId, message, wordLength, rejectMessage]);

  const pressKey = useCallback(
    (key: string) => {
      if (!canType) return;
      const normalized = key === 'Enter' || key === 'ENTER' ? 'ENTER' : key === 'Backspace' || key === 'BACKSPACE' ? 'BACKSPACE' : key;
      if (normalized === 'ENTER') {
        void submitMessage();
      } else if (normalized === 'BACKSPACE') {
        setMessage((current) => current.slice(0, -1));
      } else if (isLetter(normalized)) {
        setMessage((current) => (current.length >= wordLength ? current : current + normalized.toUpperCase()));
      }
    },
    [canType, submitMessage, wordLength],
  );

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.repeat || event.isComposing || event.metaKey || event.ctrlKey || event.altKey) return;
      pressKey(event.key);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [pressKey]);

  /** The best verdict each used key has earned, for the on-screen keyboard. */
  const keyStates = useMemo(() => {
    const rank: Record<WordleTile, number> = { correct: 2, present: 1, absent: 0 };
    const states: Record<string, WordleTile | undefined> = {};
    participation?.guesses.forEach((guess, rowIndex) => {
      const verdicts = participation.results[rowIndex] ?? [];
      [...guess].forEach((letter, columnIndex) => {
        const verdict = verdicts[columnIndex];
        if (!verdict) return;
        const key = letter.toUpperCase();
        const previous = states[key];
        if (!previous || rank[verdict] > rank[previous]) states[key] = verdict;
      });
    });
    return states;
  }, [participation]);

  const guesses = participation?.guesses ?? [];
  const results = participation?.results ?? [];

  if (loading) {
    return (
      <div>
        <GamesPageHeader eyebrow="Word play" title="Wordle" />
        <div className="flex justify-center py-16 text-muted-foreground">
          <Loader2 className="h-6 w-6 animate-spin" />
        </div>
      </div>
    );
  }

  if (loadError) {
    return (
      <div>
        <GamesPageHeader eyebrow="Word play" title="Wordle" />
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

  if (wordle.session_status === 'pending' && gameId !== null) {
    return (
      <div>
        <GamesPageHeader
          eyebrow="Word play"
          title="Wordle"
          description="Five letters, six guesses, one word a day."
        />
        <Card>
          <CardContent className="flex flex-col items-center gap-4 py-12 text-center">
            <p className="text-lg font-semibold">Today&apos;s word has not been set yet.</p>
            <p className="max-w-sm text-sm text-muted-foreground">
              {wordle.message ??
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
          title="Wordle"
          description="Five letters, six guesses, one word a day."
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

  const currentRowIndex = isComplete ? maxAttempts : guesses.length;
  const currentRowKey = `row-${currentRowIndex}-shake-${shakeKey}`;

  return (
    <div>
      <GamesPageHeader
        eyebrow="Word play"
        title="Wordle"
        description="Five letters, six guesses, one word a day."
      />

      <div className="flex flex-col items-center gap-6">
        <div
          role="grid"
          aria-label="Wordle board"
          className="grid gap-1.5"
          style={{ gridTemplateRows: `repeat(${maxAttempts}, minmax(0, 1fr))` }}
        >
          {Array.from({ length: maxAttempts }, (_, rowIndex) => {
            const filledGuess = guesses[rowIndex] ?? '';
            const verdicts = results[rowIndex] ?? [];
            const isCurrentRow = rowIndex === currentRowIndex;

            return (
              <div
                role="row"
                key={rowIndex}
                data-row={rowIndex}
                className={cn('grid grid-cols-5 gap-1.5', isCurrentRow && shakeKey > 0 && 'animate-wordle-shake')}
              >
                {Array.from({ length: wordLength }, (_, columnIndex) => {
                  const isCurrent = isCurrentRow && !isComplete;
                  const letter = isCurrent
                    ? message[columnIndex] ?? ''
                    : (filledGuess[columnIndex] ?? '').toUpperCase();
                  const verdict = isCurrent ? undefined : verdicts[columnIndex];

                  return (
                    <div
                      role="gridcell"
                      key={isCurrent ? `${columnIndex}-${currentRowKey}` : columnIndex}
                      data-verdict={verdict}
                      className={cn(
                        'flex h-12 w-12 items-center justify-center rounded-md border-2 text-xl font-extrabold uppercase sm:h-14 sm:w-14',
                        verdict ? TILE_STYLES[verdict] : EMPTY_TILE,
                        isCurrent && columnIndex === message.length && message.length < wordLength && 'ring-2 ring-romantic/60',
                      )}
                    >
                      {letter}
                    </div>
                  );
                })}
              </div>
            );
          })}
        </div>

        <div role="group" aria-label="Keyboard" className="w-full max-w-xl">
          {KEY_ROWS.map((row, rowIndex) => (
            <div key={rowIndex} className="mb-2 flex justify-center gap-1.5">
              {row.map((key) => {
                const isEnter = key === 'ENTER';
                const isBackspace = key === 'BACKSPACE';
                const state = keyStates[key];
                return (
                  <button
                    key={key}
                    type="button"
                    aria-label={isEnter ? 'Enter' : isBackspace ? 'Backspace' : key}
                    disabled={!canType || submitting}
                    onClick={() => pressKey(key)}
                    className={cn(
                      'flex h-12 flex-1 items-center justify-center rounded-md border text-sm font-bold uppercase transition-colors',
                      isEnter || isBackspace ? 'max-w-24' : 'max-w-11',
                      isBackspace ? 'text-xs' : '',
                      state
                        ? TILE_STYLES[state]
                        : 'border-border bg-secondary/70 text-foreground hover:bg-secondary',
                      'disabled:cursor-not-allowed disabled:opacity-60',
                    )}
                  >
                    {isBackspace ? '⌫' : key}
                  </button>
                );
              })}
            </div>
          ))}
        </div>

        {gameId !== null && (
          <p className="text-sm text-muted-foreground" data-testid="attempt-label">
            {guesses.length} of {maxAttempts} guesses used
          </p>
        )}
      </div>

      {isComplete && participation !== null && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 p-4 backdrop-blur-sm">
          <Card className="w-full max-w-sm">
            <CardContent className="flex flex-col items-center gap-4 py-8 text-center">
              <p className="text-xs font-bold uppercase tracking-[0.12em] text-romantic">
                {participation.success ? 'Solved' : 'Out of guesses'}
              </p>
              <p className="text-3xl font-bold uppercase tracking-tight">
                {participation.answer ?? '?????'}
              </p>
              <p className="text-sm text-muted-foreground">
                {participation.success
                  ? `You solved today's word in ${participation.attempt_count} guess${
                      participation.attempt_count === 1 ? '' : 'es'
                    }.`
                  : 'The word is above. Come back tomorrow for another one.'}
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

export default WordleGamePage;