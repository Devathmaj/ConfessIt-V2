import { useCallback, useEffect, useState } from 'react';
import { NavLink } from 'react-router-dom';
import { CheckCircle2, Loader2, Trophy } from 'lucide-react';
import { toast } from 'sonner';
import { GamesPageHeader } from '@/components/games/GamesPageHeader';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { describeApiError } from '@/lib/apiError';
import {
  canSubmitSelection,
  formatResponseTime,
  instructionForMode,
  toggleSelection,
  type TriviaAnswerMode,
} from '@/lib/triviaDrop';
import { cn } from '@/lib/utils';
import {
  getCurrentTrivia,
  getTriviaResults,
  submitTriviaAnswer,
  type TriviaCurrentQuestion,
  type TriviaResults,
} from '@/services/api';

/**
 * The live Trivia Drop question: one question, one submission, fastest wins.
 *
 * The screen reads the live question once on mount and never invents state
 * itself: the backend owns which question is live, and a scheduled question
 * never arrives early through this page. Selecting and deselecting options
 * stores nothing -- only the explicit submit consumes the one submission, and
 * an answered player reads a confirmation rather than a board.
 *
 * The key never travels here. The live question carries display options plus
 * the derived mode the screen needs for its instruction ("Select one answer"
 * with single-selection, "Select all that apply" with multi-selection) --
 * never which options are correct, never even how many. Correctness and the
 * leaderboard surface on the ended question's results, never while live.
 */

