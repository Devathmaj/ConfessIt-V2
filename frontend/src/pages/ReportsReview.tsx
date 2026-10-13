import { useEffect, useRef, useState } from 'react';
import {
  getAdminConfessionReports,
  getAdminLoveNoteReports,
  getAdminMessageReports,
  updateAdminConfessionReportStatus,
  updateAdminLoveNoteReportStatus,
  updateAdminMessageReportStatus,
  unhideAdminComment,
  unhideAdminConfession,
  unhideAdminMessage,
} from '@/services/api';
import { AdminNavigation } from '@/components/AdminNavigation';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useAuth } from '@/contexts/AuthContext';
import { CAPABILITIES } from '@/lib/capabilities';
import { authorLabel, partyName, partyRef, type Person } from '@/lib/reportAuthors';
import { formatDateTimeDDMMYYYY } from '@/lib/utils';
import { toast } from 'sonner';
import { ChevronDown, Eye, Flag, MessageSquare, MessageCircleHeart, Search, ShieldQuestion, ShieldX, Trash2 } from 'lucide-react';

/** Every state a report row can be in, including the one only history produces.
 *
 * `reviewed` is the word the close-out used before the screen grew a remove button. It is
 * still a real value in the database and still a real filter, so rows closed under the old
 * vocabulary keep showing up under their own name instead of disappearing from the list a
 * moderator is using to audit what was decided.
 */
type ReportStatus = 'pending' | 'reviewed' | 'dismissed' | 'removed';
type StatusFilter = 'all' | ReportStatus;

/** The two decisions this screen offers.
 *
 * `reviewed` is deliberately absent: it is a synonym for `dismissed` that the server still
 * accepts for callers holding a stale vocabulary, but offering it as a third button is how the
 * same action ends up recorded three different ways in the database.
 */
type Resolution = 'dismissed' | 'removed';

/** The confession a reported comment was left on.
 *
 * Only ever present on a comment report. Its purpose is context: whether a comment reads as a
 * threat or an argument for leaving it up often turns on the post it answers, and a moderator
 * shown the remark alone has to guess.
 */
interface ParentConfession {
  id: string;
  body: string | null;
  recipient_name: string | null;
  created_at: string | null;
  comment_count: number;
  anonymous: boolean;
  content_removed: boolean;
  /** Null when the parent is anonymous -- withheld on purpose, not missing by accident. */
  author: Person | null;
}

/** A report against a confession, or against a comment on one. */
interface ConfessionReport {
  id: string;
  target_type: 'confession' | 'comment';
  target_id: string;
  confession_id: string | null;
  parent_recipient_name: string | null;
  /** Only on comment reports. Null on a confession report, where the parent is the content. */
  parent_confession: ParentConfession | null;
  anonymous: boolean;
  content: string | null;
  content_created_at: string | null;
  author: Person | null;
  author_hidden: boolean;
  reporter: Person;
  reason: string | null;
  reported_at: string | null;
  status: ReportStatus;
  reviewed_at: string | null;
  reviewed_by_regno: string | null;
  /** Whether the reported content is currently taken down. Kept separate from `status` because
   *  they answer different questions: `status` is what this report was decided as, this is what
   *  happened to the content -- which can also have been removed by a different report. */
  content_removed: boolean;
  removed_at?: string | null;
  removed_by_regno?: string | null;
}

/** A report against a love note a recipient received. */
interface LoveNoteReport {
  id: string;
  love_note_id: string;
  /** The PII-redacted preview of the note, never the original body: this queue is a
   *  moderator-facing read of the same note the moderation list shows, so the raw text
   *  has no path out through it. Null means no preview could be produced. */
  moderation_body: string | null;
  anonymous: boolean;
  note_status: string | null;
  note_created_at: string | null;
  image_url: string | null;
  reporter: Person;
  reported_user: Person;
  reason: string | null;
  reported_at: string | null;
  status: ReportStatus;
  reviewed_at: string | null;
  reviewed_by_regno: string | null;
  /** Same split as the confession queue: the note's own approval status is untouched by a
   *  removal, so this is the only field that says the content came down. */
  content_removed: boolean;
  removed_at?: string | null;
  removed_by_regno?: string | null;
}

/** The conversation a reported chat message was sent in.
 *
 * A reported message is a single sentence lifted out of a thread, and the thread is what makes
 * it legible. `message_count` against `removed_message_count` is the part worth having: a thread
 * where nothing has been taken down yet reads as one lapse, and a thread where two messages are
 * already gone reads as a pattern, and those two warrant very different rulings.
 */
interface ReportedConversation {
  id: string;
  status: string | null;
  participants: Person[];
  message_count: number;
  removed_message_count: number;
}

/** A report against a chat message. */
interface MessageReport {
  id: string;
  message_id: string;
  body: string | null;
  conversation_id: string | null;
  /** Absent when the message row is gone, which the queue has to survive. */
  message_created_at: string | null;
  conversation: ReportedConversation | null;
  reporter: Person;
  reported_user: Person;
  reason: string | null;
  reported_at: string | null;
  status: ReportStatus;
  reviewed_at: string | null;
  reviewed_by_regno: string | null;
  content_removed: boolean;
  removed_at?: string | null;
  removed_by_regno?: string | null;
  /** "user" for a participant report, "azure_content_safety" for an automated one. */
  source?: string;
  /** Which message surface this came from. Older servers omit it; matchmaking is the default. */
  kind?: 'matchmaking' | 'case_closed';
}

/** The three id spaces the server owns, named once so the queue keys cannot drift apart. */
type QueueKey = 'confessions' | 'loveNotes' | 'messages';

/** The one thing the three queues share, which is also the one thing they are all for.
 *
 * `openReport` is passed in per queue rather than switching on a kind inside the renderer,
 * because the server owns the mapping: a report id means nothing until you know which table
 * it came from, and a component that guessed would be guessing about an id space.
 *
 * There is no `fetch` on here. Each queue's loader owns its own state slot, so a shared
 * signature would have to be a cast of one loader to another; the queues are uniform in what
 * they render, not in what they fetch.
 */
