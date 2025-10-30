import { useState, type ComponentType } from "react";
import { NavLink } from "react-router-dom";
import { Heart, LayoutDashboard, ShieldQuestion, MessageCircleHeart, UsersRound, Menu, X } from "lucide-react";
import { ModeToggle } from "@/components/ui/ModeToggle";

interface NavItem {
  to: string;
  label: string;
  icon: ComponentType<{ className?: string }>;
  exact?: boolean;
}

const navItems: NavItem[] = [
  { to: "/admin", label: "Dashboard", icon: LayoutDashboard, exact: true },
  { to: "/admin/confessions", label: "Confession Review", icon: ShieldQuestion },
  { to: "/admin/love-notes", label: "Love Notes Review", icon: MessageCircleHeart },
  { to: "/admin/matchmaking", label: "Matchmaking Review", icon: Heart },
  { to: "/admin/profile-review", label: "Profile Review", icon: UsersRound },
];

const baseLink = "flex items-center gap-2 px-4 py-2 rounded-lg text-sm transition-all duration-200";
const activeLink = "bg-pink-100 text-pink-600 dark:bg-pink-900/40 dark:text-pink-300";
const inactiveLink = "text-foreground/70 hover:text-foreground hover:bg-secondary/50";

export const AdminNavigation = () => {
  const [open, setOpen] = useState(false);

  const renderLink = (item: NavItem) => (
    <NavLink
      key={item.to}
      to={item.to}
      end={item.exact}
      onClick={() => setOpen(false)}
      className={({ isActive }) => `${baseLink} ${isActive ? activeLink : inactiveLink}`}
    >
      <item.icon className="h-4 w-4" />
      <span>{item.label}</span>
    </NavLink>
  );

  return (
    <nav className="fixed top-4 left-4 right-4 z-50">
      <div className="mx-auto flex max-w-7xl items-center justify-between rounded-2xl border border-white/10 bg-background/70 px-4 py-3 shadow-xl backdrop-blur-xl">
        <NavLink to="/admin" className="flex items-center gap-2" onClick={() => setOpen(false)}>
          <div className="rounded-xl bg-gradient-to-br from-pink-500 to-rose-500 p-2">
            <Heart className="h-5 w-5 text-white" />
          </div>
          <div className="leading-tight">
            <p className="text-lg font-semibold text-pink-600 dark:text-pink-300">ConfessIt</p>
            <p className="text-xs uppercase tracking-wide text-foreground/60">Admin</p>
          </div>
        </NavLink>

        <div className="hidden items-center gap-2 md:flex">
          {navItems.map(renderLink)}
          <ModeToggle />
        </div>

        <button
          onClick={() => setOpen((prev) => !prev)}
          className="flex items-center justify-center rounded-md p-2 text-foreground/70 hover:bg-secondary md:hidden"
        >
          <span className="sr-only">Toggle navigation</span>
          {open ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}
        </button>
      </div>

      {open && (
        <div className="mt-2 rounded-2xl border border-white/10 bg-background/95 p-4 shadow-xl backdrop-blur-xl md:hidden">
          <div className="flex flex-col gap-2">
            {navItems.map(renderLink)}
          </div>
          <div className="mt-4 flex items-center justify-between rounded-xl border border-white/10 px-3 py-2">
            <span className="text-sm font-medium text-foreground/80">Theme</span>
            <ModeToggle />
          </div>
        </div>
      )}
    </nav>
  );
};
