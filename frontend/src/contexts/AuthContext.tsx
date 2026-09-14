// src/contexts/AuthContext.tsx

import React, { createContext, useContext, useState, ReactNode, useEffect, useCallback } from 'react';
import { jwtDecode } from 'jwt-decode';
import { getCurrentUser } from '../services/api';
import { AUTH_EXPIRED_EVENT } from '../lib/authEvents';
import { AccountType, asAccountType, Capability, can } from '../lib/capabilities';


// Used to define the structure of the User object
export interface User {
  id: string;
  name: string;
  regno: string;
  email?: string;
  emoji: string;
  bio?: string;
  class_name?: string;
  profile_picture_url?: string;
  gender?: string;
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
}

// Exported so route guards can be rendered against a supplied session in tests.
export const AuthContext = createContext<AuthContextType | undefined>(undefined);


export const AuthProvider = ({ children }: { children: ReactNode }) => {
  const [user, setUser] = useState<User | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);

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
        setUser({
          ...userData,
          id: userId, // Used to ensure the user object always has a consistent 'id' property
          // Narrowed rather than cast, and in the fail-closed direction. If the server ever
          // sends an account table this build does not know, the account keeps working as a
          // participant instead of the client acting on a value it cannot reason about. The
          // capability list is left exactly as sent; `can` is what treats an absent one as
          // empty.
          account_type: asAccountType(userData.account_type),
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

  // Reads the capability list off the current user rather than closing over `user`, so a
  // consumer that captured this function still sees the answer for whoever is signed in
  // now. Recreated only when the list itself changes.
  const capabilities = user?.capabilities;
  const hasCapability = useCallback(
    (capability: Capability) => can(capabilities, capability),
    [capabilities]
  );

  return (
    <AuthContext.Provider value={{ user, logout, isAuthenticated, token, isLoading, verifyMagicToken, fetchAndSetUser, can: hasCapability }}>
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
