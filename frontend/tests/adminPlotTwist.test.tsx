import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import type { ContextType } from 'react';

import { AdminPlotTwistManage } from '@/pages/AdminPlotTwistManage';
import { AuthContext } from '@/contexts/AuthContext';
import { GamesConfigProvider } from '@/contexts/GamesConfigContext';
import type { Capability } from '@/lib/capabilities';
import type {
  AiGenerationState,
  AiSessionHistoryEntry,
  GameConfig,
  PlotTwistSessionOverview,
} from '@/services/api';

/**
 * The Plot Twist admin console against its API boundary.
 *
 * Sessions generate and publish automatically now, so the contract pinned
 * here is the simplified console: difficulty and frequency are the only
 * content knobs, the published story is inspectable (scenes, choices,
 * endings), generation status explains an empty screen, revoke is a distinct
 * confirmable action, and history pages through the backend. None of the
 * blueprint/approve/package/publish workflow is reachable from this screen.
 */

vi.mock('@/services/api', () => ({
  getGameConfigs: vi.fn(),
  updateGameConfig: vi.fn(),
  getFeaturedGames: vi.fn(),
  replaceFeaturedGame: vi.fn(),
  getPlotTwistSessionOverview: vi.fn(),
  getPlotTwistSessionHistory: vi.fn(),
  revokePlotTwistSession: vi.fn(),
}));

vi.mock('sonner', () => ({
  toast: Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
  }),
  Toaster: () => null,
}));

const api = await import('@/services/api');
const {
  getGameConfigs,
  getFeaturedGames,
  getPlotTwistSessionOverview,
  getPlotTwistSessionHistory,
  revokePlotTwistSession,
  updateGameConfig,
} = api;

type AuthContextValue = ContextType<typeof AuthContext>;

const authValue = (): AuthContextValue =>
  ({
    user: {
      id: 'admin-1',
      name: 'Admin',
      regno: 'ADMIN001',
      email: 'admin@example.com',
    },
    isAuthenticated: true,
    isLoading: false,
    token: 'token',
    logout: vi.fn(),
    verifyMagicToken: vi.fn(),
    fetchAndSetUser: vi.fn(),
    can: (_capability: Capability) => true,
    signInPath: '/login/admin',
  }) as AuthContextValue;

const gameConfig = (overrides: Partial<GameConfig> = {}): GameConfig => ({
  game_id: 'plot-twist',
  enabled: true,
  is_hidden: false,
  content_mode: null,
  reset_cadence: 'daily',
  grid_size: null,
  difficulty: 'medium',
  ...overrides,
});

const generation = (overrides: Partial<AiGenerationState> = {}): AiGenerationState => ({
  id: 'job-1',
  game_id: 'plot-twist',
  cycle_start_date: '2026-10-19',
  cycle_end_date: '2026-10-20',
  difficulty: 'medium',
  status: 'published',
  attempt: 1,
  max_attempts: 3,
  requested_at: '2026-10-19T00:01:00Z',
  started_at: '2026-10-19T00:01:05Z',
  finished_at: '2026-10-19T00:02:00Z',
  next_attempt_at: null,
  blueprint_id: 'bp-1',
  package_id: 'pkg-1',
  error_code: null,
  last_error: null,
  created_by: 'scheduler',
  ...overrides,
});

const publishedOverview = (): PlotTwistSessionOverview => ({
  game_id: 'plot-twist',
  session: {
    id: 'game-1',
    status: 'active',
    period_start_date: '2026-10-19',
    period_end_date: '2026-10-20',
    package_id: 'pkg-1',
    title: 'Locked In',
    package: {
      title: 'Locked In',
      summary: 'Locked in the auditorium overnight.',
      opening_scene_id: 's_open',
      scenes: [
        {
          id: 's_open',
          type: 'opening',
          title: 'Locked In',
          narrative_text: 'The doors click shut behind you.',
          actor: 'participant_one',
          slot: 'core',
          choices: [
            {
              id: 'c_loading',
              text: 'Sneak out through the loading area.',
              consequence_text: 'You slip backstage.',
              next_scene_id: 's_free',
            },
          ],
          is_terminal: false,
        },
        {
          id: 's_free',
          type: 'ending',
          title: 'Out Into the Night',
          narrative_text: 'You made it out together.',
          actor: 'either',
          slot: 'core',
          choices: [],
          is_terminal: true,
          ending_id: 'e_escape',
        },
      ],
      endings: [
        {
          id: 'e_escape',
          type: 'escape',
          title: 'Great Escape',
          narrative: 'Freedom tastes sweet.',
          success: true,
          reward_tier: 'silver',
        },
      ],
    },
    created_by: 'scheduler',
    started_at: '2026-10-19T00:02:00Z',
    ended_at: null,
    created_at: '2026-10-19T00:02:00Z',
  },
  generation: generation(),
});

