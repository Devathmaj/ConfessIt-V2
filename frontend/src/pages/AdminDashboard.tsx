import { useState, useEffect, type ReactNode, type KeyboardEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '@/contexts/AuthContext';
import { Button } from '@/components/ui/button';
import {
  Users,
  Activity,
  MessageCircle,
  Heart,
  Mail,
  ShieldAlert,
  LogOut,
  UsersRound,
  ClipboardList,
  Loader2
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { toast } from 'sonner';
import { resolveProfilePictureUrl } from '@/lib/utils';
import { AdminNavigation } from '@/components/AdminNavigation';
import { getAdminStats, getAdminActiveSessions } from '@/services/api';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Badge } from '@/components/ui/badge';

import DashboardWebm from '@/assets/Dashboard.webm';
import DashboardMp4 from '@/assets/Dashboard.mp4';
import DashboardWebp from '@/assets/Dashboard.webp';

interface AdminStats {
  total_users: number;
  active_sessions: number;
  total_confessions: number;
  total_love_notes: number;
  pending_love_notes: number;
  blocked_users: number;
}

interface StatCardData {
  id: string;
  title: string;
  description: string;
  icon: LucideIcon;
  value: string | number;
  isLoading: boolean;
  onClick?: () => void;
}

interface QuickAction {
  id: string;
  title: string;
  description: string;
  icon: LucideIcon;
  to: string;
}

const recentActivity = [
  'Daily review window started.',
  'Awaiting new confession approvals.',
  'Monitoring love-note submissions.',
  'Watching matchmaking requests.',
  'Keeping an eye on reported content.'
];

interface GlassCardProps {
  children: ReactNode;
  className?: string;
  onClick?: () => void;
  role?: string;
  tabIndex?: number;
  onKeyDown?: (event: KeyboardEvent<HTMLDivElement>) => void;
}

const GlassCard = ({ children, className = '', onClick, role, tabIndex, onKeyDown }: GlassCardProps) => (
  <div
    onClick={onClick}
    role={role}
    tabIndex={tabIndex}
    onKeyDown={onKeyDown}
    className={`group relative overflow-hidden rounded-2xl border border-white/10 bg-white/5 backdrop-blur-xl shadow-lg transition-all duration-300 hover:bg-white/10 hover:border-white/20 ${onClick ? 'cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-pink-400 focus-visible:ring-offset-2 focus-visible:ring-offset-transparent' : ''} ${className}`}
  >
    <div className="absolute -inset-1 rounded-2xl bg-gradient-to-r from-pink-600/10 to-purple-600/10 opacity-0 transition-opacity duration-500 group-hover:opacity-100" />
    <div className="relative z-10 h-full">{children}</div>
  </div>
);

const StatCard = ({ item }: { item: StatCardData }) => {
  const Icon = item.icon;
  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!item.onClick) {
      return;
    }
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      item.onClick();
    }
  };

  return (
    <GlassCard
      className="p-6 flex flex-col justify-between"
      onClick={item.onClick}
      role={item.onClick ? 'button' : undefined}
      tabIndex={item.onClick ? 0 : undefined}
      onKeyDown={item.onClick ? handleKeyDown : undefined}
    >
      <div className="flex items-start justify-between">
        <div>
          <p className="text-sm text-white/70">{item.title}</p>
          {item.isLoading ? (
            <div className="mt-2 h-8 w-24 animate-pulse rounded bg-white/20" />
          ) : (
            <p className="mt-2 text-3xl font-bold">{item.value}</p>
          )}
          <p className="mt-3 text-xs text-white/50">{item.description}</p>
        </div>
        <div className="rounded-xl bg-gradient-to-br from-pink-500/20 to-rose-500/20 p-3">
          <Icon className="h-6 w-6 text-white" />
        </div>
      </div>
    </GlassCard>
  );
};