export const TriviaDropGamePage = () => {
  const [question, setQuestion] = useState<TriviaCurrentQuestion | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selection, setSelection] = useState<number[]>([]);
  const [submitting, setSubmitting] = useState(false);
  // Two-step submit: the first press arms the confirmation, the second press
  // sends it -- so an accidental click never locks the answer.
  const [confirmArmed, setConfirmArmed] = useState(false);
  const [answeredId, setAnsweredId] = useState<string | null>(null);
  const [results, setResults] = useState<TriviaResults | null>(null);
  const [resultsState, setResultsState] = useState<'idle' | 'loading' | 'waiting' | 'ready' | 'error'>('idle');

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const data = await getCurrentTrivia();
      setQuestion(data);
      setSelection([]);
      setConfirmArmed(false);
      if (data.submitted && data.question_id) {
        setAnsweredId(data.question_id);
      }
    } catch (error) {
      setLoadError(describeApiError(error, 'Could not load the live question.'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const choose = (index: number, mode: TriviaAnswerMode) => {
    setSelection((previous) => toggleSelection(previous, index, mode));
    setConfirmArmed(false);
  };

  const submitFinal = async () => {
    if (!question?.question_id || submitting) return;
    if (!canSubmitSelection(selection)) {
      toast.info('Select at least one answer before submitting.');
      return;
    }
    if (!confirmArmed) {
      setConfirmArmed(true);
      return;
    }
    setSubmitting(true);
    try {
      const response = await submitTriviaAnswer(question.question_id, [...selection].sort((a, b) => a - b));
      setAnsweredId(response.question_id);
      setQuestion((previous) =>
        previous ? { ...previous, submitted: true, session_status: 'submitted' } : previous,
      );
      toast.success(response.message);
    } catch (error) {
      toast.error(describeApiError(error, 'Could not submit the answer.'));
      setConfirmArmed(false);
    } finally {
      setSubmitting(false);
    }
  };

  const openResults = async () => {
    if (!answeredId) return;
    setResultsState('loading');
    try {
      setResults(await getTriviaResults(answeredId));
      setResultsState('ready');
    } catch (error) {
      const status = (error as { response?: { status?: number } })?.response?.status;
      if (status === 409) {
        // Live questions refuse results: the key belongs to history, not play.
        setResultsState('waiting');
      } else {
        toast.error(describeApiError(error, 'Could not load the results.'));
        setResultsState('error');
      }
    }
  };

  if (loading) {
    return (
      <div>
        <GamesPageHeader eyebrow="Trivia" title="Trivia Drop" />
        <div className="flex justify-center py-16 text-muted-foreground">
          <Loader2 className="h-6 w-6 animate-spin" />
        </div>
      </div>
    );
  }

  if (loadError) {
    return (
      <div>
        <GamesPageHeader eyebrow="Trivia" title="Trivia Drop" />
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

  if (!question) {
    return (
      <div>
        <GamesPageHeader eyebrow="Trivia" title="Trivia Drop" />
        <Card>
          <CardContent className="flex flex-col items-center gap-4 py-10 text-center">
            <p className="text-sm text-muted-foreground">Could not load the live question.</p>
            <NavLink to="/games">
              <Button variant="outline">Back to Games</Button>
            </NavLink>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (question.submitted) {
    return (
      <div>
        <GamesPageHeader
          eyebrow="Trivia"
          title="Trivia Drop"
          description="One live question at a time — answer fast, answer once."
        />
        <Card>
          <CardContent className="flex flex-col items-center gap-4 py-12 text-center">
            <CheckCircle2 className="h-10 w-10 text-romantic" />
            <p className="text-lg font-semibold">Answer recorded</p>
            <p className="max-w-sm text-sm text-muted-foreground">
              Your answer has been recorded. Results appear here once this
              question ends.
            </p>
            {answeredId && resultsState === 'idle' && (
              <Button type="button" variant="outline" onClick={openResults}>
                <Trophy />
                View results
              </Button>
            )}
            {answeredId && resultsState !== 'idle' && (
              <ResultsPanel
                state={resultsState}
                results={results}
                onRetry={openResults}
              />
            )}
            <NavLink to="/games">
              <Button variant="outline">Back to Games</Button>
            </NavLink>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (question.question_id === null) {
    return (
      <div>
        <GamesPageHeader
          eyebrow="Trivia"
          title="Trivia Drop"
          description="One live question at a time — answer fast, answer once."
        />
        <Card>
          <CardContent className="flex flex-col items-center gap-4 py-12 text-center">
            <p className="text-lg font-semibold">No question is live right now. Check back soon.</p>
            <p className="max-w-sm text-sm text-muted-foreground">
              {question.message ??
                'Questions drop without warning — or look at what else the Games section has to offer.'}
            </p>
            <NavLink to="/games">
              <Button variant="outline">Back to Games</Button>
            </NavLink>
          </CardContent>
        </Card>
      </div>
    );
  }

  const mode = question.answer_mode ?? 'single';
  const instruction = instructionForMode(mode);

  return (
    <div>
      <GamesPageHeader
        eyebrow="Trivia"
        title="Trivia Drop"
        description="One live question at a time — answer fast, answer once."
      />

      <div className="mx-auto flex w-full max-w-2xl flex-col gap-6">
        <Card>
          <CardContent className="flex flex-col gap-5 py-6">
            <h2 className="text-xl font-semibold">{question.question_text}</h2>
            <p className="text-sm font-bold uppercase tracking-wider text-romantic">
              {instruction}
            </p>

            <div role="group" aria-label={instruction} className="flex flex-col gap-2">
              {question.options.map((option) => {
                const checked = selection.includes(option.option_index);
                const inputId = `trivia-option-${option.option_index}`;
                return (
                  <label
                    key={option.option_index}
                    htmlFor={inputId}
                    className={cn(
                      'flex cursor-pointer items-center gap-3 rounded-xl border-2 px-4 py-3 transition-colors',
                      checked
                        ? 'border-romantic bg-romantic/5'
                        : 'border-border/60 hover:border-romantic/50',
                    )}
                  >
                    <input
                      id={inputId}
                      type={mode === 'single' ? 'radio' : 'checkbox'}
                      name="trivia-answer"
                      checked={checked}
                      onChange={() => choose(option.option_index, mode)}
                      className="h-4 w-4 shrink-0 accent-rose-500"
                    />
                    <span className="text-sm font-medium">{option.option_text}</span>
                  </label>
                );
              })}
            </div>

            <div className="flex flex-wrap items-center gap-3">
              <Button
                type="button"
                onClick={submitFinal}
                disabled={!canSubmitSelection(selection) || submitting}
              >
                {submitting
                  ? 'Submitting...'
                  : confirmArmed
                    ? 'Click again to confirm your answer'
                    : 'SUBMIT ANSWER'}
              </Button>
              {selection.length > 0 && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setSelection([]);
                    setConfirmArmed(false);
                  }}
                >
                  Clear selection
                </Button>
              )}
            </div>
            <p className="text-xs text-muted-foreground">
              {mode === 'single'
                ? 'Choosing another option replaces your pick.'
                : 'Tap options to select and deselect them — everything chosen submits as one answer.'}
            </p>
          </CardContent>
        </Card>
      </div>
    </div>
  );
};

const ResultsPanel = ({
  state,
  results,
  onRetry,
}: {
  state: 'loading' | 'waiting' | 'ready' | 'error';
  results: TriviaResults | null;
  onRetry: () => void;
}) => {
  if (state === 'loading') {
    return (
      <div className="flex justify-center py-4 text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin" />
      </div>
    );
  }
  if (state === 'waiting') {
    return (
      <p className="max-w-sm text-sm text-muted-foreground">
        This question is still live — results appear once it ends.
      </p>
    );
  }
  if (state === 'error' || !results) {
    return (
      <Button type="button" variant="outline" size="sm" onClick={onRetry}>
        Try loading results again
      </Button>
    );
  }
  return <ResultsTable results={results} />;
};

const ResultsTable = ({ results }: { results: TriviaResults }) => (
  <div className="flex w-full max-w-xl flex-col gap-4 rounded-2xl border border-border/60 p-4 text-left">
    <div>
      <p className="text-xs font-bold uppercase tracking-wider text-muted-foreground">
        Correct answer{results.correct_indexes.length > 1 ? 's' : ''}
      </p>
      <p className="mt-1 text-sm font-semibold">
        {results.correct_indexes
          .map((index) => results.options.find((option) => option.option_index === index)?.option_text)
          .filter(Boolean)
          .join(', ')}
      </p>
    </div>
    {results.my_submission && (
      <p className="text-sm">
        Your answer:{' '}
        <span className={cn('font-semibold', results.my_submission.is_correct ? 'text-green-600' : 'text-muted-foreground')}>
          {results.my_submission.is_correct ? 'Correct' : 'Incorrect'}
        </span>{' '}
        <span className="font-mono text-muted-foreground">
          ({formatResponseTime(results.my_submission.response_time_ms)})
        </span>
      </p>
    )}
    <ol className="flex flex-col gap-1">
      {results.leaderboard.map((entry) => (
        <li
          key={entry.user_id}
          className="flex items-center justify-between gap-3 rounded-lg bg-secondary/40 px-3 py-2 text-sm"
        >
          <span className="flex min-w-0 items-center gap-2">
            <span className="font-mono font-bold tabular-nums">{entry.rank}.</span>
            <span className="truncate font-medium">{entry.display_name}</span>
            {entry.is_correct ? (
              <span className="shrink-0 text-xs font-bold text-green-600">Correct</span>
            ) : (
              <span className="shrink-0 text-xs text-muted-foreground">Incorrect</span>
            )}
          </span>
          <span className="shrink-0 font-mono tabular-nums text-muted-foreground">
            {formatResponseTime(entry.response_time_ms)}
          </span>
        </li>
      ))}
    </ol>
  </div>
);

export default TriviaDropGamePage;
