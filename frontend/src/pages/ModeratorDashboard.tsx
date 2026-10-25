import { logger } from '@/lib/logger';
import { useEffect, useState, type ComponentType } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  ArrowRight,
  Flag,
  LogOut,
  Mail,
  MessageCircle,
  MessageCircleHeart,
  ShieldQuestion,
  UsersRound,
} from 'lucide-react';
import { toast } from 'sonner';
import { useAuth } from '@/contexts/AuthContext';
import { AdminNavigation } from '@/components/AdminNavigation';
import { CAPABILITIES } from '@/lib/capabilities';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { ProfileAvatar } from '@/components/ProfileAvatar';
import {
  getAdminConfessionReports,
  getAdminLoveNoteReports,
  getAdminLoveNotes,
  getAdminMessageReports,
} from '@/services/api';

/**
 * The landing page for an account that moderates content.
 *
 * Built around the one question a moderator opens this page to answer -- "what is waiting for
 * me" -- and answered with counts of the queues, not with a censored copy of the administration
 * console. The two pages deliberately differ in what they can show. The console answers "what
 * is the state of the platform" and is gated on `stats:read`, because `/admin/stats` returns a
 * census of accounts, blocks and sessions that a moderator has no power to act on. This page
 * answers "what is outstanding in the work I was given", and every figure below is a queue the
 * signed-in account could open by clicking it.
 *
 * So nothing here is a platform figure and nothing here calls `/admin/stats`. There are no
 * totals of users, no session count and no block count, and there could not be: the endpoint
 * that produces them refuses this account. What replaces them is four counts a moderator can
 * act on -- the three report queues and the love-note backlog -- plus the reports total those
 * three add up to.
 *
 * Each count is read through the same endpoint the queue page itself uses, so a card cannot
 * drift from the screen it links to: the report counts are the `total` of the three report
 * envelopes filtered to `pending`, which is the filter the queue defaults to, and the love-note
 * count is the `pending_review` slice of the notes list the review screen groups by. Asking for
 * one row and reading the envelope's `total` rather than downloading fifty reported messages to
 * add them up client-side is the same count at a fraction of the payload; a landing page is the
 * worst possible place to fetch a queue nobody has asked to see yet.
 *
 * Presentation only, like every capability check in the client: each queue's page and each
 * endpoint behind it re-check its own capability, so a card offered here that could not be acted
 * on would fail rather than expose anything.
 */

interface QueueCard {
  id: string;
  title: string;
  /** What the figure counts, in the reviewer's terms rather than the query's. */
  caption: string;
  icon: ComponentType<{ className?: string }>;
  value: number | null;
  to: string;
  /** Spans two columns: the headline carries its breakdown underneath. */
  wide?: boolean;
  breakdown?: string;
}

const QUEUE_KEYS = [
  'confession-reports',
  'love-note-reports',
  'message-reports',
  'pending-love-notes',
] as const;

type QueueKey = (typeof QUEUE_KEYS)[number];

interface Reading {
  value: number | null;
  ok: boolean;
}

/** The `total` of a `{ reports, total, skip, limit }` envelope, when it is genuinely a number. */
const envelopeTotal = (payload: unknown): number | null => {
  const total = (payload as { total?: unknown } | null)?.total;
  // Typed rather than coerced: a `total` that arrived as a string or went missing is a payload
  // this page does not understand, and inventing a 0 from it would report an empty queue.
  return typeof total === 'number' ? total : null;
};

/**
 * How many submitted notes are still waiting to be approved or rejected.
 *
 * Against the paginated endpoint this is the `total` of a `pending_review` read, counted
 * in SQL under the same filter -- asking for one row and reading the count beside it, so
 * the dashboard never loads the queue to count it. A legacy bare array (unit-test mocks
 * and older servers) is counted by hand instead.
 */
const pendingNotesIn = (payload: unknown): number | null => {
  const total = (payload as { total?: unknown } | null)?.total;
  if (typeof total === 'number') {
    return total;
  }
  if (Array.isArray(payload)) {
    return payload.filter((note) => (note as { status?: unknown })?.status === 'pending_review').length;
  }
  return null;
};

