import { useEffect, useState } from 'react'; // Import useState
import { useSearchParams, useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
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
  const { verifyMagicToken } = useAuth();
  const [isVerifying, setIsVerifying] = useState(false); // Add a state to track verification

  useEffect(() => {
    // Used to extract the token from the URL when the component loads.
    const token = searchParams.get('token');

    // This check prevents the verification from running more than once.
    if (isVerifying || !token) {
      if (!token && !isVerifying) {
        toast.error('No verification token found in the link.');
        navigate('/login');
      }
      return;
    }

    const verify = async () => {
      setIsVerifying(true); // Set the flag to true before starting verification
      
      // Used to call the authentication context to verify the token with the backend.
      const result = await verifyMagicToken(token);
      
      if (result.success && result.redirectUrl) {
        discardPendingLoginState();
        toast.success('Login successful! Welcome back.');
        // Used to redirect the user to the correct dashboard on success.
        navigate(result.redirectUrl, { replace: true });
      } else {
        toast.error('Your magic link is invalid or has expired.');
        // Used to send the user back to the login page on failure.
        navigate('/login', { replace: true });
      }
    };

    verify();
  }, [searchParams, navigate, verifyMagicToken, isVerifying]);

  return (
    <div className="min-h-screen flex items-center justify-center bg-background text-foreground">
      <div className="text-center">
        <p className="text-xl font-semibold">Verifying your magic link...</p>
        <p className="text-muted-foreground">Please wait a moment.</p>
      </div>
    </div>
  );
};
