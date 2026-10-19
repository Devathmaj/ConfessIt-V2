import React, { useState } from 'react';
import { NavLink } from 'react-router-dom';
import { Heart, User, Menu, X, MessageSquareText, Shuffle, Mail, Gamepad2, Bell, MessageCircle, LogOut } from 'lucide-react';
import { toast } from 'sonner';
// This component is referenced in your ProfilePage.tsx, so we assume it exists here.
import { ModeToggle } from '@/components/ui/ModeToggle';
import { useAuth } from '@/contexts/AuthContext';

/**
 * A responsive, floating navigation bar for the application.
 * It includes navigation links, a brand logo, a theme toggle, and a profile link.
 * The mobile view uses a collapsible menu.
 */
export const Navigation = () => {
  // State to manage the visibility of the mobile menu
  const [isOpen, setIsOpen] = useState(false);
  const { logout, isAuthenticated } = useAuth();

  /**
   * Used to log the user out of the application and display a farewell message.
   */
  const handleLogout = () => {
    void logout();
    setIsOpen(false);
    toast.success('Goodbye! Come back soon ❤️');
  };

  // Configuration for navigation links to keep the code clean and maintainable.
  const navLinks = [
    { to: "/confessions", text: "Confess", icon: MessageSquareText },
    { to: "/matchmaking", text: "Match", icon: Shuffle },
    { to: "/notifications", text: "Alerts", icon: Bell },
    { to: "/inbox", text: "Inbox", icon: MessageCircle },
    { to: "/love-notes", text: "Notes", icon: Mail },
    { to: "/games", text: "Games", icon: Gamepad2 },
  ];

  // Common Tailwind classes for NavLink components to ensure consistency.
  // Added w-full to maintain consistent size and prevent enlargement on active state
  // Nav states (color guide §6.2): quiet secondary text; active = visible surface +
  // readable text + marker icon + weight (never color alone). Transparent border
  // reserves the active border width so toggling never shifts layout.
  const baseLinkClass = "flex items-center gap-2 px-3 py-2 min-h-[44px] border border-transparent transition-colors duration-200 whitespace-nowrap [border-radius:var(--edge-pill)]";
  const activeLinkClass = "bg-[var(--nav-active-bg)] text-[var(--nav-active-text)] border-[var(--border)] font-semibold [&_svg]:text-[var(--nav-active-marker)]";
  const inactiveLinkClass = "text-foreground/70 hover:text-foreground hover:bg-secondary/50";

  return (
    // The main navigation bar, styled to be a floating glass-like card.
    // It's fixed to the top of the viewport and has an entry animation.
    // Improved visibility on tablets and proper spacing to prevent overlap
    <nav className="fixed top-4 left-2 right-2 sm:left-4 sm:right-4 z-50 animate-fadeInUp" style={{ animation: 'fadeInUp 0.6s ease-out' }}>
      <div className="relative max-w-7xl mx-auto border border-[var(--nav-border)] bg-[var(--nav-bg)] backdrop-blur-xl shadow-[var(--shadow-nav)] [border-radius:var(--edge-card-sm)]">
        <div className="flex items-center justify-between h-16 px-3 sm:px-4 lg:px-6">
          
          {/* Left side: Brand/Logo */}
          <div className="flex-shrink-0">
            <NavLink to="/dashboard" className="flex items-center gap-2 group">
              <div className="p-2 bg-[var(--brand)] group-hover:scale-110 transition-transform duration-300 [border-radius:var(--edge-icon)]">
                <Heart className="h-5 w-5 sm:h-6 sm:w-6 text-white" />
              </div>
              <span className="text-xl sm:text-2xl font-bold brand-wordmark transition-colors">
                ConfessIt
              </span>
            </NavLink>
          </div>

          {/* Center: Desktop Navigation Links - Hidden on tablets (including iPad Pro), shown on large desktops */}
          <div className="hidden xl:flex items-center gap-1 2xl:gap-2">
            {navLinks.map((link) => (
              <NavLink
                key={link.to}
                to={link.to}
                className={({ isActive }) => `${baseLinkClass} ${isActive ? activeLinkClass : inactiveLinkClass}`}
              >
                <link.icon className="h-4 w-4 flex-shrink-0" />
                <span className="text-sm">{link.text}</span>
              </NavLink>
            ))}
          </div>

          {/* Right side: Theme Toggle, Profile Link and Logout */}
          <div className="hidden xl:flex items-center gap-2">
            <ModeToggle />
            <NavLink
              to="/profile"
              className={({ isActive }) => `p-2 transition-colors duration-200 [border-radius:var(--edge-pill)] ${isActive ? activeLinkClass : inactiveLinkClass}`}
              aria-label="Profile"
            >
              <User className="h-5 w-5" />
            </NavLink>
            {isAuthenticated && (
              <button
                type="button"
                onClick={handleLogout}
                aria-label="Logout"
                title="Logout"
                className="p-2 text-foreground/70 hover:text-destructive hover:bg-secondary/50 transition-colors duration-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring [border-radius:var(--edge-pill)]"
              >
                <LogOut className="h-5 w-5" />
              </button>
            )}
          </div>

          {/* Mobile/Tablet Menu Button - Show on screens smaller than xl (including iPad Pro) */}
          <div className="flex items-center xl:hidden">
            <button
              onClick={() => setIsOpen(!isOpen)}
              type="button"
              className="inline-flex items-center justify-center p-2 text-foreground/80 hover:text-foreground hover:bg-secondary focus:outline-none [border-radius:var(--edge-control)]"
              aria-controls="mobile-menu"
              aria-expanded={isOpen}
            >
              <span className="sr-only">Open main menu</span>
              {isOpen ? <X className="block h-6 w-6" /> : <Menu className="block h-6 w-6" />}
            </button>
          </div>
        </div>

        {/* Mobile Menu Dropdown */}
        {isOpen && (
          <div className="xl:hidden border-t border-border/50" id="mobile-menu">
            <div className="px-2 pt-2 pb-3 space-y-1">
              {navLinks.map((link) => (
                <NavLink
                  key={link.to}
                  to={link.to}
                  onClick={() => setIsOpen(false)} // Close menu on navigation
                  className={({ isActive }) => `block ${baseLinkClass} ${isActive ? activeLinkClass : inactiveLinkClass}`}
                >
                  <link.icon className="h-5 w-5 flex-shrink-0" />
                  <span className="text-sm">{link.text}</span>
                </NavLink>
              ))}
              <div className="border-t border-border/50 pt-3 mt-3 flex items-center justify-between px-2">
                 <NavLink
                  to="/profile"
                  onClick={() => setIsOpen(false)}
                  className={({ isActive }) => `flex-grow ${baseLinkClass} ${isActive ? activeLinkClass : inactiveLinkClass}`}
                >
                  <User className="h-5 w-5 flex-shrink-0" />
                  <span className="text-sm">Profile</span>
                </NavLink>
                <div className="ml-4 flex-shrink-0">
                  <ModeToggle />
                </div>
              </div>
              {isAuthenticated && (
                <button
                  type="button"
                  onClick={handleLogout}
                  className={`w-full ${baseLinkClass} text-destructive hover:bg-destructive/10`}
                >
                  <LogOut className="h-5 w-5 flex-shrink-0" />
                  <span className="text-sm">Logout</span>
                </button>
              )}
            </div>
          </div>
        )}
      </div>
    </nav>
  );
};

export default Navigation;
