import { Toaster } from "@/components/ui/toaster";
import { Toaster as Sonner } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider, useAuth } from "./contexts/AuthContext";
import { Login } from "./pages/Login";
import { UserDashboard } from "./pages/UserDashboard";
import { AdminDashboard } from "./pages/AdminDashboard";
import { ConfessionsPage } from "./pages/ConfessionsPage";
import { MatchmakingPage } from "./pages/MatchmakingPage";
import { ProfilePage } from "./pages/ProfilePage";
import { LoveNotesPage } from "./pages/LoveNotesPage";
import { MiniGamesPage } from "./pages/MiniGamesPage";
import { About } from "./pages/About";
import NotFound from "./pages/NotFound";
import { MagicLinkVerification } from "./services/MagicLinkVerification";
import { useEffect } from "react";

const queryClient = new QueryClient();

const ProtectedRoute = ({ children }: { children: React.ReactNode }) => {
  const { isAuthenticated, isLoading } = useAuth();

  // Used to wait for the authentication check to complete.
  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <p>Loading session...</p>
      </div>
    );
  }

  // Used to protect routes by redirecting to login if the user is not authenticated after the check.
  return isAuthenticated ? <>{children}</> : <Navigate to="/login" replace />;
};

const AppRoutes = () => {
  const { user, isLoading } = useAuth();

  // While loading, we can show a blank screen or a global loader
  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <p>Initializing...</p>
      </div>
    );
  }

  return (
    <Routes>
      {/* Public routes that anyone can access */}
      <Route path="/login" element={<Login />} />
      <Route path="/verify-login" element={<MagicLinkVerification />} />

      {/* Protected Routes */}
      <Route
        path="/"
        element={
          <ProtectedRoute>
            {/* Used to redirect logged-in users from the root path to their respective dashboard based on their role. */}
            <Navigate to={user?.user_role === 'admin' ? '/admin' : '/dashboard'} replace />
          </ProtectedRoute>
        }
      />
      <Route
        path="/dashboard"
        element={
          <ProtectedRoute>
            <UserDashboard />
          </ProtectedRoute>
        }
      />
      <Route
        path="/admin"
        element={
          <ProtectedRoute>
            <AdminDashboard />
          </ProtectedRoute>
        }
      />
      <Route
        path="/confessions"
        element={
          <ProtectedRoute>
            <ConfessionsPage />
          </ProtectedRoute>
        }
      />
      <Route
        path="/matchmaking"
        element={
          <ProtectedRoute>
            <MatchmakingPage />
          </ProtectedRoute>
        }
      />
      <Route
        path="/profile" // Changed from /settings
        element={
          <ProtectedRoute>
            <ProfilePage /> {/* Changed from SettingsPage */}
          </ProtectedRoute>
        }
      />
      <Route
        path="/love-notes"
        element={
          <ProtectedRoute>
            <LoveNotesPage />
          </ProtectedRoute>
        }
      />
      <Route
        path="/mini-games"
        element={
          <ProtectedRoute>
            <MiniGamesPage />
          </ProtectedRoute>
        }
      />
      <Route
        path="/about"
        element={
          <ProtectedRoute>
            <About />
          </ProtectedRoute>
        }
      />

      {/* Fallback for any other path */}
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
};

const App = () => (
  <QueryClientProvider client={queryClient}>
    <TooltipProvider>
      <Toaster />
      <Sonner />
      <AuthProvider>
        <BrowserRouter>
          <AppRoutes />
        </BrowserRouter>
      </AuthProvider>
    </TooltipProvider>
  </QueryClientProvider>
);

export default App;
