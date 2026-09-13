import type { ReactNode } from "react";
import { Toaster } from "@/components/ui/toaster";
import { Toaster as Sonner } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider, useAuth } from "./contexts/AuthContext";
import { CAPABILITIES, Capability } from "./lib/capabilities";
import { Login } from "./pages/Login";
import { UserDashboard } from "./pages/UserDashboard";
import { AdminDashboard } from "./pages/AdminDashboard";
import { ConfessionsPage } from "./pages/ConfessionsPage";
import { MatchmakingPage } from "./pages/MatchmakingPage";
import { InboxPage } from "./pages/InboxPage";
import { MessageBoxPage } from "./pages/MessageBoxPage";
import { ProfilePage } from "./pages/ProfilePage";
import { LoveNotesPage } from "./pages/LoveNotesPage";
import { MiniGamesPage } from "./pages/MiniGamesPage";
import { About } from "./pages/About";
import NotFound from "./pages/NotFound";
import { MagicLinkVerification } from "./services/MagicLinkVerification";
import { ConfessionsAdmin } from "./pages/ConfessionsAdmin";
import { MatchmakingReview } from "./pages/MatchmakingReview";
import { LoveNotesReview } from "./pages/LoveNotesReview";
import { ProfileReview } from "./pages/ProfileReview";
import ChatMonitor from "./pages/ChatMonitor";

const queryClient = new QueryClient();

/**
 * Used to keep a signed-in user out of the login screen.
 * Once a session exists there is nothing left to log into, and the form would only bounce
 * them straight back here, so they are sent to the dashboard their role actually uses --
 * the same target the root path already resolves to.
 */
export const PublicOnlyRoute = ({ children }: { children: ReactNode }) => {
  const { isAuthenticated, isLoading, can } = useAuth();

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <p>Loading session...</p>
      </div>
    );
  }

  return isAuthenticated ? (
    <Navigate to={landingPathFor(can)} replace />
  ) : (
    <>{children}</>
  );
};

/**
 * Where a signed-in account belongs after signing in or hitting the root path.
 *
 * Decided by the capability list rather than by the role name, matching the server's own
 * rule for the magic-link redirect: staff land in the moderation area, participants on
 * their dashboard. A role check here would send a moderator to /dashboard, which is a page
 * they have no use for.
 *
 * Takes the context's `can` rather than the user object so the whole app asks the
 * capabilities question the same way.
 */
const landingPathFor = (can: (capability: Capability) => boolean) =>
  can(CAPABILITIES.STATS_READ) ? '/admin' : '/dashboard';

/**
 * Used to protect routes that require authentication.
 * It checks if the user is authenticated and redirects to the login page if not.
 * It also shows a loading indicator while the authentication status is being checked.
 */
const ProtectedRoute = ({ children }: { children: ReactNode }) => {
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

/**
 * Used to protect a moderation screen behind the one capability it needs.
 *
 * The requirement is per-route rather than "is staff", because moderator and admin do not
 * see the same screens: a moderator is refused /admin/profile-review, and an admin who
 * somehow lost the user-management capability would be refused it too. Sending a signed-in
 * account that lacks the capability back to its own landing page avoids showing a dead
 * screen or a page whose every fetch would 403.
 *
 * This is a convenience, not a boundary. Each of these routes re-checks the same capability
 * on the server, so bypassing this component in a console grants nothing.
 */
const CapabilityRoute = ({
  capability,
  children,
}: {
  capability: Capability;
  children: ReactNode;
}) => {
  const { isAuthenticated, isLoading, can } = useAuth();

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <p>Loading session...</p>
      </div>
    );
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  if (!can(capability)) {
    return <Navigate to={landingPathFor(can)} replace />;
  }

  return <>{children}</>;
};

const AppRoutes = () => {
  const { isLoading, can } = useAuth();

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
      <Route
        path="/login"
        element={
          <PublicOnlyRoute>
            <Login />
          </PublicOnlyRoute>
        }
      />
      <Route path="/verify-login" element={<MagicLinkVerification />} />

      {/* Protected Routes */}
      <Route
        path="/"
        element={
          <ProtectedRoute>
            {/* Used to redirect logged-in users from the root path to their respective dashboard based on their role. */}
            <Navigate to={landingPathFor(can)} replace />
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
      
      {/* Moderation routes. Each is gated on the capability its endpoints require, so a
          moderator sees the review screens and is refused the account-management one. */}
      <Route
        path="/admin"
        element={
          <CapabilityRoute capability={CAPABILITIES.STATS_READ}>
            <AdminDashboard />
          </CapabilityRoute>
        }
      />
      <Route
        path="/admin/confessions"
        element={
          <CapabilityRoute capability={CAPABILITIES.CONFESSIONS_REVIEW}>
            <ConfessionsAdmin />
          </CapabilityRoute>
        }
      />
       <Route
        path="/admin/matchmaking"
        element={
          <CapabilityRoute capability={CAPABILITIES.MATCHMAKING_REVIEW}>
            <MatchmakingReview />
          </CapabilityRoute>
        }
      />
      <Route
        path="/admin/conversations"
        element={
          <CapabilityRoute capability={CAPABILITIES.CONVERSATIONS_REVIEW}>
            <ChatMonitor />
          </CapabilityRoute>
        }
      />
       <Route
        path="/admin/love-notes"
        element={
          <CapabilityRoute capability={CAPABILITIES.LOVE_NOTES_REVIEW}>
            <LoveNotesReview />
          </CapabilityRoute>
        }
      />
      <Route
        path="/admin/profile-review"
        element={
          <CapabilityRoute capability={CAPABILITIES.USERS_REVIEW}>
            <ProfileReview />
          </CapabilityRoute>
        }
      />

      {/* User-specific protected routes */}
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
        path="/notifications"
        element={
          <ProtectedRoute>
            <InboxPage />
          </ProtectedRoute>
        }
      />
      <Route
        path="/inbox"
        element={
          <ProtectedRoute>
            <MessageBoxPage />
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
        <BrowserRouter future={{ v7_relativeSplatPath: true }}>
          <AppRoutes />
        </BrowserRouter>
      </AuthProvider>
    </TooltipProvider>
  </QueryClientProvider>
);

export default App;
