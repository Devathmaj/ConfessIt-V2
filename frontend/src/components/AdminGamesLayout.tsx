import { NavLink, Outlet } from 'react-router-dom';
import { Dices, LayoutDashboard, Trophy, type LucideIcon } from 'lucide-react';
import { AdminNavigation } from '@/components/AdminNavigation';
import { cn } from '@/lib/utils';

/**
 * The shell for `/admin/games`: one place that declares the section's own navigation, so the
 * three screens share an address bar, a section identity and a guard instead of each page
 * re-declaring its siblings' links.
 *
 * Mirrors the player-facing `GamesLayout` deliberately -- parent route, tabs, `<Outlet />` --
 * because the two sections are the same idea from opposite sides: one navigates the games a
 * player can open, the other navigates the games an administrator can change. The route
 * carrying this layout is the one wrapped in `CapabilityRoute`, so every child inherits
 * admin-only access without repeating it three times.
 */
interface GamesAdminTab {
  to: string;
  label: string;
  icon: LucideIcon;
  /** Only the overview highlights on `/admin/games` itself, not on its siblings. */
  end?: boolean;
}

const gamesAdminTabs: GamesAdminTab[] = [
  { to: '/admin/games', label: 'Overview', icon: LayoutDashboard, end: true },
  { to: '/admin/games/manage', label: 'Manage games', icon: Dices },
  { to: '/admin/games/leaderboard', label: 'Leaderboard', icon: Trophy },
];

export const AdminGamesLayout = () => (
  <div className="relative min-h-screen bg-background">
    <AdminNavigation />

    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-4 pb-16 pt-28">
      <nav
        aria-label="Games section"
        className="flex flex-wrap items-center gap-2 border-b border-border/60 pb-4"
      >
        {gamesAdminTabs.map((tab) => (
          <NavLink
            key={tab.to}
            to={tab.to}
            end={tab.end}
            className={({ isActive }) =>
              cn(
                'inline-flex items-center gap-2 [border-radius:var(--edge-chip)] px-3.5 py-2 text-xs font-bold uppercase tracking-wider transition-all sm:px-4 sm:text-sm',
                isActive
                  ? 'bg-gradient-romantic text-white shadow-[var(--shadow-romantic)]'
                  : 'text-muted-foreground hover:bg-secondary/70 hover:text-foreground',
              )
            }
          >
            <tab.icon className="h-4 w-4" />
            {tab.label}
          </NavLink>
        ))}
      </nav>

      <Outlet />
    </div>
  </div>
);

export default AdminGamesLayout;
