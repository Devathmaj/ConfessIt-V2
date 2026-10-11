import { useCallback, useEffect, useState } from 'react';
import { Fingerprint, Loader2, RefreshCw, Users } from 'lucide-react';
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
  getCaseClosedSessionHistory,
  getCaseClosedSessionOverview,
  getGameConfigs,
  revokeCaseClosedSession,
  updateGameConfig,
  type AiSessionHistoryEntry,
  type CaseClosedSessionOverview,
  type GameConfig,
} from '@/services/api';

const DIFFICULTIES: Array<NonNullable<GameConfig['difficulty']>> = ['easy', 'medium', 'hard'];

/** One backend history read: ten small session rows per "Show more" click. */
const HISTORY_PAGE_SIZE = 10;

/**
 * The Case Closed console: its own page under /admin/games/manage.
 *
 * Cases are generated and published automatically on the configured schedule,
 * so the administrator configures only the difficulty and frequency, inspects
 * the currently published scenario (opening, suspects, leads, outcome), pages
 * back through session history, and can revoke the live event. The
 * blueprint/package workflow is internal to the generation pipeline and is
 * deliberately absent from this screen.
 */
export const AdminCaseClosedConfig = () => {
  const [difficulty, setDifficulty] = useState<NonNullable<GameConfig['difficulty']>>('medium');
  const [savedDifficulty, setSavedDifficulty] =
    useState<NonNullable<GameConfig['difficulty']>>('medium');
  const [overview, setOverview] = useState<CaseClosedSessionOverview | null>(null);
  const [history, setHistory] = useState<AiSessionHistoryEntry[]>([]);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [revoking, setRevoking] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [expandedLead, setExpandedLead] = useState<string | null>(null);

  /**
   * One console read: the current published case plus generation state, and
   * the first history page. Every state write happens after the awaits, so an
   * effect that calls this never sets state synchronously.
   */
  const load = useCallback(async (options?: { silent?: boolean }) => {
    try {
      const [current, page] = await Promise.all([
        getCaseClosedSessionOverview(),
        getCaseClosedSessionHistory({ skip: 0, limit: HISTORY_PAGE_SIZE }),
      ]);
      setOverview(current);
      setHistory(page.sessions);
      setHistoryTotal(page.total);
      setError(null);
      if (!options?.silent) {
        const configs = await getGameConfigs();
        const level = configs.find((entry) => entry.game_id === 'case-closed')?.difficulty;
        if (level) {
          setDifficulty(level);
          setSavedDifficulty(level);
        }
      }
    } catch (loadError) {
      setError(describeApiError(loadError, 'Could not load the Case Closed console.'));
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

  useEffect(() => {
    if (!generationLive) return undefined;
    const timer = window.setTimeout(() => void load({ silent: true }), 8000);
    return () => window.clearTimeout(timer);
  }, [generationLive, generation?.status, load]);

  const showMoreHistory = async () => {
    if (loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await getCaseClosedSessionHistory({
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
      const updated = await updateGameConfig('case-closed', { difficulty });
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
      const result = await revokeCaseClosedSession();
      if (result.action === 'revoked') {
        toast.success(
          result.revoked_until
            ? `Event revoked. The next one is generated no earlier than ${result.revoked_until}.`
            : 'Event revoked.',
        );
      } else {
        toast.success('No event was live; nothing to revoke.');
      }
      await load({ silent: true });
    } catch (revokeError) {
      setNotice(describeApiError(revokeError, 'Could not revoke the event.'));
    } finally {
      setRevoking(false);
    }
  };

  const session = overview?.session ?? null;
  const casePackage = session?.package ?? {};
  const suspects = casePackage.suspects ?? [];
  const leads = casePackage.lead_nodes ?? [];
  const solution = casePackage.solution;
  const reveal = casePackage.solution_reveal;
  const culprit = suspects.find((suspect) => suspect.id === solution?.culprit_id);

  return (
    <Card className="[border-radius:var(--edge-card)] border-border/60 bg-card/70 backdrop-blur">
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="flex items-center gap-2 text-xl">
              <Fingerprint className="h-5 w-5" />
              Case Closed
            </CardTitle>
            <CardDescription>
              A cooperative mystery for couples. New cases are generated and published
              automatically on the configured frequency — configure the difficulty, inspect the
              current case, or revoke it below.
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
        <AdminGameStatusControls gameId="case-closed" gameName="Case Closed" />

        <div className="flex flex-col gap-3 border-t border-border/40 pt-4">
          <h4 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            Difficulty
          </h4>
          <div className="flex flex-wrap items-end gap-3">
            <div className="flex flex-col gap-2">
              <Label htmlFor="case-closed-difficulty">Case difficulty</Label>
              <Select
                value={difficulty}
                onValueChange={(next) =>
                  setDifficulty(next as NonNullable<GameConfig['difficulty']>)
                }
                disabled={saving}
              >
                <SelectTrigger id="case-closed-difficulty" className="w-44">
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
              Loading the current event…
            </p>
          ) : error ? (
            <p role="alert" className="text-sm font-medium text-destructive">
              {error}
            </p>
          ) : session === null ? (
            <div className="flex flex-col gap-2">
              <p className="text-sm text-muted-foreground">
                No case is active right now.
              </p>
              <AiGenerationStatusLine generation={generation} />
            </div>
          ) : (
            <div className="flex flex-col gap-3 [border-radius:var(--edge-card-sm)] border border-border/60 bg-muted/40 p-4">
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <Badge variant="default">{session.status}</Badge>
                <span className="font-semibold">{session.title ?? 'The current case'}</span>
                <span className="text-muted-foreground">
                  {session.period_start_date ?? '—'} → {session.period_end_date ?? '—'}
                </span>
                <Badge variant="secondary">{session.finalized ? 'revealed' : 'in progress'}</Badge>
              </div>

              {casePackage.opening_narrative && (
                <p className="whitespace-pre-line text-sm text-muted-foreground">
                  {casePackage.opening_narrative}
                </p>
              )}

              <h5 className="mt-1 flex items-center gap-2 text-xs font-bold uppercase tracking-wide text-muted-foreground">
                <Users className="h-4 w-4" />
                Suspects ({suspects.length})
              </h5>
              <ul className="flex flex-col gap-2 text-sm">
                {suspects.map((suspect) => (
                  <li key={suspect.id} className="rounded-xl border border-border/50 bg-background/60 p-3">
                    <p className="font-semibold">
                      {suspect.name}{' '}
                      <span className="font-normal text-muted-foreground">
                        · {suspect.relationship} · {suspect.background}
                      </span>
                    </p>
                  </li>
                ))}
              </ul>

              <h5 className="mt-1 text-xs font-bold uppercase tracking-wide text-muted-foreground">
                Leads and choices ({leads.length})
              </h5>
              <div className="flex flex-col gap-2">
                {leads.map((lead) => (
                  <div key={lead.id} className="rounded-xl border border-border/50 bg-background/60 p-3">
                    <button
                      type="button"
                      onClick={() =>
                        setExpandedLead((current) => (current === lead.id ? null : lead.id))
                      }
                      aria-expanded={expandedLead === lead.id}
                      aria-label={`Inspect lead ${lead.id}`}
                      className="flex w-full items-center justify-between gap-2 text-left text-sm"
                    >
                      <span className="font-semibold">
                        {lead.title}{' '}
                        <span className="font-normal text-muted-foreground">
                          · {lead.description}
                        </span>
                      </span>
                      <Badge variant="outline">{expandedLead === lead.id ? '–' : '+'}</Badge>
                    </button>
                    {expandedLead === lead.id && (
                      <div className="mt-2 space-y-1 text-sm">
                        <p className="text-muted-foreground">{lead.discovery_text}</p>
                        <p className="font-mono text-xs">
                          follows: {lead.next_lead_ids.length > 0 ? lead.next_lead_ids.join(', ') : '—'}
                        </p>
                      </div>
                    )}
                  </div>
                ))}
                {leads.length === 0 && (
                  <p className="text-sm text-muted-foreground">This case has no leads.</p>
                )}
              </div>

              {solution && (
                <>
                  <h5 className="mt-1 text-xs font-bold uppercase tracking-wide text-muted-foreground">
                    {session.finalized ? 'Case result' : 'Case solution (not yet revealed to players)'}
                  </h5>
                  <div className="rounded-xl border border-border/50 bg-background/60 p-3 text-sm">
                    <p>
                      Culprit:{' '}
                      <span className="font-semibold">{culprit?.name ?? solution.culprit_id}</span>
                      {solution.motive ? ` · ${solution.motive}` : ''}
                    </p>
                    {reveal?.narrative && (
                      <p className="mt-1 whitespace-pre-line text-muted-foreground">
                        {reveal.narrative}
                      </p>
                    )}
                    {reveal?.why_others_ruled_out && (
                      <p className="mt-1 whitespace-pre-line text-muted-foreground">
                        {reveal.why_others_ruled_out}
                      </p>
                    )}
                  </div>
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

export default AdminCaseClosedConfig;
