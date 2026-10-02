import { NavLink, Outlet } from 'react-router-dom';
import { ArrowLeft, Dices, Gamepad2, LayoutDashboard, Sparkles, Trophy } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { ModeToggle } from '@/components/ui/ModeToggle';
import { cn } from '@/lib/utils';

/**
 * The shell for the whole Games section.
 *
 * Games is a section of ConfessIt with its own navigation rather than a set of pages under
 * the main navbar: everything under /games renders inside this layout, so the back link, the
 * section brand and the tab strip are declared once and persist across every Games route via
 * the nested <Outlet />. The main ConfessIt navbar deliberately does not render here -- the
 * Games tabs replace it, with a single escape hatch back to /dashboard.
 */

interface GamesNavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  /** The dashboard tab should only highlight on /games itself, not on its siblings. */
  end?: boolean;
}

const gamesNavItems: GamesNavItem[] = [
  { to: '/games', label: 'Dashboard', icon: LayoutDashboard, end: true },
  { to: '/games/select', label: 'Select a Game', icon: Dices },
  { to: '/games/featured', label: 'Featured', icon: Sparkles },
  { to: '/games/leaderboard', label: 'Leaderboard', icon: Trophy },
];

/** Soft brand glows behind the section, layered over the app's own page background. */
const sectionBackdropStyle = {
  backgroundImage: [
    'radial-gradient(60% 45% at 50% 0%, hsl(var(--romantic) / 0.16), transparent 70%)',
    'radial-gradient(45% 40% at 100% 100%, hsl(var(--passion) / 0.12), transparent 70%)',
    'radial-gradient(45% 40% at 0% 85%, hsl(var(--love-deep) / 0.12), transparent 70%)',
  ].join(', '),
};

export const GamesLayout = () => (
  <div className="flex min-h-screen flex-col">
    <div
      aria-hidden
      className="pointer-events-none fixed inset-0 -z-10"
      style={sectionBackdropStyle}
    />

    <header className="sticky top-0 z-40 border-b border-border/60 bg-background/85 backdrop-blur-xl">
      <div className="mx-auto max-w-7xl px-3 sm:px-6">
        <div className="flex h-14 items-center justify-between gap-3 sm:h-16">
          {/* The one way out of the section: straight back to the main dashboard. */}
          <NavLink
            to="/dashboard"
            className="inline-flex shrink-0 items-center gap-2 rounded-full border border-border/70 bg-card/70 px-3 py-2 text-xs font-bold text-foreground/80 transition-colors hover:border-romantic/60 hover:text-romantic sm:px-4 sm:text-sm"
          >
            <ArrowLeft className="h-4 w-4" />
            <span className="sm:hidden">Back</span>
            <span className="hidden sm:inline">Back to ConfessIt</span>
          </NavLink>

          <NavLink to="/games" end className="flex min-w-0 items-center gap-2">
            <span className="rounded-xl bg-gradient-romantic p-2 shadow-[var(--shadow-romantic)]">
              <Gamepad2 className="h-4 w-4 text-white sm:h-5 sm:w-5" />
            </span>
            <span className="truncate font-display text-base font-semibold tracking-tight sm:text-lg">
              <span className="hidden font-semibold text-muted-foreground sm:inline">
                ConfessIt{' '}
              </span>
              <span className="text-romantic">Games</span>
            </span>
          </NavLink>

          <ModeToggle />
        </div>
      </div>

      {/* Section tabs. One row that scrolls sideways on small screens instead of collapsing
          into a menu, so every Games route stays reachable without extra interaction. */}
      <div className="border-t border-border/50">
        <nav className="mx-auto max-w-7xl px-2 sm:px-6" aria-label="Games">
          <ul className="no-scrollbar flex gap-1.5 overflow-x-auto py-2 sm:gap-2">
            {gamesNavItems.map((item) => (
              <li key={item.to} className="shrink-0">
                <NavLink
                  to={item.to}
                  end={item.end}
                  className={({ isActive }) =>
                    cn(
                      'inline-flex items-center gap-2 rounded-full px-3.5 py-2 text-xs font-bold uppercase tracking-wider transition-all sm:px-4 sm:text-sm',
                      isActive
                        ? 'bg-gradient-romantic text-white shadow-[var(--shadow-romantic)]'
                        : 'text-muted-foreground hover:bg-secondary/70 hover:text-foreground',
                    )
                  }
                >
                  <item.icon className="h-4 w-4" />
                  {item.label}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>
      </div>
    </header>

    <main className="mx-auto w-full max-w-7xl flex-1 px-3 py-6 sm:px-6 sm:py-9">
      <Outlet />
    </main>

    <footer className="border-t border-border/60 bg-background/60 backdrop-blur">
      <div className="mx-auto flex max-w-7xl flex-col items-center justify-between gap-2 px-3 py-4 text-xs text-muted-foreground sm:flex-row sm:px-6">
        <span className="font-semibold">ConfessIt Games</span>
        <span>More titles are on the way.</span>
        <NavLink to="/dashboard" className="font-semibold text-romantic hover:underline">
          Back to ConfessIt
        </NavLink>
      </div>
    </footer>
  </div>
);

export default GamesLayout;
