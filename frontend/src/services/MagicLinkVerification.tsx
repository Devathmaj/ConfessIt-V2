import { useEffect, useRef, useState } from 'react'; // Import useState
import { useSearchParams, useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { refreshAccessToken } from './api';
import { toast } from 'sonner';

/**
 * The pending sign-in state the login doors keep (`login:pending:<door>`) must not survive a
 * successful login: once the magic link is redeemed the person is signed in, and a later
 * log-out has to land back on the registration-number input screen, not on a stale
 * verification-pending screen that still believes a link was on its way.
 *
 * Every door's key is cleared rather than only the one that just signed in. That is
 * deliberate: any pending sign-in is moot once a session exists, and the verify response
 * does not say which door redeemed it, so a door-scoped clear would have to guess.
 */
const discardPendingLoginState = () => {
  try {
    Object.keys(window.localStorage)
      .filter((key) => key.startsWith('login:pending:'))
      .forEach((key) => window.localStorage.removeItem(key));
  } catch {
    // A storage that is unavailable has nothing left to clear.
  }
};

export const MagicLinkVerification = () => {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { verifyMagicToken, fetchAndSetUser } = useAuth();
  const [isVerifying, setIsVerifying] = useState(false); // Add a state to track verification
  // The magic-link token is single-use server-side: a second redemption always fails
  // with "already used". This ref is set synchronously before the first request fires,
  // so even if the effect re-runs (verifyMagicToken is not referentially stable, and
  // StrictMode double-invokes effects in dev) the token is redeemed exactly once. The
  // old isVerifying-state guard raced: two rapid effect runs could both pass it before
  // either state update committed, burning the token and bouncing a fresh login back
  // to /login with a spurious "invalid or expired" toast.
  const attemptedTokenRef = useRef<string | null>(null);

  useEffect(() => {
    // Used to extract the token from the URL when the component loads.
    const token = searchParams.get('token');

    if (!token) {
      toast.error('No verification token found in the link.');
      navigate('/login');
      return;
    }

    // This check prevents the verification from running more than once per token.
    if (attemptedTokenRef.current === token) {
      return;
    }
    attemptedTokenRef.current = token;

    // After a failed redemption, check whether a live session exists anyway (the token
    // lost a same-token race but the login succeeded). Returns true when the session was
    // recovered. Only the genuinely-bad-link case falls through to the error toast, so a
    // race never reports "expired" to a person who is actually signed in.
    const recoverRacedSession = async (): Promise<boolean> => {
      const restored = await refreshAccessToken();
      if (!restored) return false;
      await fetchAndSetUser(restored);
      return true;
    };

    const verify = async () => {
      setIsVerifying(true); // Set the flag to true before starting verification

      // Used to call the authentication context to verify the token with the backend.
      const result = await verifyMagicToken(token);

      // Stable toast ids: sonner replaces (rather than stacks) a toast with the same
      // id, so a double-fired flow -- two tabs redeeming, a re-run effect, a resent link
      // opened twice -- shows one "login successful" instead of two stacked copies.
      if (result.success && result.redirectUrl) {
        discardPendingLoginState();
        toast.success('Login successful! Welcome back.', { id: 'login-success' });
        // Used to redirect the user to the correct dashboard on success.
        navigate(result.redirectUrl, { replace: true });
      } else if (await recoverRacedSession()) {
        // The redemption lost a same-token race (a second tab, a re-fired effect)
        // while the login itself succeeded: the refresh cookie is shared
        // browser-wide, so the live session below is ours. Land on `/` and let the
        // route guards send the account to its own dashboard.
        discardPendingLoginState();
        toast.success('Login successful! Welcome back.', { id: 'login-success' });
        navigate('/', { replace: true });
      } else {
        toast.error('Your magic link is invalid or has expired.', { id: 'login-error' });
        // Used to send the user back to the login page on failure.
        navigate('/login', { replace: true });
      }
    };

    verify();
    // verifyMagicToken intentionally omitted: its identity changes every render, which
    // re-ran this effect and redeemed the single-use token a second time. The ref above
    // is the once-only guard instead.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams, navigate]);

  return (
    <div className="min-h-screen flex items-center justify-center bg-background text-foreground">
      <div className="text-center">
        <p className="text-xl font-semibold">Verifying your magic link...</p>
        <p className="text-muted-foreground">Please wait a moment.</p>
      </div>
    </div>
  );
};
