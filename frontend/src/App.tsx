import type { ReactNode } from "react";
import { Toaster } from "@/components/ui/toaster";
import { Toaster as Sonner } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider, useAuth } from "./contexts/AuthContext";
import { CAPABILITIES, Capability } from "./lib/capabilities";
import { landingPathFor } from "./lib/landing";
import { Login, AdminLogin, ModeratorLogin } from "./pages/Login";
import { UserDashboard } from "./pages/UserDashboard";
import { AdminDashboard } from "./pages/AdminDashboard";
import { ModeratorDashboard } from "./pages/ModeratorDashboard";
import { ConfessionsPage } from "./pages/ConfessionsPage";
import { MatchmakingPage } from "./pages/MatchmakingPage";
import { InboxPage } from "./pages/InboxPage";
import { MessageBoxPage } from "./pages/MessageBoxPage";
import { ProfilePage } from "./pages/ProfilePage";
import { LoveNotesPage } from "./pages/LoveNotesPage";
import { GamesLayout } from "./components/games/GamesLayout";
import { GamesDashboardPage } from "./pages/GamesDashboardPage";
import { GamesLeaderboardPage } from "./pages/GamesLeaderboardPage";
import { GamesFeaturedPage } from "./pages/GamesFeaturedPage";
import { GamesSelectPage } from "./pages/GamesSelectPage";
import { About } from "./pages/About";
import NotFound from "./pages/NotFound";
import { MagicLinkVerification } from "./services/MagicLinkVerification";
import { ConfessionsAdmin } from "./pages/ConfessionsAdmin";
import { LoveNotesReview } from "./pages/LoveNotesReview";
import { ReportsReview } from "./pages/ReportsReview";
import { ProfileReview } from "./pages/ProfileReview";
import { AppConfiguration } from "./pages/AppConfiguration";

const queryClient = new QueryClient();

/**
 * Used to keep a signed-in user out of the login screen.
 * Once a session exists there is nothing left to log into, and the form would only bounce
 * them straight back here, so they are sent to the dashboard their account actually uses --
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
 * Used to protect routes that require authentication.
 * It checks if the user is authenticated and redirects to the login page if not.
 * It also shows a loading indicator while the authentication status is being checked.
 *
 * The redirect goes to the door this browser signed in through, not always to /login: a
 * moderator whose session lapses belongs back at the moderator form, where their regno
 * resolves. See `signInPath` in the auth context for why that survives the logout.
 */
export const ProtectedRoute = ({ children }: { children: ReactNode }) => {
  const { isAuthenticated, isLoading, signInPath } = useAuth();

  // Used to wait for the authentication check to complete.
  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <p>Loading session...</p>
      </div>
    );
  }

  // Used to protect routes by redirecting to login if the user is not authenticated after the check.
  return isAuthenticated ? <>{children}</> : <Navigate to={signInPath} replace />;
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
 * `without` names a capability that disqualifies an account *even when it holds* `capability`,
 * and it exists for exactly one route. `/moderation` is gated on `reports:review`, which an admin
 * holds too, so the capability alone would leave the hub reachable by typing its URL -- a second
 * staff landing page for an account that already has one, offering that admin the same queues
 * their own console already lists. The console is where an admin lands; the hub is the other
 * staff account's landing page and nothing more. Stating the exclusion here rather than inside
 * the page is what keeps the nav bar and the route in agreement: both ask this one question, so
 * the hub cannot be offered in one and refused in the other.
 *
 * This is a convenience, not a boundary. Each of these routes re-checks the same capability
 * on the server, so bypassing this component in a console grants nothing.
 */
export const CapabilityRoute = ({
  capability,
  without,
  children,
}: {
  capability: Capability;
  without?: Capability;
  children: ReactNode;
}) => {
  const { isAuthenticated, isLoading, can, signInPath } = useAuth();

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <p>Loading session...</p>
      </div>
    );
  }

  if (!isAuthenticated) {
    return <Navigate to={signInPath} replace />;
  }

  // Both halves of the same question, and the redirect is the same either way: an account that
  // cannot be here goes to the page that is actually its own. For the hub that is the console
  // for an admin, and for an anonymous account it is whichever staff page its capabilities
  // reach, which is the destination the sign-in redirect would have chosen.
  if (!can(capability) || (without !== undefined && can(without))) {
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
      {/* Public routes that anyone can access.

          One route per account table. Each renders the same form but posts to the endpoint
          for its own table, which is what keeps a regno that exists as both a participant and
          a staff account from resolving to whichever one happened to be found first. All
          three are guest-only: an account that already has a session is sent to the landing
          path its capabilities grant, so visiting the wrong door is harmless. */}
      <Route
        path="/login"
        element={
          <PublicOnlyRoute>
            <Login />
          </PublicOnlyRoute>
        }
      />
      <Route
        path="/login/admin"
        element={
          <PublicOnlyRoute>
            <AdminLogin />
          </PublicOnlyRoute>
        }
      />
      <Route
        path="/login/moderator"
        element={
          <PublicOnlyRoute>
            <ModeratorLogin />
          </PublicOnlyRoute>
        }
      />
      <Route path="/verify-login" element={<MagicLinkVerification />} />

      {/* Protected Routes */}
      <Route
        path="/"
        element={
          <ProtectedRoute>
            {/* Used to redirect logged-in users from the root path to the dashboard their
                account's capabilities point at. */}
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
          moderator sees the review screens and is refused the account-management one.

          The two dashboards are separated by the same gate, which is why a moderator signing
          in lands on `/moderation` rather than here: `stats:read` guards the console because
          the console shows what `/admin/stats` returns, and that census of the platform is
          admin-only. The rest of the area is unchanged -- a moderator reaches every review
          screen below exactly as before. */}
      <Route
        path="/admin"
        element={
          <CapabilityRoute capability={CAPABILITIES.STATS_READ}>
            <AdminDashboard />
          </CapabilityRoute>
        }
      />
      <Route
        path="/moderation"
        element={
          /* `without: stats:read` is what keeps this page off an admin's map. It is the other
              staff landing page, not a second door into this one: an admin's landing page is
              the console, which already lists every queue below, so the hub would offer them a
              smaller version of the page they were just on. */
          <CapabilityRoute capability={CAPABILITIES.REPORTS_REVIEW} without={CAPABILITIES.STATS_READ}>
            <ModeratorDashboard />
          </CapabilityRoute>
        }
      />
      <Route
        path="/admin/configuration"
        element={
          <CapabilityRoute capability={CAPABILITIES.APP_CONFIG_READ}>
            <AppConfiguration />
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
        path="/admin/reports"
        element={
          <CapabilityRoute capability={CAPABILITIES.REPORTS_REVIEW}>
            <ReportsReview />
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
      {/* The Games section. One shell around every /games route, so the section's own
          navigation (back to ConfessIt, dashboard, select, featured, leaderboard) is
          declared once in GamesLayout and persists as the user moves between routes. */}
      <Route
        path="/games"
        element={
          <ProtectedRoute>
            <GamesLayout />
          </ProtectedRoute>
        }
      >
        <Route index element={<GamesDashboardPage />} />
        <Route path="leaderboard" element={<GamesLeaderboardPage />} />
        <Route path="featured" element={<GamesFeaturedPage />} />
        <Route path="select" element={<GamesSelectPage />} />
      </Route>
      {/* The old /mini-games page is gone; deep links retire to the section that replaced
          it rather than 404ing. */}
      <Route path="/mini-games" element={<Navigate to="/games" replace />} />
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
