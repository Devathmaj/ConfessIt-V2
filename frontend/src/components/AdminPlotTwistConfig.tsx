import { useCallback, useEffect, useState } from 'react';
import { BookOpenText, HeartHandshake, Loader2, RefreshCw } from 'lucide-react';
import { toast } from 'sonner';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { AdminGameStatusControls } from '@/components/AdminGameStatusControls';
import {
  AiGenerationStatusLine,
  AiSessionHistory,
  RevokeSessionControl,
} from '@/components/AdminAiSession';
import { describeApiError } from '@/lib/apiError';
import {
  getGameConfigs,
  getPlotTwistSessionHistory,
  getPlotTwistSessionOverview,
  revokePlotTwistSession,
  updateGameConfig,
  type AiSessionHistoryEntry,
  type GameConfig,
  type PlotTwistSessionOverview,
} from '@/services/api';

const DIFFICULTIES: Array<NonNullable<GameConfig['difficulty']>> = ['easy', 'medium', 'hard'];

/**
 * How many sessions one history read returns. Session rows are small, so ten
 * keeps a read cheap while "Show more" still grows the list by a visible amount.
 */
const HISTORY_PAGE_SIZE = 10;

/**
 * The Plot Twist console: its own page under /admin/games/manage.
 *
 * Sessions are generated and published automatically on the configured
 * schedule, so the administrator configures only the difficulty and frequency,
 * inspects the currently published story (its scenes, choices and endings),
 * pages back through session history, and can revoke the live session. The
 * blueprint/package workflow is internal to the generation pipeline and is
 * deliberately absent from this screen.
 */
