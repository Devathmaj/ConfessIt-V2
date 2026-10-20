import { useEffect, type ReactNode } from "react";
import { Toaster } from "@/components/ui/toaster";
import { Toaster as Sonner } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Routes, Route, Navigate, useLocation } from "react-router-dom";
import { AuthProvider, useAuth } from "./contexts/AuthContext";
import { CAPABILITIES, Capability } from "./lib/capabilities";
import { landingPathFor } from "./lib/landing";
import { rememberVisitedPage } from "./lib/navigationMemory";
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
import { AdminGamesLayout } from "./components/AdminGamesLayout";
import { GamesConfigProvider } from "./contexts/GamesConfigContext";
import { GamesDashboardPage } from "./pages/GamesDashboardPage";
import { GamesLeaderboardPage } from "./pages/GamesLeaderboardPage";
import { GamesFeaturedPage } from "./pages/GamesFeaturedPage";
import { GamesSelectPage } from "./pages/GamesSelectPage";
import { WordleGamePage } from "./pages/WordleGamePage";
import { AnagramGamePage } from "./pages/AnagramGamePage";
import { ConnectionsGamePage } from "./pages/ConnectionsGamePage";
import { TriviaDropGamePage } from "./pages/TriviaDropGamePage";
import { CoupleQuestionnairePage } from "./pages/CoupleQuestionnairePage";
import { TwoKeyEscapePage } from "./pages/TwoKeyEscapePage";
import { ReactionTimeGamePage } from "./pages/ReactionTimeGamePage";
import { PlotTwistGamePage } from "./pages/PlotTwistGamePage";
import { CaseClosedGamePage } from "./pages/CaseClosedGamePage";
import { AdminGamesDashboard } from "./pages/AdminGamesDashboard";
import { AdminGamesManage } from "./pages/AdminGamesManage";
import { AdminFeaturedManage } from "./pages/AdminFeaturedManage";
import { AdminWordleManage } from "./pages/AdminWordleManage";
import { AdminAnagramManage } from "./pages/AdminAnagramManage";
import { AdminCoupleQuestionnaireManage } from "./pages/AdminCoupleQuestionnaireManage";
import { AdminTwoKeyEscapeManage } from "./pages/AdminTwoKeyEscapeManage";
import { AdminReactionTimeManage } from "./pages/AdminReactionTimeManage";
import { AdminTriviaDropManage } from "./pages/AdminTriviaDropManage";
import { AdminConnectionsManage } from "./pages/AdminConnectionsManage";
import { AdminPlotTwistManage } from "./pages/AdminPlotTwistManage";
import { AdminCaseClosedManage } from "./pages/AdminCaseClosedManage";
import { AdminGamesLeaderboard } from "./pages/AdminGamesLeaderboard";
import { About } from "./pages/About";
import NotFound from "./pages/NotFound";
import { MagicLinkVerification } from "./services/MagicLinkVerification";
import { CopyLoginLink } from "./pages/CopyLoginLink";
import { ConfessionsAdmin } from "./pages/ConfessionsAdmin";
import { LoveNotesReview } from "./pages/LoveNotesReview";
import { ReportsReview } from "./pages/ReportsReview";
import { ProfileReview } from "./pages/ProfileReview";
import { StaffNotificationsPage } from "./pages/StaffNotificationsPage";
import { AppConfiguration } from "./pages/AppConfiguration";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 60 * 1000,
      gcTime: 5 * 60 * 1000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

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
 * Used to protect routes that are only for participants (non-staff).
 * Staff roles (admin and moderator) must not be able to access user-facing endpoints.
 * If a staff user tries to access, redirect them to their appropriate landing page.
 */
