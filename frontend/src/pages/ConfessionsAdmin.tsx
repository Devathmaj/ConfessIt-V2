
import { logger } from '@/lib/logger';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
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
import { getAdminConfessions, hideAdminComment, hideAdminConfession, searchAdminConfessions, unhideAdminComment, unhideAdminConfession, type HybridSemanticStatus } from '@/services/api';
import { toast } from 'sonner';
import { apiErrorStatus } from '@/lib/apiError';
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
  };
  report_count: number;
  reported_by: string[];
  like_count: number;
  dislike_count: number;
  reports: AdminReport[];
  // Set by the server, never derived here, and the same three fields the parent confession
  // carries: a comment hidden from this screen and one taken down by a report ruling produce
  // the same state on the same column, so this is the only thing the screen can key its
  // "already hidden" state off.
  content_removed?: boolean;
  removed_at?: string | null;
  removed_by_regno?: string | null;
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

/** System-hidden marker (removed_by_regno) vs human moderator hides. */
const AZURE_REMOVED_BY = 'azure-content-safety';
const isAzureRemoved = (removedBy: string | null | undefined) => removedBy === AZURE_REMOVED_BY;
const removedByLabel = (removedBy: string | null | undefined) =>
  isAzureRemoved(removedBy) ? 'Azure Content Safety' : removedBy;

/**
 * How many confessions one read returns. Rows here are heavy -- every confession carries
 * its comments, reports and reactor names -- so the page is smaller than the love-note
 * one, and the backend clamps anything larger regardless.
 */
const CONFESSIONS_PAGE_SIZE = 10;

/** The rows inside a `getAdminConfessions` answer, whether it is the page envelope or a legacy bare array. */
const confessionsOf = (answer: unknown): AdminConfession[] => {
  if (Array.isArray(answer)) return answer as AdminConfession[];
  const confessions = (answer as { confessions?: AdminConfession[] } | null | undefined)?.confessions;
  return Array.isArray(confessions) ? confessions : [];
};

/** The matching-row count beside a `getAdminConfessions` answer, falling back to the rows at hand. */
const totalOf = (answer: unknown, fallback: number): number => {
  if (Array.isArray(answer)) return answer.length;
  const total = (answer as { total?: unknown } | null | undefined)?.total;
  return typeof total === 'number' && Number.isFinite(total) ? total : fallback;
};

/** The rows inside one hybrid section (`keyword`/`semantic`), keyed by `items`. */
const itemsOf = (section: unknown): AdminConfession[] => {
  const items = (section as { items?: AdminConfession[] } | null | undefined)?.items;
  return Array.isArray(items) ? items : [];
};

/**
 * One server row into the shape the screen renders, filling every field the review
 * needs so a sparse row cannot break a card further down. Shared by the first page,
 * every appended page and every refresh alike.
 */
