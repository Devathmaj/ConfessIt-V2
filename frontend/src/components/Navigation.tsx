import React, { useState } from 'react';
import { NavLink } from 'react-router-dom';
import { Heart, User, Menu, X, MessageSquareText, Shuffle, Mail, Gamepad2, Inbox } from 'lucide-react';
// This component is referenced in your ProfilePage.tsx, so we assume it exists here.
import { ModeToggle } from '@/components/ui/ModeToggle';

/**
 * A responsive, floating navigation bar for the application.
 * It includes navigation links, a brand logo, a theme toggle, and a profile link.
 * The mobile view uses a collapsible menu.
 */
export const Navigation = () => {
  // State to manage the visibility of the mobile menu
  const [isOpen, setIsOpen] = useState(false);

  // Configuration for navigation links to keep the code clean and maintainable.
  const navLinks = [
    { to: "/confessions", text: "Confessions", icon: MessageSquareText },
    { to: "/matchmaking", text: "Matchmaking", icon: Shuffle },
    { to: "/inbox", text: "Inbox", icon: Inbox },
    { to: "/love-notes", text: "Love Notes", icon: Mail },
    { to: "/mini-games", text: "Mini Games", icon: Gamepad2 },
  ];

  // Common Tailwind classes for NavLink components to ensure consistency.
  const baseLinkClass = "flex items-center gap-2 px-4 py-2 rounded-lg transition-all duration-300";
  const activeLinkClass = "bg-pink-100 dark:bg-pink-900/50 text-pink-600 dark:text-pink-400 font-semibold";
  const inactiveLinkClass = "text-foreground/70 hover:text-foreground hover:bg-secondary/50";

  return (
    // The main navigation bar, styled to be a floating glass-like card.
    // It's fixed to the top of the viewport and has an entry animation.
    <nav className="fixed top-4 left-4 right-4 z-50 animate-fadeInUp" style={{ animation: 'fadeInUp 0.6s ease-out' }}>
      <div className="relative max-w-7xl mx-auto rounded-xl border border-white/10 bg-background/60 backdrop-blur-xl shadow-lg">
        <div className="flex items-center justify-between h-16 px-4 sm:px-6 lg:px-8">
          
          {/* Left side: Brand/Logo */}
          <div className="flex-shrink-0">
            <NavLink to="/dashboard" className="flex items-center gap-2 group">
              <div className="p-2 bg-gradient-to-br from-pink-500 to-rose-500 rounded-lg group-hover:scale-110 transition-transform duration-300">
                <Heart className="h-6 w-6 text-white" />
              </div>
              <span className="text-2xl font-bold text-pink-600 dark:text-pink-400 group-hover:text-pink-500 transition-colors">
                ConfessIt
              </span>
            </NavLink>
          </div>

          {/* Center: Desktop Navigation Links */}
          <div className="hidden md:flex items-center space-x-2">
            {navLinks.map((link) => (
              <NavLink
                key={link.to}
                to={link.to}
                className={({ isActive }) => `${baseLinkClass} ${isActive ? activeLinkClass : inactiveLinkClass}`}
              >
                <link.icon className="h-4 w-4" />
                <span>{link.text}</span>
              </NavLink>
            ))}
          </div>

          {/* Right side: Theme Toggle and Profile Link */}
          <div className="hidden md:flex items-center space-x-2">
            <ModeToggle />
            <NavLink
              to="/profile"
              className={({ isActive }) => `p-2 rounded-full ${isActive ? activeLinkClass : inactiveLinkClass}`}
              aria-label="Profile"
            >
              <User className="h-5 w-5" />
            </NavLink>
          </div>

          {/* Mobile Menu Button */}
          <div className="flex items-center md:hidden">
            <button
              onClick={() => setIsOpen(!isOpen)}
              type="button"
              className="inline-flex items-center justify-center p-2 rounded-md text-foreground/80 hover:text-foreground hover:bg-secondary focus:outline-none"
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
          <div className="md:hidden" id="mobile-menu">
            <div className="px-2 pt-2 pb-3 space-y-1 sm:px-3">
              {navLinks.map((link) => (
                <NavLink
                  key={link.to}
                  to={link.to}
                  onClick={() => setIsOpen(false)} // Close menu on navigation
                  className={({ isActive }) => `block ${baseLinkClass} ${isActive ? activeLinkClass : inactiveLinkClass}`}
                >
                  <link.icon className="h-5 w-5" />
                  <span>{link.text}</span>
                </NavLink>
              ))}
              <div className="border-t border-border pt-4 mt-3 flex items-center justify-between px-2">
                 <NavLink
                  to="/profile"
                  onClick={() => setIsOpen(false)}
                  className={({ isActive }) => `flex-grow ${baseLinkClass} ${isActive ? activeLinkClass : inactiveLinkClass}`}
                >
                  <User className="h-5 w-5" />
                  <span>Profile</span>
                </NavLink>
                <div className="ml-4">
                  <ModeToggle />
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </nav>
  );
};

export default Navigation;