export const ParticipantOnlyRoute = ({ children }: { children: ReactNode }) => {
  const { isAuthenticated, isLoading, can, signInPath, user } = useAuth();

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

  // Staff users (those with staff capabilities) cannot access participant routes
  const isStaff = can(CAPABILITIES.STATS_READ) || can(CAPABILITIES.REPORTS_REVIEW) ||
    can(CAPABILITIES.CONFESSIONS_REVIEW) || can(CAPABILITIES.LOVE_NOTES_REVIEW) ||
    can(CAPABILITIES.USERS_REVIEW) || can(CAPABILITIES.APP_CONFIG_READ);
  const accountType = user?.account_type;
  const isStaffByType = accountType === 'admin' || accountType === 'moderator';
  if (isStaff || isStaffByType) {
    return <Navigate to={landingPathFor(can)} replace />;
  }

  return <>{children}</>;
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
      <Route path="/copy-login-link" element={<CopyLoginLink />} />

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
          <ParticipantOnlyRoute>
            <UserDashboard />
          </ParticipantOnlyRoute>
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
      {/* One notification inbox per staff role. The admin's is console-gated
          (it carries game alerts); the moderator's is hub-gated and shows
          reports plus love notes awaiting approval. */}
      <Route
        path="/admin/notifications"
        element={
          <CapabilityRoute capability={CAPABILITIES.STATS_READ}>
            <StaffNotificationsPage />
          </CapabilityRoute>
        }
      />
      <Route
        path="/moderation/notifications"
        element={
          <CapabilityRoute capability={CAPABILITIES.REPORTS_REVIEW} without={CAPABILITIES.STATS_READ}>
            <StaffNotificationsPage />
          </CapabilityRoute>
        }
      />

      {/* The Games console. One guard around the section rather than one per screen: every
          screen below reads and writes the same catalogue state, so `stats:read` -- the
          admin-only capability behind the console -- gates the layout and the three children
          inherit it. Moderators are refused here, not merely left without a link. The stray
          child route sends a mistyped deep link back to the overview instead of leaving the
          shell rendered with nothing inside it. */}
      <Route
        path="/admin/games"
        element={
          <CapabilityRoute capability={CAPABILITIES.STATS_READ}>
            <AdminGamesLayout />
          </CapabilityRoute>
        }
      >
        <Route index element={<AdminGamesDashboard />} />
        <Route path="manage" element={<AdminGamesManage />} />
        <Route path="manage/featured" element={<AdminFeaturedManage />} />
        <Route path="manage/wordle" element={<AdminWordleManage />} />
        <Route path="manage/anagram" element={<AdminAnagramManage />} />
        <Route path="manage/couple-questionnaire" element={<AdminCoupleQuestionnaireManage />} />
        <Route path="manage/two-key-escape" element={<AdminTwoKeyEscapeManage />} />
        <Route path="manage/reaction-time" element={<AdminReactionTimeManage />} />
        <Route path="manage/connections" element={<AdminConnectionsManage />} />
        <Route path="manage/trivia-drop" element={<AdminTriviaDropManage />} />
        <Route path="manage/plot-twist" element={<AdminPlotTwistManage />} />
        <Route path="manage/case-closed" element={<AdminCaseClosedManage />} />
        <Route path="leaderboard" element={<AdminGamesLeaderboard />} />
        <Route path="*" element={<Navigate to="/admin/games" replace />} />
      </Route>

      {/* User-specific protected routes */}
      <Route
        path="/confessions"
        element={
          <ParticipantOnlyRoute>
            <ConfessionsPage />
          </ParticipantOnlyRoute>
        }
      />
      <Route
        path="/matchmaking"
        element={
          <ParticipantOnlyRoute>
            <MatchmakingPage />
          </ParticipantOnlyRoute>
        }
      />
      <Route
        path="/notifications"
        element={
          <ParticipantOnlyRoute>
            <InboxPage />
          </ParticipantOnlyRoute>
        }
      />
      <Route
        path="/inbox"
        element={
          <ParticipantOnlyRoute>
            <MessageBoxPage />
          </ParticipantOnlyRoute>
        }
      />
      <Route
        path="/profile" // Changed from /settings
        element={
          <ParticipantOnlyRoute>
            <ProfilePage /> {/* Changed from SettingsPage */}
          </ParticipantOnlyRoute>
        }
      />
      <Route
        path="/love-notes"
        element={
          <ParticipantOnlyRoute>
            <LoveNotesPage />
          </ParticipantOnlyRoute>
        }
      />
      {/* The Games section. One shell around every /games route, so the section's own
          navigation (back to ConfessIt, dashboard, select, featured, leaderboard) is
          declared once in GamesLayout and persists as the user moves between routes. */}
      <Route
        path="/games"
        element={
          <ParticipantOnlyRoute>
            <GamesLayout />
          </ParticipantOnlyRoute>
        }
      >
        <Route index element={<GamesDashboardPage />} />
        <Route path="leaderboard" element={<GamesLeaderboardPage />} />
        <Route path="featured" element={<GamesFeaturedPage />} />
        <Route path="select" element={<GamesSelectPage />} />
        <Route path="wordle" element={<WordleGamePage />} />
        <Route path="anagram" element={<AnagramGamePage />} />
        <Route path="connections" element={<ConnectionsGamePage />} />
        <Route path="trivia-drop" element={<TriviaDropGamePage />} />
        <Route path="couple-questionnaire" element={<CoupleQuestionnairePage />} />
        <Route path="two-key-escape" element={<TwoKeyEscapePage />} />
        <Route path="reaction-time" element={<ReactionTimeGamePage />} />
        <Route path="plot-twist" element={<PlotTwistGamePage />} />
        <Route path="case-closed" element={<CaseClosedGamePage />} />
      </Route>
      {/* The old /mini-games page is gone; deep links retire to the section that replaced
          it rather than 404ing. */}
      <Route path="/mini-games" element={<Navigate to="/games" replace />} />
      <Route
        path="/about"
        element={
          <ParticipantOnlyRoute>
            <About />
          </ParticipantOnlyRoute>
        }
      />

      {/* Fallback for any other path */}
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
};

/**
 * Records the main-app page a person is on, for the Games section's
 * "Back to ConfessIt" links. Renders nothing; mounted inside the router so every
 * navigation passes through it. Games and auth routes are skipped inside
 * `rememberLastPage`, so this only ever stores a page those links may return to.
 */
const RememberLastPage = () => {
  const { pathname, search, hash } = useLocation();
  useEffect(() => {
    rememberVisitedPage(pathname, search, hash);
  }, [pathname, search, hash]);
  return null;
};

const App = () => (
  <QueryClientProvider client={queryClient}>
    <TooltipProvider>
      <Toaster />
      <Sonner />
      <AuthProvider>
        {/* Around the router rather than inside it: the configuration has nothing to do with
            the URL, and both the player-facing Games pages and the admin console read the one
            instance it owns. */}
        <GamesConfigProvider>
          <BrowserRouter>
            <RememberLastPage />
            <AppRoutes />
          </BrowserRouter>
        </GamesConfigProvider>
      </AuthProvider>
    </TooltipProvider>
  </QueryClientProvider>
);

export default App;
