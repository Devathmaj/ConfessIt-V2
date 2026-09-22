import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ContextType } from 'react';
import { MemoryRouter, Route, Routes, type NavigateFunction, useNavigate } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { toast } from 'sonner';

import { AdminGamesLayout } from '@/components/AdminGamesLayout';
import { AuthContext, type User } from '@/contexts/AuthContext';
import { GamesConfigProvider } from '@/contexts/GamesConfigContext';
import { AdminGamesDashboard } from '@/pages/AdminGamesDashboard';
import { AdminGamesLeaderboard } from '@/pages/AdminGamesLeaderboard';
import { AdminGamesManage } from '@/pages/AdminGamesManage';
import { GamesFeaturedPage } from '@/pages/GamesFeaturedPage';
import { GamesSelectPage } from '@/pages/GamesSelectPage';
import { CAPABILITIES, type Capability, can } from '@/lib/capabilities';
import { AVAILABLE_GAMES } from '@/lib/gamesCatalog';
import { initialGamesConfig, resolveFeaturedGame } from '@/lib/gamesConfig';

/**
 * The Games console as one system: the three admin screens plus the two player screens they
 * reach, all reading the one `GamesConfigProvider` the way `App` mounts it.
 *
 * The properties worth pinning are the ones a mock cannot give back for free -- that a switch
 * thrown on the manage screen is the reason a title disappears from the player's grid, that a
 * pick made here is the pick `/games/featured` shows, and that a setting change surfaces in the
 * activity feed the overview reads. Those are the wiring the section exists to prove, so the
 * tests drive the real components against each other rather than asserting each screen in
 * isolation.
 *
 * `fireEvent` rather than `user-event`, matching the other component tests here. Radix `Select`
 * is driven the way `profileReviewCohortFilters` drives it -- pointer down on the trigger,
 * then a click on the portalled option -- because a combobox holds no value a change event
 * could move.
 */

vi.mock('sonner', () => ({
  toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn(), info: vi.fn() }),
  Toaster: () => null,
}));

type AuthContextValue = ContextType<typeof AuthContext>;

const asUser = (overrides: Partial<User> = {}): User => ({
  id: '1',
  name: 'Test User',
  regno: 'TEST001',
  email: 'test@example.com',
  ...overrides,
});

const ADMIN_CAPABILITIES = [
  CAPABILITIES.CONFESSIONS_REVIEW,
  CAPABILITIES.REPORTS_REVIEW,
  CAPABILITIES.LOVE_NOTES_REVIEW,
  CAPABILITIES.STATS_READ,
];

const adminSession: AuthContextValue = {
  user: asUser({ account_type: 'admin' }),
  isAuthenticated: true,
  isLoading: false,
  token: 'token',
  logout: vi.fn(),
  verifyMagicToken: vi.fn(),
  fetchAndSetUser: vi.fn(),
  can: (capability: Capability) => can(ADMIN_CAPABILITIES, capability),
  signInPath: '/login',
};

/** Set by `NavProbe` on every render, so a test can walk to a sibling screen the way a link does. */
let navigateTo: NavigateFunction | null = null;

const NavProbe = () => {
  navigateTo = useNavigate();
  return null;
};

/** The section's routes plus the two player screens that must see the same configuration. */
const renderSection = (path: string) => {
  navigateTo = null;
  return render(
    <AuthContext.Provider value={adminSession}>
      <GamesConfigProvider>
        <MemoryRouter initialEntries={[path]}>
          <NavProbe />
          <Routes>
            <Route path="/admin/games" element={<AdminGamesLayout />}>
              <Route index element={<AdminGamesDashboard />} />
              <Route path="manage" element={<AdminGamesManage />} />
              <Route path="leaderboard" element={<AdminGamesLeaderboard />} />
            </Route>
            <Route path="/games/select" element={<GamesSelectPage />} />
            <Route path="/games/featured" element={<GamesFeaturedPage />} />
          </Routes>
        </MemoryRouter>
      </GamesConfigProvider>
    </AuthContext.Provider>,
  );
};

const goTo = async (path: string) => {
  await act(async () => {
    navigateTo?.(path);
  });
};

/** Opens the `index`th Radix combobox on screen and picks an option from its portalled list. */
const chooseOption = async (index: number, option: string) => {
  fireEvent.pointerDown(screen.getAllByRole('combobox')[index], {
    button: 0,
    ctrlKey: false,
    pointerType: 'mouse',
  });
  fireEvent.click(await screen.findByRole('option', { name: option }));
};

/** Rows in the standings, counted by the action every row carries rather than by a test id. */
const standings = () => screen.getByRole('region', { name: 'Standings' });
const rowActionCount = () =>
  within(standings()).queryAllByRole('button', { name: /remove score/i }).length;

