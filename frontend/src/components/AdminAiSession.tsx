import { useState } from 'react';
import { ChevronDown, ChevronUp, Loader2 } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import type { AiGenerationState, AiSessionHistoryEntry } from '@/services/api';

/**
 * Shared read-only surfaces for the two automatically generated games.
 *
 * Plot Twist and Case Closed no longer expose a blueprint/package workflow to
 * the administrator: the scheduler enqueues a generation cycle at the game's
 * boundary, a background worker publishes the validated content, and the
 * console only inspects what was published -- the current session's content,
 * the latest generation state (or its error), and the paginated history.
 * These two pieces are identical on both pages, so they live here.
 */

const GENERATION_LABELS: Record<AiGenerationState['status'], string> = {
  queued: 'Generation queued',
  running: 'Generation in progress',
  retry_wait: 'Retrying generation',
  published: 'Published',
  failed: 'Generation failed',
  cancelled: 'Generation cancelled',
};

const LIVE_STATUSES: Array<AiGenerationState['status']> = ['queued', 'running', 'retry_wait'];

/** The simple generation status (or last error) an admin needs for an empty screen. */
export const AiGenerationStatusLine = ({
  generation,
}: {
  generation: AiGenerationState | null;
}) => {
  if (!generation) {
    return (
      <p className="text-sm text-muted-foreground">
        No generation has run yet. The next scheduled cycle starts one automatically.
      </p>
    );
  }
  const live = LIVE_STATUSES.includes(generation.status);
  return (
    <div className="flex flex-col gap-1 text-sm">
      <p className="flex flex-wrap items-center gap-2">
        <Badge
          variant={
            generation.status === 'published'
              ? 'default'
              : generation.status === 'failed'
                ? 'destructive'
                : 'outline'
          }
        >
          {GENERATION_LABELS[generation.status] ?? generation.status}
        </Badge>
        {live && <Loader2 className="h-3 w-3 animate-spin text-muted-foreground" />}
        <span className="text-xs text-muted-foreground">
          cycle {generation.cycle_start_date ?? '—'} → {generation.cycle_end_date ?? '—'} · attempt{' '}
          {generation.attempt}/{generation.max_attempts} · {generation.difficulty}
        </span>
      </p>
      {generation.last_error && (
        <p role="alert" className="text-sm font-medium text-destructive">
          {generation.last_error}
        </p>
      )}
    </div>
  );
};

/**
 * The backend-paginated session history, collapsed so a long season of past
 * sessions does not push the working controls off the screen.
 *
 * Each read is a SQL page: "Show more" carries the offset of what is already
 * shown and appends the next page beneath it, exactly like the Wordle console.
 */
export const AiSessionHistory = ({
  entries,
  total,
  loadingMore,
  onShowMore,
}: {
  entries: AiSessionHistoryEntry[];
  total: number;
  loadingMore: boolean;
  onShowMore: () => void;
}) => {
  const [open, setOpen] = useState(false);
  return (
    <div className="flex flex-col gap-3 border-t border-border/40 pt-4">
      <div className="flex items-center justify-between gap-3">
        <h4 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          Session history
        </h4>
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">{total} sessions</span>
          {entries.length > 0 && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setOpen((value) => !value)}
              aria-expanded={open}
              aria-label={open ? 'Hide session history' : 'Show session history'}
            >
              {open ? <ChevronUp /> : <ChevronDown />}
              {open ? 'Hide' : 'Show'}
            </Button>
          )}
        </div>
      </div>

      {open && entries.length === 0 && (
        <p className="text-sm text-muted-foreground">No sessions yet.</p>
      )}

      {open && (
        <ul className="flex flex-col gap-2">
          {entries.map((row) => (
            <li
              key={row.id}
              className="flex flex-wrap items-center gap-2 rounded-xl border border-border/60 p-3 text-sm"
            >
              <span className="font-mono text-xs">
                {row.period_start_date ?? row.created_at.slice(0, 10)} → {row.period_end_date ?? '—'}
              </span>
              <Badge variant={row.status === 'active' ? 'default' : 'outline'}>{row.status}</Badge>
              {row.revoked && <Badge variant="destructive">revoked</Badge>}
              {row.finalized !== undefined && (
                <Badge variant="secondary">{row.finalized ? 'revealed' : 'unrevealed'}</Badge>
              )}
              <span className="min-w-0 flex-1 truncate">{row.title ?? 'Untitled'}</span>
            </li>
          ))}
        </ul>
      )}

      {open && entries.length < total && (
        <div className="flex justify-center pt-1">
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={onShowMore}
            disabled={loadingMore}
            aria-label="Show more sessions"
          >
            {loadingMore ? 'Loading…' : `Show more (${total - entries.length} remaining)`}
          </Button>
        </div>
      )}
    </div>
  );
};

/** The two-step revoke control: the second click is the confirmation. */
export const RevokeSessionControl = ({
  disabled,
  busy,
  onConfirm,
}: {
  disabled: boolean;
  busy: boolean;
  onConfirm: () => void;
}) => {
  const [confirming, setConfirming] = useState(false);
  if (!confirming) {
    return (
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={disabled || busy}
        onClick={() => setConfirming(true)}
      >
        Revoke session
      </Button>
    );
  }
  return (
    <span className="flex flex-wrap items-center gap-2">
      <span className="text-xs text-muted-foreground">
        Ends the live session; the next one waits for its scheduled cycle.
      </span>
      <Button
        type="button"
        variant="destructive"
        size="sm"
        disabled={busy}
        onClick={() => {
          setConfirming(false);
          onConfirm();
        }}
      >
        Confirm revoke
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        disabled={busy}
        onClick={() => setConfirming(false)}
      >
        Cancel
      </Button>
    </span>
  );
};