interface ActiveSessionRecord {
  session_id: string;
  status: string;
  last_seen?: string | null;
  ip?: string | null;
  device?: string | null;
  user?: {
    id?: string | null;
    Name?: string | null;
    Regno?: string | null;
    email?: string | null;
    username?: string | null;
    user_role?: string | null;
    is_blocked?: boolean;
  };
}

export const AdminDashboard = () => {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [profilePictureUrl, setProfilePictureUrl] = useState<string | null>(null);
  const [stats, setStats] = useState<AdminStats | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [showActiveSessions, setShowActiveSessions] = useState(false);
  const [activeSessions, setActiveSessions] = useState<ActiveSessionRecord[]>([]);
  const [activeSessionsLoading, setActiveSessionsLoading] = useState(false);

  useEffect(() => {
    setProfilePictureUrl(resolveProfilePictureUrl(user?.profile_picture_id ?? null));

    const fetchStats = async () => {
      try {
        setLoading(true);
        const data = await getAdminStats();
        setStats(data);
      } catch (error) {
        console.error('Failed to load admin statistics', error);
        toast.error('Unable to load dashboard statistics right now.');
      } finally {
        setLoading(false);
      }
    };

    fetchStats();
  }, [user]);

  const fetchActiveSessions = async () => {
    try {
      setActiveSessionsLoading(true);
  const data = await getAdminActiveSessions();
  setActiveSessions(Array.isArray(data?.sessions) ? data.sessions : []);
      if (typeof data?.count === 'number') {
        setStats((previous) =>
          previous
            ? {
                ...previous,
                active_sessions: data.count,
              }
            : previous
        );
      }
    } catch (error) {
      console.error('Failed to load active sessions', error);
      setActiveSessions([]);
      toast.error('Unable to load active sessions right now.');
    } finally {
      setActiveSessionsLoading(false);
    }
  };

  const handleActiveSessionsClick = () => {
    setShowActiveSessions(true);
    void fetchActiveSessions();
  };

  const statCards: StatCardData[] = [
    {
      id: 'total-users',
      title: 'Total Users',
      description: 'Registered community members',
      icon: Users,
      value: stats?.total_users ?? '—',
      isLoading: loading,
    },
    {
      id: 'active-sessions',
      title: 'Active Sessions',
      description: 'Logged in within 24 hours',
      icon: Activity,
      value: stats?.active_sessions ?? '—',
      isLoading: loading,
      onClick: handleActiveSessionsClick,
    },
    {
      id: 'confessions',
      title: 'Total Confessions',
      description: 'Posts currently in the system',
      icon: MessageCircle,
      value: stats?.total_confessions ?? '—',
      isLoading: loading,
    },
    {
      id: 'love-notes',
      title: 'Love Notes',
      description: 'Submitted love notes',
      icon: Heart,
      value: stats?.total_love_notes ?? '—',
      isLoading: loading,
    },
    {
      id: 'pending-love-notes',
      title: 'Pending Love Notes',
      description: 'Awaiting your review',
      icon: Mail,
      value: stats?.pending_love_notes ?? '—',
      isLoading: loading,
    },
    {
      id: 'blocked-users',
      title: 'Blocked Users',
      description: 'Users restricted by admins',
      icon: ShieldAlert,
      value: stats?.blocked_users ?? '—',
      isLoading: loading,
    }
  ];

  const activeSessionAccountCount = new Set(
    activeSessions
      .map((session) => session.user?.id)
      .filter((value): value is string => Boolean(value))
  ).size;

  const quickActions: QuickAction[] = [
    {
      id: 'confessions',
      title: 'Review Confessions',
      description: 'See reports, reactions and remove harmful content.',
      icon: ShieldAlert,
      to: '/admin/confessions'
    },
    {
      id: 'love-notes',
      title: 'Moderate Love Notes',
      description: 'Approve or reject submitted love notes.',
      icon: Mail,
      to: '/admin/love-notes'
    },
    {
      id: 'matchmaking',
      title: 'Matchmaking Queue',
      description: 'Review recent matchmaking submissions.',
      icon: Heart,
      to: '/admin/matchmaking'
    },
    {
      id: 'profiles',
      title: 'Profile Review',
      description: 'Inspect user profiles and manage blocks.',
      icon: UsersRound,
      to: '/admin/profile-review'
    },
    {
      id: 'reports',
      title: 'Reports Inbox',
      description: 'Stay ahead of flagged activity and feedback.',
      icon: ClipboardList,
      to: '/admin/confessions'
    }
  ];

  const handleLogout = () => {
    logout();
    toast.success('Logged out of admin mode.');
  };

  const handleNavigate = (location: string) => {
    navigate(location);
  };

  return (
    <div className="relative min-h-screen w-full overflow-hidden text-white">
      <AdminNavigation />

      <div className="fixed inset-0 -z-10">
        <video
          poster={DashboardWebp}
          autoPlay
          loop
          muted
          playsInline
          className="h-full w-full object-cover"
        >
          <source src={DashboardWebm} type="video/webm" />
          <source src={DashboardMp4} type="video/mp4" />
        </video>
        <div className="absolute inset-0 bg-gradient-to-b from-black/70 via-black/40 to-black/80" />
      </div>

      <div className="relative z-10 mx-auto flex min-h-screen w-full max-w-6xl flex-col gap-8 px-4 pb-16 pt-28">
        <header className="flex flex-col gap-6 rounded-3xl border border-white/10 bg-black/20 p-6 shadow-xl backdrop-blur">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <p className="text-sm uppercase tracking-wider text-white/60">Administrator Dashboard</p>
              <h1 className="mt-1 text-3xl font-semibold sm:text-4xl">
                Welcome back, {user?.Name ?? 'Admin'}
              </h1>
              <p className="mt-2 max-w-xl text-sm text-white/70">
                Review community activity, moderate submissions, and keep ConfessIt safe for everyone.
              </p>
            </div>
            <div className="flex items-center gap-4">
              <div className="flex h-16 w-16 items-center justify-center overflow-hidden rounded-full border border-white/20 bg-white/10">
                {profilePictureUrl ? (
                  <img
                    src={profilePictureUrl}
                    alt="Admin profile"
                    className="h-full w-full object-cover"
                    onError={(event) => {
                      (event.currentTarget as HTMLImageElement).style.display = 'none';
                    }}
                  />
                ) : (
                  <span className="text-2xl font-semibold">{user?.Name?.[0] ?? 'A'}</span>
                )}
              </div>
              <div>
                <p className="text-sm text-white/60">Signed in as</p>
                <p className="text-base font-medium">{user?.email}</p>
                <p className="text-xs text-white/50">Reg. No. {user?.Regno}</p>
              </div>
              <Button
                onClick={handleLogout}
                variant="ghost"
                className="ml-auto flex items-center gap-2 rounded-full border border-white/10 bg-white/10 px-4 text-white hover:bg-white/20"
              >
                <LogOut className="h-4 w-4" />
                Logout
              </Button>
            </div>
          </div>
        </header>

        <section className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {statCards.map((card) => (
            <div key={card.id}>
              <StatCard item={card} />
            </div>
          ))}
        </section>

        <section className="grid grid-cols-1 gap-4 lg:grid-cols-5">
          <div className="lg:col-span-3 space-y-4">
            <GlassCard className="p-6">
              <div className="mb-4 flex items-center justify-between">
                <h2 className="text-lg font-semibold">Quick Actions</h2>
                <p className="text-xs text-white/60">Navigate directly to moderation tools</p>
              </div>
              <div className="grid gap-3 md:grid-cols-2">
                {quickActions.map((action) => {
                  const Icon = action.icon;
                  return (
                    <button
                      key={action.id}
                      type="button"
                      onClick={() => handleNavigate(action.to)}
                      className="group flex flex-col gap-2 rounded-2xl border border-white/10 bg-white/5 p-4 text-left transition hover:bg-white/10"
                    >
                      <div className="flex items-center gap-3">
                        <span className="rounded-xl bg-gradient-to-br from-pink-500/20 to-purple-500/20 p-2 text-white">
                          <Icon className="h-5 w-5" />
                        </span>
                        <p className="text-sm font-medium">{action.title}</p>
                      </div>
                      <p className="text-xs text-white/70">{action.description}</p>
                    </button>
                  );
                })}
              </div>
            </GlassCard>
          </div>

          <GlassCard className="lg:col-span-2 p-6">
            <div className="mb-4 flex items-center gap-2">
              <Activity className="h-5 w-5 text-pink-300" />
              <h2 className="text-lg font-semibold">Recent Admin Focus</h2>
            </div>
            <div className="space-y-3">
              {recentActivity.map((entry, index) => (
                <div
                  key={index}
                  className="rounded-xl border border-white/10 bg-white/5 p-3 text-sm text-white/80"
                >
                  {entry}
                </div>
              ))}
            </div>
          </GlassCard>
        </section>

        <footer className="pb-6 text-center text-xs text-white/50">
          ConfessIt Administration · Maintaining a safe and supportive community
        </footer>
      </div>

      <Dialog open={showActiveSessions} onOpenChange={setShowActiveSessions}>
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle>Active sessions</DialogTitle>
            <DialogDescription>
              Accounts seen in the last 24 hours. Refresh to capture the latest activity.
            </DialogDescription>
          </DialogHeader>

          {activeSessionsLoading ? (
            <div className="flex h-40 items-center justify-center">
              <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
            </div>
          ) : activeSessions.length === 0 ? (
            <div className="py-6 text-center text-sm text-muted-foreground">
              No active accounts detected in this window.
            </div>
          ) : (
            <ScrollArea className="max-h-[400px] pr-4">
              <ul className="space-y-3">
                {activeSessions.map((session) => {
                  const user = session.user || {};
                  const displayName = user.Name || user.username || user.Regno || 'Unknown user';
                  const lastSeen = session.last_seen ? new Date(session.last_seen).toLocaleString() : 'Unknown';
                  return (
                    <li key={session.session_id} className="rounded-2xl border border-border bg-muted/30 p-4">
                      <div className="flex flex-wrap items-center justify-between gap-3">
                        <div>
                          <p className="text-sm font-medium text-foreground">{displayName}</p>
                          <p className="text-xs text-muted-foreground">
                            {user.email || 'No email on file'}
                            {user.Regno ? ` · ${user.Regno}` : ''}
                          </p>
                        </div>
                        <div className="flex items-center gap-2">
                          <Badge variant="outline">{session.status}</Badge>
                          {user.is_blocked && <Badge variant="destructive">Blocked</Badge>}
                          <Badge variant={user.user_role === 'admin' ? 'default' : 'secondary'}>
                            {user.user_role || 'user'}
                          </Badge>
                        </div>
                      </div>
                      <p className="mt-2 text-xs text-muted-foreground">
                        Last seen {lastSeen}
                        {session.ip ? ` · IP ${session.ip}` : ''}
                      </p>
                      <p className="mt-1 text-xs text-muted-foreground">
                        Device: {session.device || 'Unknown device'}
                      </p>
                    </li>
                  );
                })}
              </ul>
            </ScrollArea>
          )}

          <div className="flex items-center justify-between pt-2">
            <p className="text-xs text-muted-foreground">
              Showing {activeSessions.length} session{activeSessions.length === 1 ? '' : 's'} across {activeSessionAccountCount} account{activeSessionAccountCount === 1 ? '' : 's'} in the past 24 hours.
            </p>
            <Button variant="ghost" size="sm" onClick={() => void fetchActiveSessions()} disabled={activeSessionsLoading}>
              Refresh
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
};
