import { useCallback, useEffect, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Mail, ShieldCheck, Clock } from 'lucide-react';
import { FloatingHearts } from '@/components/ui/floating-hearts';
import { ScatteredIcons } from '@/components/ScatteredIcons';
import ConfessItWebP from '@/assets/ConfessIt.webp';
import ConfessItWebM from '@/assets/ConfessIt.webm';
import ConfessItMP4 from '@/assets/ConfessIt.mp4';
import { HeartLocket, type LocketState } from '@/components/heart-locket/HeartLocket';
import { magicLinkEndpoint, SIGN_IN_CATEGORIES, type SignInCategory } from '@/lib/signInCategories';
import { warmStaticAssets } from '@/lib/assetWarmup';
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
 * screen asks for a registration number and sends it to the door's endpoint. The server
 * answers every well-formed request identically (200) whether the number is registered,
 * blocked or unknown -- telling those apart would turn the form into an oracle for
 * account existence -- so a 200 always opens the verification-pending screen: it shows
 * the submitted registration number, keeps the link's expiry deadline for the expired
 * state, holds the resend button until the account cooldown that answer charged has
 * passed, and can then send the link again or start over with a different number. A 429
 * or 400 never leaves the first screen -- neither mints anything, and neither varies with
 * account state, so there is no token to verify and nothing to learn. The pending state is
 * kept under a localStorage key that is this door's alone, so refreshing the page keeps
 * the resend cooldown running and the token expiry intact instead of restarting them.
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
 * Fifteen minutes matches the magic-link lifetime, so a resend unlocks as the current
 * link expires.
 */
