// src/contexts/AuthContext.tsx

import React, { createContext, useContext, useState, ReactNode, useEffect, useCallback, useRef } from 'react';
import { toast } from 'sonner';
import { getCurrentUser, serverLogout, refreshAccessToken, setInMemoryToken, clearInMemoryToken, isTokenRejected } from '../services/api';
import { API_URL } from '../lib/apiBase';
import { purgeLegacyPersistedToken, subscribeToAccessToken } from '../lib/tokenStore';
import { announceSessionEnded, onSessionEnded } from '../lib/sessionChannel';
import { awaitPendingRefresh, invalidatePendingRefreshes } from '../lib/sessionRefresh';
import { AUTH_EXPIRED_EVENT, SESSION_SUPERSEDED_EVENT } from '../lib/authEvents';
import { signInPathFor } from '../lib/signInCategories';
import { AccountType, asAccountType, Capability, can } from '../lib/capabilities';
import type { UserGender } from '../lib/accountFields';


// Used to define the structure of the User object
export interface User {
  id: string;
  name: string;
  regno: string;
  /** Required on every account, so the server always sends one. */
  email: string;
  bio?: string;
  /**
   * Year of study and department. Participant-only: a staff account has no row in `users`, so the
   * server omits both rather than sending nulls for values the row does not have.
   */
    year?: number;
  department?: string;
  profile_picture_url?: string;
  /**
   * One of `USER_GENDER_VALUES`, so the three are the only possibilities.
   *
   * Optional because only a participant has one: the staff tables carry no gender column, so
   * `/auth/me` answers without the key for an admin or a moderator rather than with a value
   * the row does not hold.
   */
  gender?: UserGender;
  interests?: string[];
  matchmaking_enabled?: boolean;
  notifications_enabled?: boolean;
  love_notes_receive_enabled?: boolean;
  love_notes_send_enabled?: boolean; // Used to track if the user has sent a love note
  /**
   * Which account table this account lives in, as decided by the server. It is a fact about
   * storage rather than a privilege: `users` is a participant and the other two are staff.
   * Nothing here should branch on it to decide what to show -- that is what `capabilities`
   * is for -- but the admin screens do echo it back, so a staff account is labelled with the
   * table it came from rather than a stale role name.
   */
  account_type?: AccountType;
  /**
   * What this account may reach, as decided by the server. Screens and menu entries are
   * gated on these rather than on `account_type`, so the client holds no privilege table of
   * its own. See src/lib/capabilities.ts.
   */
  capabilities?: string[];
  /** Whether the server reports this account as staff. Informational, for labels only. */
  is_staff?: boolean;
  is_blocked?: boolean;
  /**
   * When the participant last replaced their profile picture, as ISO text.
   *
   * Participant-only, like the picture itself: a staff row has no upload route, so the
   * server omits the key rather than sending null for a column the row does not have. Read
   * by the Profile screen to show the three-day wait before a file is chosen; the refusal
   * itself is the server's to give. See src/lib/pictureCooldown.ts.
   */
  last_profile_picture_at?: string | null;
}

export interface VerifyTokenResult {
  success: boolean;
  redirectUrl?: string;
}

/**
 * What came of a read of the signed-in account.
 *
 * Three answers for three different situations, and the boot sequence acts on
 * the difference:
 *
 * * `read` -- the account was read; the session is live and the app is signed in.
 * * `rejected` -- the server refused the token (or there was none): the session
 *   is over, and it ends.
 * * `unavailable` -- the *request* failed: a timeout, a dropped connection, a 500
 *   while the backend is deploying. The token was never judged, so the session is
 *   still alive, and another attempt is worth making.
 *
 * The last one is why this is not a boolean. A transport failure used to be read
 * as a refusal, which ended the session -- and the logout revokes it server-side
 * too, so a backend having a bad moment for half a minute signed the person out
 * for good.
 */
export type AccountRead = 'read' | 'rejected' | 'unavailable';

