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
import { InboxPage } from "./pages/InboxPage";
import { ProfilePage } from "./pages/ProfilePage";
import { LoveNotesPage } from "./pages/LoveNotesPage";
import { MiniGamesPage } from "./pages/MiniGamesPage";
import { About } from "./pages/About";
import NotFound from "./pages/NotFound";
import { MagicLinkVerification } from "./services/MagicLinkVerification";
import { ConfessionsAdmin } from "./pages/ConfessionsAdmin";
import { MatchmakingReview } from "./pages/MatchmakingReview";
import { LoveNotesReview } from "./pages/LoveNotesReview";

const queryClient = new QueryClient();

/**
 * Used to protect routes that require authentication.
 * It checks if the user is authenticated and redirects to the login page if not.
 * It also shows a loading indicator while the authentication status is being checked.
 */
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

/**
 * Used to protect routes that are only accessible to administrators.
 * It checks for authentication and verifies the user role is 'admin'.
 * Non-admin users are redirected to the user dashboard.
 */
const AdminRoute = ({ children }: { children: React.ReactNode }) => {
    const { user, isAuthenticated, isLoading } = useAuth();
  
    // Used to wait for the authentication check to complete.
    if (isLoading) {
      return (
        <div className="min-h-screen flex items-center justify-center">
          <p>Loading session...</p>
        </div>
      );
    }
  
    // Used to redirect to login if the user is not authenticated.
    if (!isAuthenticated) {
      return <Navigate to="/login" replace />;
    }
  
    // Used to redirect to the main dashboard if the user is not an admin.
    if (user?.user_role !== 'admin') {
      return <Navigate to="/dashboard" replace />;
    }
  
    // Used to render the component if the user is an authenticated admin.
    return <>{children}</>;
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
      
      {/* Admin Routes */}
      <Route
        path="/admin"
        element={
          <AdminRoute>
            <AdminDashboard />
          </AdminRoute>
        }
      />
       <Route
        path="/admin/confessions-review"
        element={
          <AdminRoute>
            <ConfessionsAdmin />
          </AdminRoute>
        }
      />
       <Route
        path="/admin/matchmaking-review"
        element={
          <AdminRoute>
            <MatchmakingReview />
          </AdminRoute>
        }
      />
       <Route
        path="/admin/love-notes-review"
        element={
          <AdminRoute>
            <LoveNotesReview />
          </AdminRoute>
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
        path="/inbox"
        element={
          <ProtectedRoute>
            <InboxPage />
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