interface Queue {
  key: QueueKey;
  label: string;
  icon: typeof Flag;
  /** Human sentence for the tab and the empty state. */
  describe: (count: number) => string;
  openReport: (reportId: string, status: 'reviewed' | 'dismissed') => Promise<unknown>;
}

const formatTimestamp = (value: string | null): string =>
  value ? formatDateTimeDDMMYYYY(value) : 'an unknown time';

/** The outcome a report was decided as, phrased for a human.
 *
 * `reviewed` and `dismissed` mean the same thing and are both shown as "Dismissed": the row
 * in the database keeps the word it was written with, the reviewer sees one meaning.
 */
const RESOLUTION_LABELS: Record<Resolution, { verb: string; toast: string }> = {
  dismissed: { verb: 'Dismiss', toast: 'Report dismissed. The content was left up.' },
  removed: { verb: 'Remove content', toast: 'Content removed and everyone affected notified.' },
};

/** What the dialog leads with, per outcome.
 *
 * The lead line states the consequence before the button asks for the decision, because the
 * only thing that makes a confirm dialog worth having is that it is the last place the
 * consequence is stated.
 */
const RESOLUTION_COPY: Record<Resolution, { title: string }> = {
  dismissed: { title: 'The content will stay up and will not be touched.' },
  removed: { title: 'This will remove the reported content for good.' },
};

function statusBadge(status: ReportStatus, reporterName?: string | null) {
  if (status === 'pending') return <Badge variant="secondary">Awaiting review</Badge>;
  if (status === 'reviewed') return <Badge variant="outline">Dismissed</Badge>;
  if (status === 'removed') {
    if (reporterName === 'Azure Content Safety') {
      return <Badge variant="destructive">Content removed by Azure Content Safety</Badge>;
    }
    return <Badge variant="destructive">Content removed</Badge>;
  }
  return <Badge variant="outline">Dismissed</Badge>;
}

const displayReviewer = (regno: string | null | undefined) =>
  regno === 'azure-content-safety' ? 'Azure Content Safety' : regno;

/** A one-line excerpt of a field, for the collapsed row.
 *
 * Collapsing by height alone would hide the very thing a moderator is scanning for -- what was
 * complained about -- so the collapsed row keeps a clipped version of both the reason and the
 * reported content. Clipping is done here rather than with CSS so the character count is the
 * same in the DOM as it looks, and so the full text is genuinely absent until the card is
 * opened instead of merely scrolled out of view.
 */
const excerpt = (value: string | null | undefined, limit = 120): string | null => {
  if (!value) return null;
  const collapsed = value.replace(/\s+/g, ' ').trim();
  if (collapsed.length <= limit) return collapsed;
  return `${collapsed.slice(0, limit - 1).trimEnd()}…`;
};

/** One report row: a compact summary that opens into the full report.
 *
 * The whole header is the toggle, so the row is reachable by keyboard rather than only by
 * clicking the chevron, and `aria-expanded` is what tells a screen reader whether the detail
 * below is there. The chevron rotates rather than changing icon, matching Confessions Admin.
 *
 * **The excerpt and the full text are mutually exclusive**, not both rendered at once. Rendering
 * both would put two copies of the reason in the DOM when open -- so search matches twice, and
 * anything reading the card's text sees the clipped version and the whole one side by side.
 */
function ReportCard({
  open,
  onToggle,
  title,
  status,
  meta,
  reason,
  content,
  actions,
  children,
}: {
  open: boolean;
  onToggle: () => void;
  title: string;
  status: React.ReactNode;
  /** The facts that fit on a collapsed row: when things happened, and what kind of thing it is. */
  meta: React.ReactNode;
  reason: string | null;
  /** The reported content itself, so the row is triageable without being opened. */
  content: string | null;
  actions: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <Card className="overflow-hidden rounded-2xl border-border bg-muted/20">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="w-full px-4 py-3 text-left transition hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <div className="flex flex-wrap items-start justify-between gap-3">
          <span className="flex min-w-0 flex-wrap items-center gap-2 text-base font-semibold">
            <Flag className="h-4 w-4 shrink-0 text-destructive" />
            {title}
          </span>
          <span className="flex shrink-0 items-center gap-2">
            {status}
            <ChevronDown
              className={`h-4 w-4 text-muted-foreground transition-transform ${open ? 'rotate-180' : ''}`}
            />
          </span>
        </div>

        {/* Collapsed only. Open, the body below carries the same two fields in full, and
            rendering both would put two copies of the reason in the DOM. */}
        {!open && (
          <>
            {excerpt(reason) && (
              <p className="mt-2 line-clamp-2 text-sm">
                <span className="text-muted-foreground">Reported for: </span>
                {excerpt(reason)}
              </p>
            )}
            {excerpt(content) && (
              <p className="mt-1 line-clamp-2 text-sm text-muted-foreground">{excerpt(content)}</p>
            )}
          </>
        )}

        <p className="mt-2 text-xs text-muted-foreground">{meta}</p>
      </button>

      {open && (
        <CardContent className="space-y-4 border-t border-border pt-4">
          {children}
          {actions}
        </CardContent>
      )}
    </Card>
  );
}

/**
 * How many reports one queue read returns. The backend clamps to 200; twenty keeps one
 * read small while a "show more" click still grows the queue by a visible amount.
 */
const QUEUE_PAGE_SIZE = 20;

/** The rows inside a queue answer, whether it is the page envelope or a legacy bare array. */
const reportsOf = <T,>(answer: unknown): T[] => {
  if (Array.isArray(answer)) return answer as T[];
  const reports = (answer as { reports?: T[] } | null | undefined)?.reports;
  return Array.isArray(reports) ? reports : [];
};

/** The matching-row count beside a queue answer, falling back to the rows at hand. */
const totalOf = (answer: unknown, fallback: number): number => {
  if (Array.isArray(answer)) return answer.length;
  const total = (answer as { total?: unknown } | null | undefined)?.total;
  return typeof total === 'number' && Number.isFinite(total) ? total : fallback;
};

interface QueueWindow<T> {
  /** Reports loaded so far: always the contiguous window [0, items.length) of the server order. */
  items: T[];
  /** How many reports match the current question on the server, loaded or not. */
  total: number;
}

const emptyWindow = <T,>(): QueueWindow<T> => ({ items: [], total: 0 });

