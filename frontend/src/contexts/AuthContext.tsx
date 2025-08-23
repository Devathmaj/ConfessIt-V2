// src/contexts/AuthContext.tsx

import React, { createContext, useContext, useState, ReactNode, useEffect, useCallback } from 'react';
import { jwtDecode } from 'jwt-decode';
import { getCurrentUser } from '../services/api'; 

// Used to define the structure of the User object
export interface User {
  Name: string;
  Regno: string;
  email?: string;
  emoji: string;
  username: string;
  bio?: string;
  which_class?: string;
  profile_picture_id?: string;
  gender?: string;
  interests?: string[];
  isMatchmaking?: boolean;
  isNotifications?: boolean;
  isLovenotesRecieve?: boolean;
  isLovenotesSend?: boolean; // Used to track if the user has sent a love note
  user_role?: 'user' | 'admin';
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
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

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
      const decoded: any = jwtDecode(currentToken);
      setUser({
        ...userData,
        username: decoded.sub,
        user_role: userData.user_role || 'user',
      });
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

  return (
    <AuthContext.Provider value={{ user, logout, isAuthenticated, token, isLoading, verifyMagicToken, fetchAndSetUser }}>
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