import { useCallback, useEffect, useState } from 'react';
import { HeartHandshake, Loader2, RefreshCw, Users } from 'lucide-react';
import { toast } from 'sonner';
import { GamesPageHeader } from '@/components/games/GamesPageHeader';
import { PartnerPicker } from '@/components/games/PartnerPicker';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { describeApiError } from '@/lib/apiError';
import { cn } from '@/lib/utils';
import {
  getCouplePartners,
  getCoupleState,
  selectCouplePartner,
  submitCoupleAnswer,
  submitCouplePrediction,
  type CoupleCurrentState,
} from '@/services/api';

/**
 * The Couple Questionnaire: pick a partner, answer for yourself, predict them.
 *
 * The screen never invents state: it reads the caller's state on mount and after
 * every write, and renders whichever phase the server reports -- partner
 * selection, answering, waiting, predicting, or the settled score. A refresh, a
 * navigation away and back, or a reconnect all land on the same phase with the
 * same questions, because the session, its 25 questions and every answer live in
 * the database rather than in this component.
 *
 * Choosing a partner notifies nobody and starts nothing: the choice is pending
 * until the chosen user independently chooses back, at which point the server
 * creates the one session for the pair. Answering renders the bank's `X` as
 * "you"; predicting renders it as the partner's name -- both substitutions
 * arrive already done from the server, so this page never rewrites a question.
 *
 * The partner list itself is the shared PartnerPicker: paged, searched and
 * filtered in SQL, so the page never holds the participant table.
 */

const ProgressDots = ({ done, total }: { done: number; total: number }) => (
  <div className="flex flex-wrap gap-1" aria-label={`${done} of ${total} done`}>
    {Array.from({ length: total }, (_, index) => (
      <span
        key={index}
        className={cn(
          'h-2 w-2 rounded-full',
          index < done ? 'bg-gradient-romantic' : 'bg-secondary',
        )}
      />
    ))}
  </div>
);