const normalizeConfession = (item: Partial<AdminConfession>, index: number): AdminConfession => ({
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
        content_removed: comment.content_removed ?? false,
        removed_at: comment.removed_at ?? null,
        removed_by_regno: comment.removed_by_regno ?? null,
      }))
    : [],
});

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
  /**
   * The confessions on screen: always the contiguous window [0, items.length) of the
   * server order for the current question, so a refresh is one clean re-read and a
   * "show more" click appends beneath what is already read.
   */
  const [items, setItems] = useState<AdminConfession[]>([]);
  /** How many confessions match the current question on the server, loaded or not. */
  const [total, setTotal] = useState(0);
  /**
   * The one unfiltered list, only ever set from legacy bare-array answers (older servers
   * and unit-test mocks), which ignore request params. The real server answers envelopes
   * and leaves this `null`, which is what switches the screen from client filtering to
   * server pages: a bare array re-filtered client-side would hide rows the server
   * correctly returned and break the totals beside them.
   */
  const [legacyRows, setLegacyRows] = useState<AdminConfession[] | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  /** A filter change, order flip, search submit or moderation action re-reading while the list stays up. */
  const [refreshing, setRefreshing] = useState(false);
  /** A next page appending beneath the list, so only its button reads busy. */
  const [loadingMore, setLoadingMore] = useState(false);
  /** Whether the first load is still in flight, which is the only one that takes the loading screen. */
  const isFirstLoad = useRef(true);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [selectedConfessionId, setSelectedConfessionId] = useState<string | null>(null);
  // The comment awaiting confirmation, and whether it lives on a confession that is already
  // loaded. One slot rather than a per-comment `useState`, because the dialog has to name the
  // comment actually being hidden and there is exactly one dialog.
  const [pendingCommentId, setPendingCommentId] = useState<string | null>(null);
  // Restore targets for Azure-hidden rows only; manual hides never offer restore.
  const [pendingRestoreConfessionId, setPendingRestoreConfessionId] = useState<string | null>(null);
  const [pendingRestoreCommentId, setPendingRestoreCommentId] = useState<string | null>(null);
  const [restoring, setRestoring] = useState(false);
  // Set while the comment request is in flight, so a second confirm cannot fire for the same
  // comment before the first has answered. A failed request clears it in `finally`; otherwise
  // one failure would leave the dialog permanently disabled.
  const [hidingComment, setHidingComment] = useState(false);
  const [visibleSenders, setVisibleSenders] = useState<Record<string, boolean>>({});
  // One open reaction at a time, across every row, keyed `confessionId:reaction`. A record would
  // allow several at once, which turns a snapshot into a wall of names; a single slot keeps it
  // scannable and means a reviewer expanding a second one has implicitly closed the first.
  const [expandedReaction, setExpandedReaction] = useState<string | null>(null);
  const [sortOption, setSortOption] = useState<ConfessionSortOption>('reports');
  const [filterOption, setFilterOption] = useState<ConfessionFilterOption>('all');
  /** What the search box currently says. Draft only: nothing is fetched until it is submitted. */
  const [searchTerm, setSearchTerm] = useState('');
  /**
   * The search text the server has actually been asked about.
   *
   * Set only by the Search button (or Enter), never by typing: each keystroke firing a
   * read of confession threads -- every comment, report and reactor row attached -- would
   * be a self-inflicted load test, and the half-typed query it answered would be obsolete
   * before it returned. The box stays a draft until submitted, so the request that goes
   * out is the question the operator meant.
   */
  const [appliedSearch, setAppliedSearch] = useState('');
  /** Semantic half of a hybrid search: top-K similar-in-meaning with its status. */
  const [semanticItems, setSemanticItems] = useState<AdminConfession[]>([]);
  const [semanticStatus, setSemanticStatus] = useState<HybridSemanticStatus>('ok');
  /** True while the review list is answering a hybrid question (submitted text). */
  const hybridActive = legacyRows === null && appliedSearch.trim() !== '';

  const semanticNotice = (status: HybridSemanticStatus): string | null => {
    switch (status) {
      case 'ok':
        return null;
      case 'skipped_short':
        return 'Too short for meaning search — showing keyword matches.';
      case 'disabled':
        return 'Meaning search is disabled — showing keyword matches.';
      case 'rate_limited':
        return 'Meaning search is rate-limited — showing keyword matches. Try again shortly.';
      default:
        return 'Meaning search is temporarily unavailable — showing keyword matches.';
    }
  };

  /**
   * Submit the draft search box as the query the read is issued with, back on the first
   * page: the previous offset may not exist under the new question, and staying on it
   * would show an empty list for a question that has answers.
   */
  const submitSearch = () => {
    setAppliedSearch(searchTerm.trim());
  };

  /** A new filter question or order starts on the first page, for the same reason. */
  const changeFilterOption = (value: ConfessionFilterOption) => {
    setFilterOption(value);
  };

  const changeSortOption = (value: ConfessionSortOption) => {
    setSortOption(value);
  };

  /** Clear the search and the filter together, back on the first page. The sort stays:
   * it orders whatever matches rather than narrowing it. */
  const clearSearchAndFilter = () => {
    setSearchTerm('');
    setAppliedSearch('');
    setFilterOption('all');
    setSemanticItems([]);
    setSemanticStatus('ok');
  };

  /**
   * One read: the next `limit` confessions for the current question. The submitted query
   * is a SQL WHERE across each confession and its sender, the filter picker a SQL WHERE
   * and the sort picker a SQL ORDER BY, so the backend reads at most one page no matter
   * how many confessions exist.
   */
  const readPage = async (
    skip: number,
    limit: number,
    question: { q?: string; filter: ConfessionFilterOption; sort: ConfessionSortOption },
  ): Promise<{ rows: AdminConfession[]; total: number }> => {
    const answer = await getAdminConfessions({
      skip,
      limit,
      ...(question.q ? { q: question.q } : {}),
      ...(question.filter !== 'all' ? { filter: question.filter } : {}),
      sort: question.sort,
    });
    return {
      rows: confessionsOf(answer).map((item, index) => normalizeConfession(item, skip + index)),
      total: totalOf(answer, 0),
    };
  };

  /**
   * One hybrid read: the keyword page plus the top-K semantic section. Falls
   * back to the keyword-only page when the hybrid endpoint is unreachable
   * (older servers), so review never goes blank for lack of meaning search.
   */
  const readHybrid = async (
    skip: number,
    limit: number,
    question: { q: string; filter: ConfessionFilterOption; sort: ConfessionSortOption },
  ): Promise<{ rows: AdminConfession[]; total: number }> => {
    try {
      const answer = await searchAdminConfessions({
        q: question.q,
        filter: question.filter,
        sort: question.sort,
        skip,
        limit,
      });
      if (Array.isArray(answer)) throw new Error('legacy hybrid answer');
      const rows = itemsOf(answer.keyword).map((item, index) =>
        normalizeConfession(item, skip + index),
      );
      setSemanticItems(
        itemsOf(answer.semantic).map((item, index) => normalizeConfession(item, index)),
      );
      setSemanticStatus((answer.semantic as { status?: HybridSemanticStatus })?.status ?? 'unavailable');
      return { rows, total: totalOf(answer.keyword, 0) };
    } catch (error) {
      logger.error('Hybrid search unavailable, falling back to keyword-only', error);
      const page = await readPage(skip, limit, question);
      setSemanticItems([]);
      setSemanticStatus('unavailable');
      return page;
    }
  };

  /**
   * First page, freshly: what a search submit, a filter change, an order flip and the
   * first load all do. A bare-array answer means the params went nowhere, so the one
   * list they all are becomes the legacy rows the screen filters itself below.
   * A submitted search term takes the hybrid endpoint (keyword + meaning).
   */
  useEffect(() => {
    let cancelled = false;
    const fetchConfessions = async () => {
      try {
        if (isFirstLoad.current) setLoading(true);
        else setRefreshing(true);
        const question = {
          q: appliedSearch.trim() || undefined,
          filter: filterOption,
          sort: sortOption,
        };
        if (question.q) {
          const hybrid = await readHybrid(0, CONFESSIONS_PAGE_SIZE, {
            q: question.q,
            filter: question.filter,
            sort: question.sort,
          });
          if (cancelled) return;
          setLegacyRows(null);
          setItems(hybrid.rows);
          setTotal(hybrid.total);
        } else {
          const answer = await getAdminConfessions({
            skip: 0,
            limit: CONFESSIONS_PAGE_SIZE,
            ...(question.filter !== 'all' ? { filter: question.filter } : {}),
            sort: question.sort,
          });
          if (cancelled) return;
          if (Array.isArray(answer)) {
            setLegacyRows(
              confessionsOf(answer).map((item, index) => normalizeConfession(item, index)),
            );
          } else {
            setLegacyRows(null);
            setItems(
              confessionsOf(answer).map((item, index) => normalizeConfession(item, index)),
            );
            setTotal(totalOf(answer, 0));
          }
          setSemanticItems([]);
          setSemanticStatus('ok');
        }
      } catch (error) {
        if (!cancelled) {
          logger.error('Failed to fetch confessions', error);
          toast.error('Unable to load confessions for review.');
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
          setRefreshing(false);
          isFirstLoad.current = false;
        }
      }
    };

    fetchConfessions();
    return () => {
      cancelled = true;
    };
    // readHybrid is re-created each render; listing it would refetch continuously.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appliedSearch, filterOption, sortOption]);

  /** Whether answers arrive as server pages (`null` legacy rows) or one legacy list. */
  const serverDriven = legacyRows === null;

  const processedConfessions = useMemo(() => {
    // Against the server the reads themselves are already searched, filtered and
    // ordered: re-applying any of it here would narrow one database page a second time,
    // hiding rows the server correctly returned and breaking the totals beside them.
    if (serverDriven) return items;
    let entries = [...(legacyRows ?? [])];

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
  }, [legacyRows, serverDriven, items, filterOption, searchTerm, sortOption]);

  /**
   * Re-read the whole window currently on screen, so a moderation action pulls the
   * freshly modified rows back in their new state: hidden flags stamped, comment counts
   * decremented, totals true. The window stays contiguous from zero, which is what keeps
   * a later "show more" a single clean append.
   */
  const refreshWindow = async () => {
    setRefreshing(true);
    try {
      const draft = appliedSearch.trim();
      if (draft) {
        const page = await readHybrid(0, Math.max(items.length, CONFESSIONS_PAGE_SIZE), {
          q: draft,
          filter: filterOption,
          sort: sortOption,
        });
        setItems(page.rows);
        setTotal(page.total);
      } else {
        const question = {
          q: undefined,
          filter: filterOption,
          sort: sortOption,
        };
        const page = await readPage(0, Math.max(items.length, CONFESSIONS_PAGE_SIZE), question);
        setItems(page.rows);
        setTotal(page.total);
      }
    } catch (error) {
      logger.error('Failed to refresh confessions', error);
      toast.error('Unable to refresh the confessions.');
    } finally {
      setRefreshing(false);
    }
  };

  /**
   * Grow the list downwards: the next page appends beneath the confessions already on
   * screen, so the reader's place never moves and the accordion they have open stays
   * open. Shown only while the server holds more than the list is showing.
   */
  const loadMore = async () => {
    if (loadingMore || refreshing) return;
    setLoadingMore(true);
    try {
      const question = {
        q: appliedSearch.trim() || undefined,
        filter: filterOption,
        sort: sortOption,
      };
      const page = await readPage(items.length, CONFESSIONS_PAGE_SIZE, question);
      // The semantic section was computed against the first keyword page: drop
      // appended rows it already shows so the two sections never overlap.
      const seen = new Set(semanticItems.map((item) => item.id));
      setItems((prev) => [...prev, ...page.rows.filter((row) => !seen.has(row.id))]);
      setTotal(page.total);
    } catch (error) {
      logger.error('Failed to load more confessions', error);
      toast.error('Unable to load more confessions.');
    } finally {
      setLoadingMore(false);
    }
  };

  /** Whether the search box, the filter, or both are narrowing the list right now. */
  const activeSearch = serverDriven ? appliedSearch : searchTerm;
  const filtersNarrowed = activeSearch.trim() !== '' || filterOption !== 'all';

  /** Rows while a hybrid search is active: keyword first, notice, meaning below. */
  type ReviewRow =
    | { kind: 'header'; key: string; title: string }
    | { kind: 'notice'; key: string; text: string }
    | { kind: 'review'; key: string; confession: AdminConfession };

  const hybridRows = (): ReviewRow[] => {
    const rows: ReviewRow[] = [];
    if (processedConfessions.length > 0) {
      rows.push({ kind: 'header', key: 'hybrid-keyword', title: 'Keyword matches' });
      for (const confession of processedConfessions) {
        rows.push({ kind: 'review', key: confession.id, confession });
      }
    }
    const notice = semanticNotice(semanticStatus);
    if (notice) {
      rows.push({ kind: 'notice', key: 'hybrid-notice', text: notice });
    }
    if (semanticItems.length > 0) {
      rows.push({ kind: 'header', key: 'hybrid-semantic', title: 'Similar in meaning' });
      for (const confession of semanticItems) {
        rows.push({ kind: 'review', key: confession.id, confession });
      }
    }
    return rows;
  };

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
      if (!serverDriven) {
        // Legacy single list: the move is local, exactly as before pagination existed.
        setLegacyRows((prev) =>
          prev
            ? prev.map((item) =>
                item.id === confessionId
                  ? {
                      ...item,
                      content_removed: true,
                      removed_at: result?.removed_at ?? item.removed_at ?? null,
                      removed_by_regno: result?.removed_by_regno ?? item.removed_by_regno ?? null,
                    }
                  : item
              )
            : prev
        );
      } else {
        // The hiding stamps the row server-side, so the window is re-read to fetch the
        // newly modified row back in its hidden state with true totals beside it.
        await refreshWindow();
      }
      toast.success(
        result?.already_hidden
          ? 'This confession was already hidden.'
          : 'Confession hidden from public view.',
      );
    } catch (error) {
      logger.error('Failed to hide confession', error);
      toast.error('Could not hide the confession.');
    } finally {
      setDialogOpen(false);
      setSelectedConfessionId(null);
    }
  };

  const confirmHideComment = async () => {
    if (!pendingCommentId) return;
    const commentId = pendingCommentId;
    try {
      const result = await hideAdminComment(commentId);
      if (!serverDriven) {
        // Legacy single list: the move is local, exactly as before pagination existed.
        setLegacyRows((prev) =>
          prev
            ? prev.map((item) => {
                if (!item.comments.some((comment) => comment.id === commentId)) return item;
                // The parent's comment count is decremented server-side with the removal, so it is
                // restated here from the same `Math.max` the row already uses.
                const nextTotal = Math.max((item.comment_count ?? 0) - 1, 0);
                return {
                  ...item,
                  comment_count: nextTotal,
                  comments: item.comments.map((comment) =>
                    comment.id === commentId
                      ? {
                          ...comment,
                          content_removed: true,
                          removed_at: result?.removed_at ?? comment.removed_at ?? null,
                          removed_by_regno: result?.removed_by_regno ?? comment.removed_by_regno ?? null,
                        }
                      : comment
                  ),
                };
              })
            : prev
        );
      } else {
        // The hiding stamps the comment and decrements its parent server-side, so the window
        // is re-read to fetch the newly modified rows back with true counts beside them.
        await refreshWindow();
      }
      toast.success(
        result?.already_hidden
          ? 'This comment was already hidden.'
          : 'Comment hidden from the confession.',
      );
    } catch (error) {
      logger.error('Failed to hide comment', error);
      toast.error('Could not hide the comment.');
    } finally {
      setPendingCommentId(null);
      setHidingComment(false);
    }
  };

  const confirmRestoreConfession = async () => {
    if (!pendingRestoreConfessionId) return;
    const confessionId = pendingRestoreConfessionId;
    setRestoring(true);
    try {
      await unhideAdminConfession(confessionId);
      if (!serverDriven) {
        setLegacyRows((prev) =>
          prev
            ? prev.map((item) =>
                item.id === confessionId
                  ? { ...item, content_removed: false, removed_at: null, removed_by_regno: null }
                  : item
              )
            : prev
        );
      } else {
        await refreshWindow();
      }
      toast.success('Confession restored to public view.');
    } catch (error) {
      if (apiErrorStatus(error) === 409) {
        toast.error('This confession was hidden by a moderator and cannot be restored here.');
      } else {
        toast.error('Could not restore the confession.');
      }
    } finally {
      setPendingRestoreConfessionId(null);
      setRestoring(false);
    }
  };

  const confirmRestoreComment = async () => {
    if (!pendingRestoreCommentId) return;
    const commentId = pendingRestoreCommentId;
    setRestoring(true);
    try {
      await unhideAdminComment(commentId);
      if (!serverDriven) {
        setLegacyRows((prev) =>
          prev
            ? prev.map((item) => {
                if (!item.comments.some((comment) => comment.id === commentId)) return item;
                return {
                  ...item,
                  comment_count: (item.comment_count ?? 0) + 1,
                  comments: item.comments.map((comment) =>
                    comment.id === commentId
                      ? { ...comment, content_removed: false, removed_at: null, removed_by_regno: null }
                      : comment
                  ),
                };
              })
            : prev
        );
      } else {
        await refreshWindow();
      }
      toast.success('Comment restored to public view.');
    } catch (error) {
      if (apiErrorStatus(error) === 409) {
        toast.error('This comment was hidden by a moderator and cannot be restored here.');
      } else {
        toast.error('Could not restore the comment.');
      }
    } finally {
      setPendingRestoreCommentId(null);
      setRestoring(false);
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
            Review confessions and comments, hide harmful content, and notify authors. Hiding is irreversible and logged. Policy rulings belong on the Reports page; anonymous posts stay anonymous.
          </p>
          <div className="flex flex-wrap items-center gap-3">
            <p className="text-xs text-muted-foreground/80">
              Total confessions: {serverDriven ? total : processedConfessions.length}
              {refreshing ? ' · Updating…' : ''}
            </p>
            {/* The total is the server count under the current question; the list is the one
                page this screen holds, so the two disagree by design once confessions outgrow
                a page. */}
            <Badge variant="secondary" className="w-max gap-2">
              Showing {processedConfessions.length} of {serverDriven ? total : processedConfessions.length}
            </Badge>
          </div>
        </header>

        <section className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
          {/* The box is a draft; the button sends it. Firing a thread read per keystroke --
              every comment, report and reactor row attached -- would answer a query the
              operator had already retyped, so typing only edits the draft and Search (or
              Enter) issues the one request the database answers. */}
          <div className="flex w-full gap-2 md:max-w-xl">
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
                placeholder="Search by confession text or sender details"
                className="pl-9"
              />
            </div>
            <Button variant="secondary" onClick={submitSearch} disabled={refreshing} aria-label="Search confessions">
              <Search className="mr-2 h-4 w-4" />
              Search
            </Button>
          </div>

          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:gap-4">
            <div className="flex items-center gap-2">
              <Label htmlFor="confession-sort" className="text-xs font-medium uppercase text-muted-foreground">Sort</Label>
              <Select
                value={sortOption}
                onValueChange={(value) => changeSortOption(value as ConfessionSortOption)}
              >
                <SelectTrigger id="confession-sort" className="w-44">
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
              <Label htmlFor="confession-filter" className="text-xs font-medium uppercase text-muted-foreground">Filter</Label>
              <Select
                value={filterOption}
                onValueChange={(value) => changeFilterOption(value as ConfessionFilterOption)}
              >
                <SelectTrigger id="confession-filter" className="w-48">
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

            {/* Only rendered when something is actually narrowing the list, so a control that
                clears nothing is not sitting there implying that something is. */}
            {filtersNarrowed && (
              <Button variant="ghost" size="sm" onClick={clearSearchAndFilter}>
                Clear
              </Button>
            )}
          </div>
        </section>

        <div className="space-y-6">
          {loading ? (
            <Card>
              <CardContent className="py-12 text-center text-sm text-muted-foreground">
                Loading confessions for review...
              </CardContent>
            </Card>
          ) : processedConfessions.length === 0 && (!hybridActive || semanticItems.length === 0) ? (
            <Card>
              <CardContent className="py-12 text-center text-sm text-muted-foreground">
                No confessions match the current filters.
              </CardContent>
            </Card>
          ) : (
            <>
            <Accordion type="single" collapsible className="space-y-3">
              {(hybridActive
                ? hybridRows()
                : processedConfessions.map((confession) => ({
                    kind: 'review' as const,
                    key: confession.id,
                    confession,
                  }))
              ).map((row) => {
                if (row.kind === 'header') {
                  return (
                    <h3 key={row.key} aria-label={row.title} className="pt-2 text-sm font-semibold text-muted-foreground">
                      {row.title}
                    </h3>
                  );
                }
                if (row.kind === 'notice') {
                  return (
                    <p key={row.key} role="status" className="text-xs text-muted-foreground">
                      {row.text}
                    </p>
                  );
                }
                const confession = row.confession;
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
                                          <span className="text-xs">
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
                                {/* A hidden comment is dimmed rather than hidden here, matching
                                    the confession: staff need to see what is already removed in
                                    order to decide on the reports still sitting against it. */}
                                <div className={comment.content_removed ? 'opacity-60' : undefined}>
                                  <p className="text-sm leading-relaxed">{comment.body}</p>
                                </div>
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
                                        <p className="text-xs uppercase tracking-wide opacity-75">
                                          {formatTimestamp(report.created_at)}
                                        </p>
                                        <p className="mt-2 text-sm text-destructive-foreground/90">
                                          {report.reason ?? 'No reason provided.'}
                                        </p>
                                      </div>
                                    ))}
                                  </div>
                                )}
                                <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-border/70 pt-3">
                                  {comment.content_removed ? (
                                    <p className="flex items-center gap-2 text-xs text-muted-foreground">
                                      <EyeOff className="h-3.5 w-3.5" />
                                      {isAzureRemoved(comment.removed_by_regno)
                                        ? 'Content Removed by Azure Content Safety'
                                        : 'Already hidden'}
                                      {comment.removed_at ? ` on ${formatTimestamp(comment.removed_at)}` : ''}
                                      {comment.removed_by_regno && !isAzureRemoved(comment.removed_by_regno)
                                        ? ` by ${comment.removed_by_regno}`
                                        : ''}.
                                    </p>
                                  ) : (
                                    <span />
                                  )}
                                  {!comment.content_removed && (
                                    <Button
                                      variant="outline"
                                      size="sm"
                                      className="border-destructive text-destructive hover:bg-destructive hover:text-destructive-foreground"
                                      onClick={() => setPendingCommentId(comment.id)}
                                    >
                                      <EyeOff className="mr-2 h-3.5 w-3.5" /> Hide comment
                                    </Button>
                                  )}
                                  {comment.content_removed && isAzureRemoved(comment.removed_by_regno) && (
                                    <Button
                                      variant="outline"
                                      size="sm"
                                      onClick={() => setPendingRestoreCommentId(comment.id)}
                                    >
                                      <Eye className="mr-2 h-3.5 w-3.5" /> Restore comment
                                    </Button>
                                  )}
                                </div>
                              </AccordionContent>
                            </AccordionItem>
                          ))}
                        </Accordion>
                      )}
                    </div>

                    <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-end">
                      {confession.content_removed ? (
                        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-end">
                          <p className="flex items-center gap-2 text-sm text-muted-foreground">
                            <EyeOff className="h-4 w-4" />
                            {isAzureRemoved(confession.removed_by_regno)
                              ? 'Content Removed by Azure Content Safety'
                              : 'Already hidden from public view'}
                            {confession.removed_at ? ` on ${formatTimestamp(confession.removed_at)}` : ''}
                            {confession.removed_by_regno && !isAzureRemoved(confession.removed_by_regno)
                              ? ` by ${removedByLabel(confession.removed_by_regno)}`
                              : ''}.
                          </p>
                          {isAzureRemoved(confession.removed_by_regno) ? (
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => setPendingRestoreConfessionId(confession.id)}
                            >
                              <Eye className="mr-2 h-4 w-4" /> Restore confession
                            </Button>
                          ) : (
                            <p className="text-xs text-muted-foreground">
                              Hidden by a moderator and cannot be restored here.
                            </p>
                          )}
                        </div>
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
            {/* The list only grows downwards: the next page appends beneath the confessions
                already read, so the reader's place never moves and the thread they have open
                stays open. Shown only while the server holds more than the list is showing. */}
            {serverDriven && total - processedConfessions.length > 0 && (
              <div className="flex justify-center pt-1">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void loadMore()}
                  disabled={loadingMore || refreshing}
                  aria-label="Show more confessions"
                >
                  {loadingMore
                    ? 'Loading…'
                    : `Show more (${total - processedConfessions.length} remaining)`}
                </Button>
              </div>
            )}
            </>
          )}
        </div>
      </div>

      <AlertDialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Hide this confession?</AlertDialogTitle>
            <AlertDialogDescription>
              This will hide the confession from public view and notify its author. A confession
              hidden here cannot be unhidden. Only content hidden by Azure Content Safety can be
              restored. The confession stays on this screen as a record of the decision.
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

      {/* One dialog for every comment, rendered from `pendingCommentId` so there is exactly one
          place where "a comment is being confirmed for removal" can be true. */}
      <AlertDialog
        open={pendingCommentId !== null}
        onOpenChange={(open) => {
          // Every close route clears the slot, so nothing survives a cancel for the next comment
          // to pick up by mistake.
          if (!open) setPendingCommentId(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Hide this comment?</AlertDialogTitle>
            <AlertDialogDescription>
              This will hide the comment from its confession and notify the person who wrote it.
              The confession itself stays up. A comment hidden here cannot be unhidden. Only
              content hidden by Azure Content Safety can be restored. The comment stays on this
              screen as a record of the decision, and any reports filed against it survive with it.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={hidingComment}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={hidingComment}
              onClick={(event) => {
                // Held open for the duration of the request so a second confirm cannot be
                // clicked mid-flight; `confirmHideComment` clears both flags when it settles.
                event.preventDefault();
                if (!pendingCommentId) return;
                setHidingComment(true);
                void confirmHideComment();
              }}
              className="bg-destructive hover:bg-destructive/90"
            >
              {hidingComment ? 'Working...' : 'Hide comment'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={pendingRestoreConfessionId !== null}
        onOpenChange={(open) => {
          if (!open) setPendingRestoreConfessionId(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Restore this confession?</AlertDialogTitle>
            <AlertDialogDescription>
              Users will now be able to see this confession.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={restoring}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={restoring}
              onClick={(event) => {
                event.preventDefault();
                void confirmRestoreConfession();
              }}
            >
              {restoring ? 'Working...' : 'Restore confession'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={pendingRestoreCommentId !== null}
        onOpenChange={(open) => {
          if (!open) setPendingRestoreCommentId(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Restore this comment?</AlertDialogTitle>
            <AlertDialogDescription>
              Users will now be able to see this comment.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={restoring}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={restoring}
              onClick={(event) => {
                event.preventDefault();
                void confirmRestoreComment();
              }}
            >
              {restoring ? 'Working...' : 'Restore comment'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
};