const emptyOverview = (
  generationState: AiGenerationState | null = null,
): PlotTwistSessionOverview => ({
  game_id: 'plot-twist',
  session: null,
  generation: generationState,
});

const historyEntry = (
  id: string,
  overrides: Partial<AiSessionHistoryEntry> = {},
): AiSessionHistoryEntry => ({
  id,
  status: 'ended',
  period_start_date: '2026-10-18',
  period_end_date: '2026-10-19',
  package_id: 'pkg-old',
  title: `Session ${id}`,
  revoked: false,
  created_by: 'scheduler',
  started_at: '2026-10-18T00:02:00Z',
  ended_at: '2026-10-19T00:01:00Z',
  created_at: '2026-10-18T00:02:00Z',
  ...overrides,
});

const historyPage = (
  sessions: AiSessionHistoryEntry[],
  total: number,
  skip = 0,
  limit = 10,
) => ({ sessions, total, skip, limit });

const renderConsole = () =>
  render(
    <AuthContext.Provider value={authValue()}>
      <GamesConfigProvider>
        <MemoryRouter initialEntries={['/admin/games/manage/plot-twist']}>
          <Routes>
            <Route path="/admin/games/manage/plot-twist" element={<AdminPlotTwistManage />} />
          </Routes>
        </MemoryRouter>
      </GamesConfigProvider>
    </AuthContext.Provider>,
  );

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getGameConfigs).mockResolvedValue([gameConfig()]);
  vi.mocked(getFeaturedGames).mockResolvedValue([]);
  vi.mocked(getPlotTwistSessionOverview).mockResolvedValue(emptyOverview());
  vi.mocked(getPlotTwistSessionHistory).mockResolvedValue(historyPage([], 0));
  vi.mocked(updateGameConfig).mockResolvedValue(gameConfig());
  vi.mocked(revokePlotTwistSession).mockResolvedValue({
    game_id: 'plot-twist',
    action: 'revoked',
    session_id: 'game-1',
    revoked_until: '2026-10-20',
  });
});

describe('Plot Twist console: configuration only', () => {
  it('exposes difficulty and frequency, and none of the old workflow actions', async () => {
    renderConsole();

    expect(await screen.findByText('Difficulty')).toBeInTheDocument();
    expect(document.getElementById('plot-twist-difficulty')).not.toBeNull();
    // The frequency picker rides the shared status controls every game uses.
    expect(await screen.findByText(/session reset/i)).toBeInTheDocument();

    for (const label of [
      /generate blueprint/i,
      /approve/i,
      /publish/i,
      /generate package/i,
    ]) {
      expect(screen.queryByRole('button', { name: label })).not.toBeInTheDocument();
    }
  });

  it('stages a difficulty change and saves it through the game-config endpoint', async () => {
    renderConsole();

    const picker = document.getElementById('plot-twist-difficulty') as HTMLElement;
    await waitFor(() => expect(picker).toHaveTextContent(/medium/i));
    fireEvent.pointerDown(picker, { button: 0, ctrlKey: false, pointerType: 'mouse' });
    fireEvent.click(await screen.findByRole('option', { name: 'Hard' }));
    fireEvent.click(await screen.findByRole('button', { name: /^save$/i }));

    await waitFor(() =>
      expect(updateGameConfig).toHaveBeenCalledWith('plot-twist', { difficulty: 'hard' }),
    );
  });
});