export const AdminPlotTwistConfig = () => {
  const [difficulty, setDifficulty] = useState<NonNullable<GameConfig['difficulty']>>('medium');
  const [savedDifficulty, setSavedDifficulty] =
    useState<NonNullable<GameConfig['difficulty']>>('medium');
  const [overview, setOverview] = useState<PlotTwistSessionOverview | null>(null);
  const [history, setHistory] = useState<AiSessionHistoryEntry[]>([]);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [revoking, setRevoking] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [expandedScene, setExpandedScene] = useState<string | null>(null);

  /**
   * One console read: the current published session plus generation state, and
   * the first history page. Every state write happens after the awaits, so an
   * effect that calls this never sets state synchronously. The silent variant
   * keeps an in-flight generation fresh without touching the staged
   * difficulty picker.
   */
  const load = useCallback(async (options?: { silent?: boolean }) => {
    try {
      const [current, page] = await Promise.all([
        getPlotTwistSessionOverview(),
        getPlotTwistSessionHistory({ skip: 0, limit: HISTORY_PAGE_SIZE }),
      ]);
      setOverview(current);
      setHistory(page.sessions);
      setHistoryTotal(page.total);
      setError(null);
      if (!options?.silent) {
        const configs = await getGameConfigs();
        const level = configs.find((entry) => entry.game_id === 'plot-twist')?.difficulty;
        if (level) {
          setDifficulty(level);
          setSavedDifficulty(level);
        }
      }
    } catch (loadError) {
      setError(describeApiError(loadError, 'Could not load the Plot Twist console.'));
    } finally {
      setLoading(false);
    }
  }, []);

  // The first read runs on the next tick: starting the request inside the timer
  // callback keeps this effect free of state writes (and satisfies the
  // set-state-in-effect rule) while still loading the console immediately.
  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  const refreshNow = async () => {
    setRefreshing(true);
    try {
      await load();
    } finally {
      setRefreshing(false);
    }
  };

  const generation = overview?.generation ?? null;
  const generationLive =
    generation !== null &&
    (generation.status === 'queued' ||
      generation.status === 'running' ||
      generation.status === 'retry_wait');

  // While a cycle is queued or generating, refresh quietly so the session
  // appears (or the failure is reported) without the admin reloading the page.
  useEffect(() => {
    if (!generationLive) return undefined;
    const timer = window.setTimeout(() => void load({ silent: true }), 8000);
    return () => window.clearTimeout(timer);
  }, [generationLive, generation?.status, load]);

  /** Grow the history downwards: the next backend page appends beneath what is shown. */
  const showMoreHistory = async () => {
    if (loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await getPlotTwistSessionHistory({
        skip: history.length,
        limit: HISTORY_PAGE_SIZE,
      });
      setHistory((previous) => [...previous, ...page.sessions]);
      setHistoryTotal(page.total);
    } catch (loadError) {
      setError(describeApiError(loadError, 'Could not load more sessions.'));
    } finally {
      setLoadingMore(false);
    }
  };

  const dirty = difficulty !== savedDifficulty;

  const saveDifficulty = async () => {
    setSaving(true);
    setNotice(null);
    try {
      const updated = await updateGameConfig('plot-twist', { difficulty });
      const level = updated.difficulty ?? difficulty;
      setDifficulty(level);
      setSavedDifficulty(level);
      toast.success('Difficulty saved. It applies from the next generated session.');
    } catch (saveError) {
      setNotice(describeApiError(saveError, 'Could not save the difficulty.'));
    } finally {
      setSaving(false);
    }
  };

  const revoke = async () => {
    setRevoking(true);
    setNotice(null);
    try {
      const result = await revokePlotTwistSession();
      if (result.action === 'revoked') {
        toast.success(
          result.revoked_until
            ? `Session revoked. The next one is generated no earlier than ${result.revoked_until}.`
            : 'Session revoked.',
        );
      } else {
        toast.success('No session was live; nothing to revoke.');
      }
      await load({ silent: true });
    } catch (revokeError) {
      setNotice(describeApiError(revokeError, 'Could not revoke the session.'));
    } finally {
      setRevoking(false);
    }
  };

  const session = overview?.session ?? null;
  const storyPackage = session?.package ?? {};
  const scenes = storyPackage.scenes ?? [];
  const endings = storyPackage.endings ?? [];

  return (
    <Card className="[border-radius:var(--edge-card)] border-border/60 bg-card/70 backdrop-blur">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2 text-xl">
              <HeartHandshake className="h-5 w-5" />
              Plot Twist
            </CardTitle>
            <CardDescription>
              An asynchronous story for couples. New sessions are generated and published
              automatically on the configured frequency — configure the difficulty, inspect the
              current story, or revoke it below.
            </CardDescription>
          </div>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => void refreshNow()}
            disabled={refreshing}
          >
            <RefreshCw className={refreshing ? 'animate-spin' : undefined} />
            Refresh
          </Button>
        </div>
      </CardHeader>

      <CardContent className="space-y-6">
        <AdminGameStatusControls gameId="plot-twist" gameName="Plot Twist" />

        <div className="flex flex-col gap-3 border-t border-border/40 pt-4">
          <h4 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            Difficulty
          </h4>
          <div className="flex flex-wrap items-end gap-3">
            <div className="flex flex-col gap-2">
              <Label htmlFor="plot-twist-difficulty">Story difficulty</Label>
              <Select
                value={difficulty}
                onValueChange={(next) =>
                  setDifficulty(next as NonNullable<GameConfig['difficulty']>)
                }
                disabled={saving}
              >
                <SelectTrigger id="plot-twist-difficulty" className="w-44">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {DIFFICULTIES.map((level) => (
                    <SelectItem key={level} value={level}>
                      {level[0].toUpperCase() + level.slice(1)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {dirty && (
              <>
                <Button type="button" size="sm" disabled={saving} onClick={() => void saveDifficulty()}>
                  {saving ? 'Saving…' : 'Save'}
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  disabled={saving}
                  onClick={() => setDifficulty(savedDifficulty)}
                >
                  Discard
                </Button>
              </>
            )}
          </div>
          <p className="text-xs text-muted-foreground">
            The pipeline reads this when it starts the next scheduled generation cycle; a change
            never rewrites content already being generated.
          </p>
        </div>

        <div className="flex flex-col gap-3 border-t border-border/40 pt-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h4 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
              Current session
            </h4>
            <RevokeSessionControl
              disabled={session === null}
              busy={revoking}
              onConfirm={() => void revoke()}
            />
          </div>

          {notice && (
            <p role="alert" className="text-sm font-medium text-destructive">
              {notice}
            </p>
          )}

          {loading && !overview ? (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Loading the current session…
            </p>
          ) : error ? (
            <p role="alert" className="text-sm font-medium text-destructive">
              {error}
            </p>
          ) : session === null ? (
            <div className="flex flex-col gap-2">
              <p className="text-sm text-muted-foreground">
                No session is active right now.
              </p>
              <AiGenerationStatusLine generation={generation} />
            </div>
          ) : (
            <div className="flex flex-col gap-3 [border-radius:var(--edge-card-sm)] border border-border/60 bg-muted/40 p-4">
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <Badge variant="default">{session.status}</Badge>
                <span className="font-semibold">{session.title ?? 'The current story'}</span>
                <span className="text-muted-foreground">
                  {session.period_start_date ?? '—'} → {session.period_end_date ?? '—'}
                </span>
              </div>
              {storyPackage.summary && (
                <p className="whitespace-pre-line text-sm text-muted-foreground">
                  {storyPackage.summary}
                </p>
              )}

              <h5 className="mt-1 flex items-center gap-2 text-xs font-bold uppercase tracking-wide text-muted-foreground">
                <BookOpenText className="h-4 w-4" />
                Scenes and choices ({scenes.length})
              </h5>
              <div className="flex flex-col gap-2">
                {scenes.map((scene) => (
                  <div key={scene.id} className="rounded-xl border border-border/50 bg-background/60 p-3">
                    <button
                      type="button"
                      onClick={() =>
                        setExpandedScene((current) => (current === scene.id ? null : scene.id))
                      }
                      aria-expanded={expandedScene === scene.id}
                      aria-label={`Inspect scene ${scene.id}`}
                      className="flex w-full items-center justify-between gap-2 text-left text-sm"
                    >
                      <span className="font-semibold">
                        {scene.title}{' '}
                        <span className="font-normal text-muted-foreground">
                          · {scene.type} · {scene.actor} · slot {scene.slot ?? 'core'}
                        </span>
                      </span>
                      <Badge variant="outline">{expandedScene === scene.id ? '–' : '+'}</Badge>
                    </button>
                    {expandedScene === scene.id && (
                      <div className="mt-2 space-y-2 text-sm">
                        <p className="whitespace-pre-line text-muted-foreground">
                          {scene.narrative_text}
                        </p>
                        {scene.choices.map((choice) => (
                          <div key={choice.id} className="rounded-lg border border-border/40 p-2">
                            <p className="font-medium">{choice.text}</p>
                            {choice.consequence_text && (
                              <p className="text-xs text-muted-foreground">
                                {choice.consequence_text}
                              </p>
                            )}
                            <p className="mt-1 font-mono text-xs">
                              → {choice.next_scene_id ?? '—'}
                              {choice.reward_tier ? ` · reward: ${choice.reward_tier}` : ''}
                            </p>
                          </div>
                        ))}
                        {scene.choices.length === 0 && (
                          <p className="text-xs text-muted-foreground">
                            {scene.is_terminal
                              ? `Terminal scene → ending ${scene.ending_id ?? ''}`
                              : 'No choices on this scene.'}
                          </p>
                        )}
                      </div>
                    )}
                  </div>
                ))}
                {scenes.length === 0 && (
                  <p className="text-sm text-muted-foreground">This story has no scenes.</p>
                )}
              </div>

              {endings.length > 0 && (
                <>
                  <h5 className="mt-1 text-xs font-bold uppercase tracking-wide text-muted-foreground">
                    Outcomes ({endings.length})
                  </h5>
                  <ul className="flex flex-col gap-2 text-sm">
                    {endings.map((ending) => (
                      <li key={ending.id} className="rounded-xl border border-border/50 bg-background/60 p-3">
                        <p className="font-semibold">
                          {ending.title}{' '}
                          <span className="font-normal text-muted-foreground">
                            · {ending.type}
                            {ending.success ? ' · success' : ''}
                            {ending.reward_tier ? ` · tier ${ending.reward_tier}` : ''}
                          </span>
                        </p>
                        <p className="mt-1 whitespace-pre-line text-muted-foreground">
                          {ending.narrative}
                        </p>
                      </li>
                    ))}
                  </ul>
                </>
              )}

              <p className="text-xs text-muted-foreground">
                Published session {session.id.slice(0, 8)}… · package{' '}
                {session.package_id?.slice(0, 8) ?? '—'}… · generated automatically.
              </p>
            </div>
          )}
        </div>

        <AiSessionHistory
          entries={history}
          total={historyTotal}
          loadingMore={loadingMore}
          onShowMore={() => void showMoreHistory()}
        />
      </CardContent>
    </Card>
  );
};

export default AdminPlotTwistConfig;
