import { useCallback, useEffect, useState } from 'react';
import {
  getAdminConfessionReports,
  getAdminLoveNoteReports,
  getAdminMessageReports,
  updateAdminConfessionReportStatus,
  updateAdminLoveNoteReportStatus,
  updateAdminMessageReportStatus,
} from '@/services/api';
import { AdminNavigation } from '@/components/AdminNavigation';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
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
import { ChevronDown, Flag, MessageSquare, MessageCircleHeart, ShieldQuestion, ShieldX, Trash2 } from 'lucide-react';

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
  body: string | null;
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

function statusBadge(status: ReportStatus) {
  if (status === 'pending') return <Badge variant="secondary">Awaiting review</Badge>;
  if (status === 'reviewed') return <Badge variant="outline">Dismissed</Badge>;
  if (status === 'removed') return <Badge variant="destructive">Content removed</Badge>;
  return <Badge variant="outline">Dismissed</Badge>;
}

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

export const ReportsReview = () => {
  const { can } = useAuth();
  // Read and close-out are separately grantable on the server, so they are hidden separately
  // here. This is presentation only: both endpoints re-check their capability on every call.
  const canModerate = can(CAPABILITIES.REPORTS_MODERATE);
  const [activeTab, setActiveTab] = useState('confessions');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');

  // The decision awaiting confirmation, or null when no dialog is open. Held as state rather
  // than wired through a per-card `useState` because the copy has to name the actual content
  // being removed, which the dialog is rendered from -- and because one slot is the only thing
  // that makes "a click opened a dialog, nothing happened yet" true by construction rather than
  // by every button remembering to check a flag.
  const [pendingDecision, setPendingDecision] = useState<{
    reportId: string;
    queue: QueueKey;
    status: Resolution;
  } | null>(null);

  // Whether the confirmed request is in flight. Without it the confirm button stays live for
  // the whole round trip and a second click sends a second request for the same report.
  const [submitting, setSubmitting] = useState(false);

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

  // One state slot per queue rather than one shared list, because the three payloads have
  // nothing in common but a reason and a status. A single union list would need a cast on
  // every field of every render to read one of the three.
  const [confessionReports, setConfessionReports] = useState<ConfessionReport[]>([]);
  const [loveNoteReports, setLoveNoteReports] = useState<LoveNoteReport[]>([]);
  const [messageReports, setMessageReports] = useState<MessageReport[]>([]);
  const [loading, setLoading] = useState<Record<string, boolean>>({
    confessions: true,
    loveNotes: true,
    messages: true,
  });

  const setQueueLoading = (key: string, value: boolean) =>
    setLoading((previous) => ({ ...previous, [key]: value }));

  const loadConfessions = useCallback(async () => {
    setQueueLoading('confessions', true);
    try {
      const response = await getAdminConfessionReports(
        statusFilter === 'all' ? undefined : statusFilter
      );
      setConfessionReports(Array.isArray(response?.reports) ? response.reports : []);
    } catch (error) {
      console.error('Failed to load confession reports', error);
      toast.error('Unable to load confession reports.');
    } finally {
      setQueueLoading('confessions', false);
    }
  }, [statusFilter]);

  const loadLoveNotes = useCallback(async () => {
    setQueueLoading('loveNotes', true);
    try {
      const response = await getAdminLoveNoteReports(
        statusFilter === 'all' ? undefined : statusFilter
      );
      setLoveNoteReports(Array.isArray(response?.reports) ? response.reports : []);
    } catch (error) {
      console.error('Failed to load love note reports', error);
      toast.error('Unable to load love note reports.');
    } finally {
      setQueueLoading('loveNotes', false);
    }
  }, [statusFilter]);

  const loadMessages = useCallback(async () => {
    setQueueLoading('messages', true);
    try {
      const response = await getAdminMessageReports(
        statusFilter === 'all' ? undefined : statusFilter
      );
      setMessageReports(Array.isArray(response?.reports) ? response.reports : []);
    } catch (error) {
      console.error('Failed to load message reports', error);
      toast.error('Unable to load message reports.');
    } finally {
      setQueueLoading('messages', false);
    }
  }, [statusFilter]);

  // Every queue refetches when the filter changes, because the filter is the server's to
  // apply: a client-side filter over the first page would show a reviewer "no pending
  // reports" while pending reports exist on page two.
  useEffect(() => {
    loadConfessions();
    loadLoveNotes();
    loadMessages();
  }, [loadConfessions, loadLoveNotes, loadMessages]);

  const pendingCount = (reports: { status: ReportStatus }[]) =>
    reports.filter((report) => report.status === 'pending').length;

  const handleClose = async (
    reportId: string,
    queue: QueueKey,
    status: Resolution
  ) => {
    const close = {
      confessions: updateAdminConfessionReportStatus,
      loveNotes: updateAdminLoveNoteReportStatus,
      messages: updateAdminMessageReportStatus,
    }[queue];
    const setReports = {
      confessions: setConfessionReports,
      loveNotes: setLoveNoteReports,
      messages: setMessageReports,
    }[queue];

    try {
      const result = await close(reportId, status);
      // Patched locally rather than refetched: the row the reviewer just acted on does not
      // move, and a refetch would make the list jump under the next click.
      //
      // `content_removed` comes from the server rather than from `status === 'removed'` because
      // they are not the same question. The server re-deciding a report whose content was
      // already taken down by an earlier report has not removed anything this time, and a
      // button that claimed otherwise would tell a moderator they removed something when the
      // content had been gone since before they arrived.
      setReports((previous) =>
        previous.map((report) =>
          report.id === reportId
            ? {
                ...report,
                status,
                reviewed_at: new Date().toISOString(),
                content_removed: result?.content_removed ?? report.content_removed,
              }
            : report
        )
      );
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

  const counts = {
    confessions: confessionReports.length,
    loveNotes: loveNoteReports.length,
    messages: messageReports.length,
  };

  // A click opens the dialog and nothing else. The request is made from the dialog's confirm
  // button, so the network call is only ever reachable from a deliberate second action.
  const renderCloseButtons = (reportId: string, queue: Queue['key']) =>
    canModerate ? (
      <div className="flex flex-wrap items-center justify-end gap-2">
        <Button
          variant="outline"
          size="sm"
          className="border-muted-foreground text-muted-foreground hover:bg-muted"
          onClick={() => setPendingDecision({ reportId, queue, status: 'dismissed' })}
        >
          <ShieldX className="mr-2 h-4 w-4" /> Dismiss
        </Button>
        <Button
          variant="destructive"
          size="sm"
          onClick={() => setPendingDecision({ reportId, queue, status: 'removed' })}
        >
          <Trash2 className="mr-2 h-4 w-4" /> Remove content
        </Button>
      </div>
    ) : null;

  const renderFooter = (
    reportId: string,
    queue: Queue['key'],
    status: ReportStatus,
    reviewedAt: string | null,
    reviewedBy: string | null,
    reporterRef: string,
    targetRef: string,
    contentRemoved: boolean,
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
            This content has been removed from every participant-facing view. The record of it
            is kept for auditing.
          </p>
        ) : null}
      </div>
      {status === 'pending' ? renderCloseButtons(reportId, queue) : null}
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

        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <span className="uppercase tracking-wide">Filter</span>
            <Select
              value={statusFilter}
              onValueChange={(value) => setStatusFilter(value as StatusFilter)}
            >
              <SelectTrigger className="w-44">
                <SelectValue placeholder="Status" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All reports</SelectItem>
                <SelectItem value="pending">Pending</SelectItem>
                <SelectItem value="dismissed">Dismissed</SelectItem>
                <SelectItem value="removed">Content removed</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <Badge variant="secondary">
            {Object.values(counts).reduce((sum, count) => sum + count, 0)} shown
          </Badge>
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
                    status={statusBadge(report.status)}
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
                      report.reviewed_by_regno,
                      partyRef(report.reporter),
                      report.target_id,
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
                      The note as the recipient received it
                    </p>
                    <p className="mt-2 whitespace-pre-line text-base leading-relaxed">
                      {report.body ?? '[The note no longer exists]'}
                    </p>
                    {report.image_url && (
                      <img
                        src={report.image_url}
                        alt="Reported love note illustration"
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
          </TabsContent>

          <TabsContent value="messages" className="mt-6 space-y-3">
            {loading.messages ? (
              renderLoading('reported messages')
            ) : messageReports.length === 0 ? (
              renderEmpty(queues[2])
            ) : (
              messageReports.map((report) => {
                const conversation = report.conversation;
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
                    title={`Message reported by ${partyName(report.reporter)}`}
                    status={statusBadge(report.status)}
                    reason={report.reason}
                    content={report.body}
                    meta={
                      <>
                        Reported {formatTimestamp(report.reported_at)} · Sent{' '}
                        {formatTimestamp(report.message_created_at)}
                        {counterpart ? ` · In a conversation with ${counterpart.name}` : ''}
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
                      report.reviewed_by_regno,
                      partyRef(report.reporter),
                      report.message_id,
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
                          The conversation it was sent in
                        </p>
                        <p className="mt-2 text-sm">
                          Between{' '}
                          {conversation.participants
                            .map((person) => person.name)
                            .join(' and ')}
                        </p>
                        <p className="mt-1 text-xs text-muted-foreground">
                          {`Conversation ${conversation.status ?? 'status unknown'}`}
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
          </TabsContent>
        </Tabs>

        <p className="text-xs text-muted-foreground">
          {Object.entries({
            confessions: pendingCount(confessionReports),
            loveNotes: pendingCount(loveNoteReports),
            messages: pendingCount(messageReports),
          })
            .map(([key, count]) => `${count} pending in ${queues.find((queue) => queue.key === key)?.label}`)
            .join(' · ')}
        </p>
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
                  pendingDecision.status
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
    </div>
  );
};
