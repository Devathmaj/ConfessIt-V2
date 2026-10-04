import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import type { ContextType } from 'react';

import { PlotTwistGamePage } from '@/pages/PlotTwistGamePage';
import { AuthContext } from '@/contexts/AuthContext';
import type { Capability } from '@/lib/capabilities';
import type { CouplePartnerCandidate, PlotTwistState } from '@/services/api';

/**
 * The Plot Twist screen against its API boundary.
 *
 * The contract pinned here is the one the player experiences: only the
 * participant assigned to the current scene sees its choices, the other
 * side waits without seeing them, and the settled ending arrives with the
 * full decision history for both sides. The screen reads its state on
 * entry and after every write; a refresh lands on the same scene because
 * the session lives in the database rather than in this component.
 */

vi.mock('@/services/api', () => ({
  getPlotTwistState: vi.fn(),
  getPlotTwistPartners: vi.fn(),
  selectPlotTwistPartner: vi.fn(),
  startPlotTwistEvent: vi.fn(),
  submitPlotTwistChoice: vi.fn(),
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

const {
  getPlotTwistState,
  getPlotTwistPartners,
  selectPlotTwistPartner,
  startPlotTwistEvent,
  submitPlotTwistChoice,
} = await import('@/services/api');

type AuthContextValue = ContextType<typeof AuthContext>;

const authValue = (gender: 'male' | 'female' | 'other' = 'male'): AuthContextValue =>
  ({
    user: {
      id: 'player-a',
      name: 'Alex',
      regno: 'TEST001',
      email: 'test@example.com',
      gender,
    },
    isAuthenticated: true,
    isLoading: false,
    token: 'token',
    logout: vi.fn(),
    verifyMagicToken: vi.fn(),
    fetchAndSetUser: vi.fn(),
    can: (_capability: Capability) => false,
    signInPath: '/login',
  }) as AuthContextValue;

const candidate = (
  overrides: Partial<CouplePartnerCandidate> = {},
): CouplePartnerCandidate => ({
  id: 'id-b',
  name: 'Sam',
  gender: 'female',
  year: 2,
  department: 'Class',
  ...overrides,
});

const baseState = (overrides: Partial<PlotTwistState> = {}): PlotTwistState => ({
  stage: 'selecting_partner',
  game_id: 'game-1',
  session_id: null,
  my_selection: null,
  partner: null,
  scene: null,
  state_version: 0,
  decisions_made: 0,
  history: [],
  result: null,
  ...overrides,
});

const sceneState = (overrides: Partial<PlotTwistState> = {}) =>
  baseState({
    stage: 'awaiting_you',
    session_id: 'session-1',
    partner: { id: 'id-b', name: 'Sam' },
    scene: {
      scene_id: 's_open',
      scene_type: 'opening',
      title: 'Locked In',
      narrative_text: 'The auditorium doors click shut behind you.',
      actor: 'participant_one',
      choices: [
        { choice_id: 'c_loading', text: 'Sneak out through the loading area.' },
        { choice_id: 'c_lobby', text: 'Head for the main lobby.' },
      ],
      is_terminal: false,
      visited_count: 1,
    },
    state_version: 0,
    ...overrides,
  });

const renderGame = (gender: 'male' | 'female' | 'other' = 'male') =>
  render(
    <AuthContext.Provider value={authValue(gender)}>
      <MemoryRouter initialEntries={['/games/plot-twist']}>
        <Routes>
          <Route path="/games/plot-twist" element={<PlotTwistGamePage />} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getPlotTwistState).mockResolvedValue(baseState());
  vi.mocked(getPlotTwistPartners).mockResolvedValue([]);
});

describe('partner selection', () => {
  it('lists candidates and saves the pick', async () => {
    vi.mocked(getPlotTwistPartners).mockResolvedValue([candidate()]);
    vi.mocked(selectPlotTwistPartner).mockResolvedValue(
      baseState({ my_selection: { selected_user_id: 'id-b', selected_user_name: 'Sam' } }),
    );
    renderGame();

    fireEvent.click((await screen.findAllByRole('button', { name: 'Choose' }))[0]);

    await waitFor(() => expect(selectPlotTwistPartner).toHaveBeenCalledWith('id-b'));
    expect(await screen.findByText(/waiting for them to choose you back/i)).toBeInTheDocument();
  });
});

describe('entering the event', () => {
  it('offers paired couples a way in without re-picking', async () => {
    vi.mocked(getPlotTwistState).mockResolvedValue(
      baseState({
        my_selection: { selected_user_id: 'id-b', selected_user_name: 'Sam' },
        partner: { id: 'id-b', name: 'Sam' },
      }),
    );
    vi.mocked(startPlotTwistEvent).mockResolvedValue(sceneState());
    renderGame();

    fireEvent.click(await screen.findByRole('button', { name: /begin the story/i }));

    await waitFor(() => expect(startPlotTwistEvent).toHaveBeenCalled());
    expect(await screen.findByText('Locked In')).toBeInTheDocument();
  });
});

describe('assigned decisions', () => {
  it('shows choices only to the assigned participant', async () => {
    vi.mocked(getPlotTwistState).mockResolvedValue(sceneState());
    renderGame();

    expect(await screen.findByText('Locked In')).toBeInTheDocument();
    expect(screen.getByText('Sneak out through the loading area.')).toBeInTheDocument();
    expect(screen.getByText('Your decision')).toBeInTheDocument();
  });

  it('hides choices from the waiting participant but keeps the narrative', async () => {
    vi.mocked(getPlotTwistState).mockResolvedValue(
      sceneState({
        stage: 'awaiting_partner',
        scene: {
          scene_id: 's_dock',
          scene_type: 'encounter',
          title: 'Loading Dock',
          narrative_text: 'The loading door has an alarm light blinking.',
          actor: 'participant_two',
          choices: [],
          is_terminal: false,
          visited_count: 1,
        },
      }),
    );
    renderGame();

    expect(await screen.findByText('Loading Dock')).toBeInTheDocument();
    expect(
      screen.getByText('The loading door has an alarm light blinking.'),
    ).toBeInTheDocument();
    expect(screen.queryByText('Sneak out through the loading area.')).not.toBeInTheDocument();
    expect(screen.getByText(/waiting for sam/i)).toBeInTheDocument();
  });

  it('submits the chosen decision with the current version', async () => {
    vi.mocked(getPlotTwistState).mockResolvedValue(sceneState());
    vi.mocked(submitPlotTwistChoice).mockResolvedValue(
      sceneState({
        stage: 'awaiting_partner',
        state_version: 1,
        decisions_made: 1,
        scene: {
          scene_id: 's_dock',
          scene_type: 'encounter',
          title: 'Loading Dock',
          narrative_text: 'The loading door has an alarm light blinking.',
          actor: 'participant_two',
          choices: [],
          is_terminal: false,
          visited_count: 1,
        },
      }),
    );
    renderGame();

    fireEvent.click(await screen.findByText('Sneak out through the loading area.'));

    await waitFor(() =>
      expect(submitPlotTwistChoice).toHaveBeenCalledWith('c_loading', 0),
    );
    expect(await screen.findByText(/waiting for sam/i)).toBeInTheDocument();
  });
});

describe('completed stories', () => {
  it('renders the ending, history and scoring note for both sides', async () => {
    vi.mocked(getPlotTwistState).mockResolvedValue(
      baseState({
        stage: 'completed',
        session_id: 'session-1',
        partner: { id: 'id-b', name: 'Sam' },
        decisions_made: 2,
        history: [
          {
            seq: 1,
            scene_id: 's_open',
            scene_title: 'Locked In',
            actor_user_id: 'player-a',
            actor_name: 'Alex',
            choice_id: 'c_loading',
            choice_text: 'Sneak out through the loading area.',
            consequence_text: 'You slip backstage.',
            created_at: '2026-10-09T00:00:00Z',
          },
        ],
        result: {
          ending_id: 'e_escape',
          ending_type: 'escape',
          ending_title: 'Great Escape',
          narrative: 'You made it out together.',
          success: true,
          treasures: ['s_treasure'],
          decisions_made: 2,
          completed_at: '2026-10-09T00:05:00Z',
        },
      }),
    );
    renderGame();

    expect(await screen.findByText('Great Escape')).toBeInTheDocument();
    expect(screen.getByText('You made it out together.')).toBeInTheDocument();
    expect(screen.getByText(/sneak out through the loading area/i)).toBeInTheDocument();
    expect(screen.getByText(/couples leaderboard/i)).toBeInTheDocument();
  });
});