describe('Plot Twist console: the current published session', () => {
  it('renders the story, its choices and its outcomes', async () => {
    vi.mocked(getPlotTwistSessionOverview).mockResolvedValue(publishedOverview());
    renderConsole();

    expect(await screen.findByText(/locked in the auditorium overnight/i)).toBeInTheDocument();
    expect(screen.getByText(/scenes and choices \(2\)/i)).toBeInTheDocument();
    expect(screen.getByText(/outcomes \(1\)/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /inspect scene s_open/i }));
    expect(screen.getByText('The doors click shut behind you.')).toBeInTheDocument();
    expect(screen.getByText('Sneak out through the loading area.')).toBeInTheDocument();
    expect(screen.getByText(/→ s_free/)).toBeInTheDocument();
  });

  it('shows an empty state with the generation status while nothing is published', async () => {
    vi.mocked(getPlotTwistSessionOverview).mockResolvedValue(
      emptyOverview(generation({ status: 'running', finished_at: null })),
    );
    renderConsole();

    expect(await screen.findByText(/no session is active right now/i)).toBeInTheDocument();
    expect(screen.getByText(/generation in progress/i)).toBeInTheDocument();
  });

  it('surfaces the last generation error when a cycle failed', async () => {
    vi.mocked(getPlotTwistSessionOverview).mockResolvedValue(
      emptyOverview(
        generation({
          status: 'failed',
          error_code: 'attempts_exhausted',
          last_error: 'The story provider refused the request.',
        }),
      ),
    );
    renderConsole();

    expect(await screen.findByText(/generation failed/i)).toBeInTheDocument();
    expect(screen.getByText(/provider refused the request/i)).toBeInTheDocument();
  });
});

describe('Plot Twist console: revoke', () => {
  it('confirms, calls the revoke endpoint and re-reads the screen', async () => {
    let overview: PlotTwistSessionOverview = publishedOverview();
    vi.mocked(getPlotTwistSessionOverview).mockImplementation(async () => overview);
    renderConsole();

    // Wait for the live session so the revoke control is enabled.
    await screen.findByText(/locked in the auditorium overnight/i);
    const revokeButton = screen.getByRole('button', { name: /revoke session/i });
    expect(revokeButton).toBeEnabled();
    fireEvent.click(revokeButton);
    expect(revokePlotTwistSession).not.toHaveBeenCalled();

    // The refresh that follows the revoke answers the empty state.
    overview = emptyOverview(generation({ status: 'cancelled' }));
    fireEvent.click(screen.getByRole('button', { name: /confirm revoke/i }));
    await waitFor(() => expect(revokePlotTwistSession).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/no session is active right now/i)).toBeInTheDocument();
    expect(getPlotTwistSessionOverview).toHaveBeenCalledTimes(2);
  });

  it('cannot revoke when no session is live', async () => {
    renderConsole();
    const button = await screen.findByRole('button', { name: /revoke session/i });
    expect(button).toBeDisabled();
  });
});

describe('Plot Twist console: paginated history', () => {
  it('grows the list downwards through backend pages', async () => {
    const first = Array.from({ length: 10 }, (_, index) => historyEntry(`hist-${index}`));
    const second = [historyEntry('hist-10'), historyEntry('hist-11')];
    vi.mocked(getPlotTwistSessionHistory).mockImplementation(async (params) =>
      params?.skip ? historyPage(second, 12, params.skip ?? 0, 10) : historyPage(first, 12),
    );
    renderConsole();

    fireEvent.click(await screen.findByRole('button', { name: /show session history/i }));
    expect(screen.getByText('Session hist-0')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /show more sessions/i }));
    await waitFor(() =>
      expect(getPlotTwistSessionHistory).toHaveBeenLastCalledWith({ skip: 10, limit: 10 }),
    );
    expect(await screen.findByText('Session hist-11')).toBeInTheDocument();
  });

  it('marks revoked sessions in the history', async () => {
    vi.mocked(getPlotTwistSessionHistory).mockResolvedValue(
      historyPage([historyEntry('hist-1', { revoked: true })], 1),
    );
    renderConsole();

    fireEvent.click(await screen.findByRole('button', { name: /show session history/i }));
    expect(screen.getByText('revoked')).toBeInTheDocument();
  });
});
