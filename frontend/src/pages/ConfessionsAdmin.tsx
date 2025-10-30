
import { useEffect, useMemo, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
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
import { getAdminConfessions, deleteAdminConfession } from '@/services/api';
import { toast } from 'sonner';
import {
  Heart,
  Laugh,
  Flame,
  HeartCrack,
  Flag,
  Trash2,
  User,
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
  timestamp: string | null;
  reporter: AdminReporter;
}

interface AdminComment {
  id: string;
  message: string;
  timestamp: string | null;
  user: {
    id?: string | null;
    username?: string | null;
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
  confession: string;
  is_anonymous: boolean;
  is_comment: boolean;
  timestamp: string | null;
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
  heart_count: number;
  haha_count: number;
  whoa_count: number;
  heartbreak_count: number;
  comment_count: number;
  comments: AdminComment[];
}

type ConfessionSortOption = 'recent' | 'reports' | 'comments' | 'reactions';
type ConfessionFilterOption = 'all' | 'reported' | 'anonymous' | 'with-comments';

const reactionDisplay = [
  { key: 'heart', label: 'Hearts', icon: Heart, color: 'text-pink-400' },
  { key: 'haha', label: 'Laughs', icon: Laugh, color: 'text-yellow-400' },
  { key: 'whoa', label: 'Whoas', icon: Flame, color: 'text-orange-400' },
  { key: 'heartbreak', label: 'Heartbreaks', icon: HeartCrack, color: 'text-rose-400' },
];

const formatTimestamp = (timestamp: string | null) => {
  if (!timestamp) return 'Unknown';
  const date = new Date(timestamp);
  return date.toLocaleString();
};

export const ConfessionsAdmin = () => {
  const [confessions, setConfessions] = useState<AdminConfession[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [selectedConfessionId, setSelectedConfessionId] = useState<string | null>(null);
  const [visibleSenders, setVisibleSenders] = useState<Record<string, boolean>>({});
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
              confession: item.confession ?? '',
              is_anonymous: item.is_anonymous ?? true,
              is_comment: item.is_comment ?? true,
              timestamp: item.timestamp ?? null,
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
                    timestamp: report.timestamp ?? null,
                    reporter: {
                      id: report.reporter?.id ?? null,
                      name: report.reporter?.name ?? null,
                    },
                  }))
                : [],
              reactions: item.reactions ?? {},
              heart_count: item.heart_count ?? 0,
              haha_count: item.haha_count ?? 0,
              whoa_count: item.whoa_count ?? 0,
              heartbreak_count: item.heartbreak_count ?? 0,
              comment_count: item.comment_count ?? 0,
              comments: Array.isArray(item.comments)
                ? item.comments.map((comment: Partial<AdminComment>, commentIndex) => ({
                    id: comment.id ?? `confession-${index}-comment-${commentIndex}`,
                    message: comment.message ?? '',
                    timestamp: comment.timestamp ?? null,
                    user: {
                      id: comment.user?.id ?? null,
                      username: comment.user?.username ?? null,
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
                          timestamp: commentReport.timestamp ?? null,
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
      entries = entries.filter((confession) => confession.is_anonymous);
    } else if (filterOption === 'with-comments') {
      entries = entries.filter((confession) => confession.comments.length > 0);
    }

    if (searchTerm.trim()) {
      const query = searchTerm.trim().toLowerCase();
      entries = entries.filter((confession) => {
        const haystack = [
          confession.confession,
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
          const aTime = a.timestamp ? new Date(a.timestamp).getTime() : 0;
          const bTime = b.timestamp ? new Date(b.timestamp).getTime() : 0;
          return bTime - aTime;
        });
    }

    return sorted;
  }, [confessions, filterOption, searchTerm, sortOption]);

  const handleDelete = (confessionId: string) => {
    setSelectedConfessionId(confessionId);
    setDialogOpen(true);
  };

  const confirmDelete = async () => {
    if (!selectedConfessionId) return;
    try {
      await deleteAdminConfession(selectedConfessionId);
      setConfessions((prev) => prev.filter((item) => item.id !== selectedConfessionId));
      toast.success('Confession removed successfully.');
    } catch (error) {
      console.error('Failed to delete confession', error);
      toast.error('Could not delete the confession.');
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
            Review every confession, see who sent it even if anonymous, and remove harmful submissions.
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
            processedConfessions.map((confession) => {
              const senderVisible = visibleSenders[confession.id];
              const anonymityVariant: 'default' | 'secondary' = confession.is_anonymous ? 'secondary' : 'default';
              const commentTotal = Math.max(confession.comment_count, confession.comments.length);
              return (
                <Card key={confession.id} className="border border-border">
                  <CardHeader>
                    <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
                      <div>
                        <CardTitle className="text-lg">Confession</CardTitle>
                        <CardDescription>
                          Submitted {formatTimestamp(confession.timestamp)} · Comments {commentTotal}
                        </CardDescription>
                      </div>
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge variant={anonymityVariant}>
                          {confession.is_anonymous ? 'Anonymous to users' : 'Public confession'}
                        </Badge>
                        {!confession.is_comment && <Badge variant="outline">Comments disabled</Badge>}
                        {confession.report_count > 0 && (
                          <Badge variant="destructive" className="flex items-center gap-1">
                            <Flag className="h-3 w-3" />
                            {confession.report_count} reports
                          </Badge>
                        )}
                      </div>
                    </div>
                  </CardHeader>
                  <CardContent className="space-y-6">
                    <p className="rounded-2xl border border-border bg-muted/40 p-4 leading-relaxed">
                      {confession.confession}
                    </p>

                    <div className="grid gap-4 lg:grid-cols-2">
                      <div className="rounded-2xl border border-border bg-muted/30 p-4">
                        <div className="mb-2 flex items-center justify-between">
                          <div className="flex items-center gap-2 text-sm font-medium">
                            <User className="h-4 w-4 text-muted-foreground" />
                            Sender details
                          </div>
                          <Button
                            variant="ghost"
                            size="sm"
                            className="flex items-center gap-1"
                            onClick={() => toggleSenderVisibility(confession.id)}
                          >
                            {senderVisible ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                            {senderVisible ? 'Hide' : 'Reveal'}
                          </Button>
                        </div>

                        {senderVisible ? (
                          <div className="grid gap-1 text-sm">
                            <p>
                              <span className="text-muted-foreground">Name:</span> {confession.sender.name ?? 'Unknown'}
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
                          {reactionDisplay.map(({ key, label, icon: Icon, color }) => (
                            <div
                              key={key}
                              className="flex items-center justify-between rounded-xl border border-border bg-background/40 px-3 py-2 text-sm"
                            >
                              <div className="flex items-center gap-2">
                                <Icon className={`h-4 w-4 ${color}`} />
                                <span>{label}</span>
                              </div>
                              <span className="font-medium">
                                {key === 'heart'
                                  ? confession.heart_count
                                  : key === 'haha'
                                  ? confession.haha_count
                                  : key === 'whoa'
                                  ? confession.whoa_count
                                  : confession.heartbreak_count}
                              </span>
                            </div>
                          ))}
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
                            <p className="text-xs opacity-80">{formatTimestamp(report.timestamp)}</p>
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
                                      {comment.user.username ?? 'Anonymous commenter'}
                                    </span>
                                    <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
                                      <span className="flex items-center gap-1">
                                        <ThumbsUp className="h-3 w-3" /> {comment.like_count}
                                      </span>
                                      <span className="flex items-center gap-1">
                                        <ThumbsDown className="h-3 w-3" /> {comment.dislike_count}
                                      </span>
                                      {comment.report_count > 0 && (
                                        <Badge variant="destructive" className="text-[10px]">
                                          {comment.report_count} reports
                                        </Badge>
                                      )}
                                    </div>
                                  </div>
                                  <span className="text-xs text-muted-foreground">
                                    {formatTimestamp(comment.timestamp)}
                                  </span>
                                </div>
                              </AccordionTrigger>
                              <AccordionContent className="px-3 pb-4">
                                <p className="text-sm leading-relaxed">{comment.message}</p>
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
                                          {formatTimestamp(report.timestamp)}
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

                    <div className="flex justify-end">
                      <Button
                        variant="outline"
                        size="sm"
                        className="border-destructive text-destructive hover:bg-destructive hover:text-destructive-foreground"
                        onClick={() => handleDelete(confession.id)}
                      >
                        <Trash2 className="mr-2 h-4 w-4" /> Delete confession
                      </Button>
                    </div>
                  </CardContent>
                </Card>
              );
            })
          )}
        </div>
      </div>

      <AlertDialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete this confession?</AlertDialogTitle>
            <AlertDialogDescription>
              This action cannot be undone. The confession will be permanently removed for every user.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={confirmDelete} className="bg-destructive hover:bg-destructive/90">
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
};
