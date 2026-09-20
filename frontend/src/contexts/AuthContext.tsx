// src/contexts/AuthContext.tsx

import React, { createContext, useContext, useState, ReactNode, useEffect, useCallback, useRef } from 'react';
import { jwtDecode } from 'jwt-decode';
import { getCurrentUser } from '../services/api';
import { AUTH_EXPIRED_EVENT } from '../lib/authEvents';
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
  emoji: string;
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
}

interface VerifyTokenResult {
  success: boolean;
  redirectUrl?: string;
}

interface AuthContextType {
  user: User | null;
  logout: () => void;
  isAuthenticated: boolean;
  isLoading: boolean;
  token: string | null;
  verifyMagicToken: (token: string) => Promise<VerifyTokenResult>;
  fetchAndSetUser: (token: string) => Promise<void>; // Expose fetchAndSetUser
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

  const logout = useCallback(() => {
    setUser(null);
    setToken(null);
    localStorage.removeItem('accessToken');
  }, []);

  const fetchAndSetUser = useCallback(async (currentToken: string) => {
    if (!currentToken) {
      setIsLoading(false);
      return;
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
      } else {
        console.error("User ID is missing from the authentication response.");
        logout();
      }
    } catch (error) {
      console.error("Failed to fetch user data, token might be expired:", error);
      logout();
    }
  }, [logout]);

  useEffect(() => {
    const initializeAuth = async () => {
      const storedToken = localStorage.getItem('accessToken');
      if (storedToken) {
        setToken(storedToken);
        await fetchAndSetUser(storedToken);
      }
      setIsLoading(false);
    };
    initializeAuth();
  }, [fetchAndSetUser]);

  // The access token expires on its own schedule, and the token is already cleared by the
  // time this fires, so `logout` only has to drop the in-memory user to finish the job.
  // Protected routes then see an unauthenticated session and redirect to /login on their
  // own, which is why nothing here needs the router.
  useEffect(() => {
    const handleAuthExpired = () => {
      console.warn('Access token was rejected as expired. Ending session.');
      logout();
    };

    window.addEventListener(AUTH_EXPIRED_EVENT, handleAuthExpired);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, handleAuthExpired);
  }, [logout]);


  const verifyMagicToken = async (magicToken: string): Promise<VerifyTokenResult> => {
    setIsLoading(true);
    try {
      const response = await fetch(`http://localhost:8001/auth/login/magic/verify?token=${magicToken}`);

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

      localStorage.setItem('accessToken', accessToken);
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
