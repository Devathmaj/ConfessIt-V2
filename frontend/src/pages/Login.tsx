import { useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Heart, Mail, ShieldCheck, Clock } from 'lucide-react';
import { FloatingHearts } from '@/components/ui/floating-hearts';
import { toast } from 'sonner';
import ConfessItWebP from '@/assets/ConfessIt.webp';
import ConfessItWebM from '@/assets/ConfessIt.webm';
import ConfessItMP4 from '@/assets/ConfessIt.mp4';
import { magicLinkEndpoint, SIGN_IN_CATEGORIES, type SignInCategory } from '@/lib/signInCategories';
import type { AccountType } from '@/lib/capabilities';

/**
 * One sign-in form, rendered three times.
 *
 * Which account table a person is signing in to is decided by the door they came through, not
 * by anything they type. A regno is unique only within a table, so the same registration
 * number can be a participant and a moderator at once; posting to this page's own endpoint is
 * what makes the server read the right one, and it hands back a link for exactly that account
 * or no link at all. It never searches the other two tables and never falls back to one.
 *
 * The page is a convenience, not the enforcement: the category lives in the URL the browser
 * called, so editing this component cannot make one door serve another's accounts.
 *
 * Signing in is two steps, and only the server's answer moves you between them. The first
 * screen asks for a registration number and sends it to the door's endpoint. When the answer
 * is a minted link (200) a verification-pending screen takes over: it shows the validated
 * registration number, counts the token's remaining life down from the deadline the server
 * stored with it, holds the resend button until the account cooldown that answer charged has
 * passed, and can then send the link again or start over with a different number. A 404,
 * 403, 429 or 400 never leaves the first screen -- whoever answered, nothing was minted, so
 * there is no token to verify. The pending state is kept under a localStorage key that is
 * this door's alone, so refreshing the page keeps both countdowns running instead of
 * restarting them, and an expired token shows as expired rather than silently dying.
 */

/**
 * Mirrors `LOGIN_TOKEN_EXPIRE_MINUTES` in `backend/app/config.py`. It is only a fallback:
 * the mint response now carries the token's real ``expires_at``, and that exact deadline is
 * what is persisted, so a countdown that survives a refresh still counts against the server's
 * own clock.
 */
const LOGIN_TOKEN_EXPIRE_MINUTES = 15;

/**
 * Mirrors `LOGIN_ACCOUNT_COOLDOWN_SECONDS` in `backend/app/config.py`: the wait one
 * answered request charges for its registration number. A minted link charges it like
 * anything else, so the resend button is held for this window -- and when the server
 * answers a resend with a 429 that names its own `Retry-After`, that exact count is
 * adopted over this default, keeping the button in step with the window that enforces it.
 */
const LOGIN_ACCOUNT_COOLDOWN_SECONDS = 300;

/**
 * One door's share of the persistence namespace. The pending state is keyed by account table
 * because it is about a token minted on that door: the participants door and a staff door can
 * each be mid-sign-in without confusing the other, and a refresh returns to the same door.
 */
const pendingStateKey = (accountType: AccountType): string => `login:pending:${accountType}`;

interface PendingVerification {
  authenticationState: 'verification_pending';
  validatedRegno: string;
  /** Epoch milliseconds. Kept as a timestamp, not a remaining count, so a refresh can
   *  compute what is left from the current time instead of restarting the countdown. */
  tokenExpiresAt: number;
  /** Epoch milliseconds before the resend button may be used again. The first answered
   *  request charges the account cooldown, so the button starts locked and unlocks when
   *  this passes. A timestamp for the same reason as ``tokenExpiresAt``: a refresh must
   *  not make the wait start over. */
  resendAvailableAt: number;
}

const loadPendingVerification = (accountType: AccountType): PendingVerification | null => {
  try {
    const raw = window.localStorage.getItem(pendingStateKey(accountType));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<PendingVerification> | null;
    if (
      parsed &&
      parsed.authenticationState === 'verification_pending' &&
      typeof parsed.validatedRegno === 'string' &&
      parsed.validatedRegno &&
      Number.isFinite(parsed.tokenExpiresAt)
    ) {
      return {
        authenticationState: 'verification_pending',
        validatedRegno: parsed.validatedRegno,
        tokenExpiresAt: parsed.tokenExpiresAt as number,
        // A state written before the resend cooldown existed (or one that lost the
        // field) defaults to an already-elapsed wait, so an older restore never locks
        // the resend button forever.
        resendAvailableAt: Number.isFinite(parsed.resendAvailableAt)
          ? (parsed.resendAvailableAt as number)
          : Date.now() - 1,
      };
    }
  } catch {
    // A corrupted value is treated as no pending state; it is removed on the next write.
  }
  return null;
};

