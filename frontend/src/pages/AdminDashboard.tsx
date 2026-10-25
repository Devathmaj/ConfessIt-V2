import { logger } from '@/lib/logger';
import { useState, useEffect, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '@/contexts/AuthContext';
import { Button } from '@/components/ui/button';
import {
  Users,
  Activity,
  MessageCircle,
  Mail,
  ShieldAlert,
  LogOut,
  UsersRound,
  ClipboardList,
  Flag,
  MessageCircleHeart,
  ArrowRight
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { toast } from 'sonner';
import { AdminNavigation } from '@/components/AdminNavigation';
import { ProfileAvatar } from '@/components/ProfileAvatar';
import { CAPABILITIES, Capability } from '@/lib/capabilities';
import { getAdminStats } from '@/services/api';

import DashboardWebm from '@/assets/Dashboard.webm';
import DashboardMp4 from '@/assets/Dashboard.mp4';
import DashboardWebp from '@/assets/Dashboard.webp';

/** The report backlog, split the way the three report queues are. */
interface PendingReportQueues {
  confessions: number;
  love_notes: number;
  messages: number;
}

interface AdminStats {
  total_users: number;
  active_sessions: number;
  total_confessions: number;
  pending_love_notes: number;
  pending_reports: number;
  pending_report_queues: PendingReportQueues;
  blocked_users: number;
}

interface StatCardData {
  id: string;
  title: string;
  description: string;
  icon: LucideIcon;
  value: string | number;
  isLoading: boolean;
}

interface QuickAction {
  id: string;
  title: string;
  description: string;
  icon: LucideIcon;
  to: string;
  /** The capability a visit to this page needs; the action is hidden without it. */
  capability: Capability;
}

interface GlassCardProps {
  children: ReactNode;
  className?: string;
}

/**
 * The tile shell.
 *
 * Not clickable, and no longer pretending to be: the dashboard's tiles are figures to read,
 * and the one that used to open a panel of sessions behind it is a plain number now. Anything
 * on this page you act on is a real `<button>` that navigates, so there is no card here that
 * looks live and is not.
 */
const GlassCard = ({ children, className = '' }: GlassCardProps) => (
  <div
    className={`group relative overflow-hidden rounded-2xl border border-white/10 bg-white/5 backdrop-blur-xl shadow-lg transition-all duration-300 hover:bg-white/10 hover:border-white/20 ${className}`}
  >
    <div className="absolute -inset-1 rounded-2xl bg-gradient-to-r from-pink-600/10 to-purple-600/10 opacity-0 transition-opacity duration-500 group-hover:opacity-100" />
    <div className="relative z-10 h-full">{children}</div>
  </div>
);

const StatCard = ({ item }: { item: StatCardData }) => {
  const Icon = item.icon;

  return (
    <GlassCard className="p-6 flex flex-col justify-between">
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

export const AdminDashboard = () => {
  const { user, logout, can } = useAuth();
  const navigate = useNavigate();
  const [stats, setStats] = useState<AdminStats | null>(null);
  const [loading, setLoading] = useState<boolean>(true);

  // Every card below reads `/admin/stats`, which is `stats:read` -- so this page is only ever
  // mounted for an admin, and a moderator's landing page is the moderation hub instead.
  useEffect(() => {
    const fetchStats = async () => {
      try {
        setLoading(true);
        const data = await getAdminStats();
        setStats(data);
      } catch (error) {
        logger.error('Failed to load admin statistics', error);
        toast.error('Unable to load dashboard statistics right now.');
      } finally {
        setLoading(false);
      }
    };

    fetchStats();
  }, [user]);

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
      id: 'active-today',
      title: 'Active Today',
      description: 'Accounts that signed in within 24 hours',
      icon: Activity,
      value: stats?.active_sessions ?? '—',
      isLoading: loading,
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
      // Work someone is waiting on, rather than a total of everything ever sent: the queues
      // below this row are what an admin opens, and a report already ruled on is not a task.
      id: 'pending-reports',
      title: 'Pending Reports',
      description: 'Filed and still waiting on a ruling',
      icon: Flag,
      value: stats?.pending_reports ?? '—',
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

  // The pending-report figure is only a number; this is where it comes from. Every row is a
  // button into the review screen, so the breakdown is something to act on rather than a
  // second figure to read, and the three counts come from the same fields as the tile above
  // them -- one request, one set of numbers, no chance of the two disagreeing.
  const reportQueues: Array<{
    id: keyof PendingReportQueues;
    title: string;
    caption: string;
    icon: LucideIcon;
    value: number;
  }> = [
    {
      id: 'confessions',
      title: 'Posts & comments',
      caption: 'Reported by a participant',
      icon: ShieldAlert,
      value: stats?.pending_report_queues?.confessions ?? 0,
    },
    {
      id: 'love_notes',
      title: 'Love notes',
      caption: 'Reported by the recipient',
      icon: MessageCircleHeart,
      value: stats?.pending_report_queues?.love_notes ?? 0,
    },
    {
      id: 'messages',
      title: 'Chat messages',
      caption: 'Reported in a private conversation',
      icon: MessageCircle,
      value: stats?.pending_report_queues?.messages ?? 0,
    },
  ];

  const quickActions: QuickAction[] = [
    {
      id: 'reports',
      title: 'Reports Review',
      description: 'Everything reported, whether a post, a love note or a chat message.',
      icon: ClipboardList,
      to: '/admin/reports',
      capability: CAPABILITIES.REPORTS_REVIEW
    },
    {
      id: 'confessions',
      title: 'Review Confessions',
      description: 'Read reactions and remove harmful content.',
      icon: ShieldAlert,
      to: '/admin/confessions',
      capability: CAPABILITIES.CONFESSIONS_REVIEW
    },
    {
      id: 'love-notes',
      title: 'Moderate Love Notes',
      description: 'Approve or reject submitted love notes.',
      icon: Mail,
      to: '/admin/love-notes',
      capability: CAPABILITIES.LOVE_NOTES_REVIEW
    },
    {
      id: 'profiles',
      title: 'Profile Review',
      description: 'Inspect user profiles and manage blocks.',
      icon: UsersRound,
      to: '/admin/profile-review',
      capability: CAPABILITIES.USERS_REVIEW
    }
  ].filter((action) => can(action.capability));

  const handleLogout = () => {
    void logout();
    toast.success('Logged out of admin mode.');
  };

  const handleNavigate = (location: string) => {
    navigate(location);
  };

  return (
    <div className="relative min-h-screen w-full overflow-hidden text-white">
      {/* Dashboard navbar is always dark, regardless of the user's theme (see UserDashboard). */}
      <div className="dark" data-theme="dark">
        <AdminNavigation />
      </div>

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
                Welcome back, {user?.name ?? 'Admin'}
              </h1>
              <p className="mt-2 max-w-xl text-sm text-white/70">
                Review community activity, moderate submissions, and keep ConfessIt safe for everyone.
              </p>
            </div>
            <div className="flex items-center gap-4">
              <ProfileAvatar
                src={user?.profile_picture_url}
                name={user?.name}
                className="h-16 w-16 border border-white/20"
              />
              <div>
                <p className="text-sm text-white/60">Signed in as</p>
                <p className="text-base font-medium">{user?.email}</p>
                <p className="text-xs text-white/50">Reg. No. {user?.regno}</p>
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
            <div className="mb-4 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Flag className="h-5 w-5 text-pink-300" />
                <h2 className="text-lg font-semibold">Report Backlog</h2>
              </div>
              <p className="text-xs text-white/60">Where the backlog is</p>
            </div>

            <div className="space-y-3">
              {reportQueues.map((queue) => {
                const Icon = queue.icon;
                return (
                  <button
                    key={queue.id}
                    type="button"
                    onClick={() => handleNavigate('/admin/reports')}
                    className="group flex w-full items-center gap-3 rounded-xl border border-white/10 bg-white/5 p-3 text-left transition hover:bg-white/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-pink-400"
                  >
                    <span className="rounded-lg bg-gradient-to-br from-pink-500/20 to-purple-500/20 p-2 text-white">
                      <Icon className="h-4 w-4" />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block text-sm font-medium">{queue.title}</span>
                      <span className="block text-xs text-white/60">{queue.caption}</span>
                    </span>
                    <span className="text-xl font-semibold tabular-nums">
                      {loading ? '—' : queue.value}
                    </span>
                    <ArrowRight className="h-4 w-4 text-white/40 transition group-hover:translate-x-0.5" />
                  </button>
                );
              })}
            </div>

            <p className="mt-4 text-xs text-white/50">
              Reports waiting on a decision. Each queue is opened in Reports Review, where the
              report can be dismissed or the content behind it taken down.
            </p>
          </GlassCard>
        </section>

        <footer className="pb-6 text-center text-xs text-white/50">
          ConfessIt Administration · Maintaining a safe and supportive community
        </footer>
      </div>
    </div>
  );
};
