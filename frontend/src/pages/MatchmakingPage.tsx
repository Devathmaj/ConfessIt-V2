// src/pages/MatchmakingPage.tsx

import { useState, useEffect, useCallback, useRef } from 'react';
import { useAuth, User } from '@/contexts/AuthContext';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { CountdownTimer } from '@/components/ui/countdown-timer';
import { FloatingHearts } from '@/components/ui/floating-hearts';
import { FloralDecor } from '@/components/FloralDecor';
import { findMatch, checkMatchmakingStatus, requestConversation, getCurrentConversation } from '@/services/api';
import { parseTimestamp } from '@/lib/utils';
import { cohortLabel } from '@/lib/accountFields';
import { ConversationDialog } from '@/components/ConversationDialog';
import {
  Heart,
  MessageCircle,
  Sparkles,
  ArrowLeft,
  Send,
  Clock,
} from 'lucide-react';
import { toast } from 'sonner';
import { Navigation } from '@/components/Navigation';
import { ProfileAvatar } from '@/components/ProfileAvatar';
import {
    AlertDialog,
    AlertDialogAction,
    AlertDialogCancel,
    AlertDialogContent,
    AlertDialogDescription,
    AlertDialogFooter,
    AlertDialogHeader,
    AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { serverNow } from '@/lib/serverClock';
import { describeApiError } from '@/lib/apiError';

// Pre-written messages for quick chat options
const prewrittenMessages = [
  "Hey! I love your profile, would you like to chat? 👋",
  "Your interests caught my eye! What's your favorite book? 📚",
  "Coffee lover here too! Know any good spots? ☕",
  "Your bio made me smile! Tell me more about yourself ✨"
];

// Icebreaker questions to start conversations
const icebreakerCards = [
  "If you could have dinner with anyone, who would it be?",
  "What's your idea of a perfect day?",
  "Share your most embarrassing moment!",
  "What's on your bucket list?",
  "Describe yourself in three emojis!"
];

/**
 * Shown once a match has expired and the cooldown has lifted with it.
 *
 * No wait is quoted here on purpose, and nothing here assumes the cooldown ends when the
 * match does. The two are separate clocks: the match lives for MATCH_EXPIRY_HOURS from the
 * draw while the cooldown runs MATCHMAKING_COOLDOWN_HOURS from `last_matchmaking_at`, and a
 * moderator can close a conversation at any point in between -- which used to be the case
 * this page could not tell apart from an ordinary expiry, so it told a terminated user they
 * could go and match again while the server was still refusing them. The authority for that
 * question is GET /matchmaking/check, so `describeExpiredMatch` asks it instead of assuming.
 */
export const EXPIRED_MATCH_NOTICE = 'Your previous match has expired. You can find a new match now.';

/** What the server says about a cooldown that is still running, if one is. */
export type CooldownRemaining = {
  hours: number;
  minutes: number;
  message: string;
};

/**
 * What a check for a cooldown came back with.
 *
 * `known: false` is for a check that never came back -- a request that never arrived. That is not
 * the same as "no cooldown": a request that never came back is how a real cooldown turns into an
 * invitation.
 *
 * `cohortBlock` is carried alongside rather than folded into `remaining` because it is not a wait.
 * It is a standing condition that no amount of waiting clears, and everywhere the page would treat
 * a wait it has to treat this differently -- most visibly by refusing the click outright instead of
 * letting it become a draw that cannot succeed.
 */
export type CooldownCheck = {
  known: true;
  remaining: CooldownRemaining | null;
  /** The cohort is in no matchmaking slot, so it cannot be matched at all. */
  cohortBlock: CohortBlock | null;
} | { known: false };

/** A check that could not be made. Carries no verdict about the user. */
export const UNANSWERED_COOLDOWN: CooldownCheck = { known: false };

/**
 * Reads the server's answer to "may I match?".
 *
 * GET /matchmaking/check answers with `cooldown` while a wait is running and `eligible`
 * otherwise, so a successful response that does not say `cooldown` is an answer, not a
 * mystery. A request that never came back is the unknown case, and that is what
 * {@link UNANSWERED_COOLDOWN} is for.
 */
export const readCooldown = (response: unknown): CooldownRemaining | null => {
  const body = response as {
    status?: string;
    remaining_hours?: number;
    remaining_minutes?: number;
    message?: string;
  } | null;
  if (!body || body.status !== 'cooldown') return null;

  return {
    hours: Number(body.remaining_hours ?? 0),
    minutes: Number(body.remaining_minutes ?? 0),
    message:
      body.message ?? 'You are on a cooldown. Please try again in a few hours.',
  };
};

/**
 * A cohort that is in no matchmaking slot, as GET /matchmaking/check reports it.
 *
 * Not a wait and not a rate limit. The cohort cannot be matched by anybody, and the reason is an
 * operator's configuration rather than the user's own timer, so nothing the user does changes it.
 */
export type CohortBlock = { message: string };

/**
 * Reads the "your cohort has no slot" answer out of GET /matchmaking/check.
 *
 * `null` for every other status, including a cooldown -- that one is read by {@link readCooldown},
 * and folding it in here would make a four-hour wait look like a configuration fault.
 */
export const readCohortBlock = (response: unknown): CohortBlock | null => {
  const body = response as { status?: string; message?: string } | null;
  if (!body || body.status !== 'cohort_not_configured') return null;

  return {
    message:
      body.message ??
      'Your cohort is not in any matchmaking slot. An administrator needs to add it.',
  };
};

/**
 * Whether a check permits a draw.
 *
 * The single place that decides it, so that no caller can quietly treat an unanswered check
 * as a yes. A cohort with no slot does not permit one either, and that is the point of asking
 * before the animation rather than after it.
 */
export const cooldownPermitsMatch = (check: CooldownCheck) =>
  check.known && check.remaining === null && check.cohortBlock === null;

/**
 * What to say about a match that has lapsed.
 *
 * With a cooldown still running, the server's own message is the answer and the notice that
 * promises a new match is a lie. Without one, the match really has run out and the notice is
 * correct.
 */
export const describeExpiredMatch = (cooldown: CooldownRemaining | null) =>
  cooldown ? cooldown.message : EXPIRED_MATCH_NOTICE;

/** The remaining wait, for the page's own copy. */
export const formatRemaining = ({ hours, minutes }: CooldownRemaining) =>
  hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m`;

/** The server's stable code for a draw refused because nobody was available. */
export const NO_CANDIDATES = 'matchmaking_no_candidates';

/** The server's stable code for a draw refused because no slot names the user's cohort. */
export const COHORT_NOT_CONFIGURED = 'matchmaking_cohort_not_configured';

/** A draw that was refused, as the page needs to talk about it. */
export type DrawRefusal = {
  /** The server's own sentence, shown verbatim. */
  message: string;
  /**
   * Which refusal this was, or `null` when the server did not say.
   *
   * `null` rather than defaulting to "bad luck", because the two are not interchangeable and
   * guessing wrong is what this whole type exists to prevent.
   */
  code: string | null;
};

/**
 * Read a refused draw out of a failed request.
 *
 * `null` for anything that is not a refusal, so a network drop cannot be shown as "no match this
 * time" -- a request that never arrived has not established that there was nobody to match.
 */
export const readDrawRefusal = (error: unknown): DrawRefusal | null => {
  const body = (error as { response?: { data?: { detail?: unknown; code?: unknown } } })
    ?.response?.data;
  if (!body || typeof body !== 'object') return null;

  const detail = body.detail;
  if (typeof detail !== 'string' || !detail.trim()) return null;

  return {
    message: detail.trim(),
    code: typeof body.code === 'string' ? body.code : null,
  };
};

/**
 * What the card says about a draw that was refused.
 *
 * Two sentences, because the server has two reasons and they are not the same news. Somebody being
 * available and nobody being available is luck, and another attempt can turn it around. A cohort
 * that no slot names will not become matchable by waiting, retrying or refreshing -- an operator
 * has to add it -- so the page must not tell that user to come back later, which is the same
 * mistake as answering the refusal with a cooldown notice.
 *
 * `guidance` is the second line rather than being folded into `message`, because the server's
 * sentence is written for a toast and does not know it is being rendered as the state of the page.
 */
export const describeDrawRefusal = (refusal: DrawRefusal) => {
  if (refusal.code === COHORT_NOT_CONFIGURED) {
    return {
      headline: 'Your cohort is not set up for matchmaking yet',
      guidance: 'An administrator needs to add your cohort to a matchmaking slot. Trying again will not help until they do.',
    };
  }
  if (refusal.code === NO_CANDIDATES) {
    return {
      headline: 'No match this time',
      guidance: 'Nobody was available to match with just now. Your next attempt is in four hours.',
    };
  }
  // No code, or one this build does not know: the server's sentence is still the answer, and
  // "this time" is the safe reading because it claims nothing about whether a retry can work.
  return {
    headline: 'No match this time',
    guidance: 'Nobody was available to match with just now.',
  };
};

/**
 * Time left on a live match.
 *
 * This is the match's own remaining life, nothing more. Whether that is also the wait until
 * the next draw is a question for GET /matchmaking/check -- the two can disagree whenever a
 * conversation was terminated or an attempt was spent without a match being drawn.
 */
export const timeUntilNextMatch = (expiresAt: string) => {
  const parsedExpiry = parseTimestamp(expiresAt);
  if (!parsedExpiry) return '—';

  const minutes = Math.floor((parsedExpiry.getTime() - serverNow()) / (1000 * 60));
  if (minutes <= 0) return 'now';
  if (minutes < 60) return `${minutes}m`;

  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
};

const EXPIRED_NOTICE_SHOWN_FOR_KEY = 'expired_match_notice_shown_for';

/**
 * Announce a lapsed match at most once.
 *
 * The page re-reads the current match on every mount, and a lapsed match stays the "current"
 * one long after it ended, so without this the notice reappeared on every visit. Remembering
 * the last match id it was shown for suppresses the repeat while still letting the next match
 * to lapse announce itself. One key rather than one per match, so nothing accumulates as
 * matches come and go.
 */
export const notifyExpiredMatch = (
  matchId?: string,
  cooldown: CooldownRemaining | null = null
) => {
  if (matchId) {
    if (localStorage.getItem(EXPIRED_NOTICE_SHOWN_FOR_KEY) === matchId) return;
    localStorage.setItem(EXPIRED_NOTICE_SHOWN_FOR_KEY, matchId);
  }
  toast.info(describeExpiredMatch(cooldown));
};

const COOLDOWN_NOTICE_SUPPRESSED_UNTIL_KEY = 'matchmaking_cooldown_notice_until';

/** When a cooldown noticed `now` will have run out. */
const cooldownEndsAt = (now: number, { hours, minutes }: CooldownRemaining) =>
  now + hours * 3600_000 + minutes * 60_000;

/**
 * Whether a cooldown has already been announced for the wait that is running now.
 *
 * A cooldown lasts hours and the page is visited repeatedly across it, so a notice raised on
 * every mount is the same sentence over and over. Remembering when the current wait ends --
 * rather than a bare flag -- means the suppression expires on its own: this visit is quiet,
 * and the next cooldown still gets to speak for itself without anything having to clear it.
 */
export const cooldownNoticeSuppressed = (now: number, remaining: CooldownRemaining | null) => {
  if (!remaining) return true;
  const until = Number(localStorage.getItem(COOLDOWN_NOTICE_SUPPRESSED_UNTIL_KEY));
  return Number.isFinite(until) && until > now;
};

/**
 * Announce a cooldown, at most once per wait.
 *
 * `force` is for the heart click, where the user has just asked what is going on and a silent
 * no would read as a broken button rather than as an answer.
 */
export const notifyCooldown = (remaining: CooldownRemaining | null, { force = false } = {}) => {
  if (!remaining) return;
  if (!force && cooldownNoticeSuppressed(serverNow(), remaining)) return;

  localStorage.setItem(COOLDOWN_NOTICE_SUPPRESSED_UNTIL_KEY, String(cooldownEndsAt(serverNow(), remaining)));
  toast.info(remaining.message);
};

// Mock data for the "slot machine" animation while matching
const mockMatches: User[] = [
    { id: 'mock1', name: 'Alex', regno: '123', email: 'alex@example.com', year: 2, department: 'CSE', profile_picture_url: 'https://placehold.co/96x96/fecdd3/be123c?text=A', interests: ['Gaming', 'Music'], bio: 'Just a mock user.' },
    { id: 'mock2', name: 'Jordan', regno: '456', email: 'jordan@example.com', year: 3, department: 'ECE', profile_picture_url: 'https://placehold.co/96x96/fecdd3/be123c?text=J', interests: ['Sports', 'Movies'], bio: 'Another mock user.' },
    { id: 'mock3', name: 'Taylor', regno: '789', email: 'taylor@example.com', year: 1, department: 'MECH', profile_picture_url: 'https://placehold.co/96x96/fecdd3/be123c?text=T', interests: ['Reading', 'Hiking'], bio: 'A third mock user.' },
];

/**
 * Renders a glass-like heart SVG that fills with a liquid animation.
 * @param {object} props - The component props.
 * @param {number} props.fillPercentage - The percentage to fill the heart (0-100).
 * @returns {JSX.Element} The GlassHeart component.
 */
const GlassHeart = ({ fillPercentage }: { fillPercentage: number }) => {
    const heartPathData = "M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z";
    const viewBoxHeight = 24;
    const liquidY = viewBoxHeight * (1 - fillPercentage / 100);

    return (
        <div className="relative w-full h-full group-hover:scale-105 transition-transform duration-300">
            <svg
                viewBox="0 0 24 24"
                className="w-full h-full"
                style={{ filter: 'drop-shadow(0 5px 10px rgba(220, 38, 38, 0.4))' }}
            >
                <defs>
                    <clipPath id="heart-clip">
                        <path d={heartPathData} />
                    </clipPath>
                    <linearGradient id="liquid-gradient" x1="0%" y1="0%" x2="0%" y2="100%">
                        <stop offset="0%" stopColor="#f43f5e" />
                        <stop offset="100%" stopColor="#be123c" />
                    </linearGradient>
                </defs>
                <g clipPath="url(#heart-clip)">
                    <rect
                        x="0"
                        y={liquidY}
                        width="24"
                        height="24"
                        fill="url(#liquid-gradient)"
                        style={{ transition: 'y 0.1s linear' }}
                    />
                    <path fill="url(#liquid-gradient)">
                        <animate
                            attributeName="d"
                            dur="2s"
                            repeatCount="indefinite"
                            values={
                                `M 0 ${liquidY} C 6 ${liquidY - 1}, 6 ${liquidY - 1}, 12 ${liquidY} S 18 ${liquidY + 1}, 24 ${liquidY} V 24 H 0 Z;` +
                                `M 0 ${liquidY} C 6 ${liquidY + 1}, 6 ${liquidY + 1}, 12 ${liquidY} S 18 ${liquidY - 1}, 24 ${liquidY} V 24 H 0 Z;` +
                                `M 0 ${liquidY} C 6 ${liquidY - 1}, 6 ${liquidY - 1}, 12 ${liquidY} S 18 ${liquidY + 1}, 24 ${liquidY} V 24 H 0 Z`
                            }
                            style={{ transition: 'd 0.1s linear' }}
                        />
                    </path>
                </g>
                <path
                    d={heartPathData}
                    strokeWidth="0.5"
                    stroke="rgba(255, 255, 255, 0.7)"
                    fill="rgba(255, 255, 255, 0.2)"
                />
            </svg>
        </div>
    );
};

/**
 * A component that displays a countdown timer until a specified expiry timestamp.
 * @param {object} props - The component props.
 * @param {string} props.expiryTimestamp - The ISO string of the expiration date.
 * @returns {JSX.Element} The MatchExpiryTimer component.
 */
const MatchExpiryTimer = ({ expiryTimestamp }: { expiryTimestamp: string }) => {
    const calculateTimeLeft = useCallback(() => {
        const expiry = parseTimestamp(expiryTimestamp);
        const difference = expiry ? expiry.getTime() - serverNow() : 0;
        let timeLeft = { hours: 0, minutes: 0, seconds: 0 };

        if (difference > 0) {
            timeLeft = {
                hours: Math.floor(difference / (1000 * 60 * 60)),
                minutes: Math.floor((difference / 1000 / 60) % 60),
                seconds: Math.floor((difference / 1000) % 60),
            };
        }
        return timeLeft;
    }, [expiryTimestamp]);

    const [timeLeft, setTimeLeft] = useState(calculateTimeLeft());

    useEffect(() => {
        const timer = setInterval(() => {
            setTimeLeft(calculateTimeLeft());
        }, 1000);

        return () => clearInterval(timer);
    }, [calculateTimeLeft]);

    const formatTime = (value: number) => value.toString().padStart(2, '0');

    return (
        <div className="text-center text-sm text-muted-foreground mt-3 flex items-center justify-center gap-2 font-mono">
            <Clock className="w-4 h-4" />
            {timeLeft.hours > 0 || timeLeft.minutes > 0 || timeLeft.seconds > 0 ? (
                <span>
                    Expires in: {formatTime(timeLeft.hours)}h {formatTime(timeLeft.minutes)}m {formatTime(timeLeft.seconds)}s
                </span>
            ) : (
                <span>Match expired!</span>
            )}
        </div>
    );
};

/** What the matchmaking screen knows about a conversation it has found. */
export type ConversationStatus =
  | 'none'
  | 'pending'
  | 'requested'
  | 'accepted'
  | 'rejected'
  | 'terminated';

/**
 * A component to display a user's profile card.
 *
 * Declared at module scope like its siblings: created inside the page it would
 * remount on every parent render, discarding DOM state (image loads, transitions)
 * for no reason. It reads nothing but its props, so hoisting changes nothing it shows.
 */
const UserCard = ({ profile }: { profile: User }) => (
  <Card className="confession-card">
    <CardHeader className="items-center text-center">
      <ProfileAvatar
        src={profile.profile_picture_url}
        name={profile.name}
        className="w-24 h-24 border-4 border-background shadow-lg"
      />
      <CardTitle className="text-2xl font-dancing text-romantic pt-2">{profile.name}</CardTitle>
      <CardDescription>{cohortLabel(profile.year, profile.department)}</CardDescription>
    </CardHeader>
    <CardContent>
      <div className="space-y-3">
        <div className="text-sm">
          <span className="font-semibold text-romantic">Interests:</span>
          <div className="flex flex-wrap gap-2 mt-1">
            {profile.interests?.map((interest) => (
              <span key={interest} className="px-2 py-1 bg-gradient-love rounded-full text-xs text-romantic-dark">
                {interest}
              </span>
            ))}
          </div>
        </div>
        <p className="text-sm text-muted-foreground">"{profile.bio || 'No bio yet!'}"</p>
      </div>
    </CardContent>
  </Card>
);

/**
 * The main component for the matchmaking page.
 * Handles matchmaking logic, UI state, and chat functionality.
 * @returns {JSX.Element} The MatchmakingPage component.
 */
export const MatchmakingPage = () => {
  const { user } = useAuth();
  const [isMatching, setIsMatching] = useState(false);
  const [matchedProfile, setMatchedProfile] = useState<User & { match_id?: string } | null>(null);
  const [slotMachineUser, setSlotMachineUser] = useState<User | null>(null);
  const [heartFill, setHeartFill] = useState(0);
  const [showChat, setShowChat] = useState(false);
  const [chatMessages, setChatMessages] = useState<string[]>([]);
  const [conversationStatus, setConversationStatus] = useState<ConversationStatus>('none');
  const [showConversationDialog, setShowConversationDialog] = useState(false);
  const [hasActiveConversation, setHasActiveConversation] = useState(false);
  const [expiresAt, setExpiresAt] = useState<string | null>(null);
  const [cooldown, setCooldown] = useState<CooldownRemaining | null>(null);
  /**
   * The last draw that was refused, and the only reason the card below the slot machine has
   * anything to say other than "waiting". Cleared whenever a new draw starts, so an outcome is
   * never carried over to an attempt it does not describe.
   */
  const [drawRefusal, setDrawRefusal] = useState<DrawRefusal | null>(null);
  /**
   * The cohort is in no matchmaking slot, so no draw of theirs can succeed.
   *
   * A standing condition rather than an outcome, which is why it is read before the animation and
   * why the card treats it the same way it treats a refused draw: from the user's side these are
   * the same fact -- no match is coming -- discovered at different moments.
   */
  const [cohortBlock, setCohortBlock] = useState<CohortBlock | null>(null);
  /**
   * The one reason to show on the card, whichever way it was found.
   *
   * A cohort with no slot and a refused draw are the same fact to the person looking at this -- no
   * match is coming -- so they share one branch and one set of words. Discovered differently: the
   * block is known from the eligibility check before anything was attempted, the refusal only after
   * a draw was.
   */
  const noMatchReason: DrawRefusal | null =
    cohortBlock
      ? { message: cohortBlock.message, code: COHORT_NOT_CONFIGURED }
      : drawRefusal;
  const cardCopy = noMatchReason ? describeDrawRefusal(noMatchReason) : null;
  /**
   * Whether `cooldown` is the server's answer rather than the absence of one.
   *
   * Tracked apart from the value because the two are easy to conflate and only one of them is
   * worth announcing: a lapse noticed by the sweep can only be described honestly if the wait
   * is actually known.
   */
  const [cooldownKnown, setCooldownKnown] = useState(false);

  const [showConfirmationDialog, setShowConfirmationDialog] = useState(false);
  const [showRareMatchDialog, setShowRareMatchDialog] = useState(false);

  // The periodic sweep and the callbacks below run against the newest render's values, but
  // they are created once and must not restart when those change. A ref is what keeps the
  // read current without turning the interval into a dependency.
  const latest = useRef({ cooldown: null as CooldownRemaining | null, cooldownKnown: false, expiresAt, matchId: undefined as string | undefined });
  useEffect(() => {
    latest.current = { cooldown, cooldownKnown, expiresAt, matchId: matchedProfile?.match_id };
  });

/**
 * Asks the server whether a draw is allowed right now, and keeps the answer on the page.
 *
 * The server is the only authority here. `last_matchmaking_at` is what it counts, and a
 * match's expiry is a different clock entirely -- which is why nothing on this page infers
 * eligibility from `is_expired` any more.
 *
 * A check that could not be made is reported as `known: false` rather than folded into
 * `remaining: null`. The two are different answers -- one means "go ahead", the other means
 * "nobody said" -- and a failed request must not read to the user as permission to match.
 */
const fetchCooldown = useCallback(async (): Promise<CooldownCheck> => {
  try {
    const response = await checkMatchmakingStatus();
    const remaining = readCooldown(response);
    const cohortBlock = readCohortBlock(response);
    setCooldown(remaining);
    // Held in state, not just returned, because the block outlives the click that discovered it:
    // the invite to draw has to stay withdrawn for as long as the cohort is unconfigured, which is
    // until an operator changes something rather than until the page is next asked.
    setCohortBlock(cohortBlock);
    setCooldownKnown(true);
    return { known: true, remaining, cohortBlock };
  } catch (error: any) {
    // The previous state stands and the draw endpoint still refuses a real cooldown with the
    // real remaining time, so nothing is lost by reporting the failure instead of guessing.
    console.error("Failed to check matchmaking status:", error.response?.data?.detail || error.message);
    setCooldownKnown(false);
    return UNANSWERED_COOLDOWN;
  }
}, []);


  /**
   * Reads the current conversation and puts it on the page.
   *
   * A match that has lapsed is not shown as a match: the empty state is the honest one, and
   * the notice says whether the cooldown has lifted with it. A moderator-closed conversation
   * is the opposite case -- it is not a live match and cannot be written to, but it is still
   * this user's conversation and they need to be able to read it, so it stays on the page.
   *
   * `knownCooldown` is the answer the caller already has, passed in rather than read back out
   * of state: on mount the check has only just returned and the notice must be written against
   * it, not against whatever the last render happened to hold.
   */
  const checkForExistingConversation = useCallback(async (
    knownCooldown = latest.current.cooldown
  ) => {
    try {
      const response = await getCurrentConversation();
      if (response.status !== 'success') {
        setHasActiveConversation(false);
        setConversationStatus('none');
        return;
      }

      const status = response.conversation.status as ConversationStatus;
      setExpiresAt(response.match.expires_at);
      setConversationStatus(status);
      setHasActiveConversation(status === 'accepted');

      if (status === 'terminated') {
        setMatchedProfile({ ...response.other_user, match_id: response.match.id });
        return;
      }

      if (!response.match.is_expired) {
        // Only show the matched profile if not expired
        setMatchedProfile({
          ...response.other_user,
          match_id: response.match.id
        });
      } else {
        // The match is gone but its conversation is still readable, so the page falls back
        // to the empty state and says what the real situation is.
        setMatchedProfile(null);
        setExpiresAt(null);
        setConversationStatus('none');
        notifyExpiredMatch(response.match.id, knownCooldown);
      }
    } catch (error: any) {
      console.error('Failed to check for existing conversation:', error);
      setHasActiveConversation(false);
      setConversationStatus('none');
    }
  }, []);

  const refreshConversationStatus = useCallback(async () => {
    await checkForExistingConversation();
  }, [checkForExistingConversation]);

  const handleConversationAccepted = () => {
    setConversationStatus('accepted');
    setHasActiveConversation(true);
  };

  /**
   * Handles when the initiator clicks "Send Message Request"
   * Updates conversation from 'pending' to 'requested' and notifies receiver
   */
  const handleSendMessageRequest = async () => {
    console.log("matchedProfile:", matchedProfile);
    if (!matchedProfile?.match_id) {
        toast.error("Cannot send message request: Match ID is missing.");
        console.log("match_id is missing from:", matchedProfile);
        return;
    }
    try {
        console.log("Sending message request with match_id:", matchedProfile.match_id);
        await requestConversation(matchedProfile.match_id);
        toast.success(`Message request sent to ${matchedProfile.name}!`);
        setConversationStatus('requested');
        await refreshConversationStatus(); // Refresh to get updated status
    } catch (error: any) {
        toast.error(describeApiError(error, "Failed to send message request."));
    }
  };

  // On mount: settle the cooldown first, then read the conversation against it.
  //
  // The order is the fix. These two used to be fired in parallel, so a conversation check
  // that found a lapsed match announced "you can find a new match now" before -- or instead
  // of -- the answer to whether a cooldown was still running.
  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      const check = await fetchCooldown();
      if (cancelled) return;
      // Nothing truthful can be said about the wait, so nothing is said: no notice, and no
      // conversation notice either, since that notice would have to promise a new match.
      if (!check.known) return;
      // Announced once per wait rather than on every visit: this modal used to open every
      // time the page loaded, for as long as the cooldown ran, which is hours of the same
      // sentence with an OK to click each time.
      notifyCooldown(check.remaining);
      await checkForExistingConversation(check.remaining);
    };

    load();

    return () => {
      cancelled = true;
    };
  }, [fetchCooldown, checkForExistingConversation]);

  // Periodic sweep for a match that lapsed while the page was open.
  useEffect(() => {
    const interval = setInterval(async () => {
      const {
        expiresAt: currentExpiry,
        matchId,
        cooldown: currentCooldown,
        cooldownKnown,
      } = latest.current;

      if (currentExpiry) {
        const now = new Date(serverNow());
        const expiry = parseTimestamp(currentExpiry);
        if (!expiry || now >= expiry) {
          setMatchedProfile(null);
          setExpiresAt(null);
          setConversationStatus('none');
          setHasActiveConversation(false);
          // The match is gone either way, but what to say about it depends on a wait that only
          // the server knows. With no answer in hand the notice is skipped rather than guessed
          // at, so a cooldown still running is never announced as a fresh chance to match.
          if (cooldownKnown) notifyExpiredMatch(matchId, currentCooldown);
        }
      }
    }, 60000); // Check every minute

    return () => clearInterval(interval);
  }, []);

  /**
   * Handles the click event on the main heart icon.
   * Checks the user's eligibility and either shows a confirmation dialog
   * or says how long the cooldown has left if they've matched recently.
   */
  const handleHeartClick = async () => {
    if (isMatching || matchedProfile) return;

    try {
      // Asked at the click rather than trusted from mount: the page may have been left open
      // for hours, and a confirmation dialog that opens on a lapsed cooldown only becomes a
      // failed draw at the far end of the matching animation.
      const check = await fetchCooldown();
      if (check.known && check.remaining) {
        // Forced past the once-per-wait suppression: the user just clicked, and a click that
        // silently does nothing is indistinguishable from a broken button.
        notifyCooldown(check.remaining, { force: true });
        return;
      }
      if (check.known && check.cohortBlock) {
        // Refused here rather than at the end of five seconds of animation.
        //
        // This is not the cooldown case above, and the difference matters to the user in two ways.
        // Nothing is waiting, so there is no "try again in four hours" to say; and no attempt is
        // spent -- the draw endpoint refuses this before it writes anything -- so the moment an
        // administrator adds the slot, the next click works. Telling them to wait would be wrong
        // twice over: waiting does not help, and there is nothing to wait out.
        //
        // The message is the server's, so the cohort named in it is the one the operator needs.
        toast.error(check.cohortBlock.message);
        return;
      }
      if (!check.known) {
        // Opening the dialog here is what sends the user through the whole animation to be
        // refused at the end, so an unanswered check closes this click with nothing.
        toast.error("Could not confirm you are eligible to match. Please try again.");
        return;
      }
      setShowConfirmationDialog(true);
    } catch (error: any) {
      if (error.response?.status === 429) {
        // The draw endpoint is the last word on the wait, so its own message is what is shown.
        toast.info(describeApiError(error, "You have drawn recently. Please try again later."));
      } else {
        toast.error("An error occurred. Please try again later.");
      }
    }
  };


  /**
   * Initiates the matchmaking process after user confirmation.
   * Triggers animations and calls the API to find a match.
   */
  const startMatching = async () => {
    setShowConfirmationDialog(false);
    setIsMatching(true);
    setMatchedProfile(null);
    setHeartFill(0);
    setShowChat(false);
    setChatMessages([]);
    setExpiresAt(null);
    setCooldown(null);
    // The previous attempt's outcome says nothing about this one, and leaving it up would have
    // the card explaining a refusal that is no longer the reason the page is in this state.
    setDrawRefusal(null);

    const slotInterval = setInterval(() => {
      const randomIndex = Math.floor(Math.random() * mockMatches.length);
      setSlotMachineUser(mockMatches[randomIndex]);
    }, 100);

    const totalDuration = 5000;
    const intervalDuration = 50;
    const totalSteps = totalDuration / intervalDuration;
    let currentStep = 0;

    const fillInterval = setInterval(() => {
      currentStep++;
      const newFill = (currentStep / totalSteps) * 100;
      setHeartFill(newFill);

      if (currentStep >= totalSteps) {
          clearInterval(fillInterval);
          clearInterval(slotInterval);

          findMatch()
            .then(finalMatch => {
              // The backend now returns { matched_with, match_id, expires_at, is_rare_match }
              // and automatically creates a pending conversation
              const newMatchedProfile = { 
                ...finalMatch.matched_with, 
                match_id: finalMatch.match_id 
              };
              setMatchedProfile(newMatchedProfile);
              setExpiresAt(finalMatch.expires_at);
              setConversationStatus('pending'); // Automatically created as pending
              setHasActiveConversation(false); // Not active until receiver accepts
              
              // Check if this is a rare same-gender match
              if (finalMatch.is_rare_match) {
                setShowRareMatchDialog(true);
              } else {
                toast.success('Match found! 💕');
              }
              
              // Immediately show the matched profile
              console.log('Setting matched profile:', newMatchedProfile);
            })
            .catch(async error => {
              // The server's own reason, which distinguishes the two cases that matter here:
              // "no potential matches" and "your cohort has no slot". Neither is a cooldown, so
              // neither is answered with one.
              //
              // Kept as state as well as shown: the card in the right-hand column is where the
              // user is looking when they decide what to do next, and it used to go on promising
              // "Waiting for your match..." long after the draw was over -- which is a different
              // lie from the cooldown toast, and the same species of one.
              const refusal = readDrawRefusal(error);
              setDrawRefusal(refusal);
              toast.error(
                refusal?.message ?? describeApiError(error, 'Could not find a match.')
              );
              // The attempt was spent, so a wait really did start, and the page has to know that:
              // the cooldown panel and the disabled heart are both driven by this state, and
              // leaving it unset would leave the user staring at a heart that does nothing.
              //
              // Settled without announcing it. `notifyCooldown` is forced on a heart click because
              // a click that silently does nothing reads as a broken button, and there is no such
              // ambiguity here -- the user just watched five seconds of animation and was told the
              // reason in the toast above. A second toast saying they are on a cooldown would
              // outrank the answer they actually needed, and for an unconfigured cohort it is worse
              // than redundant: it tells them to wait for a draw that cannot succeed until an
              // operator configures a slot, which is the one thing the message must not suggest.
              await fetchCooldown();
            })
            .finally(() => {
              setSlotMachineUser(null);
              setIsMatching(false);
            });
      }
    }, intervalDuration);
  };

  /**
   * Sends a message in the chat and simulates a reply.
   * @param {string} message - The message to send.
   */
  const sendMessage = (message: string) => {
    setChatMessages(prev => [...prev, `You: ${message}`]);
    
    setTimeout(() => {
      const responses = ["That's so sweet! 💕", "I'd love to chat more!", "You seem really interesting!"];
      const response = responses[Math.floor(Math.random() * responses.length)];
      setChatMessages(prev => [...prev, `${matchedProfile?.name}: ${response}`]);
    }, 1000);
  };

  /**
   * The main component for the matchmaking page.
   * Handles matchmaking logic, UI state, and chat functionality.
   * @returns {JSX.Element} The MatchmakingPage component.
   */
  return (
    <div className="min-h-screen p-4 pt-24 relative overflow-hidden">
      <Navigation />
      <FloatingHearts />
      {/* Floral preview: 4016406 bottom-centered, edge to edge, sunk so the
          lower part clips off-screen. Single item only. */}
      <FloralDecor pair={2} className="left-1/2 bottom-[-14rem] w-[130rem] max-w-none -translate-x-1/2 opacity-90" />
      
      <div className="max-w-7xl mx-auto mb-8 px-4">
        <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-6">
          <div className="text-center lg:text-left">
            <h1 className="text-2xl sm:text-3xl md:text-4xl lg:text-5xl font-dancing page-heading mb-2 break-words">Random Matchmaking 💕</h1>
            <p className="text-sm sm:text-base md:text-xl status-hint break-words">Find your perfect Valentine match!</p>
          </div>
          <CountdownTimer />
        </div>
      </div>

      <div className="max-w-7xl mx-auto">
        {!showChat ? (
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-8 items-center">
            <div className="min-h-[400px] flex items-center justify-center match-frame">
                <div className="w-full max-w-sm">{user && <UserCard profile={user} />}</div>
            </div>

            <div className="flex flex-col items-center justify-center space-y-6 h-full">
              {!matchedProfile && (
                <div className="relative w-48 h-48 cursor-pointer group" onClick={handleHeartClick}>
                  <GlassHeart fillPercentage={heartFill} />
                </div>
              )}

{!isMatching && !matchedProfile && (
                // The invite to click the heart is withdrawn while the server is still
                // counting a cooldown down. It used to show regardless, which is what left a
                // terminated user reading "Click the heart to find your match!" next to a
                // toast promising them a match they could not have.
                //
                // Withdrawn for a cohort with no slot too, which is the same kind of promise for a
                // different reason: this user's draws cannot succeed at all, so an invitation to try
                // is an invitation to be refused.
                cooldown ? (
                  <div className="text-center space-y-4">
                    <div className="text-4xl">⏳</div>
                    <h2 className="text-2xl font-dancing status-title">Matchmaking on Cooldown</h2>
                    <p className="text-muted-foreground">{cooldown.message}</p>
                  </div>
                ) : cohortBlock ? (
                  <div className="text-center space-y-4">
                    <div className="text-4xl">🔒</div>
                    <h2 className="text-2xl font-dancing status-title">
                      No matchmaking slot for your cohort
                    </h2>
                    <p className="text-muted-foreground">{cohortBlock.message}</p>
                    <p className="text-sm text-muted-foreground">
                      You have not used an attempt, and you will not need to wait once a slot is set.
                    </p>
                  </div>
                ) : (
                  <div className="text-center space-y-4">
                    <h2 className="text-2xl font-dancing status-title">Click the heart to find your match! 💕</h2>
                    <p className="text-muted-foreground">Find someone special to connect with!</p>
                  </div>
                )
              )}

              {matchedProfile && conversationStatus === 'terminated' && (
                <div className="text-center space-y-4">
                  <div className="text-4xl">🚫</div>
                  <h2 className="text-2xl font-dancing status-title">Conversation Terminated</h2>
                  <p className="text-muted-foreground">
                    A moderator closed this conversation. You can still read it, but it can no
                    longer be used.
                  </p>
                  <Button
                    onClick={() => setShowConversationDialog(true)}
                    className="btn-romantic"
                    size="lg"
                  >
                    View Conversation
                  </Button>
                  {cooldown && (
                    <div className="text-sm text-muted-foreground">
                      <Clock className="w-4 h-4 inline mr-2" />
                      Time until your next match: {formatRemaining(cooldown)}
                    </div>
                  )}
                </div>
              )}

              {matchedProfile && hasActiveConversation && (
                <div className="text-center space-y-4">
                  <div className="text-4xl">💬</div>
                  <h2 className="text-2xl font-dancing status-title">Conversation Active!</h2>
                  <p className="text-muted-foreground">You have an active conversation with {matchedProfile.name}</p>
                  <Button 
                    onClick={() => setShowConversationDialog(true)}
                    className="btn-romantic"
                    size="lg"
                  >
                    Open Conversation
                  </Button>
                  {expiresAt && (
                    <div className="text-sm text-muted-foreground">
                      <Clock className="w-4 h-4 inline mr-2" />
                      Time until next match: {timeUntilNextMatch(expiresAt)}
                    </div>
                  )}
                </div>
              )}

              {matchedProfile && conversationStatus === 'requested' && (
                <div className="text-center space-y-4">
                  <div className="text-4xl">⏳</div>
                  <h2 className="text-2xl font-dancing status-title">Waiting for Response</h2>
                  <p className="text-muted-foreground">Waiting for {matchedProfile.name} to accept your message request</p>
                  {expiresAt && (
                    <div className="text-sm text-muted-foreground">
                      <Clock className="w-4 h-4 inline mr-2" />
                      Time until next match: {timeUntilNextMatch(expiresAt)}
                    </div>
                  )}
                </div>
              )}

              {matchedProfile && conversationStatus === 'rejected' && (
                <div className="text-center space-y-4">
                  <div className="text-4xl">❌</div>
                  <h2 className="text-2xl font-dancing status-title">Message Request Rejected</h2>
                  <p className="text-muted-foreground">{matchedProfile.name} rejected your message request</p>
                  {expiresAt && (
                    <div className="text-sm text-muted-foreground">
                      <Clock className="w-4 h-4 inline mr-2" />
                      Time until next match: {timeUntilNextMatch(expiresAt)}
                    </div>
                  )}
                </div>
              )}

              {matchedProfile && conversationStatus === 'pending' && (
                <div className="text-center space-y-4">
                  <div className="text-4xl">💕</div>
                  <h2 className="text-2xl font-dancing status-title">Match Found!</h2>
                  <p className="text-muted-foreground">You matched with {matchedProfile.name}!</p>
                  <Button 
                    onClick={handleSendMessageRequest}
                    className="btn-romantic"
                    size="lg"
                  >
                    Send Message Request
                  </Button>
                  {expiresAt && (
                    <div className="text-sm text-muted-foreground">
                      <Clock className="w-4 h-4 inline mr-2" />
                      Time until next match: {timeUntilNextMatch(expiresAt)}
                    </div>
                  )}
                </div>
              )}
            </div>

            <div className="flex flex-col items-center justify-center gap-4">
                <div className="min-h-[400px] w-full flex items-center justify-center match-frame">
                {isMatching && slotMachineUser ? (
                    <div className="w-full h-full flex items-center justify-center overflow-hidden relative">
                        <div className="absolute inset-0 bg-gradient-to-t from-card via-transparent to-card z-10" />
                        <div className="text-center animate-pulse" style={{ filter: 'blur(4px)' }}>
                            <ProfileAvatar
                                src={slotMachineUser.profile_picture_url}
                                name={slotMachineUser.name}
                                className="w-24 h-24 border-4 border-background shadow-lg mx-auto mb-4"
                            />
                            <h3 className="text-xl font-dancing text-romantic">{slotMachineUser.name}</h3>
                            <p className="text-sm text-muted-foreground">{cohortLabel(slotMachineUser.year, slotMachineUser.department)}</p>
                        </div>
                        <div className="absolute inset-0 bg-gradient-to-b from-card via-transparent to-card z-10" />
                    </div>
                ) : matchedProfile ? (
                    <div className="w-full max-w-sm animate-fade-in">
                        <UserCard profile={matchedProfile} />
                    </div>
                ) : noMatchReason ? (
                    // The draw is over and it produced nobody, so the card reports that rather than
                    // going on waiting. It used to say "Waiting for your match..." here, which
                    // stayed on screen indefinitely: nothing was in progress, the attempt had been
                    // spent, and the heart was withdrawn -- so it promised a match the page would
                    // not even let the user try for.
                    <div className="w-full max-w-sm text-center animate-fade-in">
                        <div className="text-4xl mb-4" aria-hidden="true">💔</div>
                        <p className="font-dancing text-xl text-romantic">
                            {cardCopy.headline}
                        </p>
                        <p className="text-sm text-muted-foreground mt-2">
                            {noMatchReason.message}
                        </p>
                        <p className="text-sm text-muted-foreground mt-2">
                            {cardCopy.guidance}
                        </p>
                    </div>
                ) : (
                    <div className="text-center text-muted-foreground">
                        <Heart className="w-16 h-16 mx-auto mb-4 opacity-30" />
                        <p className="font-dancing text-xl">Waiting for your match...</p>
                    </div>
                )}
                </div>
            </div>
          </div>
        ) : (
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
            <div className="lg:col-span-2">
              <Card className="confession-card">
                <CardHeader>
                  <div className="flex items-center gap-4">
                    <Button variant="ghost" size="sm" onClick={() => setShowChat(false)}>
                      <ArrowLeft className="w-4 h-4" />
                    </Button>
                    <CardTitle className="flex items-center gap-3 text-xl font-dancing text-romantic">
                       Chat with {matchedProfile?.name}
                    </CardTitle>
                  </div>
                </CardHeader>
                <CardContent>
                  <div className="h-64 overflow-y-auto border rounded-lg p-4 mb-4 space-y-2">
                    {conversationStatus === 'accepted' && (
                      <div className="text-center space-y-4">
                        <div className="text-2xl">💬</div>
                        <h3 className="text-xl font-semibold status-title">Conversation Active!</h3>
                        <p className="text-muted-foreground">You can now chat with your match!</p>
                        <Button 
                          onClick={() => setShowConversationDialog(true)}
                          className="btn-romantic"
                        >
                          Open Chat
                        </Button>
                      </div>
                    )}

                    {conversationStatus === 'requested' && (
                      <div className="text-center space-y-4">
                        <div className="text-2xl">⏳</div>
                        <h3 className="text-xl font-semibold text-romantic">Waiting for Response</h3>
                        <p className="text-muted-foreground">Waiting for your match to accept the message request.</p>
                      </div>
                    )}

                    {conversationStatus === 'pending' && (
                      <div className="text-center space-y-4">
                        <div className="text-2xl">💕</div>
                        <h3 className="text-xl font-semibold text-romantic">Send a Message Request!</h3>
                        <p className="text-muted-foreground">Click "Send Message Request" to start a conversation with {matchedProfile?.name}.</p>
                      </div>
                    )}

                    {conversationStatus === 'rejected' && (
                      <div className="text-center space-y-4">
                        <div className="text-2xl">❌</div>
                        <h3 className="text-xl font-semibold text-romantic">Request Rejected</h3>
                        <p className="text-muted-foreground">Your match rejected the message request.</p>
                      </div>
                    )}

                    {conversationStatus === 'accepted' && chatMessages.length === 0 && (
                        <p className="text-center text-muted-foreground font-dancing">Start the conversation! 💕</p>
                    )}
                    {conversationStatus === 'accepted' && chatMessages.map((message, index) => (
                        <div key={index} className="text-sm">
                            <span className="font-semibold text-romantic">{message}</span>
                        </div>
                    ))}
                  </div>

                  <div className="space-y-2">
                    <h4 className="font-semibold text-romantic">Quick Messages:</h4>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                      {prewrittenMessages.map((message, index) => (
                        <Button key={index} variant="outline" size="sm" onClick={() => sendMessage(message)} className="text-left h-auto p-2 text-xs" disabled={conversationStatus !== 'accepted'}>
                          {message}
                        </Button>
                      ))}
                    </div>
                  </div>
                </CardContent>
              </Card>
            </div>

            <Card className="confession-card">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-xl font-dancing text-romantic">
                  <Sparkles className="w-5 h-5" />
                  Icebreaker Cards
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                {icebreakerCards.map((card, index) => (
                  <div key={index} className={`p-3 bg-gradient-love rounded-lg ${conversationStatus === 'accepted' ? 'cursor-pointer hover:scale-105' : 'opacity-50 cursor-not-allowed'} transition-all duration-300 group`} onClick={() => conversationStatus === 'accepted' && sendMessage(card)}>
                    <p className="text-sm text-romantic-dark font-medium">{card}</p>
                    <div className="flex justify-end mt-2">
                      <Send className="w-4 h-4 text-romantic group-hover:translate-x-1 transition-transform" />
                    </div>
                  </div>
                ))}
              </CardContent>
            </Card>
          </div>
        )}
      </div>

      <AlertDialog open={showConfirmationDialog} onOpenChange={setShowConfirmationDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Confirm Matchmaking</AlertDialogTitle>
            <AlertDialogDescription>
              You can only find a new match once every 4 hours. Are you sure you want to use your attempt now?
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={startMatching}>Proceed</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Rare Same-Gender Match Dialog */}
      <AlertDialog open={showRareMatchDialog} onOpenChange={setShowRareMatchDialog}>
        <AlertDialogContent className="max-w-md">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-2xl font-bold text-center text-romantic">
              ✨ 🎉 🌟 Matrix Glitch Detected! 🌟 🎉 ✨
            </AlertDialogTitle>
            <AlertDialogDescription className="text-center space-y-4 py-4">
              <div className="text-lg font-semibold">
                You found the ultra-rare glitch in the Matrix!
              </div>
              <div className="text-base">
                You had a <span className="font-bold text-romantic">0.1% chance</span> of matching with the same gender — this almost never happens!
              </div>
              <div className="text-xl font-bold animate-pulse">
                🌈 You broke the algorithm! 🌈
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogAction onClick={() => setShowRareMatchDialog(false)}>
              Continue
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Conversation Dialog */}
      {showConversationDialog && (
        <ConversationDialog
          onClose={() => setShowConversationDialog(false)}
          onRefresh={refreshConversationStatus}
          onConversationAccepted={handleConversationAccepted}
        />
      )}
    </div>
  );
};
