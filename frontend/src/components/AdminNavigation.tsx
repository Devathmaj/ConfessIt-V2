import { useState, type ComponentType } from "react";
import { NavLink } from "react-router-dom";
import { Heart, LayoutDashboard, Settings, ShieldQuestion, MessageCircleHeart, UsersRound, Menu, X, Flag, ClipboardCheck, LogOut, Gamepad2, Bell } from "lucide-react";
import { ModeToggle } from "@/components/ui/ModeToggle";
import { useAuth } from "@/contexts/AuthContext";
import { CAPABILITIES, Capability } from "@/lib/capabilities";
import { landingAreaLabel, landingPathFor } from "@/lib/landing";
import { toast } from "sonner";

interface NavItem {
  to: string;
  label: string;
  icon: ComponentType<{ className?: string }>;
  exact?: boolean;
  /**
   * The capability a visit to this page needs. Each one is the same capability that gates the
   * endpoints the page calls, so a link is shown only when opening it would actually work.
   */
  capability: Capability;
  /**
   * A capability that disqualifies the account even when it holds `capability` -- the same pair
   * of conditions the matching `CapabilityRoute` declares. Only the hub needs it: an admin holds
   * every moderation capability, so `reports:review` alone would offer them the moderation
   * dashboard, which is not on an admin's way anywhere and whose route refuses them anyway.
   */
  without?: Capability;
}

const navItems: NavItem[] = [
  { to: "/admin", label: "Dashboard", icon: LayoutDashboard, exact: true, capability: CAPABILITIES.STATS_READ },
  // The hub calls no endpoint of its own -- it counts the queues it links to, each gated
  // separately -- so its capability is the queue it is built around rather than one of its own
  // requests. It is the same question the route asks, plus the exclusion, so an account that
  // cannot open it is never offered it and an admin is never shown a second landing page.
  {
    to: "/moderation",
    label: "Moderation",
    icon: ClipboardCheck,
    exact: true,
    capability: CAPABILITIES.REPORTS_REVIEW,
    without: CAPABILITIES.STATS_READ,
  },
  { to: "/admin/configuration", label: "Config", icon: Settings, capability: CAPABILITIES.APP_CONFIG_READ },
  // The Games console manages the catalogue rather than a moderation queue, but it asks the
  // route the same question every other item here does. `stats:read` is admin-only, which is
  // exactly what the section is, so a moderator is never offered a link they would be refused.
  { to: "/admin/games", label: "Games", icon: Gamepad2, capability: CAPABILITIES.STATS_READ },
  { to: "/admin/reports", label: "Reports", icon: Flag, capability: CAPABILITIES.REPORTS_REVIEW },
  // One inbox per role, like the Dashboard/Moderation pair above: an admin's
  // notices live under the console path and a moderator's under the hub path,
  // and the route guards refuse each account the other's URL.
  { to: "/admin/notifications", label: "Alerts", icon: Bell, capability: CAPABILITIES.STATS_READ },
  {
    to: "/moderation/notifications",
    label: "Alerts",
    icon: Bell,
    capability: CAPABILITIES.REPORTS_REVIEW,
    without: CAPABILITIES.STATS_READ,
  },
  { to: "/admin/confessions", label: "Confessions", icon: ShieldQuestion, capability: CAPABILITIES.CONFESSIONS_REVIEW },
  { to: "/admin/love-notes", label: "Notes", icon: MessageCircleHeart, capability: CAPABILITIES.LOVE_NOTES_REVIEW },
  { to: "/admin/profile-review", label: "Profiles", icon: UsersRound, capability: CAPABILITIES.USERS_REVIEW },
];

const baseLink = "flex shrink-0 items-center gap-2 whitespace-nowrap px-4 py-2 min-h-[44px] rounded-full border border-transparent text-sm transition-all duration-200";
const activeLink = "bg-[var(--nav-active-bg)] text-[var(--nav-active-text)] border-[var(--border)] font-semibold [&_svg]:text-[var(--nav-active-marker)]";
const inactiveLink = "text-foreground/70 hover:text-foreground hover:bg-secondary/50";

/**
 * The bar every moderation screen carries.
 *
 * The wordmark's second line names the area rather than the URL prefix. That is a deliberate
 * departure from the paths: the review screens live under `/admin/*` because those routes
 * predate the split, but "admin" there is a routing prefix and not a statement about who is
 * reading. A moderator who opens `/admin/reports` is inside the moderation area, so the bar
 * says Moderation for them on every screen -- including this one's siblings -- and says Admin
 * only to an account whose own landing page is the console.
 *
 * Nothing here is a capability check on its own account, though: the links are filtered by
 * capability, so the bar never offers a screen the account could not open, and each label is
 * answered from the same `landingPathFor` the wordmark links to.
 */
