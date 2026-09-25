import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { ContextType } from 'react';

import { TwoKeyEscapePage } from '@/pages/TwoKeyEscapePage';
import { toast } from 'sonner';
import { AuthContext } from '@/contexts/AuthContext';
import type { Capability } from '@/lib/capabilities';
import type { TwoKeyEscapeState } from '@/services/api';

/**
 * The Two-Key Escape screen against its API boundary.
 *
 * The contracts pinned here are the ones the role flow depends on: picking a
 * role option is local-only and issues no request, only the explicit confirm
 * submits; a conflict names itself and offers Change Role, which confirms the
 * alternative through the change endpoint. The board itself renders whatever
 * authoritative state the server reports.
 */

vi.mock('@/services/api', () => ({
  getTwoKeyEscapeState: vi.fn(),
  getTwoKeyEscapePartners: vi.fn(),
  selectTwoKeyEscapePartner: vi.fn(),
  submitTwoKeyEscapeRole: vi.fn(),
  changeTwoKeyEscapeRole: vi.fn(),
  moveTwoKeyEscape: vi.fn(),
  spendTwoKeyEscapeKey: vi.fn(),
  getTwoKeyEscapeLeaderboard: vi.fn(),
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
  getTwoKeyEscapeState,
  getTwoKeyEscapePartners,
  selectTwoKeyEscapePartner,
  spendTwoKeyEscapeKey,
  submitTwoKeyEscapeRole,
  changeTwoKeyEscapeRole,
  getTwoKeyEscapeLeaderboard,
} = await import('@/services/api');

type AuthContextValue = ContextType<typeof AuthContext>;

const authValue = (): AuthContextValue =>
  ({
    user: {
      id: 'player-a',
      name: 'Alex',
      regno: 'TEST001',
      email: 'test@example.com',
      gender: 'male',
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

const selectingState = (overrides: Partial<TwoKeyEscapeState> = {}): TwoKeyEscapeState => ({
  stage: 'selecting_roles',
  game_id: 'game-1',
  session_id: null,
  my_selection: null,
  partner: { id: 'player-b', name: 'Sam' },
  my_role: null,
  my_submitted_role: null,
  partner_submitted_role: null,
  roles_finalized: false,
  conflict: null,
  grid_size: 7,
  key_charges: 7,
  player_row: 0,
  player_col: 0,
  exit_row: 6,
  exit_col: 6,
  walls: [],
  locked_cells: [[1, 1]],
  blocked_cells: [],
  keys: [{ key_index: 0, target_row: 1, target_col: 1, used: false }],
  keys_remaining: 7,
  cooldown_seconds_remaining: 0,
  movement_required: false,
  moves_since_last_key: 0,
  total_moves: 0,
  total_keys: 0,
  state_version: 0,
  result: null,
  ...overrides,
});

const renderPage = () =>
  render(
    <AuthContext.Provider value={authValue()}>
      <MemoryRouter>
        <TwoKeyEscapePage />
      </MemoryRouter>
    </AuthContext.Provider>,
  );

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getTwoKeyEscapeLeaderboard).mockResolvedValue([]);
});

describe('role selection stays local until confirmed', () => {
  it('clicking a role option issues no HTTP request', async () => {
    vi.mocked(getTwoKeyEscapeState).mockResolvedValue(selectingState());

    renderPage();
    await screen.findByText('Choose your role');

    fireEvent.click(screen.getByRole('button', { name: /Navigator/ }));
    expect(submitTwoKeyEscapeRole).not.toHaveBeenCalled();
    expect(changeTwoKeyEscapeRole).not.toHaveBeenCalled();
  });

  it('confirming submits the locally picked role', async () => {
    vi.mocked(getTwoKeyEscapeState).mockResolvedValue(selectingState());
    vi.mocked(submitTwoKeyEscapeRole).mockResolvedValue(
      selectingState({ my_submitted_role: 'navigator' }),
    );

    renderPage();
    await screen.findByText('Choose your role');

    fireEvent.click(screen.getByRole('button', { name: /Navigator/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm role' }));

    await waitFor(() =>
      expect(submitTwoKeyEscapeRole).toHaveBeenCalledTimes(1),
    );
    expect(submitTwoKeyEscapeRole).toHaveBeenCalledWith('navigator');
  });
});

describe('role conflict resolution', () => {
  const conflicted = () =>
    selectingState({
      session_id: 'session-1',
      my_submitted_role: 'navigator',
      partner_submitted_role: 'navigator',
      conflict:
        'Both players selected Navigator. One player must change their role before the game can begin.',
    });

  it('shows the conflict and keeps both selections visible', async () => {
    vi.mocked(getTwoKeyEscapeState).mockResolvedValue(conflicted());

    renderPage();
    await screen.findByRole('alert');

    expect(
      screen.getByText(/Both players selected Navigator/),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Change Role' })).toBeInTheDocument();
    // Both confirmed choices stay on screen so the problem is understood.
    expect(screen.getByText(/You confirmed:/)).toBeInTheDocument();
  });

  it('change role confirms the alternative through the change endpoint', async () => {
    vi.mocked(getTwoKeyEscapeState).mockResolvedValue(conflicted());
    vi.mocked(changeTwoKeyEscapeRole).mockResolvedValue(
      conflicted(),
    );

    renderPage();
    await screen.findByRole('alert');

    fireEvent.click(screen.getByRole('button', { name: 'Change Role' }));
    const confirm = await screen.findByRole('button', { name: /Confirm Key Operator/ });
    fireEvent.click(confirm);

    await waitFor(() =>
      expect(changeTwoKeyEscapeRole).toHaveBeenCalledTimes(1),
    );
    expect(changeTwoKeyEscapeRole).toHaveBeenCalledWith('key_operator');
    expect(submitTwoKeyEscapeRole).not.toHaveBeenCalled();
  });
});

describe('role display', () => {
  it('names the pending submissions of both players', async () => {
    vi.mocked(getTwoKeyEscapeState).mockResolvedValue(
      selectingState({
        my_submitted_role: 'key_operator',
        partner_submitted_role: null,
      }),
    );

    renderPage();
    await screen.findByText('Choose your role');
    // Both the picked option and the pending-submission line name the role.
    expect(screen.getAllByText(/Key Operator/).length).toBeGreaterThanOrEqual(2);
  });
});

describe('locks on the maze are the keys', () => {
  const playingAs = (role: 'navigator' | 'key_operator', extra: Partial<TwoKeyEscapeState> = {}) =>
    selectingState({
      stage: 'playing',
      session_id: 'session-1',
      my_role: role,
      my_submitted_role: role,
      partner_submitted_role: role === 'navigator' ? 'key_operator' : 'navigator',
      roles_finalized: true,
      player_row: 0,
      player_col: 0,
      ...extra,
    });

  it('the operator unlocks by clicking the lock itself', async () => {
    vi.mocked(getTwoKeyEscapeState).mockResolvedValue(playingAs('key_operator'));
    vi.mocked(spendTwoKeyEscapeKey).mockResolvedValue(playingAs('key_operator'));

    renderPage();
    const lock = await screen.findByRole('button', { name: 'Unlock locked cell at row 2 column 2' });
    fireEvent.click(lock);

    await waitFor(() => expect(spendTwoKeyEscapeKey).toHaveBeenCalledTimes(1));
    expect(spendTwoKeyEscapeKey).toHaveBeenCalledWith(0);
  });

  it('names the lock that actually opened, not the one tapped', async () => {
    vi.mocked(getTwoKeyEscapeState).mockResolvedValue(playingAs('key_operator'));
    vi.mocked(spendTwoKeyEscapeKey).mockResolvedValue(
      playingAs('key_operator', { locked_cells: [[4, 4]] }),
    );

    renderPage();
    fireEvent.click(
      await screen.findByRole('button', { name: 'Unlock locked cell at row 2 column 2' }),
    );

    await waitFor(() => expect(toast.success).toHaveBeenCalled());
    expect(vi.mocked(toast.success)).toHaveBeenCalledWith(
      expect.stringMatching(/Opened row 2, column 2\./),
    );
  });

  it('locks stay disabled while cooling down', async () => {
    vi.mocked(getTwoKeyEscapeState).mockResolvedValue(
      playingAs('key_operator', { cooldown_seconds_remaining: 8 }),
    );

    renderPage();
    const lock = await screen.findByRole('button', { name: 'Unlock locked cell at row 2 column 2' });
    expect(lock).toBeDisabled();
    expect(screen.getByText(/Cooling down: 8s/)).toBeInTheDocument();
  });

  it('the navigator sees locks but no unlock buttons', async () => {
    vi.mocked(getTwoKeyEscapeState).mockResolvedValue(playingAs('navigator'));

    renderPage();
    await screen.findByRole('group', { name: 'Move' });
    expect(
      screen.queryByRole('button', { name: /Unlock locked cell/ }),
    ).not.toBeInTheDocument();
  });
});

describe('live board refresh', () => {
  const playingAs = (role: 'navigator' | 'key_operator') =>
    selectingState({
      stage: 'playing',
      session_id: 'session-1',
      my_role: role,
      my_submitted_role: role,
      partner_submitted_role: role === 'navigator' ? 'key_operator' : 'navigator',
      roles_finalized: true,
    });

  it("re-reads the Navigator's board within seconds while playing", async () => {
    vi.mocked(getTwoKeyEscapeState).mockResolvedValue(playingAs('navigator'));

    renderPage();
    await screen.findByRole('group', { name: 'Move' });
    expect(getTwoKeyEscapeState).toHaveBeenCalledTimes(1);

    // The playing-stage poll fires every ~3s, so a key activation lands on
    // the Navigator's board without any manual refresh.
    await waitFor(() => expect(getTwoKeyEscapeState).toHaveBeenCalledTimes(2), {
      timeout: 8000,
    });
  }, 15000);
});

describe('partner choice', () => {
  const noCouple = (overrides: Partial<TwoKeyEscapeState> = {}): TwoKeyEscapeState => ({
    ...selectingState({ stage: 'no_couple', partner: null }),
    ...overrides,
  });

  const sam = {
    id: 'id-b',
    name: 'Sam',
    gender: 'female',
    year: 2,
    department: 'Class',
  };

  it('offers everyone to choose from and chooses through the shared selection', async () => {
    vi.mocked(getTwoKeyEscapeState).mockResolvedValue(noCouple());
    vi.mocked(getTwoKeyEscapePartners).mockResolvedValue([sam]);
    vi.mocked(selectTwoKeyEscapePartner).mockResolvedValue(noCouple());

    renderPage();
    await screen.findByText('Choose your partner');
    expect(getTwoKeyEscapePartners).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'Choose Sam as partner' }));

    await waitFor(() =>
      expect(selectTwoKeyEscapePartner).toHaveBeenCalledTimes(1),
    );
    expect(selectTwoKeyEscapePartner).toHaveBeenCalledWith('id-b');
  });

  it('shows the pending pick shared with the Questionnaire', async () => {
    vi.mocked(getTwoKeyEscapeState).mockResolvedValue(
      noCouple({
        my_selection: { selected_user_id: 'id-b', selected_user_name: 'Sam' },
      }),
    );
    vi.mocked(getTwoKeyEscapePartners).mockResolvedValue([sam]);

    renderPage();
    await screen.findByText('Choose your partner');
    expect(screen.getByText(/Current pick:/)).toBeInTheDocument();
    expect(screen.getAllByText('Sam').length).toBeGreaterThanOrEqual(1);
  });
});
