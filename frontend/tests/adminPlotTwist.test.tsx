import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import type { ContextType } from 'react';

import { AdminPlotTwistManage } from '@/pages/AdminPlotTwistManage';
import { AuthContext } from '@/contexts/AuthContext';
import { GamesConfigProvider } from '@/contexts/GamesConfigContext';
import type { Capability } from '@/lib/capabilities';
import type { PlotTwistBlueprint, PlotTwistPackage } from '@/services/api';

/**
 * The Plot Twist admin console against its API boundary.
 *
 * The contract pinned here is the generation workflow: difficulty-only
 * parameters start a blueprint, validation errors name the offending
 * scene, and publication is a distinct, validated, append-only step.
 * Availability switches ride the shared game-config endpoint like every
 * other title.
 */

vi.mock('@/services/api', () => ({
  getGameConfigs: vi.fn(),
  updateGameConfig: vi.fn(),
  getFeaturedGames: vi.fn(),
  replaceFeaturedGame: vi.fn(),
  createPlotTwistBlueprint: vi.fn(),
  getPlotTwistBlueprints: vi.fn(),
  getPlotTwistBlueprint: vi.fn(),
  generatePlotTwistBlueprint: vi.fn(),
  approvePlotTwistBlueprint: vi.fn(),
  generatePlotTwistPackage: vi.fn(),
  getPlotTwistPackages: vi.fn(),
  getPlotTwistPackage: vi.fn(),
  regeneratePlotTwistScene: vi.fn(),
  updatePlotTwistPackage: vi.fn(),
  simulatePlotTwistPackage: vi.fn(),
  publishPlotTwistPackage: vi.fn(),
  getCurrentAdminPlotTwistSession: vi.fn(),
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
  getPlotTwistBlueprints,
  getPlotTwistPackages,
  getCurrentAdminPlotTwistSession,
  createPlotTwistBlueprint,
  generatePlotTwistBlueprint,
  publishPlotTwistPackage,
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

const blueprint = (overrides: Partial<PlotTwistBlueprint> = {}): PlotTwistBlueprint => ({
  id: 'bp-1',
  title: 'Locked In',
  difficulty: 'medium',
  config: { difficulty: 'medium' },
  blueprint: {
    title: 'Locked In',
    premise: 'Two students locked in overnight.',
    main_path: ['locked in', 'dock', 'escape'],
    state_vars: [{ name: 'courage' }],
    endings: [{ id: 'e_escape', title: 'Out' }],
    constraints: ['Stay indoors.'],
  },
  status: 'ready_for_review',
  validation_errors: [],
  created_by: 'adminuser',
  created_at: '2026-10-09T00:00:00Z',
  updated_at: '2026-10-09T00:00:00Z',
  ...overrides,
});

const storyPackage = (overrides: Partial<PlotTwistPackage> = {}): PlotTwistPackage => ({
  id: 'pkg-1',
  blueprint_id: 'bp-1',
  version: 1,
  title: 'Locked In',
  package: {
    title: 'Locked In',
    opening_scene_id: 's_open',
    scenes: [
      {
        id: 's_open',
        type: 'opening',
        title: 'Locked In',
        narrative_text: 'The doors click shut.',
        actor: 'participant_one',
        slot: 'core',
        requires_tags: [],
        provides_tags: [],
        forbidden_tags: [],
        choices: [
          {
            id: 'c_loading',
            text: 'Sneak out.',
            consequence_text: 'Backstage.',
            next_scene_id: 's_free',
          },
        ],
        is_terminal: false,
      },
      {
        id: 's_free',
        type: 'ending',
        title: 'Out',
        narrative_text: 'Free.',
        actor: 'either',
        slot: 'core',
        requires_tags: [],
        provides_tags: [],
        forbidden_tags: [],
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
        narrative: 'Freedom.',
        conditions: [],
        priority: 10,
        success: true,
        reward_tier: 'silver',
      },
    ],
  },
  status: 'ready_for_review',
  validation_errors: [],
  published_at: null,
  created_by: 'adminuser',
  created_at: '2026-10-09T00:00:00Z',
  updated_at: '2026-10-09T00:00:00Z',
  ...overrides,
});

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
  vi.mocked(getGameConfigs).mockResolvedValue([
    {
      game_id: 'plot-twist',
      enabled: true,
      is_hidden: false,
      content_mode: null,
      reset_cadence: 'daily',
      grid_size: null,
    },
  ]);
  vi.mocked(getFeaturedGames).mockResolvedValue([]);
  vi.mocked(getPlotTwistBlueprints).mockResolvedValue([]);
  vi.mocked(getPlotTwistPackages).mockResolvedValue([]);
  vi.mocked(getCurrentAdminPlotTwistSession).mockRejectedValue(new Error('none'));
});

describe('generation workflow', () => {
  it('starts a blueprint from the difficulty picker', async () => {
    vi.mocked(createPlotTwistBlueprint).mockResolvedValue(blueprint({ status: 'draft' }));
    vi.mocked(generatePlotTwistBlueprint).mockResolvedValue(blueprint());
    renderConsole();

    fireEvent.click(await screen.findByRole('button', { name: /generate blueprint/i }));

    await waitFor(() =>
      expect(createPlotTwistBlueprint).toHaveBeenCalledWith(
        expect.objectContaining({ difficulty: 'medium' }),
      ),
    );
  });

  it('lists validation errors naming the offending scene', async () => {
    vi.mocked(getPlotTwistPackages).mockResolvedValue([
      storyPackage({
        status: 'validation_failed',
        validation_errors: [
          { scene_id: 's_open', code: 'unknown_transition', message: 'Choice points nowhere.' },
        ],
      }),
    ]);
    renderConsole();

    expect(await screen.findByText(/validation errors \(1\)/i)).toBeInTheDocument();
    expect(screen.getByText(/s_open/)).toBeInTheDocument();
    expect(screen.getByText(/\[unknown_transition\]/)).toBeInTheDocument();
  });

  it('publishes a validated version', async () => {
    vi.mocked(getPlotTwistPackages).mockResolvedValue([storyPackage()]);
    vi.mocked(publishPlotTwistPackage).mockResolvedValue(storyPackage({ status: 'published' }));
    renderConsole();

    fireEvent.click(await screen.findByRole('button', { name: /publish version/i }));

    await waitFor(() => expect(publishPlotTwistPackage).toHaveBeenCalledWith('pkg-1'));
  });

  it('inspects scenes, choices and endings', async () => {
    vi.mocked(getPlotTwistPackages).mockResolvedValue([storyPackage()]);
    renderConsole();

    expect(await screen.findByText(/scenes \(2\)/i)).toBeInTheDocument();
    expect(screen.getByText(/endings \(1\)/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /locked in · opening/i }));
    expect(
      within(screen.getByText('Sneak out.').closest('div') as HTMLElement).getByText(/→ s_free/),
    ).toBeInTheDocument();
  });
});