export const AdminNavigation = () => {
  const [open, setOpen] = useState(false);
  const { can, logout, isAuthenticated } = useAuth();

  const handleLogout = () => {
    logout();
    setOpen(false);
    toast.success("Logged out of staff area.");
  };

  // Filtered rather than individually hidden so a moderator is simply not offered the
  // account-management screen. The route guard refuses it either way; omitting the link
  // means it is never presented as something they can open.
  //
  // Which also drops "Dashboard" for a moderator: that link is the administration console, and
  // the console is behind `stats:read`, so a moderator is offered "Moderation" in its place.
  // And it drops "Moderation" for an admin, who holds every moderation capability and would
  // otherwise be pointed at the hub as though it were a second place to work.
  const visibleItems = navItems.filter(
    (item) => can(item.capability) && !(item.without !== undefined && can(item.without)),
  );

  // The wordmark goes back to whichever landing page this account belongs on rather than
  // always to the console, so a moderator clicking it lands on their own dashboard instead of
  // being turned away from this one.
  const home = landingPathFor(can);
  const area = landingAreaLabel(can);

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
      <div className="mx-auto flex max-w-7xl items-center justify-between rounded-2xl border border-[var(--nav-border)] bg-[var(--nav-bg)] px-4 py-3 shadow-[var(--shadow-nav)] backdrop-blur-xl">
        <NavLink to={home} className="flex items-center gap-2 flex-shrink-0" onClick={() => setOpen(false)}>
          <div className="rounded-xl bg-[var(--brand)] p-2">
            <Heart className="h-5 w-5 text-white" />
          </div>
          <div className="leading-tight">
            <p className="text-lg font-semibold brand-wordmark">ConfessIt</p>
            <p className="text-xs uppercase tracking-wide text-foreground/60">{area}</p>
          </div>
        </NavLink>

        {/* The middle row owns the leftover width and may shrink to nothing: without
            `min-w-0` a flex child refuses to go narrower than its content, which is what
            pushed the pills past the bar's right edge once the Alerts link made eight. */}
        <div className="flex-1 flex justify-center min-w-0">
          {/* Full pill row only from `xl` up: eight pills plus the wordmark and actions
              need ~1200px, so at `md`/`lg` the bar shows the hamburger instead of
              overflowing the container to the right. */}
          <div className="hidden items-center gap-2 xl:flex">
            {visibleItems.map(renderLink)}
          </div>
        </div>

        <div className="hidden items-center gap-2 xl:flex flex-shrink-0">
          <ModeToggle />
          {isAuthenticated && (
            <button
              type="button"
              onClick={handleLogout}
              aria-label="Logout"
              title="Logout"
              className="flex items-center gap-2 rounded-lg px-3 py-2 text-sm text-destructive transition-all duration-200 hover:bg-destructive/10"
            >
              <LogOut className="h-4 w-4" />
              Logout
            </button>
          )}
        </div>

        <div className="flex-shrink-0 xl:hidden">
          <button
            onClick={() => setOpen((prev) => !prev)}
            className="flex items-center justify-center rounded-md p-2 text-foreground/70 hover:bg-secondary"
          >
            <span className="sr-only">Toggle navigation</span>
            {open ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}
          </button>
        </div>
      </div>

      {open && (
        <div className="mt-2 rounded-2xl border border-[var(--nav-border)] bg-[var(--nav-bg)] p-4 shadow-[var(--shadow-nav)] backdrop-blur-xl xl:hidden">
          <div className="flex flex-col gap-2">
            {visibleItems.map(renderLink)}
          </div>
          <div className="mt-4 space-y-2">
            <div className="flex items-center justify-between rounded-xl border border-[var(--nav-border)] px-3 py-2">
              <span className="text-sm font-medium text-foreground/80">Theme</span>
              <ModeToggle />
            </div>
            {isAuthenticated && (
              <button
                type="button"
                onClick={handleLogout}
                className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-sm text-destructive transition-colors hover:bg-destructive/10"
              >
                <LogOut className="h-4 w-4" />
                Logout
              </button>
            )}
          </div>
        </div>
      )}
    </nav>
  );
};
