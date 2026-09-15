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
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useAuth } from '@/contexts/AuthContext';
import { CAPABILITIES } from '@/lib/capabilities';
import { authorLabel, partyName, partyRef, type Person } from '@/lib/reportAuthors';
import { formatDateTimeDDMMYYYY } from '@/lib/utils';
import { toast } from 'sonner';
import { Flag, MessageSquare, MessageCircleHeart, ShieldQuestion, ShieldCheck } from 'lucide-react';

type ReportStatus = 'pending' | 'reviewed' | 'dismissed';
type StatusFilter = 'all' | ReportStatus;

/** A report against a confession, or against a comment on one. */
interface ConfessionReport {
  id: string;
  target_type: 'confession' | 'comment';
  target_id: string;
  confession_id: string | null;
  parent_recipient_name: string | null;
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
}

/** A report against a chat message. */
interface MessageReport {
  id: string;
  message_id: string;
  body: string | null;
  conversation_id: string | null;
  reporter: Person;
  reported_user: Person;
  reason: string | null;
  reported_at: string | null;
  status: ReportStatus;
  reviewed_at: string | null;
  reviewed_by_regno: string | null;
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

function statusBadge(status: ReportStatus) {
  if (status === 'pending') return <Badge variant="secondary">Awaiting review</Badge>;
  return <Badge variant="outline">{status}</Badge>;
}

export const ReportsReview = () => {
  const { can } = useAuth();
  // Read and close-out are separately grantable on the server, so they are hidden separately
  // here. This is presentation only: both endpoints re-check their capability on every call.
  const canModerate = can(CAPABILITIES.REPORTS_MODERATE);
  const [activeTab, setActiveTab] = useState('confessions');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');

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
    status: 'reviewed' | 'dismissed'
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
      await close(reportId, status);
      // Patched locally rather than refetched: the row the reviewer just acted on does not
      // move, and a refetch would make the list jump under the next click.
      setReports((previous) =>
        previous.map((report) =>
          report.id === reportId
            ? { ...report, status, reviewed_at: new Date().toISOString() }
            : report
        )
      );
      toast.success(`Report ${status}.`);
    } catch (error) {
      console.error('Failed to close report', error);
      toast.error('Unable to update this report.');
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

  const renderCloseButtons = (reportId: string, queue: Queue['key']) =>
    canModerate ? (
      <div className="flex flex-wrap items-center justify-end gap-2">
        <Button
          variant="outline"
          size="sm"
          className="border-green-500 text-green-500 hover:bg-green-500 hover:text-white"
          onClick={() => handleClose(reportId, queue, 'reviewed')}
        >
          <ShieldCheck className="mr-2 h-4 w-4" /> Mark reviewed
        </Button>
        <Button
          variant="outline"
          size="sm"
          className="border-muted-foreground text-muted-foreground hover:bg-muted"
          onClick={() => handleClose(reportId, queue, 'dismissed')}
        >
          Dismiss
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
    children?: React.ReactNode
  ) => (
    <div className="flex flex-wrap items-center justify-between gap-2">
      <p className="text-xs text-muted-foreground">
        Reporter {reporterRef}
        {targetRef ? ` · Content ${targetRef}` : ''}
        {reviewedBy ? ` · Closed by ${reviewedBy} on ${formatTimestamp(reviewedAt)}` : ''}
      </p>
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
            something. Closing a report records that someone looked at it — the content itself
            is ruled on in the screen that owns it.
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
                <SelectItem value="reviewed">Reviewed</SelectItem>
                <SelectItem value="dismissed">Dismissed</SelectItem>
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
              confessionReports.map((report) => (
                <Card key={report.id} className="overflow-hidden rounded-2xl border-border bg-muted/20">
                  <CardHeader className="pb-3">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <CardTitle className="flex flex-wrap items-center gap-2 text-base">
                        <Flag className="h-4 w-4 text-destructive" />
                        {report.target_type === 'comment' ? 'Comment' : 'Confession'} reported by{' '}
                        {partyName(report.reporter)}
                      </CardTitle>
                      {statusBadge(report.status)}
                    </div>
                    <CardDescription className="text-xs">
                      Reported {formatTimestamp(report.reported_at)} · Content written{' '}
                      {formatTimestamp(report.content_created_at)}
                      {report.anonymous ? ' · The post is anonymous to everyone, staff included' : ''}
                      {report.target_type === 'comment' ? ' · Reported comment on a confession' : ''}
                    </CardDescription>
                  </CardHeader>
                  <CardContent className="space-y-4">
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

                    {renderFooter(
                      report.id,
                      'confessions',
                      report.status,
                      report.reviewed_at,
                      report.reviewed_by_regno,
                      partyRef(report.reporter),
                      report.target_id
                    )}
                  </CardContent>
                </Card>
              ))
            )}
          </TabsContent>

          <TabsContent value="loveNotes" className="mt-6 space-y-3">
            {loading.loveNotes ? (
              renderLoading('reported love notes')
            ) : loveNoteReports.length === 0 ? (
              renderEmpty(queues[1])
            ) : (
              loveNoteReports.map((report) => (
                <Card key={report.id} className="overflow-hidden rounded-2xl border-border bg-muted/20">
                  <CardHeader className="pb-3">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <CardTitle className="flex flex-wrap items-center gap-2 text-base">
                        <Flag className="h-4 w-4 text-destructive" />
                        Love note reported by {partyName(report.reporter)}
                      </CardTitle>
                      {statusBadge(report.status)}
                    </div>
                    <CardDescription className="text-xs">
                      Reported {formatTimestamp(report.reported_at)} · Note sent{' '}
                      {formatTimestamp(report.note_created_at)}
                      {report.anonymous ? ' · Anonymous to the recipient' : ''}
                      {report.note_status ? ` · Note is ${report.note_status.replace('_', ' ')}` : ''}
                    </CardDescription>
                  </CardHeader>
                  <CardContent className="space-y-4">
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

                    {renderFooter(
                      report.id,
                      'loveNotes',
                      report.status,
                      report.reviewed_at,
                      report.reviewed_by_regno,
                      partyRef(report.reporter),
                      report.love_note_id
                    )}
                  </CardContent>
                </Card>
              ))
            )}
          </TabsContent>

          <TabsContent value="messages" className="mt-6 space-y-3">
            {loading.messages ? (
              renderLoading('reported messages')
            ) : messageReports.length === 0 ? (
              renderEmpty(queues[2])
            ) : (
              messageReports.map((report) => (
                <Card key={report.id} className="overflow-hidden rounded-2xl border-border bg-muted/20">
                  <CardHeader className="pb-3">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <CardTitle className="flex flex-wrap items-center gap-2 text-base">
                        <Flag className="h-4 w-4 text-destructive" />
                        Message reported by {partyName(report.reporter)}
                      </CardTitle>
                      {statusBadge(report.status)}
                    </div>
                    <CardDescription className="text-xs">
                      Reported {formatTimestamp(report.reported_at)} · Only the person who
                      received a message can report it
                    </CardDescription>
                  </CardHeader>
                  <CardContent className="space-y-4">
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
                      </p>
                    </div>

                    {renderFooter(
                      report.id,
                      'messages',
                      report.status,
                      report.reviewed_at,
                      report.reviewed_by_regno,
                      partyRef(report.reporter),
                      report.message_id
                    )}
                  </CardContent>
                </Card>
              ))
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
    </div>
  );
};
