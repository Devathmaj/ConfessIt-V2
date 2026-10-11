import { useCallback, useEffect, useState } from 'react';
import { NavLink } from 'react-router-dom';
import { AlarmClock, FileSearch, Fingerprint, Loader2, MessagesSquare, RefreshCw, Scale, Trophy, Users } from 'lucide-react';
import { toast } from 'sonner';
import { GamesPageHeader } from '@/components/games/GamesPageHeader';
import { PartnerPicker } from '@/components/games/PartnerPicker';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { describeApiError } from '@/lib/apiError';
import { cn } from '@/lib/utils';
import { CaseClosedChatDialog } from '@/components/CaseClosedChatDialog';
import {
  confirmCaseClosedAccusation,
  getCaseClosedPartners,
  getCaseClosedResult,
  getCaseClosedState,
  investigateCaseClosedLead,
  proposeCaseClosedAccusation,
  selectCaseClosedPartner,
  startCaseClosedEvent,
  type CaseClosedResult,
  type CaseClosedState,
} from '@/services/api';

const formatElapsed = (seconds: number | null | undefined) => {
  if (seconds === null || seconds === undefined) return '—';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
};

export const CaseClosedGamePage = () => {
  const [state, setState] = useState<CaseClosedState | null>(null);
  const [result, setResult] = useState<CaseClosedResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [investigatingId, setInvestigatingId] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [selectedSuspect, setSelectedSuspect] = useState<string | null>(null);
  const [actingAccusation, setActingAccusation] = useState(false);

  const refresh = useCallback(async (quiet = false) => {
    if (!quiet) setRefreshing(true);
    try {
      const next = await getCaseClosedState();
      setState(next);
      if (next.finalized || next.stage === 'completed' || next.stage === 'accusation_locked') {
        try {
          setResult(await getCaseClosedResult());
        } catch {
          /* result is best-effort before finalization */
        }
      }
    } catch (error) {
      toast.error(describeApiError(error, 'Could not load the case.'));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (state === null) return;
    if (state.stage !== 'investigating') return;
    const id = window.setInterval(() => void refresh(true), 15000);
    return () => window.clearInterval(id);
  }, [state?.stage, refresh]);

  const handleChoose = async (candidateId: string) => {
    try {
      setState(await selectCaseClosedPartner(candidateId));
    } catch (error) {
      toast.error(describeApiError(error, 'Could not select partner.'));
    }
  };

  // Suspect ids are machine keys; players should only ever read names.
  const nameForSuspect = (id: string | null | undefined) =>
    (id ? state?.suspects.find((suspect) => suspect.id === id)?.name : null) ?? id ?? '—';

  if (loading || state === null) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading the case…
      </div>
    );
  }

  if (state.stage === 'no_game') {
    return (
      <div>
        <GamesPageHeader eyebrow="Couple game" title="Case Closed" description="A cooperative mystery for two detectives." />
        <Card><CardHeader><CardTitle>No event live</CardTitle><CardDescription>Ask an admin to publish a case. Check back soon.</CardDescription></CardHeader></Card>
      </div>
    );
  }

  if (state.stage === 'selecting_partner') {
    return (
      <div className="flex flex-col gap-4">
        <GamesPageHeader eyebrow="Couple game" title={state.case_title ?? 'Case Closed'} description={state.opening_narrative ?? 'Form your detective pair to open the case.'} />
        <Card>
          <CardHeader><CardTitle className="flex items-center gap-2"><Users className="h-5 w-5" /> Choose your partner</CardTitle>
            <CardDescription>Pairing reuses your Couple Questionnaire mutual selection.</CardDescription></CardHeader>
          <CardContent className="flex flex-col gap-3">
            <PartnerPicker
              loadPartners={getCaseClosedPartners}
              selectedId={null}
              onChoose={handleChoose}
            />
            <Button disabled={starting} onClick={async () => {
              setStarting(true);
              try { setState(await startCaseClosedEvent()); } catch (e) { toast.error(describeApiError(e, 'Could not start the event.')); } finally { setStarting(false); }
            }}>{starting ? 'Opening…' : 'Open the case'}</Button>
            <Button variant="ghost" size="sm" onClick={() => void refresh()}>Refresh</Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  const locked = state.accusation.locked_suspect_id !== null;

  // The detective thread is session-aware and short-lived: it is live only
  // while the accusation is open. The first confirmation from either detective
  // ends it (these two strings mirror the backend's `THREAD_CLOSED_*` reasons),
  // and so does the case ending. The dialog uses this to close its composer
  // and drop the live socket the moment the lock lands, whether that lock was
  // the caller's own confirmation or the partner's (each 15s state poll).
  const threadClosedReason =
    locked || state.accusation.my_confirmed || state.accusation.partner_confirmed
      ? 'The detective thread closed when an accusation was confirmed'
      : state.finalized || state.stage !== 'investigating'
        ? 'The detective thread closed when the case ended'
        : null;

  return (
    <div className="flex flex-col gap-4">
      <GamesPageHeader eyebrow="Couple game" title={state.case_title ?? 'Case Closed'} description={state.opening_narrative ?? ''} />
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Badge variant="secondary"><AlarmClock className="mr-1 h-3 w-3" />{state.timer.started ? `Elapsed ${formatElapsed(state.timer.elapsed_seconds)}` : 'Timer starts on first investigation'}</Badge>
        <Badge variant="outline">You: {state.my_remaining} left</Badge>
        <Badge variant="outline">Partner: {state.partner_remaining} left</Badge>
        {state.event_deadline && <Badge variant="outline">Closes {new Date(state.event_deadline).toLocaleString()}</Badge>}
        <Button variant="ghost" size="sm" onClick={() => void refresh()} disabled={refreshing}><RefreshCw className={cn('h-3 w-3', refreshing && 'animate-spin')} /> Refresh</Button>
        <Button variant="ghost" size="sm" asChild><NavLink to="/inbox"><MessagesSquare className="h-3 w-3" /> Discuss in inbox</NavLink></Button>
      </div>

      <Card>
        <CardHeader><CardTitle className="flex items-center gap-2"><Users className="h-4 w-4" /> Suspects</CardTitle></CardHeader>
        <CardContent className="grid gap-2 md:grid-cols-3">
          {state.suspects.map((s) => (
            <div key={s.id} className={cn('rounded-xl border p-3', selectedSuspect === s.id && 'border-primary')}>
              <div className="font-medium">{s.name}</div>
              <div className="text-xs text-muted-foreground">{s.background}</div>
              <div className="text-xs italic">{s.relationship}</div>
            </div>
          ))}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle className="flex items-center gap-2"><FileSearch className="h-4 w-4" /> Available leads</CardTitle>
          <CardDescription>Choosing a branch sacrifices its alternatives along your path. Revisits to known leads are free.</CardDescription></CardHeader>
        <CardContent className="flex flex-col gap-2">
          <div className="text-xs text-muted-foreground">Your path: {state.my_path.length ? state.my_path.join(' → ') : 'not started (pick any opener)'}</div>
          {state.available_leads.length === 0 && <div className="text-sm text-muted-foreground">No leads available — discuss and accuse, or wait for your partner.</div>}
          {state.available_leads.map((lead) => (
            <div key={lead.id} className="flex items-center justify-between gap-3 rounded-xl border px-3 py-2">
              <div>
                <div className="font-medium">{lead.title} {lead.investigated && <Badge variant="secondary">known</Badge>}</div>
                <div className="text-sm text-muted-foreground">{lead.description}</div>
              </div>
              <Button size="sm" disabled={!!investigatingId || locked} onClick={async () => {
                setInvestigatingId(lead.id);
                try {
                  const res = await investigateCaseClosedLead(lead.id, state.state_version);
                  setState(res.state);
                  toast.success(res.already_known ? 'Already known — no action charged.' : 'Discovery recorded.');
                } catch (e) { toast.error(describeApiError(e, 'Investigation failed.')); void refresh(true); } finally { setInvestigatingId(null); }
              }}>{investigatingId === lead.id ? '…' : lead.investigated ? 'Revisit' : 'Investigate'}</Button>
            </div>
          ))}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle className="flex items-center gap-2"><Fingerprint className="h-4 w-4" /> Shared evidence board ({state.evidence_board.length})</CardTitle>
          <CardDescription>Every discovery lands here for both partners. Reviewing costs nothing.</CardDescription></CardHeader>
        <CardContent className="grid gap-2 md:grid-cols-2">
          {state.evidence_board.length === 0 && <div className="text-sm text-muted-foreground">No evidence yet — investigate a lead.</div>}
          {state.evidence_board.map((e) => (
            <div key={e.id} className="rounded-xl border p-3">
              <div className="font-medium">{e.title}</div>
              <div className="text-sm">{e.description}</div>
              <div className="mt-1 text-xs text-muted-foreground">via {e.source_lead_id} · {e.discovered_at ? new Date(e.discovered_at).toLocaleString() : ''} · {e.kind}</div>
            </div>
          ))}
        </CardContent>
      </Card>

      <CaseClosedChatDialog
        sessionId={state.session_id}
        chatOpen={threadClosedReason === null}
        closedReason={threadClosedReason}
      />

      <Card>
        <CardHeader><CardTitle className="flex items-center gap-2"><Scale className="h-4 w-4" /> Final accusation</CardTitle>
          <CardDescription>Both partners must explicitly confirm the same suspect. Locking is irreversible and reveals nothing until the event closes.</CardDescription></CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div className="flex flex-wrap gap-2">
            {state.suspects.map((s) => (
              <Button key={s.id} size="sm" variant={selectedSuspect === s.id ? 'default' : 'outline'} disabled={locked} onClick={() => setSelectedSuspect(s.id)}>{s.name}</Button>
            ))}
          </div>
          <div className="text-xs text-muted-foreground">
            You proposed: {state.accusation.my_proposal ?? '—'} · confirmed: {state.accusation.my_confirmed ?? '—'}<br />
            Partner proposed: {state.accusation.partner_proposal ?? '—'} · confirmed: {state.accusation.partner_confirmed ?? '—'}
            {locked && <> · <Badge>Locked: {state.accusation.locked_suspect_id}</Badge></>}
          </div>
          <div className="flex gap-2">
            <Button size="sm" variant="outline" disabled={!selectedSuspect || locked || actingAccusation} onClick={async () => {
              if (!selectedSuspect) return;
              setActingAccusation(true);
              try { setState(await proposeCaseClosedAccusation(selectedSuspect)); toast.success('Proposal recorded.'); } catch (e) { toast.error(describeApiError(e, 'Proposal failed.')); } finally { setActingAccusation(false); }
            }}>Propose</Button>
            <Button size="sm" variant="destructive" disabled={!selectedSuspect || locked || actingAccusation} onClick={async () => {
              if (!selectedSuspect) return;
              if (!window.confirm(`Lock in ${selectedSuspect} as your joint accusation? This cannot be undone.`)) return;
              setActingAccusation(true);
              try { setState(await confirmCaseClosedAccusation(selectedSuspect)); toast.success('Confirmation recorded.'); } catch (e) { toast.error(describeApiError(e, 'Confirmation failed.')); } finally { setActingAccusation(false); }
            }}>Confirm (irreversible)</Button>
          </div>
          {locked && <div className="text-sm">Accusation recorded at {state.timer.locked_at ? new Date(state.timer.locked_at).toLocaleString() : ''}. Correctness is hidden until the event closes.</div>}
        </CardContent>
      </Card>

      {(result?.finalized || state.finalized) && result && (
        <Card>
          <CardHeader><CardTitle className="flex items-center gap-2"><Trophy className="h-4 w-4" /> Solution reveal</CardTitle></CardHeader>
          <CardContent className="flex flex-col gap-2">
            <div className="font-medium">The culprit was {nameForSuspect(result.culprit_id)}.</div>
            {result.solution_title && <div className="text-sm font-medium">{result.solution_title}</div>}
            <div className="text-sm">{result.solution_narrative}</div>
            <div className="text-sm italic">{result.why_others_ruled_out}</div>
            <div className="text-sm">
              {result.locked_suspect_id
                ? `Your accusation: ${nameForSuspect(result.locked_suspect_id)} · ${result.correct ? 'Correct' : 'Incorrect'} · ${result.score} pts · ${formatElapsed(result.elapsed_seconds)}`
                : 'No accusation was locked before the case closed.'}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
};

export default CaseClosedGamePage;