export const ReportsReview = () => {
  const { can } = useAuth();
  // Read and close-out are separately grantable on the server, so they are hidden separately
  // here. This is presentation only: both endpoints re-check their capability on every call.
  const canModerate = can(CAPABILITIES.REPORTS_MODERATE);
  const [activeTab, setActiveTab] = useState('confessions');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  /** What the search box currently says. Draft only: nothing is fetched until it is submitted. */
  const [searchTerm, setSearchTerm] = useState('');
  /**
   * The search text the server has actually been asked about.
   *
   * Set only by the Search button (or Enter), never by typing: each keystroke firing three
   * queue reads would be a self-inflicted load test, and the half-typed query it answered
   * would be obsolete before it returned. The box stays a draft until submitted, so the
   * request that goes out is the question the operator meant.
   */
  const [appliedSearch, setAppliedSearch] = useState('');

  // The decision awaiting confirmation, or null when no dialog is open. Held as state rather
  // than wired through a per-card `useState` because the copy has to name the actual content
  // being removed, which the dialog is rendered from -- and because one slot is the only thing
  // that makes "a click opened a dialog, nothing happened yet" true by construction rather than
  // by every button remembering to check a flag.
  const [pendingDecision, setPendingDecision] = useState<{
    reportId: string;
    queue: QueueKey;
    status: Resolution;
    /** Which message table the report lives in; only message rows carry it. */
    kind?: 'matchmaking' | 'case_closed';
  } | null>(null);

  // Whether the confirmed request is in flight. Without it the confirm button stays live for
  // the whole round trip and a second click sends a second request for the same report.
  const [submitting, setSubmitting] = useState(false);

  // Restore target for Azure-hidden rows only; manual hides never offer restore.
  const [pendingRestore, setPendingRestore] = useState<{
    reportId: string;
    targetType: 'confession' | 'comment' | 'message';
    targetId: string;
    /** Which message table the restore targets; only message rows carry it. */
    kind?: 'matchmaking' | 'case_closed';
  } | null>(null);
  const [restoring, setRestoring] = useState(false);

  const AZURE_REMOVED_BY = 'azure-content-safety';
  const isAzureRemoved = (removedBy: string | null | undefined) => removedBy === AZURE_REMOVED_BY;

  // Which report card is open, as `queue:reportId`, or null when all are collapsed. Cards are
  // collapsed by default: the queue is read as a list first and opened one report at a time.
  //
  // One slot, not one per card. Report ids repeat across the three queues -- a comment report and
  // a message report can share an id -- so the key is qualified by queue, and holding a single
  // string means opening a card closes the previous one by construction instead of every toggle
  // remembering to sweep its neighbours shut.
  const [expandedReport, setExpandedReport] = useState<string | null>(null);

  const isExpanded = (queue: QueueKey, reportId: string) => expandedReport === `${queue}:${reportId}`;
  const toggleExpanded = (queue: QueueKey, reportId: string) =>
    setExpandedReport((previous) => (previous === `${queue}:${reportId}` ? null : `${queue}:${reportId}`));

  // One window per queue rather than one shared list, because the three payloads have
  // nothing in common but a reason and a status. A single union list would need a cast on
  // every field of every render to read one of the three.
  const [confessionQueue, setConfessionQueue] = useState<QueueWindow<ConfessionReport>>(emptyWindow());
  const [loveNoteQueue, setLoveNoteQueue] = useState<QueueWindow<LoveNoteReport>>(emptyWindow());
  const [messageQueue, setMessageQueue] = useState<QueueWindow<MessageReport>>(emptyWindow());
  /**
   * The three unfiltered lists, only ever set from legacy bare-array answers (older
   * servers and unit-test mocks), which ignore request params. The real server answers
   * envelopes and leaves this `null`, which is what switches the screen from client
   * lists to server pages: three reads that each returned the same full list would
   * otherwise triple-count every total.
   */
  const [legacyQueues, setLegacyQueues] = useState<{
    confessions: ConfessionReport[];
    loveNotes: LoveNoteReport[];
    messages: MessageReport[];
  } | null>(null);
  const [loading, setLoading] = useState<Record<string, boolean>>({
    confessions: true,
    loveNotes: true,
    messages: true,
  });
  /** A filter change, search submit or ruling re-reading while the queues stay up. */
  const [refreshing, setRefreshing] = useState(false);
  /** Which queue is appending its next page, so only its button reads busy. */
  const [loadingMore, setLoadingMore] = useState<QueueKey | null>(null);
  /** Whether the first load is still in flight, which is the only one that takes the loading cards. */
  const isFirstLoad = useRef(true);

  const setQueueLoading = (key: string, value: boolean) =>
    setLoading((previous) => ({ ...previous, [key]: value }));

  /**
   * Submit the draft search box as the query every queue is read with. Typing alone
   * fetches nothing; the submitted question re-reads all three queues from the first
   * page, because the previous offsets may not exist under it.
   */
  const submitSearch = () => {
    setAppliedSearch(searchTerm.trim());
  };

  /** Clear the search and the status filter together, back on full first pages. */
  const clearSearchAndFilter = () => {
    setSearchTerm('');
    setAppliedSearch('');
    setStatusFilter('all');
  };

  // Every queue refetches when the question changes, because the filter and the search
  // are the server's to apply: a client-side filter over the first page would show a
  // reviewer "no pending reports" while pending reports exist on page two.
  useEffect(() => {
    let cancelled = false;
    const fetchQueues = async () => {
      try {
        if (isFirstLoad.current) {
          setQueueLoading('confessions', true);
          setQueueLoading('loveNotes', true);
          setQueueLoading('messages', true);
        } else {
          setRefreshing(true);
        }
        const params = {
          ...(statusFilter === 'all' ? {} : { status: statusFilter }),
          ...(appliedSearch.trim() ? { q: appliedSearch.trim() } : {}),
          skip: 0,
          limit: QUEUE_PAGE_SIZE,
        };
        const [confessions, loveNotes, messages] = await Promise.all([
          getAdminConfessionReports(params),
          getAdminLoveNoteReports(params),
          getAdminMessageReports(params),
        ]);
        if (cancelled) return;
        const answers = [confessions, loveNotes, messages];
        if (answers.every((answer) => Array.isArray(answer))) {
          // The params went nowhere: one legacy list per queue, split nowhere, paged
          // never -- exactly the screen as it behaved before pagination existed.
          setLegacyQueues({
            confessions: reportsOf<ConfessionReport>(confessions),
            loveNotes: reportsOf<LoveNoteReport>(loveNotes),
            messages: reportsOf<MessageReport>(messages),
          });
        } else {
          setLegacyQueues(null);
          setConfessionQueue({
            items: reportsOf<ConfessionReport>(confessions),
            total: totalOf(confessions, 0),
          });
          setLoveNoteQueue({
            items: reportsOf<LoveNoteReport>(loveNotes),
            total: totalOf(loveNotes, 0),
          });
          setMessageQueue({
            items: reportsOf<MessageReport>(messages),
            total: totalOf(messages, 0),
          });
        }
      } catch (error) {
        if (!cancelled) {
          console.error('Failed to load report queues', error);
          toast.error('Unable to load reports for review.');
        }
      } finally {
        if (!cancelled) {
          setQueueLoading('confessions', false);
          setQueueLoading('loveNotes', false);
          setQueueLoading('messages', false);
          setRefreshing(false);
          isFirstLoad.current = false;
        }
      }
    };

    fetchQueues();
    return () => {
      cancelled = true;
    };
  }, [appliedSearch, statusFilter]);

  /** Whether answers arrive as server pages (`null` legacy queues) or one legacy list per queue. */
  const serverDriven = legacyQueues === null;

  // What each tab renders: the server window in paged mode, the whole legacy list in
  // legacy mode. Totals beside them are the server counts, or the list lengths where no
  // server paging exists to count with.
  const confessionReports: ConfessionReport[] = serverDriven
    ? confessionQueue.items
    : (legacyQueues?.confessions ?? []);
  const loveNoteReports: LoveNoteReport[] = serverDriven
    ? loveNoteQueue.items
    : (legacyQueues?.loveNotes ?? []);
  const messageReports: MessageReport[] = serverDriven
    ? messageQueue.items
    : (legacyQueues?.messages ?? []);
  const queueTotals: Record<QueueKey, number> = serverDriven
    ? {
        confessions: confessionQueue.total,
        loveNotes: loveNoteQueue.total,
        messages: messageQueue.total,
      }
    : {
        confessions: confessionReports.length,
        loveNotes: loveNoteReports.length,
        messages: messageReports.length,
      };

  /**
   * Grow one queue downwards: the next page appends beneath the reports already on
   * screen, so the reader's place never moves. The window stays contiguous from zero,
   * which is what keeps a later refresh a single clean re-read.
   */
  const queueWindows = {
    confessions: confessionQueue,
    loveNotes: loveNoteQueue,
    messages: messageQueue,
  };

  const loadMore = async (queue: QueueKey) => {
    if (loadingMore !== null || refreshing) return;
    const readers = {
      confessions: getAdminConfessionReports,
      loveNotes: getAdminLoveNoteReports,
      messages: getAdminMessageReports,
    };
    setLoadingMore(queue);
    try {
      const loaded = queueWindows[queue].items.length;
      const answer = await readers[queue]({
        ...(statusFilter === 'all' ? {} : { status: statusFilter }),
        ...(appliedSearch.trim() ? { q: appliedSearch.trim() } : {}),
        skip: loaded,
        limit: QUEUE_PAGE_SIZE,
      });
      const rows = reportsOf(answer);
      const total = totalOf(answer, loaded + rows.length);
      if (queue === 'confessions') {
        setConfessionQueue((previous) => ({
          items: [...previous.items, ...(rows as ConfessionReport[])],
          total,
        }));
      } else if (queue === 'loveNotes') {
        setLoveNoteQueue((previous) => ({
          items: [...previous.items, ...(rows as LoveNoteReport[])],
          total,
        }));
      } else {
        setMessageQueue((previous) => ({
          items: [...previous.items, ...(rows as MessageReport[])],
          total,
        }));
      }
    } catch (error) {
      console.error('Failed to load more reports', error);
      toast.error('Unable to load more reports.');
    } finally {
      setLoadingMore(null);
    }
  };

  /**
   * Re-read the queue a ruling touched, across the whole window currently loaded.
   *
   * A ruling changes the row's status and possibly its content flags, and may move it
   * out of the filtered question entirely -- so the window is re-read to fetch the newly
   * modified rows back in their decided state with true totals beside them, rather than
   * patching one card and leaving every count stale.
   */
  const refreshQueue = async (queue: QueueKey) => {
    const readers = {
      confessions: getAdminConfessionReports,
      loveNotes: getAdminLoveNoteReports,
      messages: getAdminMessageReports,
    };
    setRefreshing(true);
    try {
      const loaded = queueWindows[queue].items.length;
      const answer = await readers[queue]({
        ...(statusFilter === 'all' ? {} : { status: statusFilter }),
        ...(appliedSearch.trim() ? { q: appliedSearch.trim() } : {}),
        skip: 0,
        limit: Math.max(loaded, QUEUE_PAGE_SIZE),
      });
      const rows = reportsOf(answer);
      const total = totalOf(answer, rows.length);
      if (queue === 'confessions') {
        setConfessionQueue({ items: rows as ConfessionReport[], total });
      } else if (queue === 'loveNotes') {
        setLoveNoteQueue({ items: rows as LoveNoteReport[], total });
      } else {
        setMessageQueue({ items: rows as MessageReport[], total });
      }
    } catch (error) {
      console.error('Failed to refresh the report queue', error);
      toast.error('Unable to refresh the reports.');
    } finally {
      setRefreshing(false);
    }
  };

  const handleClose = async (
    reportId: string,
    queue: QueueKey,
    status: Resolution,
    kind?: 'matchmaking' | 'case_closed'
  ) => {
    const close = {
      confessions: updateAdminConfessionReportStatus,
      loveNotes: updateAdminLoveNoteReportStatus,
      messages: updateAdminMessageReportStatus,
    }[queue];

    try {
      // The message queue spans two tables; the report's own `kind` says which one to rule on,
      // defaulting to matchmaking so a row from an older server still closes.
      if (queue === 'messages') {
        await updateAdminMessageReportStatus(reportId, status, kind ?? 'matchmaking');
      } else {
        await close(reportId, status);
      }
      if (!serverDriven && legacyQueues) {
        // Legacy lists: the move is local, exactly as before pagination existed.
        setLegacyQueues((previous) => {
          if (!previous) return previous;
          const move = <T extends { id: string }>(rows: T[]): T[] =>
            rows.map((report) =>
              report.id === reportId
                ? { ...report, status, reviewed_at: new Date().toISOString() }
                : report,
            );
          return {
            confessions: move(previous.confessions),
            loveNotes: move(previous.loveNotes),
            messages: move(previous.messages),
          };
        });
      } else {
        await refreshQueue(queue);
      }
      toast.success(RESOLUTION_LABELS[status].toast);
    } catch (error) {
      console.error('Failed to close report', error);
      toast.error('Unable to update this report.');
    } finally {
      // Cleared here rather than on the dialog's own close so a failed request does not leave
      // a stale card marked as "being acted on". The `submitting` flag is released too --
      // otherwise one failed request would leave the dialog permanently disabled.
      setPendingDecision(null);
      setSubmitting(false);
    }
  };

  const queues: Queue[] = [
    {
      key: 'confessions',
      label: 'Confessions & comments',
      icon: ShieldQuestion,
      describe: (count) =>
        count === 1 ? '1 reported confession or comment' : `${count} reported confessions or comments`,
      openReport: (reportId, status) => updateAdminConfessionReportStatus(reportId, status),
    },
    {
      key: 'loveNotes',
      label: 'Love notes',
      icon: MessageCircleHeart,
      describe: (count) => (count === 1 ? '1 reported love note' : `${count} reported love notes`),
      openReport: (reportId, status) => updateAdminLoveNoteReportStatus(reportId, status),
    },
    {
      key: 'messages',
      label: 'Chat messages',
      icon: MessageSquare,
      describe: (count) => (count === 1 ? '1 reported message' : `${count} reported messages`),
      openReport: (reportId, status) => updateAdminMessageReportStatus(reportId, status),
    },
  ];

  // Tab badges read server totals under the current question -- not the loaded
  // windows -- so a queue of two hundred reads as two hundred with twenty on screen.
  const counts = queueTotals;
  const loadedTotal =
    confessionReports.length + loveNoteReports.length + messageReports.length;
  const serverTotal = queueTotals.confessions + queueTotals.loveNotes + queueTotals.messages;

  /**
   * Grow one queue downwards beneath the cards already on screen. Shown only while the
   * server holds more than the queue is showing; legacy lists never page, so they never
   * offer it.
   */
  const renderShowMore = (queue: QueueKey, label: string) => {
    const loaded =
      queue === 'confessions'
        ? confessionReports.length
        : queue === 'loveNotes'
          ? loveNoteReports.length
          : messageReports.length;
    const remaining = queueTotals[queue] - loaded;
    if (!serverDriven || remaining <= 0) return null;
    return (
      <div className="flex justify-center pt-1">
        <Button
          variant="outline"
          size="sm"
          onClick={() => void loadMore(queue)}
          disabled={loadingMore !== null || refreshing}
          aria-label={`Show more ${label}`}
        >
          {loadingMore === queue ? 'Loading…' : `Show more (${remaining} remaining)`}
        </Button>
      </div>
    );
  };

  // A click opens the dialog and nothing else. The request is made from the dialog's confirm
  // button, so the network call is only ever reachable from a deliberate second action.
  const renderCloseButtons = (reportId: string, queue: Queue['key'], kind?: 'matchmaking' | 'case_closed') =>
    canModerate ? (
      <div className="flex flex-wrap items-center justify-end gap-2">
        <Button
          variant="outline"
          size="sm"
          className="border-muted-foreground text-muted-foreground hover:bg-muted"
          onClick={() => setPendingDecision({ reportId, queue, status: 'dismissed', kind })}
        >
          <ShieldX className="mr-2 h-4 w-4" /> Dismiss
        </Button>
        <Button
          variant="destructive"
          size="sm"
          onClick={() => setPendingDecision({ reportId, queue, status: 'removed', kind })}
        >
          <Trash2 className="mr-2 h-4 w-4" /> Remove content
        </Button>
      </div>
    ) : null;

  const handleRestore = async () => {
    if (!pendingRestore) return;
    const { targetType, targetId, kind } = pendingRestore;
    setRestoring(true);
    try {
      if (targetType === 'comment') {
        await unhideAdminComment(targetId);
      } else if (targetType === 'message') {
        await unhideAdminMessage(targetId, kind ?? 'matchmaking');
      } else {
        await unhideAdminConfession(targetId);
      }
      await refreshQueue(targetType === 'message' ? 'messages' : 'confessions');
      toast.success('Content restored to public view.');
    } catch (error: any) {
      if (error?.response?.status === 409) {
        toast.error('This content was hidden by a moderator and cannot be restored here.');
      } else {
        toast.error('Could not restore the content.');
      }
    } finally {
      setPendingRestore(null);
      setRestoring(false);
    }
  };

  const renderFooter = (
    reportId: string,
    queue: Queue['key'],
    status: ReportStatus,
    reviewedAt: string | null,
    reviewedBy: string | null,
    reporterRef: string,
    targetRef: string,
    contentRemoved: boolean,
    restore?: { targetType: 'confession' | 'comment' | 'message'; targetId: string; removedBy: string | null | undefined; kind?: 'matchmaking' | 'case_closed' } | null,
    children?: React.ReactNode
  ) => (
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div className="space-y-1">
        <p className="text-xs text-muted-foreground">
          Reporter {reporterRef}
          {targetRef ? ` · Content ${targetRef}` : ''}
          {reviewedBy ? ` · Closed by ${reviewedBy} on ${formatTimestamp(reviewedAt)}` : ''}
        </p>
        {/* Said on every row where the content is down, not only on the report that took it
            down. A second report against the same content was still about content that no
            longer exists, and a moderator looking at it now needs to know that. */}
        {contentRemoved ? (
          <p className="text-xs font-medium text-destructive">
            {restore && isAzureRemoved(restore.removedBy)
              ? 'Content Removed by Azure Content Safety. The record of it is kept for auditing.'
              : 'This content has been removed from every participant-facing view. The record of it is kept for auditing.'}
          </p>
        ) : null}
      </div>
      <div className="flex flex-wrap items-center justify-end gap-2">
        {status === 'pending' ? renderCloseButtons(reportId, queue, restore?.kind) : null}
        {restore && contentRemoved && isAzureRemoved(restore.removedBy) ? (
          <Button
            variant="outline"
            size="sm"
            onClick={() => setPendingRestore({ reportId, targetType: restore.targetType, targetId: restore.targetId, kind: restore.kind })}
          >
            <Eye className="mr-2 h-4 w-4" /> Restore content
          </Button>
        ) : null}
      </div>
      {children}
    </div>
  );

  const renderEmpty = (queue: Queue) => (
    <Card>
      <CardContent className="py-12 text-center text-sm text-muted-foreground">
        {statusFilter === 'all'
          ? 'Nothing has been reported here yet.'
          : `No reports here are ${statusFilter}.`}
        <span className="mt-1 block text-xs">
          {queue.describe(0)} in this filter.
        </span>
      </CardContent>
    </Card>
  );

  const renderLoading = (label: string) => (
    <Card>
      <CardContent className="py-12 text-center text-sm text-muted-foreground">
        Loading {label}...
      </CardContent>
    </Card>
  );

  return (
    <div className="relative min-h-screen bg-background">
      <AdminNavigation />

      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-4 pb-16 pt-28">
        <header className="flex flex-col gap-2">
          <h1 className="text-3xl font-semibold">Reports Review</h1>
          <p className="text-sm text-muted-foreground">
            Everything a participant has reported, in the three places they can report
            something. Dismiss a report and the content stays up. Remove content and it leaves
            every participant-facing view for good — the record of it is kept so the decision
            can be audited — and both the person who wrote it and the person who reported it are
            told what happened.
          </p>
        </header>

        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
            {/* The box is a draft; the button sends it. Firing three queue reads per
                keystroke would answer a query the operator had already retyped, so typing
                only edits the draft and Search (or Enter) issues the one question the
                database answers -- reason and who reported it. Encrypted message bodies
                are not searchable. */}
            <div className="flex w-full gap-2 sm:max-w-xl">
              <div className="relative flex-1">
                <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  value={searchTerm}
                  onChange={(event) => setSearchTerm(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') {
                      event.preventDefault();
                      submitSearch();
                    }
                  }}
                  placeholder="Search reasons or reporter"
                  className="pl-9"
                />
              </div>
              <Button
                variant="secondary"
                onClick={submitSearch}
                disabled={refreshing}
                aria-label="Search reports"
              >
                <Search className="mr-2 h-4 w-4" />
                Search
              </Button>
            </div>
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Label htmlFor="reports-status-filter" className="uppercase tracking-wide">
                Filter
              </Label>
              <Select
                value={statusFilter}
                onValueChange={(value) => setStatusFilter(value as StatusFilter)}
              >
                <SelectTrigger id="reports-status-filter" className="w-44">
                  <SelectValue placeholder="Status" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All reports</SelectItem>
                  <SelectItem value="pending">Pending</SelectItem>
                  <SelectItem value="dismissed">Dismissed</SelectItem>
                  <SelectItem value="removed">Content removed</SelectItem>
                </SelectContent>
              </Select>
              {(appliedSearch.trim() !== '' || statusFilter !== 'all') && (
                <Button variant="ghost" size="sm" onClick={clearSearchAndFilter}>
                  Clear
                </Button>
              )}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <Badge variant="secondary">
              Showing {loadedTotal} of {serverTotal}
              {refreshing ? ' · Updating…' : ''}
            </Badge>
            <p className="text-xs text-muted-foreground">
              Search matches the reason and who reported it (message bodies stay encrypted), across all
              three queues at once.
            </p>
          </div>
        </div>

        <Tabs value={activeTab} onValueChange={setActiveTab}>
          <TabsList>
            {queues.map((queue) => (
              <TabsTrigger key={queue.key} value={queue.key}>
                <queue.icon className="mr-2 h-4 w-4" />
                {queue.label} ({counts[queue.key as keyof typeof counts]})
              </TabsTrigger>
            ))}
          </TabsList>

          <TabsContent value="confessions" className="mt-6 space-y-3">
            {loading.confessions ? (
              renderLoading('reported confessions')
            ) : confessionReports.length === 0 ? (
              renderEmpty(queues[0])
            ) : (
              confessionReports.map((report) => {
                const isComment = report.target_type === 'comment';
                const parent = report.parent_confession;
                return (
                  <ReportCard
                    key={report.id}
                    open={isExpanded('confessions', report.id)}
                    onToggle={() => toggleExpanded('confessions', report.id)}
                    title={`${isComment ? 'Comment' : 'Confession'} reported by ${partyName(report.reporter)}`}
                    status={statusBadge(report.status, report.reporter?.name)}
                    reason={report.reason}
                    content={report.content}
                    meta={
                      <>
                        Reported {formatTimestamp(report.reported_at)} · Written{' '}
                        {formatTimestamp(report.content_created_at)}
                        {report.anonymous ? ' · Anonymous to everyone, staff included' : ''}
                        {parent
                          ? ` · On a confession from ${
                              parent.author ? parent.author.name : 'an anonymous author'
                            }`
                          : ''}
                        {parent && parent.comment_count > 0
                          ? ` · ${parent.comment_count} comment${parent.comment_count === 1 ? '' : 's'} on it`
                          : ''}
                      </>
                    }
                    actions={renderFooter(
                      report.id,
                      'confessions',
                      report.status,
                      report.reviewed_at,
                      displayReviewer(report.reviewed_by_regno),
                      partyRef(report.reporter),
                      report.target_id,
                      report.content_removed,
                      {
                        targetType: isComment ? 'comment' : 'confession',
                        targetId: report.target_id,
                        removedBy: report.removed_by_regno,
                      }
                    )}
                  >
                    <div className="rounded-2xl border border-border bg-background/60 p-4">
                      <p className="text-sm uppercase tracking-wide text-muted-foreground">
                        Why it was reported
                      </p>
                      <p className="mt-2 whitespace-pre-line text-base leading-relaxed">
                        {report.reason ?? 'No reason was given.'}
                      </p>
                    </div>

                    <div className="rounded-2xl border border-border bg-background/60 p-4">
                      <p className="text-sm uppercase tracking-wide text-muted-foreground">
                        The {report.target_type} as it was written
                      </p>
                      <p className="mt-2 whitespace-pre-line text-base leading-relaxed">
                        {report.content ?? '[The reported content no longer exists]'}
                      </p>
                      <p className="mt-3 text-xs text-muted-foreground">
                        {report.author_hidden
                          ? authorLabel(report)
                          : `Written by ${authorLabel(report)} (${partyRef(report.author)})`}
                        {report.parent_recipient_name
                          ? ` · To ${report.parent_recipient_name}`
                          : ''}
                      </p>
                    </div>

                    {/* The confession a reported comment sits on. Shown after the reported
                        comment itself rather than above it, because the comment is what is on
                        trial and the parent is context for it -- not a second thing to rule on. */}
                    {parent && (
                      <div className="rounded-2xl border border-border bg-background/60 p-4">
                        <p className="text-sm uppercase tracking-wide text-muted-foreground">
                          The confession this comment was left on
                        </p>
                        <p className="mt-2 whitespace-pre-line text-base leading-relaxed text-muted-foreground">
                          {parent.body ?? '[The parent confession no longer exists]'}
                        </p>
                        <p className="mt-3 text-xs text-muted-foreground">
                          {parent.author ? (
                            <>
                              Written by {parent.author.name} ({partyRef(parent.author)})
                            </>
                          ) : (
                            /* The parent is anonymous, so its author is withheld by the server
                               and never sent to this screen at all -- there is nothing to hide
                               here, only a fact to state. */
                            'Written anonymously. The author is not available to staff.'
                          )}
                          {parent.recipient_name ? ` · To ${parent.recipient_name}` : ''}
                          {` · Written ${formatTimestamp(parent.created_at)}`}
                          {` · ${parent.comment_count} comment${parent.comment_count === 1 ? '' : 's'} in total`}
                        </p>
                        {parent.content_removed && (
                          <p className="mt-2 text-xs font-medium text-destructive">
                            This parent confession has already been removed from every
                            participant-facing view.
                          </p>
                        )}
                      </div>
                    )}
                  </ReportCard>
                );
              })
            )}
            {renderShowMore('confessions', 'reported confessions')}
          </TabsContent>

          <TabsContent value="loveNotes" className="mt-6 space-y-3">
            {loading.loveNotes ? (
              renderLoading('reported love notes')
            ) : loveNoteReports.length === 0 ? (
              renderEmpty(queues[1])
            ) : (
              loveNoteReports.map((report) => (
                <ReportCard
                  key={report.id}
                  open={isExpanded('loveNotes', report.id)}
                  onToggle={() => toggleExpanded('loveNotes', report.id)}
                  title={`Love note reported by ${partyName(report.reporter)}`}
                  status={statusBadge(report.status)}
                  reason={report.reason}
                  content={report.body}
                  meta={
                    <>
                      Reported {formatTimestamp(report.reported_at)} · Sent{' '}
                      {formatTimestamp(report.note_created_at)}
                      {report.anonymous ? ' · Anonymous to the recipient' : ''}
                      {report.note_status ? ` · Note is ${report.note_status.replace('_', ' ')}` : ''}
                    </>
                  }
                  actions={renderFooter(
                    report.id,
                    'loveNotes',
                    report.status,
                    report.reviewed_at,
                    report.reviewed_by_regno,
                    partyRef(report.reporter),
                    report.love_note_id,
                    report.content_removed
                  )}
                >
                  <div className="rounded-2xl border border-border bg-background/60 p-4">
                    <p className="text-sm uppercase tracking-wide text-muted-foreground">
                      Why it was reported
                    </p>
                    <p className="mt-2 whitespace-pre-line text-base leading-relaxed">
                      {report.reason ?? 'No reason was given.'}
                    </p>
                  </div>

                  <div className="rounded-2xl border border-border bg-background/60 p-4">
                    <p className="text-sm uppercase tracking-wide text-muted-foreground">
                      The note text, with personal information redacted
                    </p>
                    <p className="mt-2 whitespace-pre-line text-base leading-relaxed">
                      {report.moderation_body ?? '[No privacy-safe preview is available for this note]'}
                    </p>
                    {report.image_url && (
                      <img
                        src={report.image_url}
                        alt="Reported love note illustration"
                        // Non-expiring signed bearer URL: never send it as a referrer.
                        referrerPolicy="no-referrer"
                        className="mt-3 max-h-64 w-full rounded-xl object-cover"
                      />
                    )}
                    {/* The recipient was never told who sent the note, but the report is only
                        actionable if staff can see, so the sender travels with it. */}
                    <p className="mt-3 text-xs text-muted-foreground">
                      Sent by {partyName(report.reported_user)} ({partyRef(report.reported_user)})
                      {report.anonymous ? ' · named here, hidden from the recipient' : ''}
                    </p>
                  </div>
                </ReportCard>
              ))
            )}
            {renderShowMore('loveNotes', 'reported love notes')}
          </TabsContent>

          <TabsContent value="messages" className="mt-6 space-y-3">
            {loading.messages ? (
              renderLoading('reported messages')
            ) : messageReports.length === 0 ? (
              renderEmpty(queues[2])
            ) : (
              messageReports.map((report) => {
                const conversation = report.conversation;
                // Both message surfaces share this card; only the wording of the thread
                // context differs (a detective thread has no open/blocked status).
                const isCaseClosed = report.kind === 'case_closed';
                // The person the message was sent *to*. Only a recipient can report a message,
                // so this is the reporter in every real row -- which is exactly why the reporter
                // is not filtered out here: filtering it would leave the counterpart undefined
                // and drop the one fact that makes the row readable. Excluded is only the
                // sender, who is already named as the reported user.
                const reportedRegno = report.reported_user?.regno;
                const counterpart = conversation?.participants.find(
                  (person) => person.regno !== reportedRegno
                );
                return (
                  <ReportCard
                    key={report.id}
                    open={isExpanded('messages', report.id)}
                    onToggle={() => toggleExpanded('messages', report.id)}
                    title={`${isCaseClosed ? 'Detective thread message' : 'Message'} reported by ${partyName(report.reporter)}`}
                    status={statusBadge(report.status, report.reporter?.name)}
                    reason={report.reason}
                  content={report.moderation_body}
                    meta={
                      <>
                        Reported {formatTimestamp(report.reported_at)} · Sent{' '}
                        {formatTimestamp(report.message_created_at)}
                        {counterpart ? ` · In a ${isCaseClosed ? 'detective thread' : 'conversation'} with ${counterpart.name}` : ''}
                        {conversation ? ` · ${conversation.message_count} message${conversation.message_count === 1 ? '' : 's'} in it` : ''}
                        {conversation && conversation.removed_message_count > 0
                          ? ` · ${conversation.removed_message_count} already removed`
                          : ''}
                      </>
                    }
                    actions={renderFooter(
                      report.id,
                      'messages',
                      report.status,
                      report.reviewed_at,
                      displayReviewer(report.reviewed_by_regno),
                      partyRef(report.reporter),
                      report.message_id,
                      report.content_removed,
                      {
                        targetType: 'message',
                        targetId: report.message_id,
                        removedBy: report.removed_by_regno,
                        kind: report.kind ?? 'matchmaking',
                      }
                    )}
                  >
                    <div className="rounded-2xl border border-border bg-background/60 p-4">
                      <p className="text-sm uppercase tracking-wide text-muted-foreground">
                        Why it was reported
                      </p>
                      <p className="mt-2 whitespace-pre-line text-base leading-relaxed">
                        {report.reason ?? 'No reason was given.'}
                      </p>
                    </div>

                    <div className="rounded-2xl border border-border bg-background/60 p-4">
                      <p className="text-sm uppercase tracking-wide text-muted-foreground">
                        The message as it was sent
                      </p>
                      <p className="mt-2 whitespace-pre-line text-base leading-relaxed">
                        {report.body ?? '[The message no longer exists]'}
                      </p>
                      <p className="mt-3 text-xs text-muted-foreground">
                        Sent by {partyName(report.reported_user)} ({partyRef(report.reported_user)})
                        {` · Sent ${formatTimestamp(report.message_created_at)}`}
                      </p>
                    </div>

                    {/* A message is a single sentence with the rest of the thread missing, and
                        the thread is often what decides it: a threat among twenty messages that
                        are already being moderated is a different report from a threat that
                        stands alone in a two-message conversation. */}
                    {conversation && (
                      <div className="rounded-2xl border border-border bg-background/60 p-4">
                        <p className="text-sm uppercase tracking-wide text-muted-foreground">
                          {isCaseClosed ? 'The Case Closed thread it was sent in' : 'The conversation it was sent in'}
                        </p>
                        <p className="mt-2 text-sm">
                          Between{' '}
                          {conversation.participants
                            .map((person) => person.name)
                            .join(' and ')}
                        </p>
                        <p className="mt-1 text-xs text-muted-foreground">
                          {isCaseClosed
                            ? 'Case Closed detective thread'
                            : `Conversation ${conversation.status ?? 'status unknown'}`}
                          {` · ${conversation.message_count} message${conversation.message_count === 1 ? '' : 's'} in total`}
                          {conversation.removed_message_count > 0
                            ? ` · ${conversation.removed_message_count} already removed`
                            : ' · none removed'}
                        </p>
                        {/* The rest of the thread is not shown. A queue of reports that rendered
                            whole conversations would bury the reports themselves, and messages
                            that are not reported are not this screen's business. */}
                        <p className="mt-2 text-xs text-muted-foreground">
                          Reference {conversation.id}
                        </p>
                      </div>
                    )}
                  </ReportCard>
                );
              })
            )}
            {renderShowMore('messages', 'reported messages')}
          </TabsContent>
        </Tabs>
      </div>

      {/* One dialog for every card. It is rendered from `pendingDecision` rather than living
          inside each report, so there is exactly one place where "a removal is being
          confirmed" can be true and exactly one place that makes the request. */}
      <AlertDialog
        open={pendingDecision !== null}
        onOpenChange={(open) => {
          // Closing by any route -- the backdrop, Escape, Cancel -- clears the pending
          // decision, so nothing survives a cancel for the next card to pick up by mistake.
          if (!open) setPendingDecision(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {pendingDecision ? RESOLUTION_LABELS[pendingDecision.status].verb : ''} this
              report?
            </AlertDialogTitle>
            <AlertDialogDescription>
              {pendingDecision?.status === 'removed'
                ? `${RESOLUTION_COPY.removed.title} The reporter and the person who wrote it will both be notified, and the content will no longer be visible to anyone except staff. This cannot be undone from this screen.`
                : pendingDecision?.status === 'dismissed'
                  ? `${RESOLUTION_COPY.dismissed.title} The reporter is told it was reviewed and found to be within policy. Nobody else is notified, and the content stays exactly as it is.`
                  : ''}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={submitting}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              disabled={submitting}
              onClick={(event) => {
                // The dialog closes on confirm by default; holding it open for the duration of
                // the request keeps a second confirm from being possible mid-flight.
                event.preventDefault();
                if (!pendingDecision) return;
                setSubmitting(true);
                void handleClose(
                  pendingDecision.reportId,
                  pendingDecision.queue,
                  pendingDecision.status,
                  pendingDecision.kind
                );
              }}
              className={
                pendingDecision?.status === 'removed' ? 'bg-destructive text-destructive-foreground hover:bg-destructive/90' : undefined
              }
            >
              {submitting
                ? 'Working...'
                : pendingDecision
                  ? RESOLUTION_LABELS[pendingDecision.status].verb
                  : ''}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={pendingRestore !== null}
        onOpenChange={(open) => {
          if (!open) setPendingRestore(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Restore this content?</AlertDialogTitle>
            <AlertDialogDescription>
              Users will now be able to see this {pendingRestore?.targetType ?? 'content'}.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={restoring}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={restoring}
              onClick={(event) => {
                event.preventDefault();
                void handleRestore();
              }}
            >
              {restoring ? 'Working...' : 'Restore content'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
};
