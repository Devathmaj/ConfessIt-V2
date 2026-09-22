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
import {
  createWordleGame,
  drawWordleAnswer,
  getAdminWordleGames,
  type WordleGame,
} from '@/services/api';

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
 * The Wordle answer is the one control on the manage screen that talks to a server, so its
 * calls are mocked here while everything around them is real: what is asserted is the flow --
 * a word is drawn, shown, and then sent back as the save -- and the reason a refused save
 * leaves on screen, which is the part a toast would take away.
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

vi.mock('@/services/api', () => ({
  getAdminWordleGames: vi.fn(),
  drawWordleAnswer: vi.fn(),
  createWordleGame: vi.fn(),
  getAdminWordleGame: vi.fn(),
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
  CAPABILITIES.GAMES_MANAGE,
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
const renderSection = (path: string, session: AuthContextValue = adminSession) => {
  navigateTo = null;
  return render(
    <AuthContext.Provider value={session}>
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

/**
 * Rows in the standings, counted by the action every row carries rather than by a test id.
 *
 * `hidden: true` throughout: an open Radix dialog aria-hides everything behind it, and the
 * board is still there -- the dialog is on top of it, not instead of it.
 */
const standings = () => screen.getByRole('region', { name: 'Standings', hidden: true });
const rowActionCount = () =>
  within(standings()).queryAllByRole('button', { name: /remove score/i, hidden: true }).length;

/** The pick today's seeded schedule resolves to -- the same call the provider makes. */
const seededFeatured = () => resolveFeaturedGame(initialGamesConfig());

beforeEach(() => {
  vi.clearAllMocks();
  // The manage screen fetches the configured puzzles as it mounts; an empty list is the state
  // every test below starts from, so the tests that care about rows state their own.
  vi.mocked(getAdminWordleGames).mockResolvedValue([]);
});

/** The day the server would default to, which is what the date field starts on. */
const todayIso = () => new Date().toISOString().slice(0, 10);

const aPuzzle = (overrides: Partial<WordleGame> = {}): WordleGame => ({
  id: '22222222-2222-2222-2222-222222222222',
  game_date: todayIso(),
  mode: 'custom',
  answer: 'cigar',
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
  ...overrides,
});

describe('AdminGamesDashboard: the front page', () => {
  it('reports the catalogue, the spotlight and the week from the shared configuration', () => {
    renderSection('/admin/games');

    expect(screen.getByRole('heading', { name: 'Overview' })).toBeInTheDocument();
    expect(screen.getByText('Games live')).toBeInTheDocument();
    expect(screen.getByText('8/8')).toBeInTheDocument();
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

describe('AdminGamesManage: the spotlight', () => {
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
        within(standings()).queryAllByRole('button', { name: /remove score/i, hidden: true }).length,
      ).toBeGreaterThan(0),
    );
    const before = within(standings()).queryAllByRole('button', { name: /remove score/i, hidden: true }).length;

    fireEvent.click(within(standings()).getAllByRole('button', { name: /remove score/i })[0]);
    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText(/will be taken down/)).toBeInTheDocument();
    expect(within(standings()).queryAllByRole('button', { name: /remove score/i, hidden: true }).length).toBe(before);

    fireEvent.click(within(dialog).getByRole('button', { name: /cancel/i }));
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument());
    expect(within(standings()).queryAllByRole('button', { name: /remove score/i, hidden: true }).length).toBe(before);

    fireEvent.click(within(standings()).getAllByRole('button', { name: /remove score/i })[0]);
    const reopened = await screen.findByRole('alertdialog');
    fireEvent.click(within(reopened).getByRole('button', { name: /remove score/i }));

    await waitFor(() => expect(within(standings()).queryAllByRole('button', { name: /remove score/i, hidden: true }).length).toBe(before - 1));
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

describe('AdminGamesManage: the Wordle answer', () => {
  it('draws a word, shows it, and saves exactly that word for the day', async () => {
    renderSection('/admin/games/manage');
    expect(await screen.findByText(/Nothing configured yet/)).toBeInTheDocument();

    vi.mocked(drawWordleAnswer).mockResolvedValue({ word: 'cigar', token: 'signed-for-the-day' });
    const saved = aPuzzle({ answer: 'cigar', mode: 'random' });
    vi.mocked(createWordleGame).mockResolvedValue(saved);
    vi.mocked(getAdminWordleGames).mockResolvedValue([saved]);

    fireEvent.click(screen.getByRole('button', { name: /draw for me/i }));
    fireEvent.click(screen.getByRole('button', { name: /draw an answer/i }));

    // The answer is on screen before anything is written -- that is what the preview is for.
    expect(await screen.findByText('cigar')).toBeInTheDocument();
    expect(drawWordleAnswer).toHaveBeenCalledWith(todayIso());

    fireEvent.click(screen.getByRole('button', { name: /save puzzle/i }));

    await waitFor(() =>
      expect(createWordleGame).toHaveBeenCalledWith({
        mode: 'random',
        word: 'cigar',
        token: 'signed-for-the-day',
        game_date: todayIso(),
      }),
    );
    expect(toast.success).toHaveBeenCalledWith(`Wordle configured for ${todayIso()}.`);
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: `Inspect the puzzle for ${todayIso()}` }),
      ).toBeInTheDocument(),
    );

    // The save is also an action the overview's activity feed reports, like every other here.
    await goTo('/admin/games');
    expect(screen.getByText('Puzzle')).toBeInTheDocument();
    expect(screen.getByText(`Wordle answer for ${todayIso()} set`)).toBeInTheDocument();
  });

  it('keeps the server’s reason for an already-configured day on the screen', async () => {
    renderSection('/admin/games/manage');
    await screen.findByText(/Nothing configured yet/);

    vi.mocked(createWordleGame).mockRejectedValue({
      response: {
        status: 409,
        data: { detail: `A Wordle game is already configured for ${todayIso()}.` },
      },
    });

    fireEvent.change(screen.getByLabelText('Answer'), { target: { value: 'cigar' } });
    fireEvent.click(screen.getByRole('button', { name: /save puzzle/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(`A Wordle game is already configured for ${todayIso()}.`);
    // Not a toast: the reason is still here afterwards, next to a form that would only be
    // refused again for the same day.
    expect(toast.success).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: /save puzzle/i })).toBeEnabled();
  });

  it('waits for five letters, then shows what the server says about the word', async () => {
    renderSection('/admin/games/manage');
    await screen.findByText(/Nothing configured yet/);

    const saveButton = screen.getByRole('button', { name: /save puzzle/i });
    expect(saveButton).toBeDisabled();

    fireEvent.change(screen.getByLabelText('Answer'), { target: { value: 'zzzz' } });
    expect(saveButton).toBeDisabled();

    vi.mocked(createWordleGame).mockRejectedValue({
      response: { status: 400, data: { detail: 'That word is not on the approved list.' } },
    });
    fireEvent.change(screen.getByLabelText('Answer'), { target: { value: 'zzzzz' } });
    expect(saveButton).toBeEnabled();

    fireEvent.click(saveButton);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('That word is not on the approved list.');
    expect(createWordleGame).toHaveBeenCalledWith({
      mode: 'custom',
      word: 'zzzzz',
      game_date: todayIso(),
    });
  });

  it('keeps the puzzle controls from an account without games:manage', () => {
    const staffWithoutGames: AuthContextValue = {
      ...adminSession,
      can: (capability: Capability) =>
        capability !== CAPABILITIES.GAMES_MANAGE && can(ADMIN_CAPABILITIES, capability),
    };

    renderSection('/admin/games/manage', staffWithoutGames);

    expect(screen.queryByRole('heading', { name: 'Wordle' })).not.toBeInTheDocument();
    // The spotlight above is unaffected: pinning the day is local configuration, and the
    // section this gate hides is the one that publishes an answer.
    expect(screen.getByText("Today's featured")).toBeInTheDocument();
  });
});
