// src/contexts/AuthContext.tsx

import React, { createContext, useContext, useState, ReactNode, useEffect, useCallback } from 'react';
import { jwtDecode } from 'jwt-decode';
import { getCurrentUser } from '../services/api';
import { AUTH_EXPIRED_EVENT } from '../lib/authEvents';
import { Capability, can, asRole } from '../lib/capabilities';


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
  role?: 'user' | 'admin' | 'moderator';
  /**
   * What this account may reach, as decided by the server. Screens and menu entries are
   * gated on these rather than on `role`, so the client holds no privilege table of its
   * own. See src/lib/capabilities.ts.
   */
  capabilities?: string[];
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
          // Narrowed rather than cast. If the server ever sends a role this build does not
          // know, the account keeps working as a plain user instead of the client acting on
          // a value it cannot reason about.
          role: asRole(userData.role),
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
