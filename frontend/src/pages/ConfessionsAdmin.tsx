
import { useEffect, useMemo, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/components/ui/accordion';
import { AdminNavigation } from '@/components/AdminNavigation';
import { ConfessionBody } from '@/components/ConfessionBody';
import { getAdminConfessions, hideAdminConfession } from '@/services/api';
import { toast } from 'sonner';
import { formatDateTimeDDMMYYYY, timestampMs } from '@/lib/utils';
import {
  Heart,
  Laugh,
  Flame,
  HeartCrack,
  Flag,
  User,
  UserX,
  ChevronDown,
  Eye,
  EyeOff,
  MessageSquare,
  ThumbsUp,
  ThumbsDown,
  Search,
  SlidersHorizontal
} from 'lucide-react';
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

interface AdminReporter {
  id: string | null;
  name: string | null;
}

interface AdminReport {
  id: string;
  reason: string | null;
  created_at: string | null;
  reporter: AdminReporter;
}

interface AdminComment {
  id: string;
  body: string;
  created_at: string | null;
  user: {
    id?: string | null;
    name?: string | null;
    avatar?: string | null;
  };
  report_count: number;
  reported_by: string[];
  like_count: number;
  dislike_count: number;
  reports: AdminReport[];
}

interface AdminConfession {
  id: string;
  body: string;
  recipient_name?: string | null;
  anonymous: boolean;
  // Set by the server, not derived here: an anonymous confession has no sender to show, to
  // anyone. The fallback keeps the screen closed if the field is ever absent.
  sender_hidden?: boolean;
  comments_enabled: boolean;
  created_at: string | null;
  sender: {
    id: string | null;
    name: string | null;
    regno: string | null;
    email: string | null;
  };
  report_count: number;
  reported_by: string[];
  reports: AdminReport[];
  reactions: Record<string, string[]>;
  reaction_users?: Record<string, Array<{ id: string; name: string | null; withheld_reason?: string | null }>>;
  heart_count: number;
  haha_count: number;
  whoa_count: number;
  heartbreak_count: number;
  comment_count: number;
  comments: AdminComment[];
  // Set by the server, never derived here. A confession hidden from the review screen and one
  // taken down by a report ruling produce the same state on the same column, so this is the
  // only thing the screen can key its "already hidden" state off -- and it stays true for the
  // rest of the row's life, because there is no unhide.
  content_removed?: boolean;
  removed_at?: string | null;
  removed_by_regno?: string | null;
}

type ConfessionSortOption = 'recent' | 'reports' | 'comments' | 'reactions';
type ConfessionFilterOption = 'all' | 'reported' | 'anonymous' | 'with-comments';

/**
 * The denormalised counter beside each emoji, which is what every other surface shows.
 *
 * Kept as the count rather than `reactors.length` because the two are stored independently: the
 * counters are columns on the row and the ids are a JSONB array, so they can disagree. A reviewer
 * comparing the two against the reactor list is doing the audit, and quietly substituting one for
 * the other would remove the discrepancy instead of showing it.
 */
const countFor = (confession: AdminConfession, key: string) => {
  switch (key) {
    case 'heart':
      return confession.heart_count;
    case 'haha':
      return confession.haha_count;
    case 'whoa':
      return confession.whoa_count;
    default:
      return confession.heartbreak_count;
  }
};

const reactionDisplay = [
  { key: 'heart', label: 'Hearts', icon: Heart, color: 'text-pink-400' },
  { key: 'haha', label: 'Laughs', icon: Laugh, color: 'text-yellow-400' },
  { key: 'whoa', label: 'Whoas', icon: Flame, color: 'text-orange-400' },
  { key: 'heartbreak', label: 'Heartbreaks', icon: HeartCrack, color: 'text-rose-400' },
];

const formatTimestamp = (created_at: string | null) => {
  return formatDateTimeDDMMYYYY(created_at);
};

export const ConfessionsAdmin = () => {
  const [confessions, setConfessions] = useState<AdminConfession[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [selectedConfessionId, setSelectedConfessionId] = useState<string | null>(null);
  const [visibleSenders, setVisibleSenders] = useState<Record<string, boolean>>({});
  // One open reaction at a time, across every row, keyed `confessionId:reaction`. A record would
  // allow several at once, which turns a snapshot into a wall of names; a single slot keeps it
  // scannable and means a reviewer expanding a second one has implicitly closed the first.
  const [expandedReaction, setExpandedReaction] = useState<string | null>(null);
  const [sortOption, setSortOption] = useState<ConfessionSortOption>('reports');
  const [filterOption, setFilterOption] = useState<ConfessionFilterOption>('all');
  const [searchTerm, setSearchTerm] = useState('');

  useEffect(() => {
    const fetchConfessions = async () => {
      try {
        setLoading(true);
        const data = await getAdminConfessions();
        const normalized = Array.isArray(data)
          ? data.map((item: Partial<AdminConfession>, index) => ({
              id: item.id ?? `confession-${index}`,
              body: item.body ?? '',
              recipient_name: item.recipient_name ?? null,
              anonymous: item.anonymous ?? true,
              sender_hidden: item.sender_hidden ?? item.anonymous ?? true,
              comments_enabled: item.comments_enabled ?? true,
              created_at: item.created_at ?? null,
              sender: {
                id: item.sender?.id ?? null,
                name: item.sender?.name ?? null,
                regno: item.sender?.regno ?? null,
                email: item.sender?.email ?? null,
              },
              report_count: item.report_count ?? 0,
              reported_by: Array.isArray(item.reported_by) ? item.reported_by : [],
              reports: Array.isArray(item.reports)
                ? item.reports.map((report: Partial<AdminReport>, reportIndex) => ({
                    id: report.id ?? `confession-${index}-report-${reportIndex}`,
                    reason: report.reason ?? null,
                    created_at: report.created_at ?? null,
                    reporter: {
                      id: report.reporter?.id ?? null,
                      name: report.reporter?.name ?? null,
                    },
                  }))
                : [],
               reactions: item.reactions ?? {},
               reaction_users: item.reaction_users ?? {},
              heart_count: item.heart_count ?? 0,
              haha_count: item.haha_count ?? 0,
              whoa_count: item.whoa_count ?? 0,
              heartbreak_count: item.heartbreak_count ?? 0,
              comment_count: item.comment_count ?? 0,
              content_removed: item.content_removed ?? false,
              removed_at: item.removed_at ?? null,
              removed_by_regno: item.removed_by_regno ?? null,
              comments: Array.isArray(item.comments)
                ? item.comments.map((comment: Partial<AdminComment>, commentIndex) => ({
                    id: comment.id ?? `confession-${index}-comment-${commentIndex}`,
                    body: comment.body ?? '',
                    created_at: comment.created_at ?? null,
                    user: {
                      id: comment.user?.id ?? null,
                      name: comment.user?.name ?? null,
                      avatar: comment.user?.avatar ?? null,
                    },
                    report_count: comment.report_count ?? 0,
                    reported_by: Array.isArray(comment.reported_by) ? comment.reported_by : [],
                    like_count: comment.like_count ?? 0,
                    dislike_count: comment.dislike_count ?? 0,
                    reports: Array.isArray(comment.reports)
                      ? comment.reports.map((commentReport: Partial<AdminReport>, reportIndex) => ({
                          id: commentReport.id ?? `confession-${index}-comment-${commentIndex}-report-${reportIndex}`,
                          reason: commentReport.reason ?? null,
                          created_at: commentReport.created_at ?? null,
                          reporter: {
                            id: commentReport.reporter?.id ?? null,
                            name: commentReport.reporter?.name ?? null,
                          },
                        }))
                      : [],
                  }))
                : [],
            }))
          : [];
        setConfessions(normalized);
      } catch (error) {
        console.error('Failed to fetch confessions', error);
        toast.error('Unable to load confessions for review.');
      } finally {
        setLoading(false);
      }
    };

    fetchConfessions();
  }, []);

  const totalReports = useMemo(
    () =>
      confessions.reduce(
        (acc, item) => acc + (item.report_count ?? 0) + (item.reports?.length ?? 0),
        0
      ),
    [confessions]
  );

  const processedConfessions = useMemo(() => {
    let entries = [...confessions];

    if (filterOption === 'reported') {
      entries = entries.filter(
        (confession) => (confession.report_count ?? 0) > 0 || confession.reports.length > 0
      );
    } else if (filterOption === 'anonymous') {
      entries = entries.filter((confession) => confession.anonymous);
    } else if (filterOption === 'with-comments') {
      entries = entries.filter((confession) => confession.comments.length > 0);
    }

    if (searchTerm.trim()) {
      const query = searchTerm.trim().toLowerCase();
      entries = entries.filter((confession) => {
        const haystack = [
          confession.body,
          confession.recipient_name,
          confession.sender.name,
          confession.sender.email,
          confession.sender.regno,
        ]
          .filter(Boolean)
          .join(' ')
          .toLowerCase();
        return haystack.includes(query);
      });
    }

    const sorted = [...entries];
    switch (sortOption) {
      case 'reports':
        sorted.sort(
          (a, b) =>
            b.report_count + b.reports.length - (a.report_count + a.reports.length)
        );
        break;
      case 'comments':
        sorted.sort((a, b) => b.comments.length - a.comments.length);
        break;
      case 'reactions':
        sorted.sort((a, b) => {
          const reactionsA = a.heart_count + a.haha_count + a.whoa_count + a.heartbreak_count;
          const reactionsB = b.heart_count + b.haha_count + b.whoa_count + b.heartbreak_count;
          return reactionsB - reactionsA;
        });
        break;
      case 'recent':
      default:
        sorted.sort((a, b) => {
          const aTime = timestampMs(a.created_at);
          const bTime = timestampMs(b.created_at);
          return bTime - aTime;
        });
    }

    return sorted;
  }, [confessions, filterOption, searchTerm, sortOption]);

  const handleHide = (confessionId: string) => {
    setSelectedConfessionId(confessionId);
    setDialogOpen(true);
  };

  const confirmHide = async () => {
    if (!selectedConfessionId) return;
    const confessionId = selectedConfessionId;
    try {
      const result = await hideAdminConfession(confessionId);
      // The row stays in the list rather than being filtered out: a reviewer working down a queue
      // needs to see that this one is already handled, and the hidden state is the evidence.
      // `already_hidden` is answered separately so a double click says so rather than claiming
      // a second moderation action happened.
      setConfessions((prev) =>
        prev.map((item) =>
          item.id === confessionId
            ? {
                ...item,
                content_removed: true,
                removed_at: result?.removed_at ?? item.removed_at ?? null,
                removed_by_regno: result?.removed_by_regno ?? item.removed_by_regno ?? null,
              }
            : item
        )
      );
      toast.success(
        result?.already_hidden
          ? 'This confession was already hidden.'
          : 'Confession hidden from public view.',
      );
    } catch (error) {
      console.error('Failed to hide confession', error);
      toast.error('Could not hide the confession.');
    } finally {
      setDialogOpen(false);
      setSelectedConfessionId(null);
    }
  };

  const toggleSenderVisibility = (confessionId: string) => {
    setVisibleSenders((prev) => ({
      ...prev,
      [confessionId]: !prev[confessionId],
    }));
  };

  return (
    <div className="relative min-h-screen bg-background">
      <AdminNavigation />

      <div className="mx-auto flex w-full max-w-6xl flex-col gap-8 px-4 pb-16 pt-28">
        <header className="flex flex-col gap-2">
          <h1 className="text-3xl font-semibold">Confession Review</h1>
          <p className="text-sm text-muted-foreground">
            Review every confession, act on its reports and comments, and hide harmful submissions.
            Hiding takes a confession out of public view and cannot be undone; the post stays here
            as a record. An anonymous confession is anonymous here too, so action it on what was
            written. Each confession is collapsed to the line a reviewer triages on; expand one to
            read it in full and act on it.
          </p>
          <p className="text-xs text-muted-foreground/80">
            Total confessions: {confessions.length} · Reports in queue: {totalReports}
          </p>
        </header>

        <section className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
          <div className="relative w-full md:max-w-md">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={searchTerm}
              onChange={(event) => setSearchTerm(event.target.value)}
              placeholder="Search by confession text or sender details"
              className="pl-9"
            />
          </div>

          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:gap-4">
            <div className="flex items-center gap-2">
              <span className="text-xs font-medium uppercase text-muted-foreground">Sort</span>
              <Select
                value={sortOption}
                onValueChange={(value) => setSortOption(value as ConfessionSortOption)}
              >
                <SelectTrigger className="w-44">
                  <SelectValue placeholder="Sort" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="reports">Most reported</SelectItem>
                  <SelectItem value="recent">Most recent</SelectItem>
                  <SelectItem value="comments">Most comments</SelectItem>
                  <SelectItem value="reactions">Most reactions</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="flex items-center gap-2">
              <span className="text-xs font-medium uppercase text-muted-foreground">Filter</span>
              <Select
                value={filterOption}
                onValueChange={(value) => setFilterOption(value as ConfessionFilterOption)}
              >
                <SelectTrigger className="w-48">
                  <SelectValue placeholder="Filter" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All confessions</SelectItem>
                  <SelectItem value="reported">Flagged only</SelectItem>
                  <SelectItem value="anonymous">Anonymous posts</SelectItem>
                  <SelectItem value="with-comments">With comments</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
        </section>

        <div className="space-y-6">
          {loading ? (
            <Card>
              <CardContent className="py-12 text-center text-sm text-muted-foreground">
                Loading confessions for review...
              </CardContent>
            </Card>
          ) : processedConfessions.length === 0 ? (
            <Card>
              <CardContent className="py-12 text-center text-sm text-muted-foreground">
                No confessions match the current filters.
              </CardContent>
            </Card>
          ) : (
            <Accordion type="single" collapsible className="space-y-3">
              {processedConfessions.map((confession) => {
                const senderVisible = visibleSenders[confession.id];
                const senderHidden = confession.sender_hidden ?? confession.anonymous;
                const anonymityVariant: 'default' | 'secondary' = confession.anonymous ? 'secondary' : 'default';
                const commentTotal = Math.max(confession.comment_count, confession.comments.length);
                return (
                  <AccordionItem
                    key={confession.id}
                    value={confession.id}
                    className="overflow-hidden rounded-2xl border border-border bg-muted/20"
                  >
                    <AccordionTrigger className="flex w-full items-start justify-between gap-3 px-4 py-3 text-left hover:no-underline">
                      <div className="flex min-w-0 flex-1 flex-col gap-1">
{/* The confession is what a reviewer judges, so it leads the row and the recipient
                          trails it as context. A confession without a recipient is a normal
                          confession, not a missing field, so it says nothing rather than
                          announcing the absence. */}
                      <ConfessionBody clamp={2} className="text-sm leading-relaxed">
                        {confession.body}
                      </ConfessionBody>
                      <span className="text-xs text-muted-foreground">
                        {confession.recipient_name && (
                          <span>
                            To <span className="font-medium text-foreground">{confession.recipient_name}</span>{' '}
                            &middot;{' '}
                          </span>
                        )}
Submitted {formatTimestamp(confession.created_at)} &middot; Comments {commentTotal}
                      </span>
                    </div>
                      {/* shrink-0 rather than flex-wrap: these badges are the reviewer's
                          state at a glance and wrapping them to a second line makes a long
                          confession row taller than the badges, which reads as the row being
                          about the badges. */}
                      <div className="flex shrink-0 items-center gap-2">
                        <Badge variant={anonymityVariant}>
                          {confession.anonymous ? 'Anonymous' : 'Public confession'}
                        </Badge>
                        {!confession.comments_enabled && <Badge variant="outline">Comments disabled</Badge>}
                        {confession.report_count > 0 && (
                          <Badge variant="destructive">
                            <span className="flex items-center gap-1">
                              <Flag className="h-3 w-3" />
                              {confession.report_count} reports
                            </span>
                          </Badge>
                        )}
                      </div>
                    </AccordionTrigger>
                    <AccordionContent className="space-y-6 bg-background/40 px-4 pb-5 pt-2">
                      <ConfessionBody className="rounded-2xl border border-border bg-muted/40 p-4 leading-relaxed">
                        {confession.body}
                      </ConfessionBody>

                    <div className="grid gap-4 lg:grid-cols-2">
                      <div className="rounded-2xl border border-border bg-muted/30 p-4">
                        <div className="mb-2 flex items-center justify-between">
                          <div className="flex items-center gap-2 text-sm font-medium">
                            {senderHidden ? (
                              <UserX className="h-4 w-4 text-muted-foreground" />
                            ) : (
                              <User className="h-4 w-4 text-muted-foreground" />
                            )}
                            Sender details
                          </div>
                          {!senderHidden && (
                            <Button
                              variant="ghost"
                              size="sm"
                              className="flex items-center gap-1"
                              onClick={() => toggleSenderVisibility(confession.id)}
                            >
                              {senderVisible ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                              {senderVisible ? 'Hide' : 'Reveal'}
                            </Button>
                          )}
                        </div>

                        {senderHidden ? (
                          <p className="flex items-start gap-2 text-xs text-muted-foreground">
                            <UserX className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                            Submitted anonymously. The sender is withheld from staff too, so
                            judge this one on its content, reports and comments.
                          </p>
                        ) : senderVisible ? (
                          <div className="grid gap-1 text-sm">
                            <p>
                              <span className="text-muted-foreground">name:</span> {confession.sender.name ?? 'Unknown'}
                            </p>
                            <p>
                              <span className="text-muted-foreground">Reg No:</span> {confession.sender.regno ?? 'Unknown'}
                            </p>
                            <p>
                              <span className="text-muted-foreground">Email:</span> {confession.sender.email ?? 'Unknown'}
                            </p>
                            <p>
                              <span className="text-muted-foreground">User ID:</span> {confession.sender.id ?? 'Unknown'}
                            </p>
                          </div>
                        ) : (
                          <p className="text-xs text-muted-foreground">Hidden while browsing. Reveal when you need to action.</p>
                        )}
                      </div>

                      <div className="rounded-2xl border border-border bg-muted/30 p-4">
                        <div className="mb-3 flex items-center justify-between">
                          <div className="flex items-center gap-2 text-sm font-medium">
                            <SlidersHorizontal className="h-4 w-4 text-muted-foreground" />
                            Engagement snapshot
                          </div>
                          <span className="text-xs text-muted-foreground">Comments: {commentTotal}</span>
                        </div>
                        <div className="grid gap-3 sm:grid-cols-2">
                          {reactionDisplay.map(({ key, label, icon: Icon, color }) => {
                            const reactors = confession.reaction_users?.[key] ?? [];
                            return (
                              <div
                                key={key}
                                className="rounded-xl border border-border bg-background/40 px-3 py-2 text-sm"
                              >
                                {/* A button rather than a plain row so the reactor list is reachable
                                    by keyboard. It is disabled at zero rather than hidden: the four
                                    rows line up as a comparison, and a reviewer scanning them wants
                                    to see which reactions are absent, not find gaps in the grid. */}
                                <button
                                  type="button"
                                  disabled={reactors.length === 0}
                                  aria-expanded={reactors.length > 0 && expandedReaction === `${confession.id}:${key}`}
                                  onClick={() =>
                                    setExpandedReaction((prev) =>
                                      prev === `${confession.id}:${key}` ? null : `${confession.id}:${key}`,
                                    )
                                  }
                                  className="flex w-full items-center justify-between text-left disabled:cursor-default"
                                >
                                  <span className="flex items-center gap-2">
                                    <Icon className={`h-4 w-4 ${color}`} />
                                    <span>{label}</span>
                                  </span>
                                  <span className="flex items-center gap-2 font-medium">
                                    {countFor(confession, key)}
                                    {reactors.length > 0 && (
                                      <ChevronDown
                                        className={`h-3.5 w-3.5 text-muted-foreground transition-transform ${
                                          expandedReaction === `${confession.id}:${key}` ? 'rotate-180' : ''
                                        }`}
                                      />
                                    )}
                                  </span>
                                </button>
                                {expandedReaction === `${confession.id}:${key}` && reactors.length > 0 && (
                                  <ul className="mt-2 space-y-1 border-t border-border/70 pt-2">
                                    {reactors.map((reactor) => (
                                      <li key={reactor.id} className="flex items-center gap-2 text-xs">
                                        <User className="h-3 w-3 shrink-0 text-muted-foreground" />
                                        <span className="truncate text-muted-foreground">
                                          {/* Two different reasons a name is absent, and the reviewer is
                                              told which: an anonymous post's author is withheld on
                                              purpose, while a stale id means the account is gone.
                                              Rendering both as "Unknown" would make a privacy
                                              decision look like a data fault. */}
                                          {reactor.name ?? (
                                            reactor.withheld_reason === 'anonymous_author'
                                              ? 'Withheld — author of an anonymous confession'
                                              : 'Unknown — account no longer exists'
                                          )}
                                        </span>
                                      </li>
                                    ))}
                                  </ul>
                                )}
                              </div>
                            );
                          })}
                          <div className="flex items-center justify-between rounded-xl border border-border bg-background/40 px-3 py-2 text-sm">
                            <div className="flex items-center gap-2">
                              <MessageSquare className="h-4 w-4 text-violet-400" />
                              <span>Total comments</span>
                            </div>
                            <span className="font-medium">{commentTotal}</span>
                          </div>
                        </div>
                      </div>
                    </div>

                    {confession.reports.length > 0 && (
                      <div className="space-y-3 rounded-2xl border border-destructive/30 bg-destructive/10 p-4">
                        <div className="flex items-center gap-2 text-sm font-semibold text-destructive-foreground">
                          <Flag className="h-4 w-4" /> {confession.reports.length} report(s) received · System count {confession.report_count}
                        </div>
                        {confession.reports.map((report) => (
                          <div
                            key={report.id}
                            className="rounded-xl border border-destructive/20 bg-background/80 p-3 text-sm text-destructive-foreground"
                          >
                            <p className="font-medium">
                              {report.reporter.name ?? 'Anonymous reporter'}
                            </p>
                            <p className="text-xs opacity-80">{formatTimestamp(report.created_at)}</p>
                            <p className="mt-2 text-destructive-foreground/90">
                              {report.reason ?? 'No reason provided.'}
                            </p>
                          </div>
                        ))}
                      </div>
                    )}

                    <div className="rounded-2xl border border-border bg-muted/30 p-4">
                      <div className="mb-3 flex items-center justify-between">
                        <div className="flex items-center gap-2 text-sm font-medium">
                          <MessageSquare className="h-4 w-4 text-muted-foreground" />
                          Comments ({confession.comments.length})
                        </div>
                        {confession.comments.length > 0 && (
                          <span className="text-xs text-muted-foreground">
                            Expand a comment to see its full context and reports.
                          </span>
                        )}
                      </div>

                      {confession.comments.length === 0 ? (
                        <p className="text-xs text-muted-foreground">No comments have been left on this confession.</p>
                      ) : (
                        <Accordion type="multiple" className="space-y-2">
                          {confession.comments.map((comment) => (
                            <AccordionItem
                              key={comment.id}
                              value={comment.id}
                              className="overflow-hidden rounded-2xl border border-border bg-background/70 px-2"
                            >
                              <AccordionTrigger className="px-3 py-3">
                                <div className="flex w-full flex-col gap-1 text-left">
                                  <div className="flex flex-wrap items-center justify-between gap-3">
                                    <span className="text-sm font-medium">
                                      {comment.user.name ?? 'Anonymous commenter'}
                                    </span>
                                    <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
                                      <span className="flex items-center gap-1">
                                        <ThumbsUp className="h-3 w-3" /> {comment.like_count}
                                      </span>
                                      <span className="flex items-center gap-1">
                                        <ThumbsDown className="h-3 w-3" /> {comment.dislike_count}
                                      </span>
                                      {comment.report_count > 0 && (
                                        <Badge variant="destructive">
                                          <span className="text-[10px]">
                                            {comment.report_count} reports
                                          </span>
                                        </Badge>
                                      )}
                                    </div>
                                  </div>
                                  <span className="text-xs text-muted-foreground">
                                    {formatTimestamp(comment.created_at)}
                                  </span>
                                </div>
                              </AccordionTrigger>
                              <AccordionContent className="px-3 pb-4">
                                <p className="text-sm leading-relaxed">{comment.body}</p>
                                {comment.reports.length > 0 && (
                                  <div className="mt-3 space-y-2">
                                    {comment.reports.map((report) => (
                                      <div
                                        key={report.id}
                                        className="rounded-xl border border-destructive/20 bg-destructive/5 p-3 text-xs text-destructive-foreground"
                                      >
                                        <p className="font-medium">
                                          {report.reporter.name ?? 'Anonymous reporter'}
                                        </p>
                                        <p className="text-[10px] uppercase tracking-wide opacity-75">
                                          {formatTimestamp(report.created_at)}
                                        </p>
                                        <p className="mt-2 text-sm text-destructive-foreground/90">
                                          {report.reason ?? 'No reason provided.'}
                                        </p>
                                      </div>
                                    ))}
                                  </div>
                                )}
                              </AccordionContent>
                            </AccordionItem>
                          ))}
                        </Accordion>
                      )}
                    </div>

                    <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-end">
                      {confession.content_removed ? (
                        // Not a disabled button. A control the reviewer can see but cannot use
                        // invites a second click that does nothing, and a screen full of greyed-out
                        // controls is harder to scan than one that simply says what happened.
                        <p className="flex items-center gap-2 text-sm text-muted-foreground">
                          <EyeOff className="h-4 w-4" />
                          Already hidden from public view
                          {confession.removed_at ? ` on ${formatTimestamp(confession.removed_at)}` : ''}
                          {confession.removed_by_regno ? ` by ${confession.removed_by_regno}` : ''}.
                        </p>
                      ) : (
                        <Button
                          variant="outline"
                          size="sm"
                          className="border-destructive text-destructive hover:bg-destructive hover:text-destructive-foreground"
                          onClick={() => handleHide(confession.id)}
                        >
                          <EyeOff className="mr-2 h-4 w-4" /> Hide confession
                        </Button>
                      )}
                    </div>
                  </AccordionContent>
                </AccordionItem>
              );
            })}
            </Accordion>
          )}
        </div>
      </div>

      <AlertDialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Hide this confession?</AlertDialogTitle>
            <AlertDialogDescription>
              This will hide the confession from public view and notify its author. It cannot be
              unhidden -- there is no way to bring a hidden confession back. The confession stays on
              this screen as a record of the decision.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={confirmHide} className="bg-destructive hover:bg-destructive/90">
              Hide confession
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
};
