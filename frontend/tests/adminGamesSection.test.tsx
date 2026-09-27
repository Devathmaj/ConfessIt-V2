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
import { AdminFeaturedManage } from '@/pages/AdminFeaturedManage';
import { AdminWordleManage } from '@/pages/AdminWordleManage';
import { AdminAnagramManage } from '@/pages/AdminAnagramManage';
import { AdminCoupleQuestionnaireManage } from '@/pages/AdminCoupleQuestionnaireManage';
import { AdminTwoKeyEscapeManage } from '@/pages/AdminTwoKeyEscapeManage';
import { AdminReactionTimeManage } from '@/pages/AdminReactionTimeManage';
import { GamesFeaturedPage } from '@/pages/GamesFeaturedPage';
import { GamesSelectPage } from '@/pages/GamesSelectPage';
import { CAPABILITIES, type Capability, can } from '@/lib/capabilities';
import { AVAILABLE_GAMES } from '@/lib/gamesCatalog';
import { initialGamesConfig, resolveFeaturedGame } from '@/lib/gamesConfig';
import {
  clearFeaturedGame,
  createAnagramGame,
  createCoupleGame,
  createReactionTimeGame,
  createWordleGame,
  deleteScheduledAnagramWord,
  deleteScheduledWordleWord,
  drawAnagramAnswer,
  drawWordleAnswer,
  getAdminAnagramGames,
  getAdminCoupleGames,
  getAdminReactionTimeGames,
  getAdminWordleGames,
  getAdminCouplesLeaderboard,
  getCurrentAdminAnagramSession,
  getCurrentAdminCoupleSession,
  getCurrentAdminReactionTimeSession,
  getCurrentAdminWordleSession,
  getFeaturedGames,
  getFeaturedToday,
  getGameConfigs,
  getGameSelection,
  getAdminIndividualLeaderboard,
  getScheduledAnagramWords,
  getScheduledWordleWords,
  replaceFeaturedGame,
  replaceFeaturedRange,
  runScheduledResets,
  scheduleAnagramWord,
  scheduleWordleWord,
  setFeaturedGame,
  setFeaturedRange,
  updateGameConfig,
  type AnagramGame,
  type GameConfig,
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
 * Every control on the manage screen talks to a server -- the featured schedule, the
 * per-game lifecycle configuration, the session Updates and the scheduled words -- so
 * their calls are mocked here while everything around them is real: what is asserted is
 * the flow -- a word is drawn, shown, and then sent back as the update -- and the reason
 * a refused write leaves on screen, which is the part a toast would take away. The
 * session consoles share their shape on purpose (an "Update session" button, an alert
 * slot, a schedule section, an empty state), so every query below is scoped to the card
 * it is about: unscoped, "update session" and "No sessions yet" are simply two elements
 * each. The "Type a word" / "Draw for me" toggle stages the content mode without saving
 * it -- only the explicit Save persists it -- so the mode tests assert both the staged
 * banner with no call and the call itself after confirm.
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
  getCurrentAdminWordleSession: vi.fn(),
  getScheduledWordleWords: vi.fn(),
  scheduleWordleWord: vi.fn(),
  deleteScheduledWordleWord: vi.fn(),
  getAdminAnagramGames: vi.fn(),
  drawAnagramAnswer: vi.fn(),
  createAnagramGame: vi.fn(),
  getAdminAnagramGame: vi.fn(),
  getCurrentAdminAnagramSession: vi.fn(),
  getScheduledAnagramWords: vi.fn(),
  scheduleAnagramWord: vi.fn(),
  deleteScheduledAnagramWord: vi.fn(),
  getAdminCoupleGames: vi.fn(),
  createCoupleGame: vi.fn(),
  getAdminCoupleGame: vi.fn(),
  getCurrentAdminCoupleSession: vi.fn(),
  getAdminReactionTimeGames: vi.fn(),
  createReactionTimeGame: vi.fn(),
  getAdminReactionTimeGame: vi.fn(),
  getCurrentAdminReactionTimeSession: vi.fn(),
  getGameConfigs: vi.fn(),
  updateGameConfig: vi.fn(),
  getFeaturedGames: vi.fn(),
  setFeaturedGame: vi.fn(),
  setFeaturedRange: vi.fn(),
  replaceFeaturedRange: vi.fn(),
  replaceFeaturedGame: vi.fn(),
  clearFeaturedGame: vi.fn(),
  runScheduledResets: vi.fn(),
  getGameSelection: vi.fn(),
  getFeaturedToday: vi.fn(),
  getAdminIndividualLeaderboard: vi.fn(),
  getAdminCouplesLeaderboard: vi.fn(),
  getGameLeaderboard: vi.fn(),
  getMyLeagueStanding: vi.fn(),
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
              <Route path="manage/featured" element={<AdminFeaturedManage />} />
              <Route path="manage/wordle" element={<AdminWordleManage />} />
              <Route path="manage/anagram" element={<AdminAnagramManage />} />
              <Route path="manage/couple-questionnaire" element={<AdminCoupleQuestionnaireManage />} />
              <Route path="manage/two-key-escape" element={<AdminTwoKeyEscapeManage />} />
              <Route path="manage/reaction-time" element={<AdminReactionTimeManage />} />
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

/** An empty league board, the state every leaderboard test starts from. */
const emptyIndividualBoard = () => ({
  competition_type: 'individual' as const,
  period: 'weekly',
  game_id: null,
  opportunity_count: 0,
  total: 0,
  entries: [],
});

const emptyCoupleBoard = () => ({
  competition_type: 'couple' as const,
  period: 'weekly',
  game_id: null,
  opportunity_count: 0,
  total: 0,
  entries: [],
});

/** The pick today's seeded schedule resolves to -- the same call the provider makes. */
const seededFeatured = () => resolveFeaturedGame(initialGamesConfig());

beforeEach(() => {
  vi.clearAllMocks();
  // The manage screen fetches sessions, schedules and configuration as it mounts; an empty
  // list is the state every test below starts from, so the tests that care about rows state
  // their own. The current-session reads reject, which the cards render as "no session".
  vi.mocked(getAdminWordleGames).mockResolvedValue([]);
  vi.mocked(getAdminAnagramGames).mockResolvedValue([]);
  vi.mocked(getAdminCoupleGames).mockResolvedValue([]);
  vi.mocked(getAdminReactionTimeGames).mockResolvedValue([]);
  vi.mocked(getCurrentAdminWordleSession).mockRejectedValue({ response: { status: 404 } });
  vi.mocked(getCurrentAdminAnagramSession).mockRejectedValue({ response: { status: 404 } });
  vi.mocked(getCurrentAdminCoupleSession).mockRejectedValue({ response: { status: 404 } });
  vi.mocked(getCurrentAdminReactionTimeSession).mockRejectedValue({ response: { status: 404 } });
  vi.mocked(getScheduledWordleWords).mockResolvedValue([]);
  vi.mocked(getScheduledAnagramWords).mockResolvedValue([]);
  vi.mocked(getGameConfigs).mockResolvedValue([]);
  vi.mocked(getFeaturedGames).mockResolvedValue([]);
  vi.mocked(getGameSelection).mockResolvedValue({ games: [], featured_game_id: null, server_date: todayIso() });
  vi.mocked(getFeaturedToday).mockResolvedValue({ game_id: null, game_date: null, server_date: todayIso() });
  vi.mocked(getAdminIndividualLeaderboard).mockResolvedValue(emptyIndividualBoard());
  vi.mocked(getAdminCouplesLeaderboard).mockResolvedValue(emptyCoupleBoard());
});

/** The day the server would default to, which is what the date field starts on. */
const todayIso = () => new Date().toISOString().slice(0, 10);

/**
 * One puzzle console on its own editor page, found by its card heading.
 * Each editor page renders a page-level h1 plus the config card's own heading
 * with the same name, so the lookup skips headings that are not inside a card.
 */
const puzzleCard = (title: 'Wordle' | 'Anagram') => {
  const headings = screen.getAllByRole('heading', { name: title });
  for (const heading of headings) {
    const card = heading.closest('.rounded-2xl');
    if (card) return card as HTMLElement;
  }
  throw new Error(`The ${title} card could not be found from its heading.`);
};

const wordleCard = () => puzzleCard('Wordle');
const anagramCard = () => puzzleCard('Anagram');

const withinFeaturedCard = () => {
  const heading = screen.getByRole('heading', { name: 'Featured game' });
  const card = heading.closest('.rounded-2xl');
  if (!card) {
    throw new Error('The Featured game card could not be found from its heading.');
  }
  return card as HTMLElement;
};

const aPuzzle = (overrides: Partial<WordleGame> = {}): WordleGame => ({
  id: '22222222-2222-2222-2222-222222222222',
  game_date: todayIso(),
  mode: 'custom',
  answer: 'cigar',
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
  ...overrides,
});

/** A game-config row as `GET /admin/games/config` returns it. */
const gameConfig = (
  gameId: string,
  contentMode: GameConfig['content_mode'] = 'manual',
): GameConfig => ({
  game_id: gameId,
  enabled: true,
  is_hidden: false,
  content_mode: contentMode,
  reset_cadence: 'daily',
  grid_size: null,
});

describe('AdminGamesDashboard: the front page', () => {
  it('reports the catalogue, the spotlight and the week from the shared configuration', () => {
    renderSection('/admin/games');

    expect(screen.getByRole('heading', { name: 'Overview' })).toBeInTheDocument();
    expect(screen.getByText('Games live')).toBeInTheDocument();
    expect(
      screen.getByText(`${AVAILABLE_GAMES.length}/${AVAILABLE_GAMES.length}`),
    ).toBeInTheDocument();
    expect(screen.getByText('Every title switched on')).toBeInTheDocument();
    expect(screen.getByText('Leaderboard snapshot')).toBeInTheDocument();
    expect(screen.getByText('Single vs Couple')).toBeInTheDocument();

    // The spotlight tile names the pick the schedule actually resolves to, and says whether it
    // was pinned or rotated -- not a pick of the page's own.
    const featured = seededFeatured()!;
    expect(screen.getAllByText(featured.game.name).length).toBeGreaterThan(0);
    expect(screen.getByText(featured.explicit ? 'Pinned' : 'Rotation')).toBeInTheDocument();

    // The seeded activity the dashboard opens with names today's pick.
    expect(
      screen.getByText(`${featured.game.name} set as today's featured game`),
    ).toBeInTheDocument();
    // Play-derived figures are empty until events close and score.
    expect(
      screen.getByText('No standings yet. Events appear here once they close and score.'),
    ).toBeInTheDocument();
  });

  it('shows the weekly leaders once the league board answers', async () => {
    vi.mocked(getAdminIndividualLeaderboard).mockResolvedValue({
      competition_type: 'individual',
      period: 'weekly',
      game_id: null,
      opportunity_count: 2,
      total: 1,
      entries: [
        {
          rank: 1,
          user_id: 'user-1',
          display_name: 'Ada',
          score: 87.5,
          native: {},
          is_featured: true,
          opportunities_played: 2,
          per_game: { wordle: 87.5 },
        },
      ],
    });
    renderSection('/admin/games');

    expect(await screen.findByText('Ada')).toBeInTheDocument();
    expect(screen.getByText('2 events this week')).toBeInTheDocument();
  });
});

describe('AdminFeaturedManage: the dedicated schedule', () => {
  it('pins a game for a date through the server schedule', async () => {
    renderSection('/admin/games/manage/featured');
    const card = withinFeaturedCard();
    expect(await within(card).findByText(/Nothing featured yet/)).toBeInTheDocument();

    const created = {
      id: 'featured-1',
      game_date: todayIso(),
      game_id: 'wordle',
      created_by: 'TEST001',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    vi.mocked(setFeaturedGame).mockResolvedValue(created);
    vi.mocked(getFeaturedGames).mockResolvedValue([created]);

    fireEvent.click(within(card).getByRole('button', { name: /set featured/i }));

    await waitFor(() =>
      expect(setFeaturedGame).toHaveBeenCalledWith(todayIso(), 'wordle'),
    );
    expect(toast.success).toHaveBeenCalledWith(`Wordle featured for ${todayIso()}.`);
    await waitFor(() =>
      expect(within(card).getByText(new RegExp(todayIso()))).toBeInTheDocument(),
    );
  });

  it('keeps the server’s one-game-per-date refusal on the screen', async () => {
    renderSection('/admin/games/manage/featured');
    const card = withinFeaturedCard();
    await within(card).findByText(/Nothing featured yet/);

    vi.mocked(setFeaturedGame).mockRejectedValue({
      response: {
        status: 409,
        data: { detail: `A featured game is already set for ${todayIso()}.` },
      },
    });

    fireEvent.click(within(card).getByRole('button', { name: /set featured/i }));

    const alert = await within(card).findByRole('alert');
    expect(alert).toHaveTextContent('A featured game is already set');
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('confirms the exact date before erasing a single-date pick', async () => {
    vi.mocked(getFeaturedGames).mockResolvedValue([
      {
        id: 'featured-anagram',
        game_date: todayIso(),
        game_id: 'anagram',
        created_by: 'TEST001',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);
    renderSection('/admin/games/manage/featured');
    const card = withinFeaturedCard();
    await within(card).findByText(todayIso());

    // The form defaults to today + Wordle, which Anagram holds.
    fireEvent.click(within(card).getByRole('button', { name: /set featured/i }));

    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText(/Erase this featured pick/)).toBeInTheDocument();
    expect(within(dialog).getByText(new RegExp(todayIso()))).toBeInTheDocument();
    expect(setFeaturedGame).not.toHaveBeenCalled();

    const replaced = {
      id: 'featured-wordle',
      game_date: todayIso(),
      game_id: 'wordle',
      created_by: 'TEST001',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    vi.mocked(replaceFeaturedGame).mockResolvedValue(replaced);
    fireEvent.click(within(dialog).getByRole('button', { name: /erase and feature/i }));

    await waitFor(() =>
      expect(replaceFeaturedGame).toHaveBeenCalledWith(todayIso(), 'wordle'),
    );
    expect(toast.success).toHaveBeenCalledWith(
      `Wordle featured for ${todayIso()} (replacing Anagram).`,
    );
  });

  it('features a span from start to end', async () => {
    renderSection('/admin/games/manage/featured');
    const heading = await screen.findByRole('heading', { name: 'Feature a span' });
    const card = heading.closest('.rounded-2xl');
    if (!card) throw new Error('The span card could not be found from its heading.');
    const scope = within(card as HTMLElement);

    const created = [0, 1, 2].map((offset) => ({
      id: `featured-span-${offset}`,
      game_date: todayIso(),
      game_id: 'wordle',
      created_by: 'TEST001',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    }));
    vi.mocked(setFeaturedRange).mockResolvedValue(created);

    fireEvent.click(scope.getByRole('button', { name: /feature span/i }));

    await waitFor(() =>
      expect(setFeaturedRange).toHaveBeenCalledWith(todayIso(), todayIso(), 'wordle'),
    );
    expect(toast.success).toHaveBeenCalled();
  });

  it('refuses a span longer than a week without calling the server', async () => {
    renderSection('/admin/games/manage/featured');
    const heading = await screen.findByRole('heading', { name: 'Feature a span' });
    const card = heading.closest('.rounded-2xl');
    if (!card) throw new Error('The span card could not be found from its heading.');
    const scope = within(card as HTMLElement);

    const endInput = scope.getByLabelText(/End \(UTC\)/) as HTMLInputElement;
    fireEvent.change(endInput, { target: { value: '2026-10-19' } });
    const startInput = scope.getByLabelText(/Start \(UTC\)/) as HTMLInputElement;
    fireEvent.change(startInput, { target: { value: '2026-10-12' } });

    fireEvent.click(scope.getByRole('button', { name: /feature span/i }));

    expect(await scope.findByText(/at most 7 days in a row/)).toBeInTheDocument();
    expect(setFeaturedRange).not.toHaveBeenCalled();
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('keeps the server’s overlong-chain refusal on the screen', async () => {
    renderSection('/admin/games/manage/featured');
    const heading = await screen.findByRole('heading', { name: 'Feature a span' });
    const card = heading.closest('.rounded-2xl');
    if (!card) throw new Error('The span card could not be found from its heading.');
    const scope = within(card as HTMLElement);

    vi.mocked(setFeaturedRange).mockRejectedValue({
      response: {
        status: 422,
        data: { detail: 'Featuring wordle for 2026-10-12 to 2026-10-18 would run it 8 days in a row' },
      },
    });

    fireEvent.click(scope.getByRole('button', { name: /feature span/i }));

    const alert = await scope.findByRole('alert');
    expect(alert).toHaveTextContent('8 days in a row');
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('lists every covered date before erasing a span', async () => {
    const tomorrow = new Date(Date.now() + 86_400_000).toISOString().slice(0, 10);
    const held = [todayIso(), tomorrow].map((day, offset) => ({
      id: `featured-held-${offset}`,
      game_date: day,
      game_id: 'wordle',
      created_by: 'TEST001',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    }));
    vi.mocked(getFeaturedGames).mockResolvedValue(held);
    renderSection('/admin/games/manage/featured');
    const heading = await screen.findByRole('heading', { name: 'Feature a span' });
    const card = heading.closest('.rounded-2xl');
    if (!card) throw new Error('The span card could not be found from its heading.');
    const scope = within(card as HTMLElement);

    await chooseOption(0, 'Anagram');
    const endInput = scope.getByLabelText(/End \(UTC\)/) as HTMLInputElement;
    fireEvent.change(endInput, { target: { value: tomorrow } });

    fireEvent.click(scope.getByRole('button', { name: /feature span/i }));

    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText(/Erase these featured picks/)).toBeInTheDocument();
    expect(within(dialog).getAllByText(todayIso()).length).toBeGreaterThan(0);
    expect(within(dialog).getAllByText(tomorrow).length).toBeGreaterThan(0);
    expect(setFeaturedRange).not.toHaveBeenCalled();

    const created = held.map((entry) => ({ ...entry, game_id: 'anagram' }));
    vi.mocked(replaceFeaturedRange).mockResolvedValue(created);
    fireEvent.click(within(dialog).getByRole('button', { name: /erase and feature/i }));

    await waitFor(() =>
      expect(replaceFeaturedRange).toHaveBeenCalledWith(todayIso(), tomorrow, 'anagram'),
    );
    expect(toast.success).toHaveBeenCalled();
  });
});

describe('AdminGamesManage: the spotlight front door', () => {
  it('sets the chosen game as featured for today', async () => {
    renderSection('/admin/games/manage');
    const heading = await screen.findByRole('heading', { name: 'Featured spotlight' });
    const card = heading.closest('.rounded-2xl');
    if (!card) throw new Error('The spotlight card could not be found from its heading.');
    const scope = within(card as HTMLElement);
    expect(await scope.findByText(/No game holds today/)).toBeInTheDocument();

    const created = {
      id: 'featured-1',
      game_date: todayIso(),
      game_id: 'wordle',
      created_by: 'TEST001',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    vi.mocked(replaceFeaturedGame).mockResolvedValue(created);

    fireEvent.click(scope.getByRole('button', { name: /set as featured for today/i }));

    await waitFor(() =>
      expect(replaceFeaturedGame).toHaveBeenCalledWith(todayIso(), 'wordle'),
    );
    expect(toast.success).toHaveBeenCalledWith(`Wordle is featured for ${todayIso()}.`);
  });

  it('confirms before replacing today’s pick and links the full schedule', async () => {
    vi.mocked(getFeaturedGames).mockResolvedValue([
      {
        id: 'featured-wordle',
        game_date: todayIso(),
        game_id: 'wordle',
        created_by: 'TEST001',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);
    renderSection('/admin/games/manage');
    expect(await screen.findByText('Today: Wordle')).toBeInTheDocument();

    await chooseOption(0, 'Anagram');
    fireEvent.click(screen.getByRole('button', { name: /set as featured for today/i }));

    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText(/Replace today/)).toBeInTheDocument();

    const created = {
      id: 'featured-2',
      game_date: todayIso(),
      game_id: 'anagram',
      created_by: 'TEST001',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    vi.mocked(replaceFeaturedGame).mockResolvedValue(created);
    fireEvent.click(within(dialog).getByRole('button', { name: /replace featured/i }));

    await waitFor(() =>
      expect(replaceFeaturedGame).toHaveBeenCalledWith(todayIso(), 'anagram'),
    );
    expect(screen.getByRole('link', { name: /full schedule/i })).toHaveAttribute(
      'href',
      '/admin/games/manage/featured',
    );
  });
});

describe('AdminReactionTimeManage: the Reaction Time editor page', () => {
  const reactionCard = () => {
    const headings = screen.getAllByRole('heading', { name: 'Reaction Time' });
    for (const heading of headings) {
      const card = heading.closest('.rounded-2xl');
      if (card) return card as HTMLElement;
    }
    throw new Error('The Reaction Time card could not be found from its heading.');
  };

  it('reports the session period with no per-period content to author', async () => {
    vi.mocked(getCurrentAdminReactionTimeSession).mockResolvedValue({
      id: 'session-1',
      game_date: todayIso(),
      status: 'active',
      period_start_date: todayIso(),
      period_end_date: todayIso(),
    });
    renderSection('/admin/games/manage/reaction-time');
    const card = reactionCard();
    expect(await within(card).findByText(/Current session period/)).toBeInTheDocument();
    // No per-period authoring and no shared history for this game.
    expect(within(card).queryByText(/Schedule a future/)).not.toBeInTheDocument();
    expect(within(card).queryByText(/Session history/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /save featured day/i })).not.toBeInTheDocument();
  });

  it('sets itself as featured for today when nothing holds the spotlight', async () => {
    renderSection('/admin/games/manage/reaction-time');
    expect(await screen.findByText(/No game holds today/)).toBeInTheDocument();

    const created = {
      id: 'featured-1',
      game_date: todayIso(),
      game_id: 'reaction-time',
      created_by: 'TEST001',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    vi.mocked(replaceFeaturedGame).mockResolvedValue(created);

    fireEvent.click(screen.getByRole('button', { name: /set as featured/i }));

    await waitFor(() =>
      expect(replaceFeaturedGame).toHaveBeenCalledWith(todayIso(), 'reaction-time'),
    );
    expect(toast.success).toHaveBeenCalledWith(`Reaction Time is featured for ${todayIso()}.`);
  });

  it('confirms before replacing the game that holds today, keeping earned 2x', async () => {
    vi.mocked(getFeaturedGames).mockResolvedValue([
      {
        id: 'featured-wordle',
        game_date: todayIso(),
        game_id: 'wordle',
        created_by: 'TEST001',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]);
    renderSection('/admin/games/manage/reaction-time');
    expect(await screen.findByText(/is featured today/)).toBeInTheDocument();
    expect(screen.getByText('Wordle')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /set as featured/i }));

    // The popup names the displaced game and states earned scores persist.
    const dialog = await screen.findByRole('alertdialog');
    expect(within(dialog).getByText(/Replace today/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Scores already earned keep their 2x/)).toBeInTheDocument();

    const created = {
      id: 'featured-2',
      game_date: todayIso(),
      game_id: 'reaction-time',
      created_by: 'TEST001',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    vi.mocked(replaceFeaturedGame).mockResolvedValue(created);

    fireEvent.click(within(dialog).getByRole('button', { name: /replace featured/i }));

    await waitFor(() =>
      expect(replaceFeaturedGame).toHaveBeenCalledWith(todayIso(), 'reaction-time'),
    );
    expect(toast.success).toHaveBeenCalledWith(`Reaction Time is featured for ${todayIso()}.`);
  });
});

describe('AdminGamesLeaderboard: the server-backed board', () => {
  it('shows the filters with an empty board until events score', async () => {
    renderSection('/admin/games/leaderboard');

    expect(screen.getByRole('heading', { name: 'Leaderboard' })).toBeInTheDocument();
    expect(screen.getByText('Players ranked')).toBeInTheDocument();
    expect(screen.getByText('Events scored')).toBeInTheDocument();
    expect(screen.getByText('Top score')).toBeInTheDocument();
    expect(screen.getByText('Featured rows')).toBeInTheDocument();
    expect(await screen.findByText('No standings match these filters.')).toBeInTheDocument();
  });

  it('renders the standings the league board answers', async () => {
    vi.mocked(getAdminIndividualLeaderboard).mockResolvedValue({
      competition_type: 'individual',
      period: 'weekly',
      game_id: null,
      opportunity_count: 2,
      total: 2,
      entries: [
        {
          rank: 1,
          user_id: 'user-1',
          display_name: 'Ada',
          score: 150,
          native: {},
          is_featured: true,
          opportunities_played: 2,
          per_game: { wordle: 100, anagram: 50 },
        },
        {
          rank: 2,
          user_id: 'user-2',
          display_name: 'Bo',
          score: 25,
          native: {},
          is_featured: false,
          opportunities_played: 1,
        },
      ],
    });
    renderSection('/admin/games/leaderboard');

    expect(await screen.findByText('Ada')).toBeInTheDocument();
    expect(screen.getByText('Bo')).toBeInTheDocument();
    expect(screen.getByText('2×')).toBeInTheDocument();
    expect(screen.getByText(/wordle 100/)).toBeInTheDocument();
    expect(getAdminIndividualLeaderboard).toHaveBeenCalledWith(
      expect.objectContaining({ period: 'weekly', skip: 0 }),
    );
  });

  it('switches to the couples board and narrows to a couple title', async () => {
    vi.mocked(getAdminCouplesLeaderboard).mockResolvedValue({
      competition_type: 'couple',
      period: 'weekly',
      game_id: null,
      opportunity_count: 1,
      total: 1,
      entries: [
        {
          rank: 1,
          member_a_id: 'a',
          member_b_id: 'b',
          member_a_name: 'Al',
          member_b_name: 'Bo',
          score: 100,
          native: {},
          is_featured: false,
          opportunities_played: 1,
        },
      ],
    });
    renderSection('/admin/games/leaderboard');

    fireEvent.click(screen.getByRole('button', { name: 'Couples' }));

    expect(await screen.findByText('Al & Bo')).toBeInTheDocument();
    expect(screen.getByText('Couples ranked')).toBeInTheDocument();
    expect(getAdminCouplesLeaderboard).toHaveBeenCalledWith(
      expect.objectContaining({ period: 'weekly' }),
    );
    // Only couple titles are offered once switched: narrowing picks one.
    await chooseOption(1, 'Couple Questionnaire');
    await waitFor(() => {
      expect(getAdminCouplesLeaderboard).toHaveBeenCalledWith(
        expect.objectContaining({ gameId: 'couple-questionnaire' }),
      );
    });
  });

  it('narrows to one game through the game selector', async () => {
    renderSection('/admin/games/leaderboard');

    await chooseOption(1, 'Wordle');

    await waitFor(() => {
      expect(getAdminIndividualLeaderboard).toHaveBeenCalledWith(
        expect.objectContaining({ gameId: 'wordle' }),
      );
    });
  });

  it('keeps the failed fetch on screen with a retry', async () => {
    vi.mocked(getAdminIndividualLeaderboard).mockRejectedValue(new Error('network'));
    renderSection('/admin/games/leaderboard');

    expect(await screen.findByText('Standings are unavailable')).toBeInTheDocument();
    vi.mocked(getAdminIndividualLeaderboard).mockResolvedValue(emptyIndividualBoard());
    fireEvent.click(screen.getByRole('button', { name: /try again/i }));
    await waitFor(() => {
      expect(screen.getByText('No standings match these filters.')).toBeInTheDocument();
    });
  });
});

describe('AdminWordleManage: the Wordle editor page', () => {
  it('draws a word, shows it, and updates the session with exactly that word', async () => {
    renderSection('/admin/games/manage/wordle');
    const card = wordleCard();
    expect(await within(card).findByText(/No sessions yet/)).toBeInTheDocument();

    vi.mocked(drawWordleAnswer).mockResolvedValue({ word: 'cigar', token: 'signed-for-the-day' });
    const saved = aPuzzle({ answer: 'cigar', mode: 'random' });
    vi.mocked(createWordleGame).mockResolvedValue(saved);
    vi.mocked(getAdminWordleGames).mockResolvedValue([saved]);

    fireEvent.click(within(card).getByRole('button', { name: /draw for me/i }));
    fireEvent.click(within(card).getByRole('button', { name: /draw an answer/i }));

    // The answer is on screen before anything is written -- that is what the preview is for.
    expect(await within(card).findByText('cigar')).toBeInTheDocument();
    expect(drawWordleAnswer).toHaveBeenCalledWith(todayIso());

    fireEvent.click(within(card).getByRole('button', { name: /update session/i }));

    await waitFor(() =>
      expect(createWordleGame).toHaveBeenCalledWith({
        mode: 'random',
        word: 'cigar',
        token: 'signed-for-the-day',
      }),
    );
    expect(toast.success).toHaveBeenCalledWith(`Wordle session updated for ${todayIso()}.`);
    // Session history is collapsed by default: the row appears only after expanding.
    expect(
      within(card).queryByRole('button', { name: `Inspect the session for ${todayIso()}` }),
    ).not.toBeInTheDocument();
    fireEvent.click(within(card).getByRole('button', { name: /show session history/i }));
    await waitFor(() =>
      expect(
        within(card).getByRole('button', { name: `Inspect the session for ${todayIso()}` }),
      ).toBeInTheDocument(),
    );

    // The update is also an action the overview's activity feed reports, like every other here.
    await goTo('/admin/games');
    expect(screen.getByText('Puzzle')).toBeInTheDocument();
    expect(screen.getByText(`Wordle session updated for ${todayIso()}`)).toBeInTheDocument();
  });

  it('keeps the server’s reason for a refused update on the screen', async () => {
    renderSection('/admin/games/manage/wordle');
    const card = wordleCard();
    await within(card).findByText(/No sessions yet/);

    vi.mocked(createWordleGame).mockRejectedValue({
      response: {
        status: 400,
        data: { detail: 'That word is not on the approved list.' },
      },
    });

    fireEvent.change(within(card).getByLabelText('Answer'), { target: { value: 'cigar' } });
    fireEvent.click(within(card).getByRole('button', { name: /update session/i }));

    const alert = await within(card).findByRole('alert');
    expect(alert).toHaveTextContent('That word is not on the approved list.');
    // Not a toast: the reason is still here afterwards, next to a form that can be fixed.
    expect(toast.success).not.toHaveBeenCalled();
    expect(within(card).getByRole('button', { name: /update session/i })).toBeEnabled();
  });

  it('waits for five letters, then shows what the server says about the word', async () => {
    renderSection('/admin/games/manage/wordle');
    const card = wordleCard();
    await within(card).findByText(/No sessions yet/);

    const saveButton = within(card).getByRole('button', { name: /update session/i });
    expect(saveButton).toBeDisabled();

    fireEvent.change(within(card).getByLabelText('Answer'), { target: { value: 'zzzz' } });
    expect(saveButton).toBeDisabled();

    vi.mocked(createWordleGame).mockRejectedValue({
      response: { status: 400, data: { detail: 'That word is not on the approved list.' } },
    });
    fireEvent.change(within(card).getByLabelText('Answer'), { target: { value: 'zzzzz' } });
    expect(saveButton).toBeEnabled();

    fireEvent.click(saveButton);

    const alert = await within(card).findByRole('alert');
    expect(alert).toHaveTextContent('That word is not on the approved list.');
    expect(createWordleGame).toHaveBeenCalledWith({
      mode: 'custom',
      word: 'zzzzz',
    });
  });

  it('stages the content mode on toggle without saving until confirmed', async () => {
    vi.mocked(getGameConfigs).mockResolvedValue([gameConfig('wordle', 'manual')]);
    renderSection('/admin/games/manage/wordle');
    const card = wordleCard();
    await within(card).findByText(/No sessions yet/);

    // "Draw for me" stages random: the draw form appears, but nothing is saved yet.
    fireEvent.click(within(card).getByRole('button', { name: /draw for me/i }));
    expect(await within(card).findByText(/Unsaved changes/)).toBeInTheDocument();
    expect(
      within(card).getByRole('button', { name: /draw an answer/i }),
    ).toBeInTheDocument();
    expect(updateGameConfig).not.toHaveBeenCalled();

    // Clicking back clears the staged change again -- still nothing saved.
    fireEvent.click(within(card).getByRole('button', { name: /type a word/i }));
    await waitFor(() =>
      expect(within(card).queryByText(/Unsaved changes/)).not.toBeInTheDocument(),
    );
    expect(updateGameConfig).not.toHaveBeenCalled();
  });

  it('saves the staged content mode for the next session onwards', async () => {
    vi.mocked(getGameConfigs).mockResolvedValue([gameConfig('wordle', 'manual')]);
    vi.mocked(updateGameConfig).mockResolvedValue(gameConfig('wordle', 'random'));
    renderSection('/admin/games/manage/wordle');
    const card = wordleCard();
    await within(card).findByText(/No sessions yet/);

    fireEvent.click(within(card).getByRole('button', { name: /draw for me/i }));
    expect(await within(card).findByText(/Unsaved changes/)).toBeInTheDocument();
    fireEvent.click(within(card).getByRole('button', { name: /^save$/i }));

    await waitFor(() =>
      expect(updateGameConfig).toHaveBeenCalledWith('wordle', {
        content_mode: 'random',
      }),
    );
    expect(toast.success).toHaveBeenCalledWith(
      'Wordle will run on random words from the next session onwards.',
    );
  });

  it('generates a random word into the scheduling box without clearing typing', async () => {
    renderSection('/admin/games/manage/wordle');
    const card = wordleCard();
    await within(card).findByText(/No sessions yet/);

    vi.mocked(drawWordleAnswer).mockResolvedValue({ word: 'cigar', token: 'signed-for-schedule' });
    fireEvent.click(
      within(card).getByRole('button', { name: /generate a random word for the scheduled date/i }),
    );

    expect(await within(card).findByDisplayValue('cigar')).toBeInTheDocument();
    expect(drawWordleAnswer).toHaveBeenCalledWith(todayIso());

    // Typing still works after a generate.
    fireEvent.change(within(card).getByLabelText('Future word'), { target: { value: 'crane' } });
    expect(within(card).getByLabelText('Future word')).toHaveValue('crane');
  });

  it('keeps the games console from an account without games:manage', () => {
    const staffWithoutGames: AuthContextValue = {
      ...adminSession,
      can: (capability: Capability) =>
        capability !== CAPABILITIES.GAMES_MANAGE && can(ADMIN_CAPABILITIES, capability),
    };

    renderSection('/admin/games/manage', staffWithoutGames);

    // The overview is list-only: no inline editors, only title + Open Editor links.
    expect(screen.queryByRole('link', { name: /Open Wordle editor/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /Open Anagram editor/i })).not.toBeInTheDocument();
    expect(
      screen.queryByRole('link', { name: /Open Reaction Time editor/i }),
    ).not.toBeInTheDocument();
    // Every panel on this console publishes to the server now -- the featured schedule
    // and the per-game editors -- so the whole console content is gated.
    expect(screen.queryByRole('heading', { name: 'Featured game' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Featured spotlight' })).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Manage games' })).toBeInTheDocument();
  });

  it('lists every game with title, short description and Open Editor', async () => {
    renderSection('/admin/games/manage');

    for (const game of AVAILABLE_GAMES) {
      const link = await screen.findByRole('link', { name: `Open ${game.name} editor` });
      expect(link).toHaveAttribute('href', `/admin/games/manage/${game.id}`);
      expect(within(link).getByText(game.name)).toBeInTheDocument();
      expect(within(link).getByText(game.tagline)).toBeInTheDocument();
      expect(within(link).getByText('Open editor')).toBeInTheDocument();
    }
    // No inline configuration lives here anymore -- editors own their pages.
    expect(screen.queryByRole('button', { name: /update session/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /save featured day/i })).not.toBeInTheDocument();
  });
});

describe('AdminAnagramManage: the Anagram editor page', () => {
  /** The saved puzzle a test's `createAnagramGame` resolves to. */
  const anPuzzle = (overrides: Partial<AnagramGame> = {}): AnagramGame => ({
    id: '33333333-3333-3333-3333-333333333333',
    game_date: todayIso(),
    mode: 'custom',
    answer: 'GARDEN',
    created_by: 'TEST001',
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    ...overrides,
  });

  it('saves a typed word and shows the spelling the server stored', async () => {
    renderSection('/admin/games/manage/anagram');
    const card = anagramCard();
    expect(await within(card).findByText(/No sessions yet/)).toBeInTheDocument();

    vi.mocked(createAnagramGame).mockResolvedValue(anPuzzle());
    vi.mocked(getAdminAnagramGames).mockResolvedValue([anPuzzle()]);

    fireEvent.change(within(card).getByLabelText('Word'), { target: { value: '  garden  ' } });
    fireEvent.click(within(card).getByRole('button', { name: /update session/i }));

    await waitFor(() =>
      expect(createAnagramGame).toHaveBeenCalledWith({
        mode: 'custom',
        word: 'garden',
      }),
    );
    expect(toast.success).toHaveBeenCalledWith(`Anagram session updated for ${todayIso()}.`);

    // Session history is collapsed by default, so the stored spelling -- uppercase,
    // trimmed -- appears only after expanding, as the row in that card and nowhere else.
    expect(within(card).queryAllByText('GARDEN')).toHaveLength(0);
    fireEvent.click(within(card).getByRole('button', { name: /show session history/i }));
    expect((await within(card).findAllByText('GARDEN')).length).toBeGreaterThan(0);
    expect(
      within(card).getByRole('button', { name: `Inspect the session for ${todayIso()}` }),
    ).toBeInTheDocument();

    await goTo('/admin/games');
    expect(screen.getByText(`Anagram session updated for ${todayIso()}`)).toBeInTheDocument();
  });

  it('draws a word, shows it, and updates the session with exactly that word', async () => {
    renderSection('/admin/games/manage/anagram');
    const card = anagramCard();
    expect(await within(card).findByText(/No sessions yet/)).toBeInTheDocument();

    vi.mocked(drawAnagramAnswer).mockResolvedValue({ word: 'GARDEN', token: 'signed-for-the-day' });
    const saved = anPuzzle({ answer: 'GARDEN', mode: 'random' });
    vi.mocked(createAnagramGame).mockResolvedValue(saved);
    vi.mocked(getAdminAnagramGames).mockResolvedValue([saved]);

    fireEvent.click(within(card).getByRole('button', { name: /draw for me/i }));
    fireEvent.click(within(card).getByRole('button', { name: /draw a word/i }));

    // The word is on screen before anything is written -- that is what the preview is for.
    expect(await within(card).findByText('GARDEN')).toBeInTheDocument();
    expect(drawAnagramAnswer).toHaveBeenCalledWith(todayIso());

    fireEvent.click(within(card).getByRole('button', { name: /update session/i }));

    await waitFor(() =>
      expect(createAnagramGame).toHaveBeenCalledWith({
        mode: 'random',
        word: 'GARDEN',
        token: 'signed-for-the-day',
      }),
    );
    expect(toast.success).toHaveBeenCalledWith(`Anagram session updated for ${todayIso()}.`);
    fireEvent.click(within(card).getByRole('button', { name: /show session history/i }));
    await waitFor(() =>
      expect(
        within(card).getByRole('button', { name: `Inspect the session for ${todayIso()}` }),
      ).toBeInTheDocument(),
    );

    // The update is also an action the overview's activity feed reports, like every other here.
    await goTo('/admin/games');
    expect(screen.getByText(`Anagram session updated for ${todayIso()}`)).toBeInTheDocument();
  });

  it('stages the content mode on toggle and saves it on confirm', async () => {
    vi.mocked(getGameConfigs).mockResolvedValue([gameConfig('anagram', 'manual')]);
    vi.mocked(updateGameConfig).mockResolvedValue(gameConfig('anagram', 'random'));
    renderSection('/admin/games/manage/anagram');
    const card = anagramCard();
    await within(card).findByText(/No sessions yet/);

    // Staging alone saves nothing, however often the choice flips.
    fireEvent.click(within(card).getByRole('button', { name: /draw for me/i }));
    expect(await within(card).findByText(/Unsaved changes/)).toBeInTheDocument();
    fireEvent.click(within(card).getByRole('button', { name: /type a word/i }));
    fireEvent.click(within(card).getByRole('button', { name: /draw for me/i }));
    expect(updateGameConfig).not.toHaveBeenCalled();

    fireEvent.click(within(card).getByRole('button', { name: /^save$/i }));
    await waitFor(() =>
      expect(updateGameConfig).toHaveBeenCalledWith('anagram', {
        content_mode: 'random',
      }),
    );
    expect(toast.success).toHaveBeenCalledWith(
      'Anagram will run on random words from the next session onwards.',
    );
  });

  it('keeps the server’s reason for a refused update on the screen', async () => {
    renderSection('/admin/games/manage/anagram');
    const card = anagramCard();
    await within(card).findByText(/No sessions yet/);

    vi.mocked(createAnagramGame).mockRejectedValue({
      response: {
        status: 400,
        data: { detail: 'An answer is letters only -- no spaces, digits or punctuation.' },
      },
    });

    fireEvent.change(within(card).getByLabelText('Word'), { target: { value: 'garden' } });
    fireEvent.click(within(card).getByRole('button', { name: /update session/i }));

    const alert = await within(card).findByRole('alert');
    expect(alert).toHaveTextContent('An answer is letters only');
    // The Wordle editor on its own page was never asked to save anything.
    expect(createWordleGame).not.toHaveBeenCalled();
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('generates a random word into the scheduling box without clearing typing', async () => {
    renderSection('/admin/games/manage/anagram');
    const card = anagramCard();
    await within(card).findByText(/No sessions yet/);

    vi.mocked(drawAnagramAnswer).mockResolvedValue({ word: 'GARDEN', token: 'signed-for-schedule' });
    fireEvent.click(
      within(card).getByRole('button', { name: /generate a random word for the scheduled date/i }),
    );

    expect(await within(card).findByDisplayValue('GARDEN')).toBeInTheDocument();
    expect(drawAnagramAnswer).toHaveBeenCalledWith(todayIso());

    // Typing still works after a generate.
    fireEvent.change(within(card).getByLabelText('Future word'), { target: { value: 'meadow' } });
    expect(within(card).getByLabelText('Future word')).toHaveValue('meadow');
  });
});

describe('Per-game editors: status and collapsed history', () => {
  /** The status block inside one game card, found by its accessible group name. */
  const statusGroup = (card: HTMLElement, name: string) =>
    within(card).getByRole('group', { name: `${name} status and reset` });

  it('keeps session history collapsed until the admin expands it', async () => {
    const saved = aPuzzle({ answer: 'cigar', mode: 'random' });
    vi.mocked(getAdminWordleGames).mockResolvedValue([saved]);
    renderSection('/admin/games/manage/wordle');
    const card = wordleCard();

    // The count shows, but no row renders until expanded.
    expect(await within(card).findByText('1 sessions')).toBeInTheDocument();
    expect(
      within(card).queryByRole('button', { name: `Inspect the session for ${todayIso()}` }),
    ).not.toBeInTheDocument();

    fireEvent.click(within(card).getByRole('button', { name: /show session history/i }));
    expect(
      await within(card).findByText('cigar'),
    ).toBeInTheDocument();
    expect(
      within(card).getByRole('button', { name: `Inspect the session for ${todayIso()}` }),
    ).toBeInTheDocument();

    fireEvent.click(within(card).getByRole('button', { name: /hide session history/i }));
    await waitFor(() =>
      expect(
        within(card).queryByRole('button', { name: `Inspect the session for ${todayIso()}` }),
      ).not.toBeInTheDocument(),
    );
  });

  it('toggles availability from inside the game card', async () => {
    vi.mocked(getGameConfigs).mockResolvedValue([gameConfig('wordle', 'manual')]);
    vi.mocked(updateGameConfig).mockResolvedValue(gameConfig('wordle', 'manual'));
    renderSection('/admin/games/manage/wordle');
    const card = wordleCard();
    const group = statusGroup(card, 'Wordle');
    const enabledSwitch = await within(group).findByRole('switch', {
      name: 'Wordle enabled',
    });

    fireEvent.click(enabledSwitch);
    await waitFor(() =>
      expect(updateGameConfig).toHaveBeenCalledWith('wordle', { enabled: false }),
    );

    fireEvent.click(within(group).getByRole('switch', { name: 'Wordle hidden' }));
    await waitFor(() =>
      expect(updateGameConfig).toHaveBeenCalledWith('wordle', { is_hidden: true }),
    );
  });

  it('stages the reset frequency and only saves on Save', async () => {
    vi.mocked(getGameConfigs).mockResolvedValue([gameConfig('anagram', 'manual')]);
    vi.mocked(updateGameConfig).mockResolvedValue(gameConfig('anagram', 'manual'));
    renderSection('/admin/games/manage/anagram');
    const card = anagramCard();
    const group = statusGroup(card, 'Anagram');
    await within(group).findByRole('switch', { name: 'Anagram enabled' });

    fireEvent.pointerDown(within(group).getByRole('combobox'), {
      button: 0,
      ctrlKey: false,
      pointerType: 'mouse',
    });
    fireEvent.click(await screen.findByRole('option', { name: 'Weekly (Mon–Sun UTC)' }));

    // Picking alone stages without saving.
    expect(await within(group).findByText(/Unsaved changes/)).toBeInTheDocument();
    expect(updateGameConfig).not.toHaveBeenCalled();

    fireEvent.click(within(group).getByRole('button', { name: /^save$/i }));

    await waitFor(() =>
      expect(updateGameConfig).toHaveBeenCalledWith('anagram', {
        reset_cadence: 'weekly',
      }),
    );
  });

  it('discards a staged reset frequency without saving', async () => {
    vi.mocked(getGameConfigs).mockResolvedValue([gameConfig('anagram', 'manual')]);
    renderSection('/admin/games/manage/anagram');
    const card = anagramCard();
    const group = statusGroup(card, 'Anagram');
    await within(group).findByRole('switch', { name: 'Anagram enabled' });

    fireEvent.pointerDown(within(group).getByRole('combobox'), {
      button: 0,
      ctrlKey: false,
      pointerType: 'mouse',
    });
    fireEvent.click(await screen.findByRole('option', { name: 'Weekly (Mon–Sun UTC)' }));
    expect(await within(group).findByText(/Unsaved changes/)).toBeInTheDocument();

    fireEvent.click(within(group).getByRole('button', { name: /discard/i }));
    await waitFor(() =>
      expect(within(group).queryByText(/Unsaved changes/)).not.toBeInTheDocument(),
    );
    expect(updateGameConfig).not.toHaveBeenCalled();
  });
});

describe('AdminTwoKeyEscapeManage: the maze size', () => {
  const twoKeyCard = () => {
    const headings = screen.getAllByRole('heading', { name: 'Two-Key Escape' });
    for (const heading of headings) {
      const card = heading.closest('.rounded-2xl');
      if (card) return card as HTMLElement;
    }
    throw new Error('The Two-Key Escape card could not be found from its heading.');
  };

  it('stages the grid size and only saves on Save', async () => {
    vi.mocked(getGameConfigs).mockResolvedValue([
      { ...gameConfig('two-key-escape'), grid_size: 12 },
    ]);
    vi.mocked(updateGameConfig).mockResolvedValue({
      ...gameConfig('two-key-escape'),
      grid_size: 13,
    });
    renderSection('/admin/games/manage/two-key-escape');
    const card = twoKeyCard();
    await within(card).findByText(/The only gameplay setting/);

    // The page holds two pickers (reset frequency, then grid size) -- the
    // grid size is the second one.
    const gridPicker = within(card).getAllByRole('combobox')[1];
    fireEvent.pointerDown(gridPicker, {
      button: 0,
      ctrlKey: false,
      pointerType: 'mouse',
    });
    fireEvent.click(await screen.findByRole('option', { name: /13 × 13/ }));

    // Picking alone stages without saving.
    expect(await within(card).findByText(/Unsaved changes/)).toBeInTheDocument();
    expect(updateGameConfig).not.toHaveBeenCalled();

    fireEvent.click(within(card).getByRole('button', { name: /^save$/i }));

    await waitFor(() =>
      expect(updateGameConfig).toHaveBeenCalledWith('two-key-escape', { grid_size: 13 }),
    );
    expect(toast.success).toHaveBeenCalledWith('Maze size set to 13x13 (13 locks).');
  });

  it('discards a staged grid size without saving', async () => {
    vi.mocked(getGameConfigs).mockResolvedValue([
      { ...gameConfig('two-key-escape'), grid_size: 12 },
    ]);
    renderSection('/admin/games/manage/two-key-escape');
    const card = twoKeyCard();
    await within(card).findByText(/The only gameplay setting/);

    const gridPicker = within(card).getAllByRole('combobox')[1];
    fireEvent.pointerDown(gridPicker, {
      button: 0,
      ctrlKey: false,
      pointerType: 'mouse',
    });
    fireEvent.click(await screen.findByRole('option', { name: /13 × 13/ }));
    expect(await within(card).findByText(/Unsaved changes/)).toBeInTheDocument();

    fireEvent.click(within(card).getByRole('button', { name: /discard/i }));
    await waitFor(() =>
      expect(within(card).queryByText(/Unsaved changes/)).not.toBeInTheDocument(),
    );
    expect(updateGameConfig).not.toHaveBeenCalled();
  });
});
