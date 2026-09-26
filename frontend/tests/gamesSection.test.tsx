import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import { AuthContext, type User } from '@/contexts/AuthContext';
import { GamesLayout } from '@/components/games/GamesLayout';
import { GamesDashboardPage } from '@/pages/GamesDashboardPage';
import { GamesLeaderboardPage } from '@/pages/GamesLeaderboardPage';
import { GamesFeaturedPage } from '@/pages/GamesFeaturedPage';
import { GamesSelectPage } from '@/pages/GamesSelectPage';

/**
 * The Games section as a section, not as four unrelated pages.
 *
 * The product decision this pins down is that /games is its own area of ConfessIt: it owns
 * its navigation instead of the main navbar, that navigation survives moving between the
 * section's routes, and there is always a way back to the dashboard.
 *
 * It also checks the selection page's contract with the catalogue: only games
 * with a real implementation are offered, and pressing Play navigates to the
 * game's screen.
 */

vi.mock('sonner', () => ({
  toast: Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
  }),
  Toaster: () => null,
}));

const authValue = {
  user: { id: 'me', name: 'Ada Lovelace', regno: 'CS-9' } as User,
  logout: vi.fn(),
  isAuthenticated: true,
  isLoading: false,
  token: 't' as string | null,
  verifyMagicToken: vi.fn(),
  fetchAndSetUser: vi.fn(),
  can: () => false,
  signInPath: '/login',
};

const renderAt = (url: string) =>
  render(
    <AuthContext.Provider value={authValue}>
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route path="/games" element={<GamesLayout />}>
            <Route index element={<GamesDashboardPage />} />
            <Route path="leaderboard" element={<GamesLeaderboardPage />} />
            <Route path="featured" element={<GamesFeaturedPage />} />
            <Route path="select" element={<GamesSelectPage />} />
          </Route>
          <Route path="/dashboard" element={<div>dashboard page</div>} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );

/** The section's tab strip, which is the navigation the shell promises to keep around. */
const gamesNav = () => screen.getByRole('navigation', { name: 'Games' });

describe('the Games section shell', () => {
  it('offers every section route and the way back to the dashboard', () => {
    renderAt('/games');

    const nav = within(gamesNav());
    expect(nav.getByRole('link', { name: /dashboard/i })).toHaveAttribute('href', '/games');
    expect(nav.getByRole('link', { name: /select a game/i })).toHaveAttribute(
      'href',
      '/games/select',
    );
    expect(nav.getByRole('link', { name: /featured/i })).toHaveAttribute(
      'href',
      '/games/featured',
    );
    expect(nav.getByRole('link', { name: /leaderboard/i })).toHaveAttribute(
      'href',
      '/games/leaderboard',
    );

    // Header and footer both offer it; at least one must point at the dashboard.
    const backLinks = screen.getAllByRole('link', { name: /back to confessit/i });
    expect(backLinks.some((link) => link.getAttribute('href') === '/dashboard')).toBe(true);
  });

  it('keeps the section navigation while moving between its routes', () => {
    renderAt('/games');

    fireEvent.click(within(gamesNav()).getByRole('link', { name: /leaderboard/i }));
    expect(screen.getByRole('heading', { name: 'Leaderboards' })).toBeInTheDocument();
    expect(gamesNav()).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: /back to confessit/i }).length).toBeGreaterThan(0);

    fireEvent.click(within(gamesNav()).getByRole('link', { name: /select a game/i }));
    expect(screen.getByRole('heading', { name: 'Select a Game' })).toBeInTheDocument();
    expect(gamesNav()).toBeInTheDocument();
  });

  it('renders the dashboard as the section home', () => {
    renderAt('/games');

    expect(screen.getByRole('heading', { name: 'Welcome back, Ada' })).toBeInTheDocument();
    expect(screen.getByText('Quick access')).toBeInTheDocument();
    expect(screen.getByText('Points')).toBeInTheDocument();
  });
});

describe('selecting a game', () => {
  /**
   * The card around a game's name. GameCard marks itself as the hover group, which is the
   * one thing distinguishing "the card for this game" from the rest of the grid.
   */
  const cardFor = (name: string) =>
    screen.getByText(name).closest('.group') as HTMLElement;

  it('offers only games with a real implementation', () => {
    renderAt('/games/select');

    expect(screen.getByText('Wordle')).toBeInTheDocument();
    expect(screen.getByText('Anagram')).toBeInTheDocument();
    // Mock titles are gone: no placeholder catalogue entries.
    expect(screen.queryByText('Truth or Dare')).not.toBeInTheDocument();
    expect(screen.queryByText('Emoji Decoded')).not.toBeInTheDocument();
    expect(screen.queryByText(/coming soon/i)).not.toBeInTheDocument();
  });

  it('offers the catalogue games with their metadata and a play action', () => {
    renderAt('/games/select');

    const card = cardFor('Wordle');
    expect(within(card).getByText(/solo/i)).toBeInTheDocument();
    expect(within(card).getByRole('button', { name: /play now/i })).toBeEnabled();
    expect(within(card).getByText(/~3 min/)).toBeInTheDocument();
  });
});