/**
 * How long to wait before each attempt to read the account again, after the
 * request itself failed.
 *
 * Bounded and spaced on purpose: the point is to ride out a backend that is
 * deploying or a network that blipped, not to keep a browser hammering a backend
 * that is down. The delays grow, because a backend that is coming back usually
 * takes a moment longer than one that was only briefly unreachable.
 */
const ACCOUNT_RECOVERY_DELAYS_MS = [2_000, 5_000, 10_000];

interface AuthContextType {
  user: User | null;
  /**
   * End the session: locally, on the server, and in this browser's other tabs.
   *
   * Answers when the revocation has been sent, so a caller that needs "signed
   * out" to mean "the server session is over" can wait for it.
   */
  logout: () => Promise<void>;
  isAuthenticated: boolean;
  isLoading: boolean;
  token: string | null;
  verifyMagicToken: (token: string) => Promise<VerifyTokenResult>;
  /**
   * Read the signed-in account and put it in the context.
   *
   * Answers what came of the read, because the boot sequence has to act on the
   * difference between a refused token and a request that failed.
   */
  fetchAndSetUser: (token: string) => Promise<AccountRead>;
  /** Whether the signed-in account holds `capability`. False whenever nobody is signed in. */
  can: (capability: Capability) => boolean;
  /**
   * The door to send someone to when their session ends.
   *
   * Survives the logout that ends the session, which is the entire point: by the time a
   * logout completes there is no signed-in account left to ask, and this is the only record
   * of which table the person was in. A moderator returns to the moderator door and a
   * participant to the participant one.
   */
  signInPath: string;
}

// Exported so route guards can be rendered against a supplied session in tests.
export const AuthContext = createContext<AuthContextType | undefined>(undefined);


