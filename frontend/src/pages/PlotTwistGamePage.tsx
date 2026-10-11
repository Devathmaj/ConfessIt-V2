import { useCallback, useEffect, useState } from 'react';
import { BookOpenText, Gift, HeartCrack, HeartHandshake, Loader2, RefreshCw, ScrollText, Sparkles, Trophy } from 'lucide-react';
import { toast } from 'sonner';
import { GamesPageHeader } from '@/components/games/GamesPageHeader';
import { PartnerPicker } from '@/components/games/PartnerPicker';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { describeApiError } from '@/lib/apiError';
import { cn } from '@/lib/utils';
import {
  getPlotTwistPartners,
  getPlotTwistState,
  selectPlotTwistPartner,
  startPlotTwistEvent,
  submitPlotTwistChoice,
  type PlotTwistState,
} from '@/services/api';

/**
 * Plot Twist: an asynchronous cooperative story for a mutual pair.
 *
 * The screen never invents state: it reads the caller's state on mount and
 * after every write, and renders whichever phase the server reports --
 * partner selection, your decision, waiting for the partner's decision, or
 * the settled ending. Only the participant assigned to the current scene
 * sees its choices; the other side sees the narrative and waits. A
 * refresh, a navigation away and back, or a reconnect all land on the same
 * scene with the same history, because the session, its module selection
 * and every decision live in the database rather than in this component.
 *
 * The partner list is the shared PartnerPicker: paged, searched and filtered
 * in SQL, so this page never holds the participant table.
 */

/** "mystery_solved" reads as "Mystery solved" on the outcome chips. */
const humanizeEndingType = (value?: string | null): string | null =>
  value ? value.replace(/_/g, ' ').replace(/^./, (character) => character.toUpperCase()) : null;

/** One popup per settled story per browser session, keyed by that session. */
const revealStorageKey = (sessionId: string | null | undefined): string | null =>
  sessionId ? `plot-twist-reveal:${sessionId}` : null;

/** The decision a treasure id came from, for listing what the couple kept. */
const treasureLabelFor = (id: string, history: PlotTwistState['history']): string | null => {
  for (const entry of history) {
    if (entry.choice_id === id && entry.choice_text) return entry.choice_text;
    if (entry.scene_id === id && entry.scene_title) return entry.scene_title;
  }
  return null;
};

interface PlotTwistRevealProps {
  result: NonNullable<PlotTwistState['result']>;
  history: PlotTwistState['history'];
  compact?: boolean;
}

/**
 * The settled ending, in one place: the popup and the page's result card
 * both render this, so the two can never disagree about the outcome.
 *
 * "Passed" and "failed" are the ending's own ``success`` flag; treasure is
 * tracked beside it, because a run can end short of the goal and still
 * carry discoveries home. Nothing here mentions the leaderboard -- scores
 * settle separately and this screen is about the story.
 */
