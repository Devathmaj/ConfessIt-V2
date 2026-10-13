import { useState, useEffect, useRef } from 'react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Textarea } from '@/components/ui/textarea';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import { CountdownTimer } from '@/components/ui/countdown-timer';
import { FloatingHearts } from '@/components/ui/floating-hearts';
import { FloralDecor } from '@/components/FloralDecor';
import { ScatteredIcons } from '@/components/ScatteredIcons';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog';
import { AlertDialog, AlertDialogAction, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { 
  Heart, 
  Laugh, 
  Eye, 
  HeartCrack, 
  ArrowLeft, 
  TrendingUp, 
  Clock, 
  MessageSquare, 
  Send,
  Plus,
  ThumbsUp,
  ThumbsDown,
  Flag,
  Edit,
  Loader2,
} from 'lucide-react';
import { toast } from 'sonner';
import { getConfessions, getConfessionComments, createConfession, getConfessionCooldown, reactToConfession, createComment, likeComment, dislikeComment, reportComment, reportConfession, updateConfession, searchConfessions } from '../services/api';
import { useAuth } from '../contexts/AuthContext';
import { Navigation } from '@/components/Navigation';
import { ConfessionBody } from '@/components/ConfessionBody';
import { ProfileAvatar } from '@/components/ProfileAvatar';
import { formatDateTimeDDMMYYYY } from '@/lib/utils';
import { describeApiError } from '@/lib/apiError';
import {
  formatCooldownRemaining,
  readCooldownRejection,
  readConfessionCooldown,
  type ConfessionCooldown,
} from '@/lib/confessionCooldown';
import { serverNow } from '@/lib/serverClock';
import {
  COMMENT_BODY_MAX,
  CONFESSION_BODY_MAX,
  RECIPIENT_NAME_MAX,
  REPORT_REASON_MAX,
  containsUnstorableCharacter,
} from '@/lib/limits';

// Interface for user information
interface UserInfo {
  id: string;
  name: string;
  /** Author's photo; the API withholds it on an anonymous confession. */
  profile_picture_url?: string | null;
  user_reaction: 'like' | 'dislike' | null;
  report_count: number;
  reported_by: string[];
}

interface ConfessionComment {
  id: string;
  confession_id?: string;
  user_info: UserInfo;
  body: string;
  created_at: string;
  like_count: number;
  dislike_count: number;
  user_reaction: 'like' | 'dislike' | null;
  report_count: number;
  reported_by: string[];
  /** Whether the current viewer reported this comment (SEC-002; preferred over reported_by). */
  has_reported?: boolean;
}

interface HasReported {
  has_reported?: boolean;
  reported_by?: string[];
}

// Interface for a single confession post
interface Confession {
  id: string;
  body: string;
  recipient_name?: string | null;
  anonymous: boolean;
  comments_enabled: boolean;
  /** Server-computed for this viewer; no author id is needed to show ownership controls. */
  is_mine: boolean;
  user_info: UserInfo;
  created_at: string;
  comments: ConfessionComment[];
  heart_count: number;
  haha_count: number;
  whoa_count: number;
  heartbreak_count: number;
  comment_count: number;
  user_reaction: string | null;
  report_count: number;
  reported_by: string[];
  /** Whether the current viewer reported this confession (SEC-002; preferred over reported_by). */
  has_reported?: boolean;
}

/**
 * Whether the current user already reported this item (SEC-002).
 *
 * Prefers the server's per-viewer `has_reported` boolean, which discloses nothing
 * about other reporters. Falls back to the legacy `reported_by` membership check
 * for responses from servers that still send it.
 */
const hasReportedByMe = (item: HasReported, userId?: string | null): boolean => {
  if (typeof item.has_reported === 'boolean') return item.has_reported;
  if (!userId || !Array.isArray(item.reported_by)) return false;
  return item.reported_by.includes(userId);
};

/**
 * A commenter's or non-anonymous author's avatar: their photo, or their first initial when
 * they have set none. Callers must not pass the user_info of an anonymous confession:
 * hiding that identity is the whole point of the anonymous flag.
 */
const UserAvatar = ({ info, className = 'w-10 h-10' }: { info?: Pick<UserInfo, 'name' | 'profile_picture_url'> | null; className?: string }) => (
  <ProfileAvatar src={info?.profile_picture_url} name={info?.name} className={className} />
);

/**
 * How many confessions one feed read returns. Cards carry their threads, so ten keeps
 * one read cheap while a "show more" click still grows the feed by a visible amount.
 * The backend clamps anything larger regardless.
 */
const FEED_PAGE_SIZE = 10;

/**
 * How many comments one thread read returns: the opening window the feed embeds, and
 * every "show more" page inside the dialog after it.
 */
const COMMENTS_PAGE_SIZE = 10;

/**
 * Client-side relevance order for legacy bare-array answers, mirroring the server's:
 * share of likes among all votes first, then more likes, then newest. Only read where
 * no server paging exists to order with; the paged dialog never reorders a page.
 */
const compareRelevant = (
  a: { like_count: number; dislike_count: number; created_at: string },
  b: { like_count: number; dislike_count: number; created_at: string },
): number => {
  const share = (row: typeof a) =>
    row.like_count + row.dislike_count === 0
      ? 0
      : row.like_count / (row.like_count + row.dislike_count);
  return (
    share(b) - share(a) ||
    b.like_count - a.like_count ||
    (b.created_at > a.created_at ? 1 : b.created_at < a.created_at ? -1 : 0)
  );
};

export const ConfessionsPage = () => {
  /**
   * The posts on screen: always the contiguous window [0, confessions.length) of the
   * server order for the current sort, so a refresh is one clean re-read and a "show
   * more" click appends beneath the posts already read.
   */
  const [confessions, setConfessions] = useState<Confession[]>([]);
  /** How many visible confessions the server holds, loaded or not. */
  const [total, setTotal] = useState(0);
  /** False while answers arrive as legacy bare arrays (older servers, unit-test mocks). */
  const [pagedFeed, setPagedFeed] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadingMoreComments, setLoadingMoreComments] = useState(false);
  /**
   * How the open thread is ordered: newest first, or most approved first. Reset every
   * time a dialog opens, so each thread starts where the feed left it -- the embedded
   * window is always latest-first -- and an explicit tap is what re-ranks it.
   */
  const [commentSort, setCommentSort] = useState<'latest' | 'relevant'>('latest');
  const [sortBy, setSortBy] = useState<'popularity' | 'time' | 'comments'>('time');
  /** Hybrid search state: one box, keyword matches first, meaning below. */
  const [searchInput, setSearchInput] = useState('');
  const [activeSearch, setActiveSearch] = useState<string | null>(null);
  const [isSearching, setIsSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [keywordResults, setKeywordResults] = useState<Confession[]>([]);
  const [keywordTotal, setKeywordTotal] = useState(0);
  const [semanticResults, setSemanticResults] = useState<Confession[]>([]);
  const [semanticStatus, setSemanticStatus] = useState<string>('ok');
  /** Debounce timer, in-flight request id and aborter: typing never fires a
   * request per keystroke, and a slow answer can never overwrite a newer one. */
  const searchDebounce = useRef<number | null>(null);
  const searchRequestId = useRef(0);
  const searchAbort = useRef<AbortController | null>(null);
  const [newConfession, setNewConfession] = useState('');
  const [confessingTo, setConfessingTo] = useState('');
  const [isAnonymous, setIsAnonymous] = useState(true);
  const [allowComments, setAllowComments] = useState(true);
  const [isPostDialogOpen, setIsPostDialogOpen] = useState(false);
  /**
   * True while a share is in flight. Content moderation and embedding run inside the
   * request, so the round-trip is slow enough to be double-clicked -- and a second click
   * would spend a second two-hour cooldown window on a duplicate post.
   */
  const [isSubmittingConfession, setIsSubmittingConfession] = useState(false);
  const [violationDialog, setViolationDialog] = useState<{ open: boolean; message: string }>({ open: false, message: '' });
  const { user, isAuthenticated } = useAuth();

  const readViolationMessage = (error: any): string | null => {
    const detail = error?.response?.data?.detail;
    const text = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.join(' ') : '';
    if (error?.response?.status === 422 && /violat.*polic/i.test(text)) return text;
    return null;
  };

  /**
   * The wait on posting, as the server described it, plus when that wait is over.
   *
   * Stored as an absolute deadline rather than a countdown that decrements itself, so a tab
   * left in the background for ten minutes does not come back to a timer that thinks only
   * one second passed, and so the number shown is derived from `serverNow()` instead of from
   * a device clock the user can change.
   */
  const [cooldown, setCooldown] = useState<ConfessionCooldown | null>(null);
  const [cooldownEndsAt, setCooldownEndsAt] = useState<number | null>(null);
  const [cooldownTick, setCooldownTick] = useState(() => serverNow());

  const startCooldown = (next: ConfessionCooldown) => {
    setCooldown(next);
    setCooldownEndsAt(serverNow() + next.seconds * 1000);
    setCooldownTick(serverNow());
  };

  const clearCooldown = () => {
    setCooldown(null);
    setCooldownEndsAt(null);
  };

  const refreshCooldown = async () => {
    if (!isAuthenticated) {
      clearCooldown();
      return;
    }
    try {
      const next = readConfessionCooldown(await getConfessionCooldown());
      if (next) startCooldown(next);
      else clearCooldown();
    } catch {
      // Advisory only; the write enforces it regardless. Someone who sees no notice because
      // this call failed gets the same explanation from the 429 instead.
      clearCooldown();
    }
  };

  const secondsUntilEligible = cooldownEndsAt
    ? Math.max(0, Math.ceil((cooldownEndsAt - cooldownTick) / 1000))
    : 0;
  const isOnCooldown = secondsUntilEligible > 0;

  // One tick drives the whole notice. The deadline is in server time, so this is the only
  // thing re-rendering, and it stops as soon as there is no wait left.
  useEffect(() => {
    if (!isOnCooldown) return;
    setCooldownTick(serverNow());
    const timer = window.setInterval(() => setCooldownTick(serverNow()), 1000);
    return () => window.clearInterval(timer);
  }, [isOnCooldown, cooldownEndsAt]);

  useEffect(() => {
    refreshCooldown();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAuthenticated]);

  const [selectedConfession, setSelectedConfession] = useState<Confession | null>(null);
  const [newComment, setNewComment] = useState('');
  /** True while a comment is in flight (content moderation runs inside the request). */
  const [isPostingComment, setIsPostingComment] = useState(false);
  
  const [isReportConfirmOpen, setIsReportConfirmOpen] = useState(false);
  const [isReportReasonOpen, setIsReportReasonOpen] = useState(false);
  const [commentToReport, setCommentToReport] = useState<ConfessionComment | null>(null);
  const [reportReason, setReportReason] = useState("");

  const [confessionToReport, setConfessionToReport] = useState<Confession | null>(null);
  const [isConfessionReportConfirmOpen, setIsConfessionReportConfirmOpen] = useState(false);
  const [isConfessionReportReasonOpen, setIsConfessionReportReasonOpen] = useState(false);

  // State for the edit dialog
  const [confessionToEdit, setConfessionToEdit] = useState<Confession | null>(null);
  const [editIsAnonymous, setEditIsAnonymous] = useState(false);
  const [editAllowComments, setEditAllowComments] = useState(false);

  /** Split one feed answer into its rows, total and answer shape. Shared by every read. */
  const splitPage = (answer: unknown) => {
    // A legacy bare array ignores the params and hands back one unfiltered list; an
    // envelope answers the page that was asked for. Telling them apart keeps the old
    // servers and the unit-test mocks on the single-list behaviour they were written for.
    if (Array.isArray(answer)) {
      const rows = answer as Confession[];
      return { rows, total: rows.length, enveloped: false };
    }
    const page = answer as { confessions?: Confession[]; total?: unknown };
    const rows = Array.isArray(page.confessions) ? page.confessions : [];
    return {
      rows,
      total:
        typeof page.total === 'number' && Number.isFinite(page.total) ? page.total : rows.length,
      enveloped: true,
    };
  };

  useEffect(() => {
    // While a semantic search is active the feed is replaced by ranked results;
    // sort changes and reloads apply to the feed only after Clear.
    if (activeSearch !== null) return;
    let cancelled = false;
    const fetchConfessions = async () => {
      try {
        const page = splitPage(await getConfessions(sortBy, { skip: 0, limit: FEED_PAGE_SIZE }));
        if (cancelled) return;
        setConfessions(page.rows);
        setTotal(page.total);
        setPagedFeed(page.enveloped);
      } catch (error) {
        if (!cancelled) {
          toast.error("Failed to load confessions. Please try again.");
          console.error(error);
        }
      }
    };
    fetchConfessions();
    return () => {
      cancelled = true;
    };
  }, [sortBy, isAuthenticated, activeSearch]);

  const readSearchError = (error: any): string => {
    const status = error?.response?.status;
    const detail = error?.response?.data?.detail;
    const text = typeof detail === 'string' && detail.trim() ? detail : '';
    if (status === 422) {
      return text || 'Please enter a search phrase up to 1000 characters.';
    }
    if (status === 429) {
      return text || 'Too many search requests. Please wait a moment and try again.';
    }
    return text || 'Search failed. Please try again.';
  };

  /** The non-blocking note under keyword results when the meaning half degrades. */
  const semanticNotice = (status: string): string | null => {
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

  const runHybridSearch = async (query: string) => {
    const id = ++searchRequestId.current;
    searchAbort.current?.abort();
    const controller = new AbortController();
    searchAbort.current = controller;
    setIsSearching(true);
    setSearchError(null);
    try {
      const result = await searchConfessions(query, FEED_PAGE_SIZE, { signal: controller.signal });
      // A slow answer must never overwrite a newer one.
      if (searchRequestId.current !== id) return;
      setKeywordResults(result.keyword?.items ?? []);
      setKeywordTotal(result.keyword?.total ?? 0);
      setSemanticResults(result.semantic?.items ?? []);
      setSemanticStatus(result.semantic?.status ?? 'unavailable');
      setActiveSearch(result.query || query);
    } catch (error: any) {
      if (controller.signal.aborted || searchRequestId.current !== id) return;
      const message = readSearchError(error);
      setSearchError(message);
      toast.error(message);
      console.error(error);
    } finally {
      if (searchRequestId.current === id) setIsSearching(false);
    }
  };

  const handleSemanticSearch = () => {
    if (searchDebounce.current) {
      window.clearTimeout(searchDebounce.current);
      searchDebounce.current = null;
    }
    const query = searchInput.trim();
    if (!query) {
      toast.info('Type a few words to search confessions.');
      return;
    }
    void runHybridSearch(query);
  };

  /** Type-ahead: debounced so each keystroke does not fire a heavy read. */
  const queueDebouncedSearch = (value: string) => {
    setSearchInput(value);
    if (searchDebounce.current) {
      window.clearTimeout(searchDebounce.current);
      searchDebounce.current = null;
    }
    const query = value.trim();
    if (!query) return;
    searchDebounce.current = window.setTimeout(() => {
      searchDebounce.current = null;
      void runHybridSearch(query);
    }, 350);
  };

  // Drop timers and in-flight searches on unmount so no late answer touches state.
  useEffect(() => {
    return () => {
      if (searchDebounce.current) window.clearTimeout(searchDebounce.current);
      searchAbort.current?.abort();
    };
  }, []);

  const clearHybridState = () => {
    searchRequestId.current += 1;
    searchAbort.current?.abort();
    if (searchDebounce.current) {
      window.clearTimeout(searchDebounce.current);
      searchDebounce.current = null;
    }
    setKeywordResults([]);
    setKeywordTotal(0);
    setSemanticResults([]);
    setSemanticStatus('ok');
  };

  const handleClearSearch = async () => {
    setSearchInput('');
    setActiveSearch(null);
    setSearchError(null);
    clearHybridState();
    try {
      const page = splitPage(await getConfessions(sortBy, { skip: 0, limit: FEED_PAGE_SIZE }));
      setConfessions(page.rows);
      setTotal(page.total);
      setPagedFeed(page.enveloped);
    } catch (error) {
      toast.error('Failed to load confessions. Please try again.');
      console.error(error);
    }
  };

  /** Patch one confession everywhere it is rendered: feed and both search lists. */
  const patchConfessionEverywhere = (id: string, patch: (conf: Confession) => Confession) => {
    setConfessions((prev) => prev.map((conf) => (conf.id === id ? patch(conf) : conf)));
    setKeywordResults((prev) => prev.map((conf) => (conf.id === id ? patch(conf) : conf)));
    setSemanticResults((prev) => prev.map((conf) => (conf.id === id ? patch(conf) : conf)));
  };

  /** Snapshot all three lists so an optimistic update can roll back. */
  const snapshotEverywhere = () => ({
    feed: [...confessions],
    keyword: [...keywordResults],
    semantic: [...semanticResults],
  });

  const restoreEverywhere = (snap: { feed: Confession[]; keyword: Confession[]; semantic: Confession[] }) => {
    setConfessions(snap.feed);
    setKeywordResults(snap.keyword);
    setSemanticResults(snap.semantic);
  };

  /**
   * Grow the feed downwards: the next page appends beneath the posts already on screen,
   * so the reader's place never moves. Shown only while the server holds more than the
   * feed is showing; legacy answers never page.
   */
  const loadMore = async () => {
    if (loadingMore) return;
    setLoadingMore(true);
    try {
      const page = splitPage(
        await getConfessions(sortBy, { skip: confessions.length, limit: FEED_PAGE_SIZE }),
      );
      setConfessions((prev) => [...prev, ...page.rows]);
      setTotal(page.total);
      setPagedFeed(page.enveloped);
    } catch (error) {
      toast.error("Failed to load more confessions. Please try again.");
      console.error(error);
    } finally {
      setLoadingMore(false);
    }
  };

  /** Re-read the first page, where a brand-new post belongs under a time order. */
  const refreshFirstPage = async () => {
    const page = splitPage(await getConfessions(sortBy, { skip: 0, limit: FEED_PAGE_SIZE }));
    setConfessions(page.rows);
    setTotal(page.total);
    setPagedFeed(page.enveloped);
  };

  /**
   * Grow the open thread downwards: the next comments page appends beneath the comments
   * already read, in the dialog and in the feed row behind it alike, so closing and
   * reopening the dialog does not lose the appended page.
   */
  const loadMoreComments = async () => {
    if (!selectedConfession || loadingMoreComments) return;
    const confessionId = selectedConfession.id;
    const sort = commentSort;
    setLoadingMoreComments(true);
    try {
      const answer = await getConfessionComments(confessionId, {
        skip: selectedConfession.comments.length,
        limit: COMMENTS_PAGE_SIZE,
        sort,
      });
      const rows = (
        Array.isArray(answer as unknown) ? (answer as unknown as ConfessionComment[]) : answer.comments
      );
      setSelectedConfession((prev) =>
        prev && prev.id === confessionId ? { ...prev, comments: [...prev.comments, ...rows] } : prev,
      );
      setConfessions((prev) =>
        prev.map((conf) =>
          conf.id === confessionId ? { ...conf, comments: [...conf.comments, ...rows] } : conf,
        ),
      );
    } catch (error) {
      toast.error("Failed to load more comments. Please try again.");
      console.error(error);
    } finally {
      setLoadingMoreComments(false);
    }
  };

  const handleCommentClick = (confession: Confession) => {
    setCommentSort('latest');
    setSelectedConfession(confession);
  };

  /**
   * Re-rank the open thread: page one in the new order, replacing dialog and feed-row
   * copies alike so closing and reopening shows the same order. A sorter that only
   * reordered the loaded window would strand rows the new order puts first.
   */
  const changeCommentSort = async (sort: 'latest' | 'relevant') => {
    if (!selectedConfession || sort === commentSort) return;
    const id = selectedConfession.id;
    setCommentSort(sort);
    try {
      const answer = await getConfessionComments(id, {
        skip: 0,
        limit: Math.max(selectedConfession.comments.length, COMMENTS_PAGE_SIZE),
        sort,
      });
      const fetched = (
        Array.isArray(answer as unknown) ? (answer as unknown as ConfessionComment[]) : answer.comments
      );
      // Legacy answers ignore the params and hand back one unordered list, so the toggle
      // orders it here; server pages arrive in the order that was asked for.
      const rows = !pagedFeed && sort === 'relevant' ? [...fetched].sort(compareRelevant) : fetched;
      setSelectedConfession((prev) =>
        prev && prev.id === id ? { ...prev, comments: rows } : prev,
      );
      setConfessions((prev) =>
        prev.map((conf) => (conf.id === id ? { ...conf, comments: rows } : conf)),
      );
    } catch (error) {
      toast.error("Failed to reorder comments. Please try again.");
      console.error(error);
    }
  };

  const handlePostComment = async (confessionId: string) => {
    if (!newComment.trim()) {
      toast.info("Please write a comment first.");
      return;
    }
    if (!isAuthenticated || !user) {
      toast.error("You must be logged in to comment.");
      return;
    }
    // A second click while the first request is open would post the reply twice; the
    // server's moderation round-trip is what makes the window wide enough to hit.
    if (isPostingComment) return;

    setIsPostingComment(true);
    try {
      const newCommentData = await createComment(confessionId, newComment);

      if (commentSort === 'relevant' && pagedFeed) {
        // Newest-first no longer describes this thread: the reply belongs at its ranked
        // position, so the window is re-read instead of prepended -- prepending would put
        // it first under every order and skew the next offset. The count comes back with
        // the window because the write already incremented it server-side.
        const answer = await getConfessionComments(confessionId, {
          skip: 0,
          limit: Math.max(selectedConfession?.comments.length ?? 0, COMMENTS_PAGE_SIZE),
          sort: commentSort,
        });
        const rows = (
          Array.isArray(answer as unknown) ? (answer as unknown as ConfessionComment[]) : answer.comments
        );
        const total = Array.isArray(answer as unknown)
          ? undefined
          : (answer as { total?: unknown }).total;
        const applyCount = (prev: number) =>
          typeof total === 'number' && Number.isFinite(total) ? total : prev + 1;
        setSelectedConfession((prev) =>
          prev && prev.id === confessionId
            ? { ...prev, comments: rows, comment_count: applyCount(prev.comment_count) }
            : prev,
        );
        patchConfessionEverywhere(
          confessionId,
          (conf) => ({ ...conf, comments: rows, comment_count: applyCount(conf.comment_count) }),
        );
      } else {
        const appendComment = (conf: Confession) =>
          conf.id === confessionId
            ? { ...conf, comments: [newCommentData, ...conf.comments], comment_count: conf.comment_count + 1 }
            : conf;
        patchConfessionEverywhere(confessionId, appendComment);

        const updatedSelectedConfession =
          keywordResults.find((c) => c.id === confessionId) ??
          semanticResults.find((c) => c.id === confessionId) ??
          confessions.find((c) => c.id === confessionId);
        if (updatedSelectedConfession) {
          setSelectedConfession(appendComment(updatedSelectedConfession));
        }
      }

      setNewComment('');
      toast.success("Your comment has been posted!");
    } catch (error: any) {
      const violation = readViolationMessage(error);
      if (violation) {
        // Flagged+hidden: never render the returned row (there is none on
        // error) and never prepend. Clear the box since the attempt consumed
        // one of the 3 comment slots server-side.
        setNewComment('');
        setViolationDialog({ open: true, message: violation });
        return;
      }
      if (error.response && error.response.status === 403) {
        toast.error("Comments are disabled for this confession.");
      } else if (error.response && error.response.status === 400) {
        toast.error("You have reached your comment limit for this post (3 comments).");
      } else {
        toast.error("Failed to post comment. Please try again.");
      }
    } finally {
      setIsPostingComment(false);
    }
  };

  const handleReaction = async (confessionId: string, reactionType: string) => {
    if (!isAuthenticated) {
      toast.error("You must be logged in to react.");
      return;
    }
    
    const snapshot = snapshotEverywhere();

    patchConfessionEverywhere(confessionId, (conf) => {
      const newConf = { ...conf };
      const isTogglingOff = newConf.user_reaction === reactionType;

      if (newConf.user_reaction) {
        const oldCountKey = `${newConf.user_reaction}_count` as keyof Confession;
        (newConf[oldCountKey] as number)--;
      }

      if (!isTogglingOff) {
        const newCountKey = `${reactionType}_count` as keyof Confession;
        (newConf[newCountKey] as number)++;
      }

      newConf.user_reaction = isTogglingOff ? null : reactionType;
      return newConf;
    });

    try {
      const updatedConfession = await reactToConfession(confessionId, reactionType);
      patchConfessionEverywhere(confessionId, (c) => ({ ...c, ...updatedConfession }));
    } catch (error) {
      toast.error("Failed to save reaction.");
      restoreEverywhere(snapshot);
    }
  };

  const handleCommentReaction = async (confessionId: string, commentId: string, reaction: 'like' | 'dislike') => {
    if (!isAuthenticated) {
      toast.error("You must be logged in to react to comments.");
      return;
    }

    const snapshot = snapshotEverywhere();

    const updateReactionState = (conf: Confession) => {
      if (conf.id !== confessionId) return conf;

      return {
        ...conf,
        comments: conf.comments.map(c => {
          if (c.id !== commentId) return c;

          const currentReaction = c.user_reaction;
          const isTogglingOff = currentReaction === reaction;

          let newLikeCount = c.like_count;
          let newDislikeCount = c.dislike_count;

          if (currentReaction === 'like') newLikeCount--;
          if (currentReaction === 'dislike') newDislikeCount--;

          if (!isTogglingOff) {
            if (reaction === 'like') newLikeCount++;
            if (reaction === 'dislike') newDislikeCount++;
          }

          return {
            ...c,
            like_count: newLikeCount,
            dislike_count: newDislikeCount,
            user_reaction: isTogglingOff ? null : reaction,
          };
        }),
      };
    };

    patchConfessionEverywhere(confessionId, updateReactionState);
    const optimisticConfession =
      keywordResults.find((c) => c.id === confessionId) ??
      semanticResults.find((c) => c.id === confessionId) ??
      confessions.find((c) => c.id === confessionId);
    if (optimisticConfession) {
        setSelectedConfession(updateReactionState(optimisticConfession));
    }


    try {
      const apiCall = reaction === 'like' ? likeComment : dislikeComment;
      const updatedCommentFromServer = await apiCall(commentId);

      const syncWithServer = (conf: Confession) => {
        if (conf.id !== confessionId) return conf;

        return {
          ...conf,
          comments: conf.comments.map(c =>
            c.id === commentId ? { ...c, ...updatedCommentFromServer } : c
          ),
        };
      };

      patchConfessionEverywhere(confessionId, syncWithServer);
      const serverSyncedConfession =
        keywordResults.find((c) => c.id === confessionId) ??
        semanticResults.find((c) => c.id === confessionId) ??
        confessions.find((c) => c.id === confessionId);
      if (serverSyncedConfession) {
          setSelectedConfession(syncWithServer(serverSyncedConfession));
      }

    } catch (error) {
      toast.error(`Failed to ${reaction} comment.`);
      restoreEverywhere(snapshot);
      const originalSelected = snapshot.feed.find((c: Confession) => c.id === confessionId);
      setSelectedConfession(originalSelected || null);
    }
  };


  const handleSubmitConfession = async () => {
    if (!newConfession.trim()) {
      toast.error('Please write your confession first! 💌');
      return;
    }
    // Checked here as well as on the server so the message lands while the text is still
    // on screen. These characters usually arrive by pasting from a terminal or a document
    // with hidden formatting, and a 422 with no explanation reads as the app being broken.
    if (containsUnstorableCharacter(newConfession)) {
      toast.error('Your confession contains a character that cannot be saved. Try pasting it as plain text.');
      return;
    }
    if (!isAuthenticated) {
      toast.error('You must be logged in to post a confession.');
      return;
    }
    // Checked before the request as well as after, so the wait is shown with the text still
    // on screen instead of the user typing a confession only to be refused.
    if (isOnCooldown) {
      toast.error(cooldown?.message ?? 'You can post one confession every 2 hours.');
      return;
    }
    // One click, one confession. A second click while the first request is open would
    // spend a second cooldown window on a duplicate post; without the finally reset below
    // it would also leave the button dead after a failure.
    if (isSubmittingConfession) return;

    setIsSubmittingConfession(true);
    try {
      const confessionData = {
        body: newConfession,
        // Sent as null rather than "" when left blank: a confession does not have to be
        // about anyone, and the server stores an absent recipient as NULL.
        recipient_name: confessingTo.trim() || null,
        anonymous: isAnonymous,
        comments_enabled: allowComments,
      };
      await createConfession(confessionData);
      // A new post clears an active search: the ranked lists no longer
      // describe the corpus, and the new post belongs in the feed order.
      setActiveSearch(null);
      setSearchError(null);
      clearHybridState();
      // The new post belongs at the head of a time-ordered feed but anywhere in the
      // others, so the first page is re-read instead of prepending: prepending would put
      // it first under every sort and grow the window past its page.
      await refreshFirstPage();
      setNewConfession('');
      setConfessingTo('');
      setIsPostDialogOpen(false);
      // A successful post starts this account's own two hours, so the notice appears
      // immediately rather than on the next page load.
      startCooldown({
        message: 'You can post one confession every 2 hours.',
        hours: 2,
        minutes: 0,
        seconds: 2 * 60 * 60,
      });
      toast.success('Your confession has been shared! 💖');
    } catch (error) {
      // Flagged+hidden: the server kept the row hidden for audit but answers
      // 422 so we show a violation popup and never render it (no refresh with
      // the hidden row, no success toast). The 2h window was still consumed.
      const violation = readViolationMessage(error);
      if (violation) {
        setActiveSearch(null);
        setSearchError(null);
        clearHybridState();
        setNewConfession('');
        setConfessingTo('');
        setIsPostDialogOpen(false);
        startCooldown({
          message: 'You can post one confession every 2 hours.',
          hours: 2,
          minutes: 0,
          seconds: 2 * 60 * 60,
        });
        setViolationDialog({ open: true, message: violation });
        return;
      }
      // The server is the authority: this dialog can have been open for an hour, and the
      // write is what refuses. A 429 therefore starts the countdown from the server's own
      // Retry-After rather than from the stale advisory answer this page loaded with.
      const rejection = readCooldownRejection(error);
      if (rejection) {
        startCooldown(rejection);
        toast.error(rejection.message);
      } else {
        toast.error('Failed to share confession. Please try again.');
      }
    } finally {
      setIsSubmittingConfession(false);
    }
  };

  const handleReportClick = (comment: ConfessionComment) => {
    if (!isAuthenticated) {
      toast.error("You must be logged in to report a comment.");
      return;
    }
    setCommentToReport(comment);
    setIsReportConfirmOpen(true);
  };

  const handleConfirmReport = () => {
    setIsReportConfirmOpen(false);
    setIsReportReasonOpen(true);
  };

  const handleReportSubmit = async () => {
    if (!reportReason.trim()) {
      toast.info("Please provide a reason for the report.");
      return;
    }
    if (!commentToReport || !user) return;

    try {
      const updatedComment = await reportComment(commentToReport.id, reportReason);
      
      const updateCommentInState = (conf: Confession) => {
        if (conf.id === selectedConfession?.id) {
          return {
            ...conf,
            comments: conf.comments.map(c => c.id === commentToReport.id ? updatedComment : c)
          };
        }
        return conf;
      };

      patchConfessionEverywhere(selectedConfession?.id ?? '', updateCommentInState);
      if (selectedConfession) {
        setSelectedConfession(prev => prev ? {
            ...prev,
            comments: prev.comments.map(c => c.id === commentToReport.id ? updatedComment : c)
        } : null);
      }

      toast.success("Comment reported successfully.");
    } catch (error) {
      toast.error(describeApiError(error, "Failed to report comment. You may have already reported it."));
    } finally {
      setIsReportReasonOpen(false);
      setReportReason("");
      setCommentToReport(null);
    }
  };

  const handleReportConfessionClick = (confession: Confession) => {
    if (!isAuthenticated) {
        toast.error("You must be logged in to report a confession.");
        return;
    }
    setConfessionToReport(confession);
    setIsConfessionReportConfirmOpen(true);
  };

  const handleConfirmConfessionReport = () => {
      setIsConfessionReportConfirmOpen(false);
      setIsConfessionReportReasonOpen(true);
  };

  const handleConfessionReportSubmit = async () => {
      if (!reportReason.trim()) {
          toast.info("Please provide a reason for the report.");
          return;
      }
      if (!confessionToReport || !user) return;

      try {
          const updatedConfession = await reportConfession(confessionToReport.id, reportReason);

          patchConfessionEverywhere(confessionToReport.id, () => updatedConfession);

          toast.success("Confession reported successfully.");
      } catch (error) {
          toast.error(describeApiError(error, "Failed to report confession. You may have already reported it."));
      } finally {
          setIsConfessionReportReasonOpen(false);
          setReportReason("");
          setConfessionToReport(null);
      }
  };

  const handleEditClick = (confession: Confession) => {
    setConfessionToEdit(confession);
    setEditIsAnonymous(confession.anonymous);
    setEditAllowComments(confession.comments_enabled);
  };

  const handleSaveChanges = async () => {
    if (!confessionToEdit) return;

    const payload: { anonymous?: boolean; comments_enabled?: boolean } = {};

    if (editAllowComments !== confessionToEdit.comments_enabled) {
      payload.comments_enabled = editAllowComments;
    }
    if (editIsAnonymous !== confessionToEdit.anonymous) {
      payload.anonymous = editIsAnonymous;
    }

    if (Object.keys(payload).length === 0) {
      toast.info("No changes were made.");
      setConfessionToEdit(null);
      return;
    }

    try {
      const updatedConfession = await updateConfession(confessionToEdit.id, payload);

      patchConfessionEverywhere(confessionToEdit.id, () => updatedConfession);

      if (selectedConfession?.id === confessionToEdit.id) {
        setSelectedConfession(updatedConfession);
      }

      toast.success("Your confession has been updated!");
      setConfessionToEdit(null);
    } catch (error) {
      toast.error("Failed to update confession. Please try again.");
      console.error(error);
    }
  };

  // Filling the icon is only legible for the heart; the laugh, eye, and
  // heartbreak outlines become solid blobs when filled, so keep their shape
  // and let the selected button highlight carry the reaction state.
  const reactionTypes = [
    { type: 'heart', icon: Heart, countKey: 'heart_count', fillsWhenSelected: true },
    { type: 'haha', icon: Laugh, countKey: 'haha_count', fillsWhenSelected: false },
    { type: 'whoa', icon: Eye, countKey: 'whoa_count', fillsWhenSelected: false },
    { type: 'heartbreak', icon: HeartCrack, countKey: 'heartbreak_count', fillsWhenSelected: false },
  ] as const;

  /**
   * Rows on screen while a search is active: keyword matches first, then the
   * meaning notice (when degraded), then similar-in-meaning. Headers render
   * only for non-empty sections; the caller shows the overall empty state when
   * both lists are empty.
   */
  type SearchRow =
    | { kind: 'header'; key: string; title: string; label: string }
    | { kind: 'notice'; key: string; text: string }
    | { kind: 'confession'; key: string; confession: Confession };

  const searchRows = (): SearchRow[] => {
    const rows: SearchRow[] = [];
    if (keywordResults.length > 0) {
      rows.push({ kind: 'header', key: 'header-keyword', title: 'Keyword matches', label: 'Keyword matches' });
      for (const confession of keywordResults) {
        rows.push({ kind: 'confession', key: confession.id, confession });
      }
    }
    const notice = semanticNotice(semanticStatus);
    if (notice) {
      rows.push({ kind: 'notice', key: 'semantic-notice', text: notice });
    }
    if (semanticResults.length > 0) {
      rows.push({ kind: 'header', key: 'header-semantic', title: 'Similar in meaning', label: 'Similar in meaning' });
      for (const confession of semanticResults) {
        rows.push({ kind: 'confession', key: confession.id, confession });
      }
    }
    return rows;
  };

  return (
    <div className="min-h-screen p-2 sm:p-4 pt-20 sm:pt-24 relative overflow-hidden">
      <Navigation />
      <FloatingHearts />
      {/* Floral preview: 4126474 top-left corner, extra large. */}
      <FloralDecor pair={3} className="left-0 top-0 w-[34rem] opacity-90 sm:w-[44rem]" />
      {/* Floral edges: 1706715_top top-right; 1706715_bottom bottom-left. */}
      <FloralDecor pair={6} className="right-0 top-0 w-[34rem] opacity-90 sm:w-[42rem]" />
      <FloralDecor pair={7} className="bottom-0 left-0 w-[34rem] opacity-90 sm:w-[42rem]" />
      {/* Scattered chat glyphs for the feed gutters. */}
      <ScatteredIcons variant="chat" />
      <div className="max-w-6xl mx-auto mb-8 px-2 sm:px-4">
        {/*
          The persistent notice, above the fold rather than inside the dialog. A user who has
          just posted should not have to reopen the compose box to find out they have to wait,
          and `role="status"` means it is announced without stealing focus mid-read.
        */}
        {isOnCooldown && (
          <div role="status" className="relative z-10 mb-4 sm:mb-6 rounded-xl border border-romantic/40 bg-card/90 px-4 py-3 shadow-[var(--shadow-card)] backdrop-blur">
            <p className="flex items-center gap-2 text-sm font-medium sm:text-base text-foreground">
              <Clock className="w-4 h-4 shrink-0 text-romantic" />
              <span>
                {cooldown?.message ?? 'You can post one confession every 2 hours.'}{' '}
                <span className="font-bold tabular-nums">
                  {formatCooldownRemaining(secondsUntilEligible)}
                </span>{' '}
                remaining.
              </span>
            </p>
          </div>
        )}
        <div className="flex items-center gap-2 sm:gap-4 mb-4 sm:mb-6">
          <CountdownTimer />
        </div>
        <div className="relative z-10 text-center mb-6 sm:mb-8 px-2">
          <h1 className="header-halo text-2xl sm:text-3xl md:text-4xl lg:text-5xl font-dancing text-romantic mb-2 sm:mb-4 break-words">
            Anonymous Confessions 💌
          </h1>
          <p className="page-subtitle text-sm sm:text-base md:text-xl px-2 break-words">
            Share your heart safely and anonymously
          </p>
        </div>
        <div className="relative z-10 mb-4 sm:mb-6">
          <div className="flex flex-col sm:flex-row gap-2">
            <Input
              aria-label="Search confessions"
              placeholder="Search confessions — keywords or meaning…"
              value={searchInput}
              maxLength={1000}
              onChange={(e) => queueDebouncedSearch(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') handleSemanticSearch();
              }}
              className="bg-background/80 border-romantic/30 focus:border-romantic"
            />
            <div className="flex gap-2">
              <Button onClick={handleSemanticSearch} disabled={isSearching} className="btn-romantic">
                {isSearching ? 'Searching…' : 'Search'}
              </Button>
              {activeSearch !== null && (
                <Button variant="outline" onClick={() => void handleClearSearch()} aria-label="Clear search">
                  Clear
                </Button>
              )}
            </div>
          </div>
          {activeSearch !== null && (
            <p role="status" className="mt-2 text-xs sm:text-sm text-muted-foreground">
              Results for “{activeSearch}” — {keywordTotal} keyword{keywordTotal === 1 ? '' : 's'} + {semanticResults.length} similar in meaning.
            </p>
          )}
          {searchError && (
            <p role="alert" className="mt-2 text-xs sm:text-sm text-destructive">
              {searchError}
            </p>
          )}
          {activeSearch !== null && keywordResults.length === 0 && semanticResults.length === 0 && !searchError && !isSearching && (
            <p role="status" className="mt-2 text-xs sm:text-sm text-muted-foreground">
              No confessions match that yet. Try different words, or clear to see the latest feed.
            </p>
          )}
        </div>
        <div className="flex flex-col md:flex-row justify-between items-center gap-3 sm:gap-4">
          <div className="flex flex-wrap items-center gap-2 sm:gap-4 justify-center w-full md:w-auto">
            <span className="text-xs sm:text-sm text-muted-foreground">Sort by:</span>
            <Button variant={sortBy === 'time' ? 'default' : 'outline'} size="sm" onClick={() => setSortBy('time')} className={`text-xs sm:text-sm h-8 sm:h-9 px-2 sm:px-3 ${sortBy === 'time' ? 'btn-romantic' : 'btn-love'}`}>
              <Clock className="w-3 h-3 sm:w-4 sm:h-4 mr-1 sm:mr-2" />
              Recent
            </Button>
            <Button variant={sortBy === 'comments' ? 'default' : 'outline'} size="sm" onClick={() => setSortBy('comments')} className={`text-xs sm:text-sm h-8 sm:h-9 px-2 sm:px-3 ${sortBy === 'comments' ? 'btn-romantic' : 'btn-love'}`}>
              <MessageSquare className="w-3 h-3 sm:w-4 sm:h-4 mr-1 sm:mr-2" />
              Comments
            </Button>
            <Button variant={sortBy === 'popularity' ? 'default' : 'outline'} size="sm" onClick={() => setSortBy('popularity')} className={`text-xs sm:text-sm h-8 sm:h-9 px-2 sm:px-3 ${sortBy === 'popularity' ? 'btn-romantic' : 'btn-love'}`}>
              <TrendingUp className="w-3 h-3 sm:w-4 sm:h-4 mr-1 sm:mr-2" />
              Popularity
            </Button>
          </div>
          <Dialog
            open={isPostDialogOpen}
            onOpenChange={(open) => {
              setIsPostDialogOpen(open);
              if (!open) {
                setNewConfession('');
                setConfessingTo('');
                setIsAnonymous(true);
                setAllowComments(true);
              }
            }}
          >
            <DialogTrigger asChild>
              <Button className="btn-romantic">
                <Plus className="w-4 h-4 mr-2" />
                Share Your Heart
              </Button>
            </DialogTrigger>
            <DialogContent className="max-w-md">
              <DialogHeader>
                <DialogTitle className="font-dancing text-2xl text-romantic">
                  Share Your Confession 💌
                </DialogTitle>
                <DialogDescription>
                  Express your feelings safely and anonymously. To someone, or just to get it out.
                </DialogDescription>
              </DialogHeader>
              {isOnCooldown && (
                /* Repeated here as well as above the trigger: the dialog is reachable from
                   the feed's own state and the wait has to be explained where the Share
                   button is, not only where the person decided to press it. */
                <div role="status" className="rounded-xl border border-romantic/40 bg-card/90 px-3 py-2 text-sm font-medium text-foreground shadow-[var(--shadow-card)]">
                  <p className="flex items-center gap-2">
                    <Clock className="w-4 h-4 shrink-0 text-romantic" />
                    <span>{cooldown?.message ?? 'You can post one confession every 2 hours.'}</span>
                  </p>
                  <p className="mt-1 text-xs font-medium text-foreground/80">
                    Time left: <span className="font-bold text-foreground tabular-nums">{formatCooldownRemaining(secondsUntilEligible)}</span>
                  </p>
                </div>
              )}
              <div className="space-y-4">
                <Textarea placeholder="Share what's in your heart..." value={newConfession} maxLength={CONFESSION_BODY_MAX} onChange={(e) => setNewConfession(e.target.value)} className="min-h-[120px] bg-background/80 border-romantic/30 focus:border-romantic" />
                <p className="text-xs text-muted-foreground">
                  {newConfession.length} / {CONFESSION_BODY_MAX} characters
                </p>
                <div className="space-y-2">
                  <label htmlFor="confessing-to" className="text-sm text-muted-foreground">
                    Who are you confessing to? <span className="text-xs">(optional)</span>
                  </label>
                  <Input
                    id="confessing-to"
                    placeholder="Leave blank if this isn't about anyone in particular"
                    value={confessingTo}
                    maxLength={RECIPIENT_NAME_MAX}
                    onChange={(e) => setConfessingTo(e.target.value)}
                    className="bg-background/80 border-romantic/30 focus:border-romantic"
                  />
                  <p className="text-xs text-muted-foreground">
                    Add a name if this is for someone. Moderators use it to check the confession
                    reaches the right person; a confession you just want to share does not need one.
                  </p>
                </div>
                <div className="space-y-3">
                  <div className="flex items-center justify-between">
                    <span className="text-sm text-muted-foreground">Post anonymously</span>
                    <Switch checked={isAnonymous} onCheckedChange={setIsAnonymous} />
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-sm text-muted-foreground">Allow comments</span>
                    <Switch checked={allowComments} onCheckedChange={setAllowComments} />
                  </div>
                </div>
              </div>
              <DialogFooter>
                <Button
                  onClick={handleSubmitConfession}
                  disabled={isOnCooldown || isSubmittingConfession}
                  className="btn-romantic w-full"
                >
                  {isSubmittingConfession ? (
                    <>
                      <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                      Sharing…
                    </>
                  ) : (
                    <>
                      <Send className="w-4 h-4 mr-2" />
                      {isOnCooldown ? 'Waiting for your next confession' : 'Share Confession'}
                    </>
                  )}
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </div>
      </div>

      <div className="max-w-4xl mx-auto space-y-6 min-w-0">
        {(activeSearch !== null ? searchRows() : confessions.map((confession) => ({ kind: 'confession' as const, key: confession.id, confession }))).map((row, index) => {
          if (row.kind === 'header') {
            return (
              <h2 key={row.key} aria-label={row.label} className="font-dancing text-xl text-romantic pt-2">
                {row.title}
              </h2>
            );
          }
          if (row.kind === 'notice') {
            return (
              <p key={row.key} role="status" className="text-xs sm:text-sm text-muted-foreground">
                {row.text}
              </p>
            );
          }
          const confession = row.confession;
          const hasReportedConfession = user && hasReportedByMe(confession, user.id);
          // Reporting your own post is refused by the server, so the flag is not offered on
          // it at all; the edit control above is the only action an author gets.
          const isOwnConfession = confession.is_mine;
          return (
            <Card key={confession.id} className="confession-card animate-fade-in transition-all duration-300 min-w-0 overflow-hidden" style={{ animationDelay: `${index * 0.1}s` }}>
              <CardHeader>
                <div className="flex justify-between items-start gap-3">
                  {/* min-w-0 so this column is allowed to shrink. Without it the flex item
                      keeps min-width:auto, refuses to get narrower than its longest word, and
                      a long recipient name pushes the action buttons off the card. */}
                  <div className="min-w-0 flex items-center gap-3">
                    {!confession.anonymous && <UserAvatar info={confession.user_info} />}
                    <div className="min-w-0">
                      <CardTitle className="text-lg font-dancing text-romantic break-words [overflow-wrap:anywhere]">
                        {confession.anonymous ? "🤫 Anonymous" : (confession.user_info?.name || "User")}
                      </CardTitle>
                      <CardDescription>{formatDateTimeDDMMYYYY(confession.created_at)}</CardDescription>
                      {confession.recipient_name && (
                        <p className="mt-1 text-xs text-muted-foreground break-words [overflow-wrap:anywhere]">
                          Confessing to: <span className="font-medium text-foreground">{confession.recipient_name}</span>
                        </p>
                      )}
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    {isOwnConfession && (
                        <Button
                        size="icon"
                        variant="ghost"
                        className="h-8 w-8 rounded-full"
                        onClick={() => handleEditClick(confession)}
                        >
                        <Edit className="h-4 w-4" />
                        </Button>
                    )}
                    {!isOwnConfession && (
                      <Button
                        size="icon"
                        variant="ghost"
                        className={`h-8 w-8 rounded-full transition-colors ${
                          hasReportedConfession
                            ? 'bg-yellow-500 text-white hover:bg-yellow-600'
                            : 'text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-700'
                        }`}
                        onClick={() => !hasReportedConfession && handleReportConfessionClick(confession)}
                        disabled={hasReportedConfession}
                      >
                        <Flag className="h-4 w-4" />
                      </Button>
                    )}
                  </div>
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                {/* The body is the primary way in, so it is a real button rather than a
                    click handler on a paragraph: that gets Enter/Space activation and a
                    focus ring for free, and a div with onClick announces as nothing at all.
                    The <span> element inside is required -- a <p> is flow content and is
                    not valid inside a button. */}
                <button
                  type="button"
                  onClick={() => {
                    // A click that ends a text selection is a copy, not a request to open
                    // the confession. Without this, dragging across a confession to select
                    // a line of it opens the dialog instead.
                    const selection = window.getSelection();
                    if (selection && !selection.isCollapsed) return;
                    handleCommentClick(confession);
                  }}
                  className="group block w-full cursor-pointer rounded-lg text-left transition-colors hover:bg-romantic/5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-romantic focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                >
                  <ConfessionBody element="span" clamp={6} className="text-foreground leading-relaxed">
                    {confession.body}
                  </ConfessionBody>
                  <span className="sr-only">
                    Open to read in full and see {confession.comment_count}{' '}
                    {confession.comment_count === 1 ? 'comment' : 'comments'}
                  </span>
                </button>
                <div className="flex items-center justify-between pt-4 border-t border-border">
                  <div className="flex items-center gap-2 md:gap-4">
                    {reactionTypes.map(reaction => {
                      const Icon = reaction.icon;
                      const isSelected = confession.user_reaction === reaction.type;
                      return (
                        <Button
                          key={reaction.type}
                          variant={isSelected ? 'default' : 'ghost'}
                          size="sm"
                          onClick={() => handleReaction(confession.id, reaction.type)}
                          className={`transition-colors ${isSelected ? 'btn-romantic' : 'hover:bg-romantic/10'}`}
                        >
                          <Icon className={`w-4 h-4 mr-1.5 ${isSelected && reaction.fillsWhenSelected ? 'fill-current' : ''}`} />
                          {confession[reaction.countKey]}
                        </Button>
                      );
                    })}
                  </div>
                  <Button variant="ghost" size="sm" className="text-muted-foreground" onClick={() => handleCommentClick(confession)}>
                    <MessageSquare className="w-4 h-4 mr-1" />
                    {confession.comment_count}
                  </Button>
                </div>
              </CardContent>
            </Card>
          );
        })}
        {/* The feed only grows downwards: the next page appends beneath the posts already
            on screen, so the reader's place never moves. Shown only while the server holds
            more visible posts than the feed is showing; legacy answers never page.
            Hidden while a search is active: hybrid answers are single pages. */}
        {activeSearch === null && pagedFeed && confessions.length < total && (
          <div className="flex justify-center pt-2">
            <Button
              variant="outline"
              onClick={() => void loadMore()}
              disabled={loadingMore}
              aria-label="Show more confessions"
            >
              {loadingMore ? 'Loading…' : `Show more (${total - confessions.length} remaining)`}
            </Button>
          </div>
        )}
      </div>
      
      <Dialog open={!!selectedConfession} onOpenChange={(isOpen) => !isOpen && setSelectedConfession(null)}>
        <DialogContent className="sm:max-w-2xl md:max-w-3xl lg:max-w-4xl w-[95vw] h-[90vh] flex flex-col p-0">
          {selectedConfession && (
            <>
              <DialogHeader className="p-6 pb-4">
                <div className="flex items-center gap-3 w-full">
                  {!selectedConfession.anonymous && <UserAvatar info={selectedConfession.user_info} className="w-12 h-12" />}
                  <div className="min-w-0">
                    <DialogTitle className="font-dancing text-2xl text-romantic break-words [overflow-wrap:anywhere]">
                      {selectedConfession.anonymous ? "🤫 Anonymous Confession" : (selectedConfession.user_info?.name || "User") + "'s Confession"}
                    </DialogTitle>
                    <DialogDescription>{formatDateTimeDDMMYYYY(selectedConfession.created_at)}</DialogDescription>
                  </div>
                </div>
                {selectedConfession.recipient_name && (
                  <p className="mt-2 text-sm text-muted-foreground break-words [overflow-wrap:anywhere]">
                    Confessing to: <span className="font-semibold text-foreground">{selectedConfession.recipient_name}</span>
                  </p>
                )}
              </DialogHeader>
              <div className="px-6 pb-4 border-b">
                <ConfessionBody className="text-foreground leading-relaxed">
                  {selectedConfession.body}
                </ConfessionBody>
              </div>

              <div className="flex-grow overflow-y-auto p-6 space-y-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h4 className="text-lg font-semibold text-romantic">Comments ({selectedConfession.comment_count})</h4>
                  {/* Latest is newest first; Relevant ranks by share of likes, so an
                      approved remark reads above a piled-on one. The order is a server
                      question -- switching re-reads page one rather than shuffling the
                      loaded window, which would strand rows the new order puts first. */}
                  <div className="flex items-center gap-1" role="group" aria-label="Sort comments">
                    {(
                      [
                        ['relevant', 'Relevant'],
                        ['latest', 'Latest'],
                      ] as const
                    ).map(([option, label]) => (
                      <Button
                        key={option}
                        variant={commentSort === option ? 'default' : 'ghost'}
                        size="sm"
                        onClick={() => void changeCommentSort(option)}
                        className="h-7 px-2 text-xs"
                      >
                        {label}
                      </Button>
                    ))}
                  </div>
                </div>
                {selectedConfession.comments.length > 0 ? (
                  selectedConfession.comments.map(comment => {
                    const hasReported = user && hasReportedByMe(comment, user.id);
                    // As with confessions, the server refuses a self-report, so the flag is
                    // not offered on a comment written by the viewer.
                    const isOwnComment = !!user && user.id === comment.user_info.id;
                    return (
                    <div key={comment.id} className="text-sm p-3 bg-muted/50 rounded-lg shadow-sm">
                      <div className="flex items-center gap-2">
                         <UserAvatar info={comment.user_info} className="w-8 h-8" />
                         <p className="font-bold text-foreground">{comment.user_info.name}</p>
                      </div>
                      <p className="my-2">{comment.body}</p>
                      <div className="flex items-center justify-between">
                          <p className="text-xs text-muted-foreground">{formatDateTimeDDMMYYYY(comment.created_at)}</p>
                          <div className="flex items-center gap-2">
                              <Button 
                                size="icon" 
                                variant="ghost" 
                                className={`h-7 w-7 rounded-full transition-colors ${
                                  comment.user_reaction === 'like' 
                                  ? 'bg-red-500 text-white hover:bg-red-600' 
                                  : 'text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-700'
                                }`} 
                                onClick={() => handleCommentReaction(selectedConfession.id, comment.id, 'like')}
                              >
                                  <ThumbsUp className="h-4 w-4" />
                              </Button>
                              <span className="text-xs font-medium">{comment.like_count}</span>
                              <Button 
                                size="icon" 
                                variant="ghost" 
                                className={`h-7 w-7 rounded-full transition-colors ${
                                  comment.user_reaction === 'dislike' 
                                  ? 'bg-red-500 text-white hover:bg-red-600' 
                                  : 'text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-700'
                                }`} 
                                onClick={() => handleCommentReaction(selectedConfession.id, comment.id, 'dislike')}
                              >
                                  <ThumbsDown className="h-4 w-4" />
                              </Button>
                              <span className="text-xs font-medium">{comment.dislike_count}</span>
                               {!isOwnComment && (
                                 <Button
                                  size="icon"
                                  variant="ghost"
                                  className={`h-7 w-7 rounded-full transition-colors ${
                                    hasReported
                                    ? 'bg-yellow-500 text-white hover:bg-yellow-600'
                                    : 'text-gray-500 hover:bg-gray-200 dark:hover:bg-gray-700'
                                  }`}
                                  onClick={() => !hasReported && handleReportClick(comment)}
                                  disabled={hasReported}
                                >
                                    <Flag className="h-4 w-4" />
                                </Button>
                               )}
                          </div>
                      </div>
                    </div>
                  )})
                ) : (
                  <p className="text-sm text-center text-muted-foreground py-4">No comments yet. Be the first to share your thoughts!</p>
                )}
                {/* The thread only grows downwards: the next page appends beneath the
                    comments already read, so the reader's place never moves. Shown only
                    while the post holds more visible comments than the dialog is showing;
                    legacy answers carry whole threads and never page. */}
                {pagedFeed &&
                  selectedConfession.comments.length < (selectedConfession.comment_count ?? 0) && (
                    <div className="flex justify-center pt-2">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => void loadMoreComments()}
                        disabled={loadingMoreComments}
                        aria-label="Show more comments"
                      >
                        {loadingMoreComments
                          ? 'Loading…'
                          : `Show more (${(selectedConfession.comment_count ?? 0) - selectedConfession.comments.length} remaining)`}
                      </Button>
                    </div>
                  )}
              </div>

              <DialogFooter className="p-4 border-t bg-background/95">
                {selectedConfession.comments_enabled ? (
                    <div className="flex gap-2 items-start w-full">
                        <Textarea
                        placeholder="Write a thoughtful comment..."
                        value={newComment}
                        maxLength={COMMENT_BODY_MAX}
                        onChange={(e) => setNewComment(e.target.value)}
                        className="flex-grow bg-background border-romantic/30 focus:border-romantic"
                        rows={2}
                        />
                        <Button
                          onClick={() => handlePostComment(selectedConfession.id)}
                          className="btn-romantic"
                          size="icon"
                          aria-label="Send comment"
                          disabled={isPostingComment}
                        >
                          {isPostingComment ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
                        </Button>
                    </div>
                ) : (
                    <div className="w-full text-center text-sm text-muted-foreground">
                        Comments are disabled for this post.
                    </div>
                )}
              </DialogFooter>
            </>
          )}
        </DialogContent>
      </Dialog>
      
      <Dialog open={isReportConfirmOpen} onOpenChange={setIsReportConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Report Comment</DialogTitle>
            <DialogDescription>
              Are you sure you want to report this comment? This action cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setIsReportConfirmOpen(false)}>Cancel</Button>
            <Button className="btn-romantic" onClick={handleConfirmReport}>Yes, Report</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={isReportReasonOpen} onOpenChange={setIsReportReasonOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Reason for Reporting</DialogTitle>
            <DialogDescription>
              Please provide a reason for reporting this comment. This will help our moderators review it.
            </DialogDescription>
          </DialogHeader>
          <Textarea 
            placeholder="e.g., Harassment, spam, inappropriate content..."
            value={reportReason}
            maxLength={REPORT_REASON_MAX}
            onChange={(e) => setReportReason(e.target.value)}
            className="my-4"
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setIsReportReasonOpen(false)}>Cancel</Button>
            <Button className="btn-romantic" onClick={handleReportSubmit}>Submit Report</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={isConfessionReportConfirmOpen} onOpenChange={setIsConfessionReportConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Report Confession</DialogTitle>
            <DialogDescription>
              Are you sure you want to report this confession? This action cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setIsConfessionReportConfirmOpen(false)}>Cancel</Button>
            <Button className="btn-romantic" onClick={handleConfirmConfessionReport}>Yes, Report</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={isConfessionReportReasonOpen} onOpenChange={setIsConfessionReportReasonOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Reason for Reporting</DialogTitle>
            <DialogDescription>
              Please provide a reason for reporting this confession. This will help our moderators review it.
            </DialogDescription>
          </DialogHeader>
          <Textarea 
            placeholder="e.g., Harassment, spam, inappropriate content..."
            value={reportReason}
            maxLength={REPORT_REASON_MAX}
            onChange={(e) => setReportReason(e.target.value)}
            className="my-4"
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setIsConfessionReportReasonOpen(false)}>Cancel</Button>
            <Button className="btn-romantic" onClick={handleConfessionReportSubmit}>Submit Report</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Edit Confession Dialog */}
        <Dialog open={!!confessionToEdit} onOpenChange={(isOpen) => !isOpen && setConfessionToEdit(null)}>
        <DialogContent>
            <DialogHeader>
            <DialogTitle>Edit Your Confession</DialogTitle>
            <DialogDescription>
                Manage settings for your confession.
            </DialogDescription>
            </DialogHeader>
            <div className="space-y-4 py-4">
            <div className="flex items-center justify-between">
                <span className="text-sm text-muted-foreground">Allow comments</span>
                <Switch
                checked={editAllowComments}
                onCheckedChange={setEditAllowComments}
                />
            </div>
            <div className="flex items-center justify-between">
                <span className="text-sm text-muted-foreground">Post anonymously</span>
                <Switch
                checked={editIsAnonymous}
                onCheckedChange={setEditIsAnonymous}
                disabled={!confessionToEdit?.anonymous}
                />
            </div>
            {!confessionToEdit?.anonymous && (
                <p className="text-xs text-muted-foreground">You cannot make a public confession anonymous after posting.</p>
            )}
            </div>
            <DialogFooter>
            <Button variant="outline" onClick={() => setConfessionToEdit(null)}>Cancel</Button>
            <Button className="btn-romantic" onClick={handleSaveChanges}>Save Changes</Button>
            </DialogFooter>
        </DialogContent>
        </Dialog>

      <AlertDialog open={violationDialog.open} onOpenChange={(open) => setViolationDialog((prev) => ({ ...prev, open }))}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Content removed</AlertDialogTitle>
            <AlertDialogDescription>
              {violationDialog.message || 'Your content was removed because it violated our content policies.'}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogAction onClick={() => setViolationDialog({ open: false, message: '' })}>Understood</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
};
