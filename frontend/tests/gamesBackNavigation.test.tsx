import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { GamesLayout } from '@/components/games/GamesLayout';
import {
  lastConfessItPage,
  lastGamesPage,
  rememberVisitedPage,
} from '@/lib/navigationMemory';

/**
 * The two escape hatches out of the Games section:
 *
 * - "Back to ConfessIt" returns to the last main-app page the person had open,
 *   with its query string, instead of a hard-coded dashboard.
 * - A game screen offers its own "Back to games" control, returning to the last
 *   Games page that launched it (selection grid, spotlight, dashboard), never to
 *   another game and never to nowhere on a direct deep link.
 *
 * The app records visits through `rememberVisitedPage`; here it is called the way
 * the router tracker calls it, so the layout reads the same memory it does in the
 * running app.
 */

const renderGames = (entries: string[], initialIndex = entries.length - 1) =>
  render(
    <MemoryRouter initialEntries={entries} initialIndex={initialIndex}>
      <Routes>
        <Route path="/games" element={<GamesLayout />}>
          <Route index element={<div>games dashboard screen</div>} />
          <Route path="select" element={<div>select screen</div>} />
          <Route path="featured" element={<div>featured screen</div>} />
          <Route path="leaderboard" element={<div>leaderboard screen</div>} />
          <Route path="wordle" element={<div>wordle screen</div>} />
        </Route>
        <Route path="/confessions" element={<div>confessions screen</div>} />
      </Routes>
    </MemoryRouter>,
  );

beforeEach(() => {
  window.sessionStorage.clear();
});

afterEach(() => {
  window.sessionStorage.clear();
});

describe('Back to ConfessIt', () => {
  it('returns to the last main-app page, query string included', () => {
    rememberVisitedPage('/confessions', '?sort=recent');
    renderGames(['/confessions', '/games'], 1);

    const links = screen.getAllByRole('link', { name: /back to confessit/i });
    expect(links.length).toBeGreaterThan(0);
    for (const link of links) {
      expect(link).toHaveAttribute('href', '/confessions?sort=recent');
    }
  });

  it('falls back to the dashboard when this tab has no remembered page', () => {
    renderGames(['/games']);

    expect(screen.getAllByRole('link', { name: /back to confessit/i })[0]).toHaveAttribute(
      'href',
      '/dashboard',
    );
  });

  it('does not remember the Games section or door screens as the way back', () => {
    rememberVisitedPage('/confessions');
    rememberVisitedPage('/games/wordle');
    rememberVisitedPage('/games/select');
    rememberVisitedPage('/login');
    rememberVisitedPage('/verify-login');
    rememberVisitedPage('/');

    expect(lastConfessItPage()).toBe('/confessions');
  });
});

describe('a game screen back control', () => {
  it('returns to the Games page that launched the game', () => {
    rememberVisitedPage('/games/select');
    renderGames(['/games/wordle']);

    const back = screen.getByRole('link', { name: /back to games/i });
    expect(back).toHaveAttribute('href', '/games/select');

    fireEvent.click(back);
    expect(screen.getByText('select screen')).toBeInTheDocument();
  });

  it('keeps the spotlight or dashboard the game was opened from', () => {
    rememberVisitedPage('/games/featured');
    const first = renderGames(['/games/wordle']);
    expect(screen.getByRole('link', { name: /back to games/i })).toHaveAttribute(
      'href',
      '/games/featured',
    );
    first.unmount();

    rememberVisitedPage('/games');
    renderGames(['/games/wordle']);
    expect(screen.getByRole('link', { name: /back to games/i })).toHaveAttribute(
      'href',
      '/games',
    );
  });

  it('falls back to the Games dashboard for a direct deep link', () => {
    renderGames(['/games/wordle']);

    const back = screen.getByRole('link', { name: /back to games/i });
    expect(back).toHaveAttribute('href', '/games');
    fireEvent.click(back);
    expect(screen.getByText('games dashboard screen')).toBeInTheDocument();
  });

  it('is absent on section pages, so a game never backs into another game', () => {
    rememberVisitedPage('/games/select');
    renderGames(['/games/leaderboard']);

    expect(screen.queryByRole('link', { name: /back to games/i })).not.toBeInTheDocument();
    // A play route is never recorded as the destination.
    expect(lastGamesPage()).toBe('/games/select');
  });
});
