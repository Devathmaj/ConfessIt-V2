import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import { GamesFeaturedPage } from '@/pages/GamesFeaturedPage';
import { GamesConfigProvider } from '@/contexts/GamesConfigContext';
import { AVAILABLE_GAMES } from '@/lib/gamesCatalog';
import { initialGamesConfig, resolveFeaturedGame } from '@/lib/gamesConfig';
import { getFeaturedToday } from '@/services/api';

vi.mock('@/services/api', () => ({
  getFeaturedToday: vi.fn(),
}));

/**
 * Regression: navigating to /games/featured rendered the locally-resolved
 * fallback game for a split second before the server answered, then swapped
 * to the real spotlight (or the empty state). The page must show a loading
 * skeleton until the server responds -- never a game it hasn't confirmed.
 */

type SpotlightResponse = {
  game_id: string | null;
  game_date: string | null;
  server_date: string;
};

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={['/games/featured']}>
      <GamesConfigProvider>
        <GamesFeaturedPage />
      </GamesConfigProvider>
    </MemoryRouter>,
  );

/** The game the provider's seeded schedule resolves to -- the old flash. */
const localFallbackName = () =>
  resolveFeaturedGame(initialGamesConfig())?.game.name;

describe('GamesFeaturedPage spotlight loading', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows a skeleton -- not the local fallback game -- while the server is answering', () => {
    vi.mocked(getFeaturedToday).mockReturnValue(new Promise(() => {}));
    renderPage();

    expect(
      screen.getByRole('status', { name: /loading today's featured game/i }),
    ).toBeInTheDocument();

    const fallback = localFallbackName();
    expect(fallback).toBeTruthy();
    if (fallback) {
      expect(
        screen.queryByRole('heading', { name: fallback }),
      ).not.toBeInTheDocument();
    }
    expect(
      screen.queryByText('No featured game right now'),
    ).not.toBeInTheDocument();
  });

  it('shows the server game once it answers', async () => {
    let resolveRequest!: (value: SpotlightResponse) => void;
    vi.mocked(getFeaturedToday).mockReturnValue(
      new Promise<SpotlightResponse>((resolve) => {
        resolveRequest = resolve;
      }),
    );
    renderPage();

    const pick = AVAILABLE_GAMES[0];
    await act(async () => {
      resolveRequest({
        game_id: pick.id,
        game_date: null,
        server_date: new Date().toISOString(),
      });
    });

    expect(
      await screen.findByRole('heading', { name: pick.name }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('status', { name: /loading today's featured game/i }),
    ).not.toBeInTheDocument();
  });

  it('shows the empty state -- with no flash of a game -- when nothing is featured', async () => {
    let resolveRequest!: (value: SpotlightResponse) => void;
    vi.mocked(getFeaturedToday).mockReturnValue(
      new Promise<SpotlightResponse>((resolve) => {
        resolveRequest = resolve;
      }),
    );
    renderPage();

    await act(async () => {
      resolveRequest({
        game_id: null,
        game_date: null,
        server_date: new Date().toISOString(),
      });
    });

    expect(
      await screen.findByText('No featured game right now'),
    ).toBeInTheDocument();
  });

  it('falls back to the local schedule only when the request itself fails', async () => {
    vi.mocked(getFeaturedToday).mockRejectedValue(new Error('offline'));
    renderPage();

    const fallback = localFallbackName();
    expect(fallback).toBeTruthy();
    if (fallback) {
      expect(
        await screen.findByRole('heading', { name: fallback }),
      ).toBeInTheDocument();
    }
  });
});