export const CoupleQuestionnairePage = () => {
  const [state, setState] = useState<CoupleCurrentState | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [submittingId, setSubmittingId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setState(await getCoupleState());
      setLoadError(null);
    } catch (error) {
      setLoadError(describeApiError(error, 'Could not load the game.'));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleChoose = async (candidateId: string) => {
    try {
      setState(await selectCouplePartner(candidateId));
      toast.success('Choice saved. The game starts when they choose you back.');
    } catch (error) {
      toast.error(describeApiError(error, 'Could not save your choice.'));
    }
  };

  const handleAnswer = async (sessionQuestionId: string, optionIndex: number) => {
    setSubmittingId(sessionQuestionId);
    try {
      setState(await submitCoupleAnswer(sessionQuestionId, optionIndex));
    } catch (error) {
      toast.error(describeApiError(error, 'Could not save your answer.'));
      await refresh();
    } finally {
      setSubmittingId(null);
    }
  };

  const handlePredict = async (sessionQuestionId: string, optionIndex: number) => {
    setSubmittingId(sessionQuestionId);
    try {
      setState(await submitCouplePrediction(sessionQuestionId, optionIndex));
    } catch (error) {
      toast.error(describeApiError(error, 'Could not save your prediction.'));
      await refresh();
    } finally {
      setSubmittingId(null);
    }
  };

  const handleRefresh = async () => {
    setRefreshing(true);
    await refresh();
  };

  // While waiting on the partner, re-read the state on an interval so the next
  // stage opens by itself: the server unlocks it the moment both sides have
  // completed the section, and this is what surfaces it without any manual
  // "check" action from the player.
  useEffect(() => {
    if (state?.stage !== 'waiting_partner') return;
    let inFlight = false;
    const timer = setInterval(() => {
      if (inFlight) return;
      inFlight = true;
      void refresh().finally(() => {
        inFlight = false;
      });
    }, 15000);
    return () => clearInterval(timer);
  }, [state?.stage, refresh]);

  if (loading) {
    return (
      <div>
        <GamesPageHeader
          eyebrow="Games"
          title="Couple Questionnaire"
          description="Answer for yourself, then predict your partner."
        />
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          Loading the game…
        </p>
      </div>
    );
  }

  if (loadError || state === null) {
    return (
      <div>
        <GamesPageHeader
          eyebrow="Games"
          title="Couple Questionnaire"
          description="Answer for yourself, then predict your partner."
        />
        <p role="alert" className="text-sm font-medium text-destructive">
          {loadError ?? 'Could not load the game.'}
        </p>
        <Button type="button" variant="outline" size="sm" className="mt-3" onClick={handleRefresh}>
          <RefreshCw />
          Try again
        </Button>
      </div>
    );
  }

  return (
    <div>
      <GamesPageHeader
        eyebrow="Games"
        title="Couple Questionnaire"
        description="Answer for yourself, then predict your partner — 15 and 15."
      />

      {state.stage === 'no_game' && (
        <Card className="mb-6 rounded-2xl border-border/60 bg-card/70">
          <CardHeader>
            <CardTitle className="text-lg">No featured day yet</CardTitle>
            <CardDescription>
              The next round opens once a day is featured. You can still pick a
              partner below — the game starts when they pick you back after a day opens.
            </CardDescription>
          </CardHeader>
        </Card>
      )}

      {(state.stage === 'selecting_partner' || state.stage === 'no_game') && (
        <Card className="rounded-2xl border-border/60 bg-card/70">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-xl">
              <Users className="h-5 w-5" />
              Choose your partner
            </CardTitle>
            <CardDescription>
              {state.my_selection
                ? `You chose ${state.my_selection.selected_user_name}. Choosing someone else replaces your pick. The game starts only when they choose you back — nobody is notified.`
                : 'Nothing starts until the two of you have chosen each other. Nobody is notified of your pick.'}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {state.my_selection && (
              <p className="rounded-xl border border-border/60 bg-muted/40 p-3 text-sm">
                Current pick:{' '}
                <span className="font-semibold">{state.my_selection.selected_user_name}</span>
              </p>
            )}

            <PartnerPicker
              loadPartners={getCouplePartners}
              selectedId={state.my_selection?.selected_user_id ?? null}
              onChoose={handleChoose}
            />
          </CardContent>
        </Card>
      )}

      {(state.stage === 'answering' || state.stage === 'waiting_partner') && (
        <div className="flex flex-col gap-4">
          <Card className="rounded-2xl border-border/60 bg-card/70">
            <CardContent className="space-y-2 pt-6">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <p className="text-sm font-semibold">
                  Your answers: {state.my_answers_submitted} of {state.my_answers_total}
                </p>
                <Badge variant="secondary">
                  {state.partner?.name ?? 'Partner'}: {state.partner_answers_submitted} of{' '}
                  {state.partner_answers_total}
                </Badge>
              </div>
              <ProgressDots done={state.my_answers_submitted} total={state.my_answers_total} />
              {state.stage === 'answering' && (
                <p className="text-sm text-muted-foreground">
                  Predictions open once both of you have answered all{' '}
                  {state.my_answers_total}.
                </p>
              )}
              {state.stage === 'waiting_partner' && state.waiting_for === 'answers' && (
                <p role="status" className="text-sm text-muted-foreground">
                  You answered all {state.my_answers_total}. Predictions open once both of
                  you have finished this section —{' '}
                  {state.partner?.name ?? 'your partner'} still has questions to answer.
                  This page checks automatically, so just stay here.
                </p>
              )}
            </CardContent>
          </Card>

          {state.questions.map((question, index) => (
            <Card key={question.session_question_id} className="rounded-2xl border-border/60 bg-card/70">
              <CardHeader>
                <CardDescription>
                  Question {index + 1} of {state.questions.length}
                  {question.shared && ' · Shared'}
                </CardDescription>
                <CardTitle className="text-lg">{question.question_text}</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="grid gap-2" role="group" aria-label={question.question_text}>
                  {question.options.map((option, optionIndex) => {
                    const selected = question.selected_option_index === optionIndex;
                    const answered = question.selected_option_index !== null;
                    return (
                      <Button
                        key={optionIndex}
                        type="button"
                        variant={selected ? 'default' : 'outline'}
                        disabled={answered || submittingId !== null}
                        aria-pressed={selected}
                        className={cn('justify-start', selected && 'font-semibold')}
                        onClick={() => void handleAnswer(question.session_question_id, optionIndex)}
                      >
                        {option}
                      </Button>
                    );
                  })}
                </div>
              </CardContent>
            </Card>
          ))}

        </div>
      )}

      {(state.stage === 'prediction' ||
        (state.stage === 'waiting_partner' && state.waiting_for === 'predictions')) && (
        <div className="flex flex-col gap-4">
          <Card className="rounded-2xl border-border/60 bg-card/70">
            <CardContent className="space-y-2 pt-6">
              <p className="text-sm font-semibold">
                Your predictions: {state.my_predictions_submitted} of {state.my_predictions_total}
              </p>
              <ProgressDots done={state.my_predictions_submitted} total={state.my_predictions_total} />
              <p className="text-sm text-muted-foreground">
                Guess what {state.partner?.name ?? 'your partner'} answered. Each correct
                prediction scores one point. The result opens once both of you have
                predicted all {state.my_predictions_total}.
              </p>
              {state.stage === 'waiting_partner' && (
                <p role="status" className="text-sm text-muted-foreground">
                  You predicted all {state.my_predictions_total}. The result opens once
                  both of you have finished this section —{' '}
                  {state.partner?.name ?? 'your partner'} still has predictions to make.
                  This page checks automatically, so just stay here.
                </p>
              )}
            </CardContent>
          </Card>

          {state.predictions.map((question, index) => (
            <Card key={question.session_question_id} className="rounded-2xl border-border/60 bg-card/70">
              <CardHeader>
                <CardDescription>
                  Prediction {index + 1} of {state.predictions.length}
                  {question.shared && ' · Shared'}
                </CardDescription>
                <CardTitle className="text-lg">{question.question_text}</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="grid gap-2" role="group" aria-label={question.question_text}>
                  {question.options.map((option, optionIndex) => {
                    const selected = question.predicted_option_index === optionIndex;
                    const predicted = question.predicted_option_index !== null;
                    return (
                      <Button
                        key={optionIndex}
                        type="button"
                        variant={selected ? 'default' : 'outline'}
                        disabled={predicted || submittingId !== null}
                        aria-pressed={selected}
                        className={cn('justify-start', selected && 'font-semibold')}
                        onClick={() => void handlePredict(question.session_question_id, optionIndex)}
                      >
                        {option}
                      </Button>
                    );
                  })}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {state.stage === 'completed' && state.result && (
        <div className="flex flex-col gap-4">
          <Card className="rounded-2xl border-border/60 bg-card/70">
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-xl">
                <HeartHandshake className="h-5 w-5" />
                You both finished
              </CardTitle>
              <CardDescription>
                One point per correct prediction, out of {state.result.max_score}.
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-wrap gap-3">
              <div className="min-w-36 flex-1 rounded-xl border border-border/60 p-4 text-center">
                <p className="text-xs uppercase tracking-wide text-muted-foreground">You</p>
                <p className="text-3xl font-bold">
                  {state.result.my_score}
                  <span className="text-lg text-muted-foreground">/{state.result.max_score}</span>
                </p>
              </div>
              <div className="min-w-36 flex-1 rounded-xl border border-border/60 p-4 text-center">
                <p className="text-xs uppercase tracking-wide text-muted-foreground">
                  {state.partner?.name ?? 'Partner'}
                </p>
                <p className="text-3xl font-bold">
                  {state.result.partner_score}
                  <span className="text-lg text-muted-foreground">/{state.result.max_score}</span>
                </p>
              </div>
            </CardContent>
          </Card>

          {state.result.review.map((item) => (
            <Card key={item.session_question_id} className="rounded-2xl border-border/60 bg-card/70">
              <CardHeader>
                <CardDescription>
                  {item.shared ? 'Shared question' : 'Their question'} ·{' '}
                  {item.correct ? 'You got it' : 'Not quite'}
                </CardDescription>
                <CardTitle className="text-lg">{item.question_text}</CardTitle>
              </CardHeader>
              <CardContent className="flex flex-col gap-2 text-sm">
                <p>
                  They answered:{' '}
                  <span className="font-semibold">{item.options[item.original_option_index]}</span>
                </p>
                <p>
                  You predicted:{' '}
                  <span className={cn('font-semibold', !item.correct && 'text-destructive')}>
                    {item.options[item.predicted_option_index]}
                  </span>
                </p>
                <Badge variant={item.correct ? 'default' : 'secondary'} className="self-start">
                  {item.correct ? '+1 point' : '0 points'}
                </Badge>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
};

export default CoupleQuestionnairePage;