const PlotTwistReveal = ({ result, history, compact = false }: PlotTwistRevealProps) => {
  const passed = Boolean(result.success);
  const treasures = result.treasures ?? [];
  const treasureLabels = treasures
    .map((id) => treasureLabelFor(id, history))
    .filter((label): label is string => Boolean(label));
  const endingLabel = humanizeEndingType(result.ending_type);

  return (
    <div className="space-y-4">
      <div
        className={cn(
          'flex flex-col items-center gap-2 rounded-2xl border px-4 text-center',
          compact ? 'py-5' : 'py-7',
          passed
            ? 'border-emerald-300/60 bg-emerald-50/60 dark:border-emerald-500/30 dark:bg-emerald-500/10'
            : 'border-amber-300/60 bg-amber-50/60 dark:border-amber-500/30 dark:bg-amber-500/10',
        )}
      >
        {passed ? (
          <Trophy className="h-9 w-9 text-amber-500" />
        ) : (
          <HeartCrack className="h-9 w-9 text-rose-400" />
        )}
        <p className={cn('font-dancing text-romantic', compact ? 'text-3xl' : 'text-4xl')}>
          {passed ? 'You made it!' : 'You didn’t make it'}
        </p>
        {result.ending_title && (
          <p className={cn('font-semibold', compact ? 'text-lg' : 'text-xl')}>{result.ending_title}</p>
        )}
        <p className="max-w-md text-sm text-muted-foreground">
          {passed
            ? 'Together, your decisions carried the story home.'
            : treasures.length > 0
              ? `The story slipped through your fingers — but you still carry ${
                  treasures.length === 1 ? 'one treasure' : `${treasures.length} treasures`
                } from it.`
              : 'The story took its own turn this time — play it again together for another path.'}
        </p>
        <div className="flex flex-wrap items-center justify-center gap-2 pt-1">
          {endingLabel && <Badge>{endingLabel}</Badge>}
          <Badge variant="secondary">
            {result.decisions_made} {result.decisions_made === 1 ? 'decision' : 'decisions'}
          </Badge>
          {treasures.length > 0 && (
            <Badge variant="outline" className="gap-1">
              <Gift className="h-3 w-3" />
              {treasures.length === 1 ? '1 treasure' : `${treasures.length} treasures`}
            </Badge>
          )}
        </div>
      </div>

      {result.narrative && (
        <div
          className={cn(
            'overflow-y-auto whitespace-pre-line rounded-2xl border border-border/50 bg-background/50 p-4 text-sm leading-relaxed',
            compact ? 'max-h-56' : 'max-h-80',
          )}
        >
          {result.narrative}
        </div>
      )}

      {treasureLabels.length > 0 && (
        <div className="rounded-2xl border border-border/50 bg-background/50 p-4">
          <p className="flex items-center gap-2 text-sm font-semibold">
            <Gift className="h-4 w-4 text-amber-500" />
            Treasures you picked up along the way
          </p>
          <ul className="mt-2 space-y-1 text-sm text-muted-foreground">
            {treasureLabels.map((label, index) => (
              <li key={`${label}-${index}`}>“{label}”</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
};

export const PlotTwistGamePage = () => {
  const [state, setState] = useState<PlotTwistState | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [submittingId, setSubmittingId] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [showReveal, setShowReveal] = useState(false);

  // The outcome pops up the moment the story completes -- for the partner
  // who made the last call and for the one the poll catches up on -- once
  // per browser session; the result card below carries it for later visits.
  // The trigger rides the data paths (load, poll, submit) rather than an
  // effect: completion is an arrival, not derived state.
  const maybeRevealFor = useCallback((next: PlotTwistState) => {
    const key = next.stage === 'completed' ? revealStorageKey(next.session_id) : null;
    if (!key) return;
    try {
      if (window.sessionStorage.getItem(key)) return;
    } catch {
      // Storage can be unavailable (private modes); the popup matters more
      // than the once-only memo it would have bought.
    }
    setShowReveal(true);
  }, []);

  const refresh = useCallback(async () => {
    try {
      const next = await getPlotTwistState();
      setState(next);
      maybeRevealFor(next);
      setLoadError(null);
    } catch (error) {
      setLoadError(describeApiError(error, 'Could not load the story.'));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [maybeRevealFor]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // While waiting for the partner's decision, re-read so their choice
  // appears without a manual refresh. Nothing else polls.
  useEffect(() => {
    if (state?.stage !== 'awaiting_partner') return;
    let cancelled = false;
    let inFlight = false;
    const tick = async () => {
      if (inFlight || cancelled) return;
      inFlight = true;
      try {
        const next = await getPlotTwistState();
        if (!cancelled) {
          setState(next);
          maybeRevealFor(next);
        }
      } catch {
        // Keep the waiting screen; the next tick retries.
      } finally {
        inFlight = false;
      }
    };
    const timer = window.setInterval(() => void tick(), 15000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [state?.stage, maybeRevealFor]);

  const handleChoose = async (candidateId: string) => {
    try {
      setState(await selectPlotTwistPartner(candidateId));
    } catch (error) {
      toast.error(describeApiError(error, 'Could not save your choice.'));
      await refresh();
    }
  };

  const handleStart = async () => {
    if (starting) return;
    setStarting(true);
    try {
      setState(await startPlotTwistEvent());
    } catch (error) {
      toast.error(describeApiError(error, 'Could not start the story.'));
      await refresh();
    } finally {
      setStarting(false);
    }
  };

  const handleChoice = async (choiceId: string) => {
    if (!state || submittingId !== null) return;
    setSubmittingId(choiceId);
    try {
      const next = await submitPlotTwistChoice(choiceId, state.state_version);
      setState(next);
      maybeRevealFor(next);
    } catch (error) {
      toast.error(describeApiError(error, 'Could not record your decision.'));
      await refresh();
    } finally {
      setSubmittingId(null);
    }
  };

  const manualRefresh = async () => {
    setRefreshing(true);
    await refresh();
  };

  const dismissReveal = () => {
    setShowReveal(false);
    const key = revealStorageKey(state?.session_id);
    if (!key) return;
    try {
      window.sessionStorage.setItem(key, '1');
    } catch {
      // No memo, just no second popup this visit.
    }
  };

  if (loading) {
    return (
      <div>
        <GamesPageHeader
          eyebrow="Couple game"
          title="Plot Twist"
          description="A story you and your partner write together, one decision at a time."
        />
        <p className="flex items-center gap-2 py-14 text-center text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          Opening the story…
        </p>
      </div>
    );
  }

  if (loadError || !state) {
    return (
      <div>
        <GamesPageHeader
          eyebrow="Couple game"
          title="Plot Twist"
          description="A story you and your partner write together, one decision at a time."
        />
        <div className="rounded-2xl border border-dashed border-border/70 px-6 py-14 text-center">
          <p className="text-base font-bold">The story is unavailable</p>
          <p className="mx-auto mt-1 max-w-sm text-sm text-muted-foreground">{loadError}</p>
          <Button type="button" variant="outline" size="sm" className="mt-5 rounded-full" onClick={manualRefresh}>
            Try again
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div>
      <GamesPageHeader
        eyebrow="Couple game"
        title="Plot Twist"
        description="A story you and your partner write together, one decision at a time."
      />

      {(state.stage === 'no_game' || state.stage === 'selecting_partner') && (
        <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-xl">
              <HeartHandshake className="h-5 w-5" />
              Choose your partner
            </CardTitle>
            <CardDescription>
              {state.stage === 'no_game'
                ? 'There is no active Plot Twist event right now — but you can still pick your partner ahead of it.'
                : 'Pick the person you’ll write this story with. The story starts once they pick you back.'}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {state.my_selection && !state.partner && (
              <p className="text-sm">
                You chose{' '}
                <span className="font-semibold">{state.my_selection.selected_user_name}</span>.
                Waiting for them to choose you back.
              </p>
            )}
            {state.partner && (
              <div className="rounded-xl border border-romantic/40 bg-romantic/5 p-4">
                <p className="text-sm">
                  You and <span className="font-semibold">{state.partner.name}</span> chose each
                  other.
                </p>
                <Button
                  type="button"
                  className="mt-3 rounded-full"
                  disabled={starting}
                  onClick={handleStart}
                >
                  {starting ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <HeartHandshake className="h-4 w-4" />
                  )}
                  {starting ? 'Opening…' : 'Begin the story'}
                </Button>
              </div>
            )}
            <PartnerPicker
              layout="grid"
              loadPartners={getPlotTwistPartners}
              selectedId={state.my_selection?.selected_user_id ?? null}
              onChoose={handleChoose}
              emptyMessage="Nobody matches — try a different search."
            />
          </CardContent>
        </Card>
      )}

      {(state.stage === 'awaiting_you' || state.stage === 'awaiting_partner') && state.scene && (
        <Card className="rounded-2xl border-border/60 bg-card/70 backdrop-blur">
          <CardHeader>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <CardTitle className="flex items-center gap-2 text-xl">
                <BookOpenText className="h-5 w-5" />
                {state.scene.title}
              </CardTitle>
              <div className="flex items-center gap-2">
                {state.decisions_made > 0 && (
                  <Badge variant="outline">{state.decisions_made} decided</Badge>
                )}
                <Badge variant={state.stage === 'awaiting_you' ? 'default' : 'secondary'}>
                  {state.stage === 'awaiting_you' ? 'Your decision' : 'Partner’s decision'}
                </Badge>
              </div>
            </div>
            {state.partner && (
              <CardDescription>
                Playing with {state.partner.name} — decisions alternate as the story assigns them.
              </CardDescription>
            )}
          </CardHeader>
          <CardContent className="space-y-5">
            <p className="whitespace-pre-line text-sm leading-relaxed">{state.scene.narrative_text}</p>

            {state.stage === 'awaiting_you' ? (
              <div className="flex flex-col gap-2">
                {state.scene.choices.length === 0 && (
                  <p className="text-sm text-muted-foreground">
                    No choices are available in the current situation. Refresh — the story may have moved on.
                  </p>
                )}
                {state.scene.choices.map((choice) => (
                  <Button
                    key={choice.choice_id}
                    type="button"
                    variant="outline"
                    className="h-auto justify-start whitespace-normal rounded-xl px-4 py-3 text-left"
                    disabled={submittingId !== null}
                    onClick={() => void handleChoice(choice.choice_id)}
                  >
                    {submittingId === choice.choice_id ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : null}
                    <span>{choice.text}</span>
                  </Button>
                ))}
              </div>
            ) : (
              <div className="rounded-xl border border-border/50 bg-background/40 p-4 text-sm text-muted-foreground">
                <p className="flex items-center gap-2 font-semibold text-foreground">
                  <HeartHandshake className="h-4 w-4" />
                  Waiting for {state.partner?.name ?? 'your partner'}
                </p>
                <p className="mt-1">
                  They hold the next decision. You’ll see the outcome here as soon as they choose —
                  this page checks back on its own.
                </p>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="mt-3 rounded-full"
                  disabled={refreshing}
                  onClick={manualRefresh}
                >
                  <RefreshCw className={cn('h-3 w-3', refreshing && 'animate-spin')} />
                  {refreshing ? 'Checking…' : 'Check now'}
                </Button>
              </div>
            )}

            {state.history.length > 0 && (
              <div className="border-t border-border/40 pt-4">
                <h3 className="flex items-center gap-2 text-sm font-semibold">
                  <ScrollText className="h-4 w-4" />
                  The story so far
                </h3>
                <ol className="mt-2 space-y-2">
                  {state.history.map((entry) => (
                    <li key={entry.seq} className="rounded-xl border border-border/50 bg-background/40 p-3 text-sm">
                      <p className="font-semibold">
                        {entry.seq}. {entry.scene_title ?? entry.scene_id}
                      </p>
                      <p className="mt-0.5 text-muted-foreground">
                        {entry.actor_name ?? 'Someone'} chose “{entry.choice_text ?? entry.choice_id}”.
                      </p>
                      {entry.consequence_text && (
                        <p className="mt-0.5 whitespace-pre-line">{entry.consequence_text}</p>
                      )}
                    </li>
                  ))}
                </ol>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {state.stage === 'completed' && state.result && (
        <Card className="overflow-hidden rounded-3xl border-romantic/40 bg-card/70 shadow-[var(--shadow-romantic)] backdrop-blur">
          <div className="bg-gradient-romantic px-6 py-5 text-white">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="space-y-1">
                <p className="text-[0.65rem] uppercase tracking-[0.25em] text-white/80">
                  {state.result.success ? 'Your story has ended — you made it' : 'Your story has ended'}
                </p>
                <CardTitle className="font-dancing text-3xl text-white">
                  {state.result.ending_title ?? 'The end'}
                </CardTitle>
              </div>
              <Button
                type="button"
                variant="secondary"
                size="sm"
                className="rounded-full"
                onClick={() => setShowReveal(true)}
              >
                <Sparkles className="h-3.5 w-3.5" />
                Replay the reveal
              </Button>
            </div>
          </div>
          <CardContent className="space-y-4 pt-6">
            <PlotTwistReveal result={state.result} history={state.history} />
            {state.history.length > 0 && (
              <div className="border-t border-border/40 pt-4">
                <h3 className="text-sm font-semibold">Choices made throughout the adventure</h3>
                <ol className="mt-2 space-y-2">
                  {state.history.map((entry) => (
                    <li key={entry.seq} className="rounded-xl border border-border/50 bg-background/40 p-3 text-sm">
                      <p className="font-semibold">
                        {entry.seq}. {entry.scene_title ?? entry.scene_id}
                      </p>
                      <p className="mt-0.5 text-muted-foreground">
                        {entry.actor_name ?? 'Someone'} chose “{entry.choice_text ?? entry.choice_id}”.
                      </p>
                      {entry.consequence_text && (
                        <p className="mt-0.5 whitespace-pre-line">{entry.consequence_text}</p>
                      )}
                    </li>
                  ))}
                </ol>
              </div>
            )}
            <p className="text-xs text-muted-foreground">
              Scores settle on the couples leaderboard once the event closes — a featured story earns double.
            </p>
          </CardContent>
        </Card>
      )}

      <Dialog
        open={showReveal}
        onOpenChange={(open) => {
          if (!open) dismissReveal();
        }}
      >
        <DialogContent className="max-h-[90vh] max-w-xl overflow-y-auto rounded-3xl border-romantic/40">
          <DialogHeader>
            <DialogTitle className="flex items-center justify-center gap-2 font-dancing text-2xl text-romantic sm:justify-start">
              <Sparkles className="h-5 w-5" />
              The story ends
            </DialogTitle>
            <DialogDescription>
              {state.partner
                ? `How your choices with ${state.partner.name} wrote the ending.`
                : 'How your choices wrote the ending.'}
            </DialogDescription>
          </DialogHeader>
          {state.result && <PlotTwistReveal result={state.result} history={state.history} compact />}
          <DialogFooter>
            <Button type="button" className="w-full rounded-full sm:w-auto" onClick={dismissReveal}>
              Keep the story
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
};

export default PlotTwistGamePage;