/** The pick today's seeded schedule resolves to -- the same call the provider makes. */
const seededFeatured = () => resolveFeaturedGame(initialGamesConfig());

beforeEach(() => {
  vi.clearAllMocks();
});

describe('AdminGamesDashboard: the front page', () => {
  it('reports the catalogue, the spotlight and the week from the shared configuration', () => {
    renderSection('/admin/games');

    expect(screen.getByRole('heading', { name: 'Overview' })).toBeInTheDocument();
    expect(screen.getByText('Games live')).toBeInTheDocument();
    expect(screen.getByText('7/7')).toBeInTheDocument();
    expect(screen.getByText('Every title switched on')).toBeInTheDocument();
    expect(screen.getByText('Leaderboard snapshot')).toBeInTheDocument();
    expect(screen.getByText('Single vs Couple')).toBeInTheDocument();

    // The spotlight tile names the pick the schedule actually resolves to, and says whether it
    // was pinned or rotated -- not a pick of the page's own.
    const featured = seededFeatured()!;
    expect(screen.getAllByText(featured.game.name).length).toBeGreaterThan(0);
    expect(screen.getByText(featured.explicit ? 'Pinned' : 'Rotation')).toBeInTheDocument();

    // The seeded activity the dashboard opens with, newest first.
    expect(screen.getByText('Weekly leaderboard standings recalculated')).toBeInTheDocument();
    expect(screen.getByText('Games catalogue synchronised with the player-facing section')).toBeInTheDocument();
  });
});

describe('AdminGamesManage: availability and the spotlight', () => {
  it('lists every catalogue title with its own settings, and none for the games that have none', () => {
    renderSection('/admin/games/manage');

    AVAILABLE_GAMES.forEach((game) => {
      expect(screen.getAllByText(game.name).length).toBeGreaterThan(0);
    });
    expect(screen.getByText(/7 of 7 titles live/)).toBeInTheDocument();
    expect(screen.getByLabelText('Rounds per game')).toBeInTheDocument();
    expect(screen.getByLabelText('Question set')).toBeInTheDocument();
    expect(screen.getByLabelText('Countdown timer')).toBeInTheDocument();
    expect(screen.getAllByText('No game-specific settings.').length).toBeGreaterThan(0);
    // Everything is already on, so there is nothing for the bulk action to do.
    expect(screen.getByRole('button', { name: /enable everything/i })).toBeDisabled();
  });

  it('switches a title off and says so', () => {
    renderSection('/admin/games/manage');

    const [truthDareSwitch] = screen.getAllByLabelText('Live');
    expect(truthDareSwitch).toHaveAttribute('aria-checked', 'true');

    fireEvent.click(truthDareSwitch);

    expect(toast.success).toHaveBeenCalledWith('Truth or Dare switched off.');
    expect(truthDareSwitch).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByText(/6 of 7 titles live, 6 eligible/)).toBeInTheDocument();
  });

  it('takes the switched-off title out of the player-facing grid', async () => {
    renderSection('/admin/games/manage');
    fireEvent.click(screen.getAllByLabelText('Live')[0]);

    await goTo('/games/select');

    expect(screen.queryByText('Truth or Dare')).not.toBeInTheDocument();
    expect(screen.getByText('Compatibility Quiz')).toBeInTheDocument();
  });

  it('pins a random pick and shows it on the player’s featured page', async () => {
    renderSection('/admin/games/manage');

    fireEvent.click(screen.getByRole('button', { name: /pick at random/i }));

    expect(toast.success).toHaveBeenCalledTimes(1);
    const message = vi.mocked(toast.success).mock.calls[0][0] as string;
    expect(message).toMatch(/ picked for today\.$/);
    const pickedName = message.replace(' picked for today.', '');
    // Today is now an explicit pick rather than whatever the rotation had chosen.
    expect(screen.getByText('Pinned')).toBeInTheDocument();
    expect(screen.getAllByText(pickedName).length).toBeGreaterThan(0);

    await goTo('/games/featured');
    expect(screen.getByRole('heading', { name: pickedName })).toBeInTheDocument();
  });

  it('pins a chosen title from the selector', async () => {
    renderSection('/admin/games/manage');
    const currentPick = seededFeatured()!.game.id;
    const target = AVAILABLE_GAMES.find((game) => game.id !== currentPick)!;

    await chooseOption(0, target.name);

    expect(toast.success).toHaveBeenCalledWith(`${target.name} featured for today.`);
  });

  it('commits a setting when the field is left, and logs it where the overview reads', async () => {
    renderSection('/admin/games/manage');

    const rounds = screen.getByLabelText('Rounds per game') as HTMLInputElement;
    fireEvent.change(rounds, { target: { value: '8' } });
    expect(rounds.value).toBe('8');
    fireEvent.blur(rounds);
    expect(rounds.value).toBe('8');

    await goTo('/admin/games');
    expect(
      screen.getByText('Truth or Dare · Rounds per game set to 8'),
    ).toBeInTheDocument();
  });
});