const persistPendingVerification = (accountType: AccountType, state: PendingVerification) => {
  window.localStorage.setItem(pendingStateKey(accountType), JSON.stringify(state));
};

const clearPendingVerification = (accountType: AccountType) => {
  window.localStorage.removeItem(pendingStateKey(accountType));
};

const SignInPage = ({ category }: { category: SignInCategory }) => {
  const [regno, setRegno] = useState('');
  const [isLoading, setIsLoading] = useState(false);

  // The same whitelist the backend enforces (app.services.auth_service._REGNO_PATTERN):
  // Unicode letters and digits from any script, underscore, and the . / - a real regno
  // carries. Rejecting here stops the request before it is made; the server is still the
  // gate, this only stops a wasted round trip and explains the mistake in the person's
  // own language.
  const REGNO_PATTERN = /^[\p{L}\p{N}_.\-/]{1,100}$/u;

  // The verification-pending state, restored when this door is refreshed. Absent means the
  // input screen. It changes only on a server answer: a 200 puts it here, a refusal keeps it
  // null, and "change number" deliberately clears it.
  const [pending, setPending] = useState<PendingVerification | null>(() =>
    loadPendingVerification(category.accountType),
  );

  // Seconds left until the token the server stored dies, ticked from `tokenExpiresAt` rather
  // than counted down by hand, so the number a refresh shows is whatever the current time
  // makes of the same deadline.
  const [remaining, setRemaining] = useState(() =>
    pending ? Math.max(0, Math.ceil((pending.tokenExpiresAt - Date.now()) / 1000)) : 0,
  );
  const [cooldownRemaining, setCooldownRemaining] = useState(() =>
    pending ? Math.max(0, Math.ceil((pending.resendAvailableAt - Date.now()) / 1000)) : 0,
  );

  useEffect(() => {
    if (!pending) return;
    const update = () => {
      const now = Date.now();
      const tokenSeconds = Math.max(0, Math.ceil((pending.tokenExpiresAt - now) / 1000));
      const resendSeconds = Math.max(0, Math.ceil((pending.resendAvailableAt - now) / 1000));
      setRemaining(tokenSeconds);
      setCooldownRemaining(resendSeconds);
    };
    update();
    const timer = setInterval(update, 1000);
    return () => clearInterval(timer);
  }, [pending]);

  /** The countdown as a person reads a clock: ``mm:ss``, ticking every second. */
  const formatClock = (seconds: number): string => {
    const total = Math.max(0, Math.floor(seconds));
    const minutes = Math.floor(total / 60);
    return `${String(minutes).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
  };

  /** The token's deadline from a mint response, falling back on the local mirror. */
  const parseExpiry = (raw: unknown): number => {
    const parsed = typeof raw === 'string' ? Date.parse(raw) : Number.NaN;
    return Number.isFinite(parsed) ? parsed : Date.now() + LOGIN_TOKEN_EXPIRE_MINUTES * 60_000;
  };

  const requestLink = async (regnoToSend: string): Promise<void> => {
    const response = await fetch(magicLinkEndpoint(category.accountType), {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      // The API answers these requests with an opaque client-id cookie used only to
      // rate-limit login attempts per browser. Cross-origin fetches drop cookies
      // unless they opt in, so without this the server would mint a fresh identity
      // on every request and the per-browser limit would never engage.
      credentials: 'include',
      body: JSON.stringify({ regno: regnoToSend }),
    });
    return { response, body: await response.json().catch(() => null) };
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsLoading(true);

    const trimmedRegno = regno.trim();
    if (!REGNO_PATTERN.test(trimmedRegno)) {
      toast.error(
        'Registration number must be 1 to 100 characters long and may contain only letters, numbers and the punctuation . / - _.',
      );
      setIsLoading(false);
      return;
    }

    try {
      const { response, body } = await requestLink(trimmedRegno);
      if (response.ok) {
        // The server's message, not a hand-written one. A real account is told a link
        // was generated; a 404/403 explains the refusal in the error toast below, and the
        // page stays on the input screen with nothing minted.
        const message =
          typeof body?.message === 'string' ? body.message : 'Magic link sent! Check your backend console.';
        toast.success(message);
        const next: PendingVerification = {
          authenticationState: 'verification_pending',
          validatedRegno: trimmedRegno,
          tokenExpiresAt: parseExpiry(body?.expires_at),
          // The mint answered this registration number, which charged the account
          // cooldown that locks the resend button for the next five minutes.
          resendAvailableAt: Date.now() + LOGIN_ACCOUNT_COOLDOWN_SECONDS * 1000,
        };
        setPending(next);
        persistPendingVerification(category.accountType, next);
        setRegno('');
      } else {
        const detail = typeof body?.detail === 'string' ? body.detail : null;
        // A refusal mints nothing, so there is no verification to wait on. The number
        // stays on the first screen and the reason is told.
        toast.error(detail ?? 'Failed to generate magic link. Please try again.');
      }
    } catch (error) {
      toast.error('An error occurred. Please try again.');
    } finally {
      setIsLoading(false);
    }
  };

  const handleResend = async () => {
    if (!pending) return;
    setIsLoading(true);
    try {
      const { response, body } = await requestLink(pending.validatedRegno);
      if (response.ok) {
        const message =
          typeof body?.message === 'string' ? body.message : 'Magic link sent! Check your backend console.';
        toast.success(message);
        const next = {
          ...pending,
          tokenExpiresAt: parseExpiry(body?.expires_at),
          // The resend answered this registration number too, so a fresh cooldown starts.
          resendAvailableAt: Date.now() + LOGIN_ACCOUNT_COOLDOWN_SECONDS * 1000,
        };
        setPending(next);
        persistPendingVerification(category.accountType, next);
      } else if (response.status === 404 || response.status === 403) {
        // The registration number is not sign-in-able any more, so the pending state's
        // premise is gone. Back to the input screen, where the person can choose again.
        toast.error(
          typeof body?.detail === 'string' && body.detail
            ? body.detail
            : 'That registration number can no longer sign in. Please try again.',
        );
        clearPendingVerification(category.accountType);
        setPending(null);
        setRegno('');
      } else {
        // Most likely 429: the cooldown is running. When the server names the exact wait
        // on its window, the countdown adopts it so the button stays in step with the
        // authority that enforces the window. Either way the existing token is still
        // counting down, so the pending screen stays and the reason is told.
        const retryAfter = Number(response.headers.get('Retry-After'));
        if (Number.isFinite(retryAfter) && retryAfter > 0) {
          const next = { ...pending, resendAvailableAt: Date.now() + retryAfter * 1000 };
          setPending(next);
          persistPendingVerification(category.accountType, next);
        }
        toast.error(
          typeof body?.detail === 'string' && body.detail
            ? body.detail
            : 'Too many requests. Please wait, then try again.',
        );
      }
    } catch (error) {
      toast.error('An error occurred. Please try again.');
    } finally {
      setIsLoading(false);
    }
  };

  const handleChangeNumber = () => {
    // Deliberately starts over: the old token is abandoned (it is per-registration-number)
    // and there is nothing valid to wait on until the next number is accepted.
    clearPendingVerification(category.accountType);
    setPending(null);
    setRegno('');
  };

  const form = (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div className="space-y-2">
        <div className="relative">
          <Mail className="absolute left-3 top-3 h-5 w-5 text-muted-foreground" />
          <Input
            type="text"
            placeholder={category.regnoPlaceholder}
            value={regno}
            onChange={(e) => setRegno(e.target.value)}
            maxLength={100}
            disabled={isLoading}
            className={
              category.themed
                ? 'pl-10 bg-background/80 border-romantic/30 focus:border-romantic'
                : 'pl-10'
            }
            required
          />
        </div>
      </div>

      <Button
        type="submit"
        className={category.themed ? 'w-full btn-romantic text-lg font-semibold' : 'w-full'}
        disabled={isLoading}
      >
        {isLoading ? category.pendingCta : category.cta}
      </Button>
    </form>
  );

  const verificationPanel = pending && (
    <div className="space-y-4 text-center">
      <div>
        <p className="text-muted-foreground">We've sent a magic link to:</p>
        <p className="mt-1 font-semibold break-all">{pending.validatedRegno}</p>
        <p className="mt-2 text-sm text-muted-foreground">Is this your registration number?</p>
      </div>

      {remaining > 0 ? (
        <p role="timer" className="text-sm text-muted-foreground">
          Token expires in: {formatClock(remaining)}
        </p>
      ) : (
        <p className="text-sm text-amber-600">
          {cooldownRemaining > 0
            ? 'Token expired.'
            : 'Token expired. You can request a new one now.'}
        </p>
      )}

      <Button
        type="button"
        variant="outline"
        className={category.themed ? 'w-full btn-romantic text-lg font-semibold' : 'w-full'}
        onClick={handleResend}
        disabled={isLoading || cooldownRemaining > 0}
      >
        {isLoading ? 'Sending...' : 'Resend Your Magic Link'}
      </Button>

      {cooldownRemaining > 0 && (
        <p role="timer" className="text-center text-xs text-muted-foreground">
          Resend available in {formatClock(cooldownRemaining)}.
        </p>
      )}

      <button
        type="button"
        disabled={isLoading}
        onClick={handleChangeNumber}
        className="w-full text-sm text-muted-foreground underline underline-offset-4 hover:text-foreground"
      >
        Wrong number? Change registration number
      </button>
    </div>
  );

  // The participant door keeps the full Valentine's treatment. Staff doors are plain on
  // purpose: a moderator reaching for the moderation console should not be looking at the
  // same screen a student sees, and there is no reason for a staff door to be pretty.
  if (!category.themed) {
    return (
      <div className="min-h-screen flex items-center justify-center p-4 bg-muted/30">
        <div className="w-full max-w-md z-10">
          <Card>
            <CardHeader className="text-center space-y-3">
              <div className="mx-auto w-16 h-16 bg-muted rounded-full flex items-center justify-center">
                {pending ? (
                  <Clock className="w-8 h-8 text-muted-foreground" />
                ) : (
                  <ShieldCheck className="w-8 h-8 text-muted-foreground" />
                )}
              </div>
              <CardTitle className="text-2xl">
                {pending ? 'Verification Pending' : category.title}
              </CardTitle>
              <CardDescription>
                {pending
                  ? "Confirm the number below, then open the link from the backend console."
                  : category.tagline}
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-6">{pending ? verificationPanel : form}</CardContent>
          </Card>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center p-4 relative overflow-hidden">
      <div className="absolute top-0 left-0 w-full h-full z-0">
        <video
          poster={ConfessItWebP}
          autoPlay
          loop
          muted
          playsInline
          className="w-full h-full object-cover"
        >
          <source src={ConfessItWebM} type="video/webm" />
          <source src={ConfessItMP4} type="video/mp4" />
          Your browser does not support the video tag.
        </video>
        <div className="absolute top-0 left-0 w-full h-full bg-black/50"></div>
      </div>

      <FloatingHearts />

      <div className="w-full max-w-md z-10">
        <Card className="confession-card border-2 border-romantic/20 bg-background/80 backdrop-blur-sm">
          <CardHeader className="text-center space-y-4">
            <div className="mx-auto w-20 h-20 bg-gradient-romantic rounded-full flex items-center justify-center mb-4">
              {pending ? (
                <Clock className="w-10 h-10 text-white" />
              ) : (
                <Heart className="w-10 h-10 text-white animate-pulse-heart" fill="currentColor" />
              )}
            </div>
            <CardTitle className="text-4xl font-dancing text-romantic">
              {pending ? 'Verification Pending' : category.title}
            </CardTitle>
            <CardDescription className="text-lg text-muted-foreground">
              {pending
                ? "Confirm the number below, then open the link from the backend console."
                : category.tagline}
            </CardDescription>
          </CardHeader>

          <CardContent className="space-y-6">
            {pending ? verificationPanel : form}

            <div className="text-center mt-6 text-sm text-muted-foreground">
              <p className="sparkles">Made with love for Valentine's Day 2026</p>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
};

/** Participant door. A regno here is looked up in the `users` table and nowhere else. */
export const Login = () => <SignInPage category={SIGN_IN_CATEGORIES.users} />;

/** Admin door. Looks the regno up in the `admin` table and nowhere else. */
export const AdminLogin = () => <SignInPage category={SIGN_IN_CATEGORIES.admin} />;

/** Moderator door. Looks the regno up in the `moderator` table and nowhere else. */
export const ModeratorLogin = () => <SignInPage category={SIGN_IN_CATEGORIES.moderator} />;