const LOGIN_ACCOUNT_COOLDOWN_SECONDS = 900;

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
  useEffect(() => {
    void warmStaticAssets();
  }, []);

  const [regno, setRegno] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

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

  // The locket is a separate explicit state machine from the authentication request. A
  // restored pending sign-in opens already open; a live submission goes through `opening`
  // before the verification screen becomes interactive.
  const [locketState, setLocketState] = useState<LocketState>(() => (pending ? 'open' : 'closed'));

  // The animation callbacks can arrive after a later state change (a transition event and
  // the fallback timer can both fire). These refs make a stale completion a no-op and stop
  // the close handler from clearing a pending sign-in twice.
  const locketStateRef = useRef<LocketState>(locketState);
  const closingHandledRef = useRef(false);
  useEffect(() => {
    locketStateRef.current = locketState;
  }, [locketState]);

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

  /** Whole seconds left until a deadline, never negative. */
  const secondsUntil = (deadline: number): number =>
    Math.max(0, Math.ceil((deadline - Date.now()) / 1000));

  /** The token's deadline from a mint response, falling back on the local mirror. */
  const parseExpiry = (raw: unknown): number => {
    const parsed = typeof raw === 'string' ? Date.parse(raw) : Number.NaN;
    return Number.isFinite(parsed) ? parsed : Date.now() + LOGIN_TOKEN_EXPIRE_MINUTES * 60_000;
  };

  // The annotation says what the function actually returns (the response plus its
  // parsed body, `null` when the body is not JSON): callers destructure both, so the
  // previous `Promise<void>` was simply wrong, not a contract anyone relied on. The
  // body stays `any` -- it is unshaped JSON, and every reader already narrows what it
  // touches with `typeof` checks.
  const requestLink = async (
    regnoToSend: string
  ): Promise<{ response: Response; body: any }> => {
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
      signal: AbortSignal.timeout(15000),
    });
    return { response, body: await response.json().catch(() => null) };
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (isLoading || locketState !== 'closed') return;
    setIsLoading(true);
    setFormError(null);

    const trimmedRegno = regno.trim();
    if (!REGNO_PATTERN.test(trimmedRegno)) {
      setFormError('Use 1-100 letters, numbers or . / - _.');
      setIsLoading(false);
      return;
    }

    try {
      const { response, body } = await requestLink(trimmedRegno);
      if (response.ok) {
        // The server answers every well-formed request identically, whether the number
        // is registered or not, so there is nothing to branch on here: a 200 always
        // opens the pending screen, and only a real mint produces a working link.
        const next: PendingVerification = {
          authenticationState: 'verification_pending',
          validatedRegno: trimmedRegno,
          tokenExpiresAt: parseExpiry(body?.expires_at),
          // The answer charged this registration number's account cooldown, so the
          // resend button is locked for the next fifteen minutes.
          resendAvailableAt: Date.now() + LOGIN_ACCOUNT_COOLDOWN_SECONDS * 1000,
        };
        setPending(next);
        setRemaining(secondsUntil(next.tokenExpiresAt));
        setCooldownRemaining(secondsUntil(next.resendAvailableAt));
        persistPendingVerification(category.accountType, next);
        setRegno('');
        setLocketState(category.themed ? 'opening' : 'open');
      } else {
        // Fixed sentence for every refusal (429, 400, anything else): the server's
        // `detail` varies by cause, and rendering it would let callers distinguish
        // outcomes -- including cooldown states -- per registration number.
        setFormError('Failed to generate magic link. Please try again.');
      }
    } catch (error) {
      setFormError('An error occurred. Please try again.');
    } finally {
      setIsLoading(false);
    }
  };

  const handleResend = async () => {
    if (!pending || locketState !== 'open' || isLoading) return;
    setIsLoading(true);
    setFormError(null);
    try {
      const { response, body } = await requestLink(pending.validatedRegno);
      if (response.ok) {
        const next = {
          ...pending,
          tokenExpiresAt: parseExpiry(body?.expires_at),
          // The resend answered this registration number too, so a fresh cooldown starts.
          resendAvailableAt: Date.now() + LOGIN_ACCOUNT_COOLDOWN_SECONDS * 1000,
        };
        setPending(next);
        setRemaining(secondsUntil(next.tokenExpiresAt));
        setCooldownRemaining(secondsUntil(next.resendAvailableAt));
        persistPendingVerification(category.accountType, next);
      } else {
        // Most likely 429: the cooldown is running. When the server names the exact wait
        // on its window, the countdown adopts it so the button stays in step with the
        // authority that enforces the window. Either way the pending screen stays -- the
        // server never reports a number as un-sign-in-able, so there is no outcome that
        // ends the wait -- and the message stays fixed: the server's sentence varies by
        // cause and must not become per-number recon.
        const retryAfter = Number(response.headers.get('Retry-After'));
        if (Number.isFinite(retryAfter) && retryAfter > 0) {
          const next = { ...pending, resendAvailableAt: Date.now() + retryAfter * 1000 };
          setPending(next);
          setCooldownRemaining(secondsUntil(next.resendAvailableAt));
          persistPendingVerification(category.accountType, next);
        }
        setFormError('Too many requests. Please wait, then try again.');
      }
    } catch (error) {
      setFormError('An error occurred. Please try again.');
    } finally {
      setIsLoading(false);
    }
  };

  const clearPendingSignIn = useCallback(() => {
    clearPendingVerification(category.accountType);
    setPending(null);
    setRegno('');
    setFormError(null);
    setRemaining(0);
    setCooldownRemaining(0);
  }, [category.accountType]);

  const handleOpeningFinished = useCallback(() => {
    if (locketStateRef.current !== 'opening') return;
    setLocketState('open');
  }, []);

  const handleClosingFinished = useCallback(() => {
    if (locketStateRef.current !== 'closing' || closingHandledRef.current) return;
    closingHandledRef.current = true;

    // The pending state is discarded only once the lid has covered it. Clearing it when
    // the link is clicked would swap the verification screen for the login form while the
    // exterior was still rotating in, which is exactly the flicker this transition avoids.
    clearPendingSignIn();
    setLocketState('closed');
  }, [clearPendingSignIn]);

  const handleChangeNumber = () => {
    if (!pending) return;

    // Staff doors have no locket; their "change number" is the original immediate reset.
    if (!category.themed) {
      clearPendingSignIn();
      setLocketState('closed');
      return;
    }

    // Deliberately starts the close instead of clearing immediately: the old token stays
    // live until the login form is visible again, and the pending state is abandoned then.
    if (locketState !== 'open') return;
    closingHandledRef.current = false;
    setFormError(null);
    setLocketState('closing');
  };

  const loginFields = (isLocket: boolean) => (
    <form onSubmit={handleSubmit} className={isLocket ? 'locket-login__form' : 'space-y-4'}>
      <div className={isLocket ? undefined : 'space-y-2'}>
        <div className={isLocket ? undefined : 'relative'}>
          {!isLocket && <Mail className="absolute left-3 top-3 h-5 w-5 text-muted-foreground" />}
          <Input
            type="text"
            placeholder={category.regnoPlaceholder}
            value={regno}
            onChange={(e) => {
              setRegno(e.target.value);
              setFormError(null);
            }}
            maxLength={100}
            disabled={isLoading || locketState !== 'closed'}
            aria-label={category.regnoPlaceholder}
            aria-invalid={formError ? true : undefined}
            aria-describedby={formError && isLocket ? 'locket-login-error' : undefined}
            className={isLocket ? 'locket-input' : 'pl-10'}
            required
          />
        </div>
      </div>

      {formError && !isLocket && (
        <p role="alert" className="text-sm text-destructive">
          {formError}
        </p>
      )}

      <Button
        type="submit"
        className={isLocket ? 'locket-submit' : 'w-full'}
        disabled={isLoading || locketState !== 'closed'}
      >
        {isLoading ? category.pendingCta : category.cta}
      </Button>
    </form>
  );

  const renderLoginForm = (isLocket: boolean) => {
    if (!isLocket) return loginFields(false);

    return (
      <div className="locket-login">
        <h1 className="locket-login__brand">{category.title}</h1>
        <div className="locket-login__subline">
          {formError ? (
            <p id="locket-login-error" role="alert" className="locket-login__error">
              {formError}
            </p>
          ) : (
            <p className="locket-login__tagline">{category.tagline}</p>
          )}
        </div>
        {loginFields(true)}
        <p className="locket-login__footer">Made with love for Valentine's Day 2026</p>
      </div>
    );
  };

  const renderVerification = (isLocket: boolean) => {
    if (!pending) return null;

    return (
      <div
        className={
          isLocket
            ? `locket-verification${formError ? ' locket-verification--error' : ''}`
            : 'space-y-4 text-center'
        }
      >
        {isLocket && <h2>Verification Pending</h2>}

        {isLocket ? (
          <p className="locket-verification__copy">Magic link sent to your email.</p>
        ) : (
          <div className="space-y-1">
            <p className="text-muted-foreground">We've sent a magic link to your email.</p>
            {!formError && (
              <p className="text-sm text-muted-foreground">
                The link is only valid for {LOGIN_TOKEN_EXPIRE_MINUTES} minutes.
              </p>
            )}
          </div>
        )}

        {isLocket ? (
          remaining <= 0 && (
            <p className="locket-verification__expired">
              {cooldownRemaining > 0
                ? 'The link has expired.'
                : 'The link has expired. You can request a new one now.'}
            </p>
          )
        ) : remaining > 0 ? (
          <p role="timer" className="text-sm text-muted-foreground">
            Expires in: {formatClock(remaining)}
          </p>
        ) : (
          <p className="text-sm text-amber-600">
            {cooldownRemaining > 0
              ? 'The link has expired.'
              : 'The link has expired. You can request a new one now.'}
          </p>
        )}

        <Button
          type="button"
          variant={isLocket ? 'default' : 'outline'}
          className={isLocket ? 'locket-resend' : 'w-full'}
          onClick={handleResend}
          disabled={isLoading || cooldownRemaining > 0 || locketState !== 'open'}
        >
          {isLoading ? 'Sending...' : 'Resend Your Magic Link'}
        </Button>

        {cooldownRemaining > 0 && (
          <p
            role="timer"
            className={
              isLocket
                ? 'locket-verification__cooldown'
                : 'text-center text-xs text-muted-foreground'
            }
          >
            Resend available in {formatClock(cooldownRemaining)}.
          </p>
        )}

        {formError && (
          <p
            role="alert"
            className={isLocket ? 'locket-verification__error' : 'text-sm text-destructive'}
          >
            {formError}
          </p>
        )}

        <div className={isLocket ? undefined : 'space-y-2'}>
          {isLocket ? (
            <p className="locket-verification__wrong">
              Entered{' '}
              <span className="locket-verification__regno" title={pending.validatedRegno}>
                {pending.validatedRegno}
              </span>
              .
            </p>
          ) : (
            <p className="text-sm text-muted-foreground">
              Wrong registration number? You entered{' '}
              <span className="font-semibold break-all">{pending.validatedRegno}</span>.
            </p>
          )}
          <button
            type="button"
            disabled={isLoading || locketState !== 'open'}
            onClick={handleChangeNumber}
            className={
              isLocket
                ? 'locket-verification__change'
                : 'w-full text-sm text-muted-foreground underline underline-offset-4 hover:text-foreground'
            }
          >
            {isLocket ? (
              <>
                <span className="locket-verification__change-long">
                  Wrong number? Change registration number
                </span>
                <span className="locket-verification__change-short">
                  Change registration number
                </span>
              </>
            ) : (
              'Change registration number'
            )}
          </button>
        </div>
      </div>
    );
  };

  // The participant door keeps the full Valentine's treatment. Staff doors are plain on
  // purpose: a moderator reaching for the moderation console should not be looking at the
  // same screen a student sees, and there is no reason for a staff door to be pretty.
  if (!category.themed) {
    return (
      <div className="min-h-screen flex items-center justify-center p-4 bg-muted/30">
        <div className="relative w-full max-w-md z-10">
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
                  ? 'Confirm the registration number below, then open the link from the backend console.'
                  : category.tagline}
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-6">
              {pending ? renderVerification(false) : renderLoginForm(false)}
            </CardContent>
          </Card>
        </div>
      </div>
    );
  }

  return (
    <div className="heart-locket-page">
      <div className="heart-locket-page__backdrop">
        <video
          poster={ConfessItWebP}
          preload="auto"
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
        <div className="heart-locket-page__veil"></div>
      </div>

      <FloatingHearts />
      <ScatteredIcons variant="romance" front />

      <main className="heart-locket-page__content">
        <HeartLocket
          state={locketState}
          loginContent={
            locketState === 'closed' || locketState === 'opening'
              ? renderLoginForm(true)
              : null
          }
          verificationContent={renderVerification(true)}
          onOpeningFinished={handleOpeningFinished}
          onClosingFinished={handleClosingFinished}
        />
      </main>
    </div>
  );
};

/** Participant door. A regno here is looked up in the `users` table and nowhere else. */
export const Login = () => <SignInPage category={SIGN_IN_CATEGORIES.users} />;

/** Admin door. Looks the regno up in the `admin` table and nowhere else. */
export const AdminLogin = () => <SignInPage category={SIGN_IN_CATEGORIES.admin} />;

/** Moderator door. Looks the regno up in the `moderator` table and nowhere else. */
export const ModeratorLogin = () => <SignInPage category={SIGN_IN_CATEGORIES.moderator} />;