describe('AdminGamesLeaderboard: the board and its destructive actions', () => {
  it('shows the standings with the filters and controls an administrator acts on', () => {
    renderSection('/admin/games/leaderboard');

    expect(screen.getByRole('heading', { name: 'Leaderboard' })).toBeInTheDocument();
    expect(screen.getByText('Players ranked')).toBeInTheDocument();
    expect(screen.getByText('Sessions')).toBeInTheDocument();
    expect(screen.getByText('Top score')).toBeInTheDocument();
    expect(screen.getByText('Incomplete')).toBeInTheDocument();
    expect(screen.getAllByRole('combobox')).toHaveLength(3);
    expect(screen.getAllByText(/Every game on one board/)[0]).toBeInTheDocument();

    const rows = rowActionCount();
    expect(rows).toBeGreaterThan(3);
    expect(within(standings()).getAllByRole('button', { name: /inspect/i })).toHaveLength(rows);
    expect(within(standings()).getAllByRole('button', { name: /reset player/i })).toHaveLength(rows);
  });

  it('narrows the board to a single title when the scope changes', async () => {
    renderSection('/admin/games/leaderboard');

    await chooseOption(0, 'Game');

    await waitFor(() => {
      expect(screen.getAllByText(/Standings for a single title/)[0]).toBeInTheDocument();
    });
    expect(screen.queryByText('Game type')).not.toBeInTheDocument();
    // Every row now reports the one title asked for, re-ranked from the top.
    const gameName = AVAILABLE_GAMES[0].name;
    await waitFor(() => {
      expect(within(standings()).getAllByText(new RegExp(`^${gameName} · `))).toHaveLength(
        rowActionCount(),
      );
    });
  });

  it('takes a score down only after the removal is confirmed', async () => {
    renderSection('/admin/games/leaderboard');
    await waitFor(() => expect(screen.getByRole('region', { name: 'Standings' })).toBeInTheDocument());
    await waitFor(() =>
      expect(
        within(standings()).queryAllByRole('button', { name: /remove score/i }).length,
      ).toBeGreaterThan(0),
    );
    const before = within(standings()).queryAllByRole('button', { name: /remove score/i }).length;

    fireEvent.click(within(standings()).getAllByRole('button', { name: /remove score/i })[0]);
    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText(/will be taken down/)).toBeInTheDocument();
    expect(within(standings()).queryAllByRole('button', { name: /remove score/i }).length).toBe(before);

    fireEvent.click(within(dialog).getByRole('button', { name: /cancel/i }));
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument());
    expect(within(standings()).queryAllByRole('button', { name: /remove score/i }).length).toBe(before);

    fireEvent.click(within(standings()).getAllByRole('button', { name: /remove score/i })[0]);
    const reopened = await screen.findByRole('alertdialog');
    fireEvent.click(within(reopened).getByRole('button', { name: /remove score/i }));

    await waitFor(() => expect(within(standings()).queryAllByRole('button', { name: /remove score/i }).length).toBe(before - 1));
    expect(toast.success).toHaveBeenCalledWith(
      expect.stringMatching(/score on .* removed\.$/),
    );
  });

  it('resets the board behind its own confirmation, and restores it on request', async () => {
    renderSection('/admin/games/leaderboard');
    const before = rowActionCount();

    fireEvent.click(screen.getByRole('button', { name: /reset leaderboard/i }));
    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText(/will be cleared/)).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole('button', { name: /reset leaderboard/i }));
    await waitFor(() => expect(rowActionCount()).toBe(0));
    expect(toast.success).toHaveBeenCalledWith('Leaderboard reset for these filters.');
    expect(screen.getByText('The leaderboard was reset for these filters.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /restore standings/i })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /restore standings/i }));
    await waitFor(() => expect(rowActionCount()).toBe(before));
  });

  it('opens the full standing behind a row for inspection', async () => {
    renderSection('/admin/games/leaderboard');

    fireEvent.click(within(standings()).getAllByRole('button', { name: /inspect/i })[0]);
    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText('Sessions')).toBeInTheDocument();
    expect(within(dialog).getByText('Rank trend')).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole('button', { name: /close/i }));
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument());
    expect(rowActionCount()).toBeGreaterThan(0);
  });
});