/**
 * One count, read so that a failure is a value rather than an exception.
 *
 * A queue that cannot be read must not take the page with it: the counts are the fastest part
 * of the dashboard and the links are the reason anyone came, so a refused or timed-out request
 * leaves that one card blank and says so underneath, and every other queue still reports.
 */
const readCount = async (
  label: string,
  read: () => Promise<unknown>,
  pick: (payload: unknown) => number | null,
): Promise<Reading> => {
  try {
    return { value: pick(await read()), ok: true };
  } catch (error) {
    logger.error(`Failed to read the ${label} count`, error);
    return { value: null, ok: false };
  }
};

const isCount = (value: number | null): value is number => typeof value === 'number';

export const ModeratorDashboard = () => {
  const { user, logout, can } = useAuth();
  const navigate = useNavigate();

  const [counts, setCounts] = useState<Record<QueueKey, number | null>>({
    'confession-reports': null,
    'love-note-reports': null,
    'message-reports': null,
    'pending-love-notes': null,
  });
  const [unreadable, setUnreadable] = useState<QueueKey[]>([]);
  const [loadingCounts, setLoadingCounts] = useState(true);

  // Booleans rather than `can` in the dependency list: `can` is a fresh closure on every render
  // of the provider, so depending on it would refetch the four queues in a loop.
  const canReadReports = can(CAPABILITIES.REPORTS_REVIEW);
  const canReadLoveNotes = can(CAPABILITIES.LOVE_NOTES_REVIEW);

  useEffect(() => {
    let active = true;

    // Built as a list rather than four awaits in sequence: the queues are independent reads and
    // a moderator should not wait for the love-note backlog to count the reports waiting on them.
    const tasks: Array<[QueueKey, Promise<Reading>]> = [];
    if (canReadReports) {
      tasks.push([
        'confession-reports',
        readCount('confession report', () => getAdminConfessionReports({ status: 'pending', limit: 1 }), envelopeTotal),
      ]);
      tasks.push([
        'love-note-reports',
        readCount('love note report', () => getAdminLoveNoteReports({ status: 'pending', limit: 1 }), envelopeTotal),
      ]);
      tasks.push([
        'message-reports',
        readCount('message report', () => getAdminMessageReports({ status: 'pending', limit: 1 }), envelopeTotal),
      ]);
    }
    if (canReadLoveNotes) {
      tasks.push([
        'pending-love-notes',
        readCount('love note', () => getAdminLoveNotes({ status: 'pending_review', limit: 1 }), pendingNotesIn),
      ]);
    }

    if (tasks.length === 0) {
      setLoadingCounts(false);
      return () => {
        active = false;
      };
    }

    setLoadingCounts(true);
    void Promise.all(tasks.map(async ([key, promise]) => [key, await promise] as const)).then(
      (results) => {
        // A response that arrives after the account changed, or after this page was left, is
        // dropped rather than written into a component that no longer wants it.
        if (!active) {
          return;
        }
        setCounts(
          results.reduce(
            (accumulated, [key, reading]) => ({ ...accumulated, [key]: reading.value }),
            {} as Record<QueueKey, number | null>,
          ),
        );
        setUnreadable(results.filter(([, reading]) => !reading.ok).map(([key]) => key));
        setLoadingCounts(false);
      },
    );

    return () => {
      active = false;
    };
  }, [canReadReports, canReadLoveNotes]);

  const confessionReports = counts['confession-reports'];
  const loveNoteReports = counts['love-note-reports'];
  const messageReports = counts['message-reports'];

  // Summed only when all three queues answered. Adding the two that worked would report a
  // smaller backlog than exists, which is the one failure mode a queue count must not have: a
  // moderator who reads "2 open reports" when there are five will go home.
  const openReports =
    isCount(confessionReports) && isCount(loveNoteReports) && isCount(messageReports)
      ? confessionReports + loveNoteReports + messageReports
      : null;

  const reportBreakdown =
    isCount(confessionReports) && isCount(loveNoteReports) && isCount(messageReports)
      ? `${confessionReports} post or comment · ${loveNoteReports} love note · ${messageReports} chat message`
      : undefined;

  const cards: QueueCard[] = [];
  if (canReadReports) {
    cards.push(
      {
        id: 'open-reports',
        title: 'Open reports',
        caption: 'Filed by a participant and still waiting on a ruling.',
        icon: Flag,
        value: openReports,
        to: '/admin/reports',
        wide: true,
        breakdown: reportBreakdown,
      },
      {
        id: 'confession-reports',
        title: 'Post & comment reports',
        caption: 'Filed against a confession, or against a comment under one.',
        icon: ShieldQuestion,
        value: confessionReports,
        to: '/admin/reports',
      },
      {
        id: 'love-note-reports',
        title: 'Love note reports',
        caption: 'Filed against a note somebody submitted.',
        icon: MessageCircleHeart,
        value: loveNoteReports,
        to: '/admin/reports',
      },
      {
        id: 'message-reports',
        title: 'Chat message reports',
        caption: 'Filed against a private message.',
        icon: MessageCircle,
        value: messageReports,
        to: '/admin/reports',
      },
    );
  }
  if (canReadLoveNotes) {
    cards.push({
      id: 'pending-love-notes',
      title: 'Love notes awaiting review',
      caption: 'Submitted, and not yet approved or rejected.',
      icon: Mail,
      value: counts['pending-love-notes'],
      to: '/admin/love-notes',
    });
  }

  // The two queues that carry no count, and why. A confession is public the moment it is
  // written, so there is no pending figure for it -- the posts waiting on a moderator are the
  // reported ones, and those are the first card. Accounts are admin business, and a moderator
  // is not shown a count of people they could not open.
  const uncountedQueues = [
    {
      id: 'confessions',
      title: 'Confession Review',
      description:
        'Read every post and its comments with the reactions on it. Nothing here waits for approval; the posts that need a ruling are the reported ones counted above.',
      icon: ShieldQuestion,
      to: '/admin/confessions',
      capability: CAPABILITIES.CONFESSIONS_REVIEW,
    },
    {
      id: 'profiles',
      title: 'Profile Review',
      description: 'Inspect accounts, edit their details and block or unblock them.',
      icon: UsersRound,
      to: '/admin/profile-review',
      capability: CAPABILITIES.USERS_REVIEW,
    },
  ].filter((queue) => can(queue.capability));

  const handleLogout = () => {
    void logout();
    toast.success('Logged out of moderator mode.');
  };

  return (
    <div className="relative min-h-screen bg-background">
      <AdminNavigation />

      <div className="mx-auto flex w-full max-w-6xl flex-col gap-8 px-4 pb-16 pt-28">
        <header className="flex flex-col gap-6 rounded-3xl border bg-card p-6 shadow-sm sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="text-xs uppercase tracking-wider text-muted-foreground">
              Moderation Dashboard
            </p>
            <h1 className="mt-1 text-3xl font-semibold sm:text-4xl">
              Welcome back, {user?.name ?? 'Moderator'}
            </h1>
            <p className="mt-2 max-w-xl text-sm text-muted-foreground">
              What is waiting on you, and where it is.
            </p>
          </div>

          <div className="flex items-center gap-4">
            <ProfileAvatar
              src={user?.profile_picture_url}
              name={user?.name}
              className="h-16 w-16 border"
            />
            <div>
              <p className="text-sm text-muted-foreground">Signed in as</p>
              <p className="text-base font-medium">{user?.email}</p>
              {/* The table the account lives in, which is the only thing that says what kind
                  of account it is. A moderator reads as "moderator" because that is the
                  account's own name, not a rank. A flex row rather than a <p>, because Badge
                  renders a div. */}
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <span>Reg. No. {user?.regno}</span>
                {user?.account_type && <Badge variant="secondary">{user.account_type}</Badge>}
              </div>
            </div>
            <Button onClick={handleLogout} variant="ghost" className="ml-auto flex items-center gap-2">
              <LogOut className="h-4 w-4" />
              Logout
            </Button>
          </div>
        </header>

        <section className="space-y-4">
          <div>
            <h2 className="text-lg font-semibold">Waiting on you</h2>
            <p className="text-sm text-muted-foreground">
              Counted the way each queue counts them, so a card and the screen behind it agree.
            </p>
          </div>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            {cards.map((card) => (
              <button
                key={card.id}
                type="button"
                onClick={() => navigate(card.to)}
                className={`group flex flex-col gap-3 rounded-2xl border bg-card p-6 text-left shadow-sm transition hover:border-primary/40 hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                  card.wide ? 'md:col-span-2' : ''
                }`}
              >
                <div className="flex items-center gap-3">
                  <span className="rounded-xl bg-gradient-to-br from-pink-500/20 to-purple-500/20 p-2.5">
                    <card.icon className="h-5 w-5" />
                  </span>
                  <span className="flex-1 text-base font-medium">{card.title}</span>
                  <ArrowRight className="h-4 w-4 text-muted-foreground transition group-hover:translate-x-0.5" />
                </div>

                {loadingCounts ? (
                  <div className="h-10 w-24 animate-pulse rounded bg-muted" />
                ) : (
                  <p className="text-4xl font-semibold tabular-nums">
                    {isCount(card.value) ? card.value : '—'}
                  </p>
                )}

                <div className="mt-auto">
                  {card.breakdown && !loadingCounts && (
                    <p className="text-xs text-muted-foreground">{card.breakdown}</p>
                  )}
                  <p className="text-sm text-muted-foreground">{card.caption}</p>
                </div>
              </button>
            ))}
          </div>

          {unreadable.length > 0 && (
            <p className="text-sm text-muted-foreground">
              Some of these counts could not be read just now, so they show a dash rather than a
              number. The queues themselves are unaffected.
            </p>
          )}
        </section>

        {uncountedQueues.length > 0 && (
          <section className="space-y-4">
            <div>
              <h2 className="text-lg font-semibold">Also yours to open</h2>
              <p className="text-sm text-muted-foreground">
                Screens with no backlog of their own.
              </p>
            </div>

            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              {uncountedQueues.map((queue) => (
                <button
                  key={queue.id}
                  type="button"
                  onClick={() => navigate(queue.to)}
                  className="group flex flex-col gap-3 rounded-2xl border bg-card p-6 text-left shadow-sm transition hover:border-primary/40 hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <div className="flex items-center gap-3">
                    <span className="rounded-xl bg-gradient-to-br from-pink-500/20 to-purple-500/20 p-2.5">
                      <queue.icon className="h-5 w-5" />
                    </span>
                    <span className="flex-1 text-base font-medium">{queue.title}</span>
                    <ArrowRight className="h-4 w-4 text-muted-foreground transition group-hover:translate-x-0.5" />
                  </div>
                  <p className="text-sm text-muted-foreground">{queue.description}</p>
                </button>
              ))}
            </div>
          </section>
        )}

        <section className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">The line between this page and the console</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 text-sm text-muted-foreground">
              <p>
                Every figure above is a queue you can open and rule on. The platform counters are
                deliberately absent: how many accounts exist, how many are blocked and who is
                signed in are admin-only, so there is nothing here that is a stale copy of
                them, and this page asks for none of it.
              </p>
              <p>
                What a moderator does: read every content queue, and hide, remove, approve or
                reject what is in it. Rule on a report either way -- dismiss it, or take the
                content behind it down -- and the ruling is recorded.
              </p>
              <p className="text-xs">
                Creating, editing, blocking or unblocking an account, the active-session list and
                the application settings stay with an admin.
              </p>
            </CardContent>
          </Card>
        </section>

        <footer className="pb-6 text-center text-xs text-muted-foreground">
          ConfessIt Moderation &middot; figures are the queues, nothing more
        </footer>
      </div>
    </div>
  );
};