export const AuthProvider = ({ children }: { children: ReactNode }) => {
  const [user, setUser] = useState<User | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  // The token in this context is a mirror of the store, never a copy of it.
  //
  // It used to be written twice -- at boot and after a magic-link sign-in -- and
  // never again, while the interceptor rotated the token in the store every time
  // it met a 401. Half an hour into a session the context held a bearer the server
  // had already expired, and the chat paths, which read the context's copy, sent
  // it: the ticket exchange answered 401, and the refusal was read as the end of
  // the session. Half an hour of use, and the person was signed out while their
  // refresh cookie was still good for another day.
  //
  // So the store announces every rotation and this context follows it. One token,
  // read from one place, at the moment it is used.
  useEffect(() => subscribeToAccessToken(setToken), []);

  // Which door this browser last signed in through, kept as a ref rather than read off `user`.
  // A logout clears the user, and a route guard that fires afterwards has nothing left to
  // inspect -- without this it would send every account to /login, and a moderator's regno
  // typed there would be refused by a table that does not hold it.
  //
  // A ref, and not state, for two reasons. It only has to outlive the state change rather than
  // drive a render, so recording it costs no extra render on every sign-in. And it keeps
  // `logout` referentially stable: making it depend on `user` would recreate it on every
  // session change, which cascades into `fetchAndSetUser` and then into the effect that runs
  // the initial sign-in -- re-running that effect sets a fresh user object, which recreates
  // `logout` again. The guards re-render on the very logout that clears the user, so a ref is
  // read at the moment the answer is needed.
  const lastAccountType = useRef<AccountType | undefined>(undefined);

  const logout = useCallback(async (): Promise<void> => {
    // Signing out, in the order that survives a refresh being in flight.
    //
    // The old code fired the revocation and cleared the local session at once,
    // and the server did the rest whenever it got there. Two races came out of
    // that, and both are the same race seen from two sides:
    //
    // * the refresh that was already in flight answered *after* the log out and
    //   put its token back into the store, so the person was signed in again
    //   without doing anything;
    // * the revocation carried the cookie value the refresh had already
    //   replaced, so the server found nothing to revoke and the session it had
    //   just extended stayed alive -- while the browser held the newer cookie,
    //   and a reload quietly signed the person back in.
    //
    // So: the pending work is invalidated first (a refresh that lands now has
    // nowhere to put its token), the local session is cleared (the screen says
    // so at once), the in-flight refresh is awaited (so the revocation carries
    // the value the browser actually holds), and the revocation is awaited in
    // turn (so "signed out" means the server session is over, not merely that
    // the screen says so).
    invalidatePendingRefreshes();
    setUser(null);
    clearInMemoryToken();

    await awaitPendingRefresh();
    await serverLogout();

    // The other tabs of this browser share the refresh cookie, so they have
    // lost this session whether they know it or not. Telling them is the
    // difference between one tab signing out and the browser pretending it did.
    announceSessionEnded();
  }, []);

  // Another tab ended the session. The cookie is shared, so this tab has lost
  // it too: the token goes, and the signed-in account with it. The server
  // session is already revoked by the tab that signed out, so there is nothing
  // to revoke again here.
  useEffect(
    () =>
      onSessionEnded(() => {
        clearInMemoryToken();
        setUser(null);
      }),
    [],
  );

  const fetchAndSetUser = useCallback(async (currentToken: string): Promise<AccountRead> => {
    if (!currentToken) {
      setIsLoading(false);
      return 'rejected';
    }
    try {
      const userData = await getCurrentUser(currentToken);
      
      // Used to robustly get the user ID, whether the backend sends 'id' or 'id'
      const userId = userData.id;

      if (userId) {
        const accountType = asAccountType(userData.account_type);
        // Remembered on the way in as well as on the way out, so the very first logout after
        // a fresh sign-in already knows the door without depending on the render order.
        lastAccountType.current = accountType;
        setUser({
          ...userData,
          id: userId, // Used to ensure the user object always has a consistent 'id' property
          // Narrowed rather than cast, and in the fail-closed direction. If the server ever
          // sends an account table this build does not know, the account keeps working as a
          // participant instead of the client acting on a value it cannot reason about. The
          // capability list is left exactly as sent; `can` is what treats an absent one as
          // empty.
          account_type: accountType,
          capabilities: userData.capabilities ?? [],
        });
        return 'read';
      }
      console.error("User ID is missing from the authentication response.");
      void logout();
      return 'rejected';
    } catch (error) {
      if (isTokenRejected(error)) {
        // The server refused the token: the session is over, and it has to end.
        console.error("The access token was rejected; ending the session.");
        void logout();
        return 'rejected';
      }
      // The *request* failed, not the token: a timeout, a dropped connection, a 500
      // while the backend is deploying. The token was never judged, so the session is
      // still alive -- and ending it here would revoke it server-side too, signing the
      // person out for good over a backend having a bad moment.
      console.error("Could not read the signed-in account; the session is kept:", error);
      return 'unavailable';
    }
  }, [logout]);

  useEffect(() => {
    // The boot handshake is bounded and cancellable: a backend that stops
    // answering must not hold the app on its splash screen for ever, and a
    // provider that goes away must not keep reading after the fact.
    let cancelled = false;
    const timers: ReturnType<typeof setTimeout>[] = [];

    const initializeAuth = async () => {
      // A token an older build persisted is wiped rather than trusted: the bearer is
      // never read back from JS-readable storage, only minted fresh below.
      purgeLegacyPersistedToken();
      // The in-memory token does not survive a reload by design, so the session is
      // restored with one silent refresh through the HttpOnly cookie. A valid cookie
      // comes back with a fresh bearer; anything else answers null and the browser
      // simply stays signed out.
      const restoredToken = await refreshAccessToken();
      if (!restoredToken) {
        setIsLoading(false);
        return;
      }
      const outcome = await fetchAndSetUser(restoredToken);
      // The app is ready either way. A session whose account could not be read is
      // still a session, and the guards decide what to show while it is recovered.
      setIsLoading(false);
      if (outcome !== 'unavailable' || cancelled) return;

      // The request failed, not the token, so the session is alive: the cookie is
      // still good for the rest of the day. Try again in the background -- the app
      // must not sit on its splash screen waiting for a backend to come back -- and
      // bounded, so a backend that stays down is not hammered.
      for (const delay of ACCOUNT_RECOVERY_DELAYS_MS) {
        await new Promise<void>((resolve) => {
          timers.push(setTimeout(resolve, delay));
        });
        if (cancelled) return;
        const retried = await fetchAndSetUser(restoredToken);
        if (retried !== 'unavailable') return;
      }
    };
    initializeAuth();
    return () => {
      cancelled = true;
      timers.forEach(clearTimeout);
    };
  }, [fetchAndSetUser]);

  // The access token expires on its own schedule, and the token is already cleared by the
  // time this fires, so `logout` only has to drop the in-memory user to finish the job.
  // Protected routes then see an unauthenticated session and redirect to /login on their
  // own, which is why nothing here needs the router.
  useEffect(() => {
    const handleAuthExpired = () => {
      console.warn('Access token was rejected as expired. Ending session.');
      void logout();
    };

    window.addEventListener(AUTH_EXPIRED_EVENT, handleAuthExpired);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, handleAuthExpired);
  }, [logout]);

  // A session replaced by a newer login is also an ended session, but it deserves to be
  // heard about: the token was valid, and the only thing that killed it was a sign-in on
  // another device. The interceptor has already cleared the token and carried the server's
  // explanation on the event, so this listener signs the person out and tells them why.
  useEffect(() => {
    const handleSessionSuperseded = (event: Event) => {
      const detail = (event as CustomEvent<string>).detail;
      console.warn('Access token was rejected because the session was replaced by a new login.', detail);
      void logout();
      toast.error(detail);
    };

    window.addEventListener(SESSION_SUPERSEDED_EVENT, handleSessionSuperseded);
    return () => window.removeEventListener(SESSION_SUPERSEDED_EVENT, handleSessionSuperseded);
  }, [logout]);


  const verifyMagicToken = async (magicToken: string): Promise<VerifyTokenResult> => {
    setIsLoading(true);
    try {
      const response = await fetch(`${API_URL}/auth/login/magic/verify?token=${magicToken}`, {
        // Per-request, like every credentialed call: the response sets the HttpOnly refresh
        // cookie, which a cross-origin fetch drops unless it opts in. Nothing else sends cookies.
        credentials: 'include',
        signal: AbortSignal.timeout(15000),
      });

      if (!response.ok) {
        console.error("Magic token verification failed with status:", response.status);
        setIsLoading(false);
        return { success: false };
      }

      const data = await response.json();
      const accessToken = data.access_token;
      const redirectUrl = data.redirect_url;

      if (!accessToken || !redirectUrl) {
        console.error("Access token or redirect URL missing in response.");
        setIsLoading(false);
        return { success: false };
      }

      setInMemoryToken(accessToken);
      setToken(accessToken);
      await fetchAndSetUser(accessToken);
      
      setIsLoading(false);
      return { success: true, redirectUrl };
    } catch (error) {
      console.error('Magic token verification failed:', error);
      setIsLoading(false);
      return { success: false };
    }
  };

  const isAuthenticated = !isLoading && !!user;

  // The participant door is the default for a browser that has never signed in, which is the
  // case for a first visit or a session that lapsed before `/auth/me` ever answered.
  const signInPath = signInPathFor(lastAccountType.current);

  // Reads the capability list off the current user rather than closing over `user`, so a
  // consumer that captured this function still sees the answer for whoever is signed in
  // now. Recreated only when the list itself changes.
  const capabilities = user?.capabilities;
  const hasCapability = useCallback(
    (capability: Capability) => can(capabilities, capability),
    [capabilities]
  );

  return (
    <AuthContext.Provider value={{ user, logout, isAuthenticated, token, isLoading, verifyMagicToken, fetchAndSetUser, can: hasCapability, signInPath }}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
};
