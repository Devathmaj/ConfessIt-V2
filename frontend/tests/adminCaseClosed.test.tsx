import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import { AdminCaseClosedManage } from '@/pages/AdminCaseClosedManage';
import type {
  AiGenerationState,
  AiSessionHistoryEntry,
  CaseClosedSessionOverview,
  GameConfig,
} from '@/services/api';

/**
 * The Case Closed admin console against its API boundary.
 *
 * Cases publish automatically now, so the console only configures the
 * difficulty (frequency rides the shared status controls), inspects the
 * published scenario -- opening, suspects, leads, solution -- surfaces the
 * generation status or last error on an empty screen, revokes the live event
 * behind a confirmation, and pages history through the backend. The old
 * blueprint -> approve -> package -> publish workflow is unreachable here.
 */

vi.mock('@/services/api', () => ({
  getGameConfigs: vi.fn(),
  updateGameConfig: vi.fn(),
  getCaseClosedSessionOverview: vi.fn(),
  getCaseClosedSessionHistory: vi.fn(),
  revokeCaseClosedSession: vi.fn(),
}));

vi.mock('@/components/AdminGameStatusControls', () => ({
  AdminGameStatusControls: () => <div />,
}));
vi.mock('@/components/AdminSetFeaturedCard', () => ({
  AdminSetFeaturedCard: () => <div />,
}));
vi.mock('sonner', () => ({
  toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }),
  Toaster: () => null,
}));

const api = await import('@/services/api');
const {
  getGameConfigs,
  getCaseClosedSessionOverview,
  getCaseClosedSessionHistory,
  revokeCaseClosedSession,
  updateGameConfig,
} = api;

const gameConfig = (overrides: Partial<GameConfig> = {}): GameConfig => ({
  game_id: 'case-closed',
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
  game_id: 'case-closed',
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

const publishedOverview = (): CaseClosedSessionOverview => ({
  game_id: 'case-closed',
  session: {
    id: 'game-1',
    status: 'active',
    period_start_date: '2026-10-19',
    period_end_date: '2026-10-20',
    package_id: 'pkg-1',
    title: 'The Missing Mascot',
    package: {
      title: 'The Missing Mascot',
      opening_narrative: 'A mascot costume vanished from a locked auditorium.',
      suspects: [
        { id: 's1', name: 'Alex', background: 'Stagehand', relationship: 'Crew' },
        { id: 's2', name: 'Blake', background: 'Actor', relationship: 'Cast' },
      ],
      evidence: [
        { id: 'e1', title: 'Entry', description: 'Entry at 6:18', kind: 'establishes_fact' },
      ],
      lead_nodes: [
        {
          id: 'lead-a',
          title: 'Security footage',
          description: 'Who entered?',
          discovery_text: 'Entry at 6:18pm.',
          next_lead_ids: ['lead-d'],
        },
      ],
      solution: { culprit_id: 's1', motive: 'Prank to delay the show' },
      solution_reveal: {
        title: 'Alex did it',
        narrative: 'Footage plus key log prove Alex.',
        why_others_ruled_out: 'Blake was on stage.',
      },
      time_scoring_config: { half_life_seconds: 43200, max_points: 100 },
    },
    created_by: 'scheduler',
    started_at: '2026-10-19T00:02:00Z',
    ended_at: null,
    created_at: '2026-10-19T00:02:00Z',
    finalized: false,
    finalized_at: null,
  },
  generation: generation(),
});

const emptyOverview = (
  generationState: AiGenerationState | null = null,
): CaseClosedSessionOverview => ({
  game_id: 'case-closed',
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
  title: `Case ${id}`,
  revoked: false,
  created_by: 'scheduler',
  started_at: '2026-10-18T00:02:00Z',
  ended_at: '2026-10-19T00:01:00Z',
  created_at: '2026-10-18T00:02:00Z',
  finalized: true,
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
    <MemoryRouter>
      <AdminCaseClosedManage />
    </MemoryRouter>,
  );

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getGameConfigs).mockResolvedValue([gameConfig()]);
  vi.mocked(getCaseClosedSessionOverview).mockResolvedValue(emptyOverview());
  vi.mocked(getCaseClosedSessionHistory).mockResolvedValue(historyPage([], 0));
  vi.mocked(updateGameConfig).mockResolvedValue(gameConfig());
  vi.mocked(revokeCaseClosedSession).mockResolvedValue({
    game_id: 'case-closed',
    action: 'revoked',
    session_id: 'game-1',
    revoked_until: '2026-10-20',
  });
});

describe('Admin Case Closed: configuration only', () => {
  it('keeps the difficulty picker and drops the generation workflow', async () => {
    renderConsole();

    expect(await screen.findByText('Difficulty')).toBeInTheDocument();
    expect(document.getElementById('case-closed-difficulty')).not.toBeNull();

    for (const label of [
      /new draft/i,
      /generate$/i,
      /approve/i,
      /package/i,
      /publish/i,
      /blueprint/i,
    ]) {
      expect(screen.queryByRole('button', { name: label })).not.toBeInTheDocument();
    }
  });

  it('saves a new difficulty through the game-config endpoint', async () => {
    renderConsole();

    const picker = document.getElementById('case-closed-difficulty') as HTMLElement;
    await waitFor(() => expect(picker).toHaveTextContent(/medium/i));
    fireEvent.pointerDown(picker, { button: 0, ctrlKey: false, pointerType: 'mouse' });
    fireEvent.click(await screen.findByRole('option', { name: 'Easy' }));
    fireEvent.click(await screen.findByRole('button', { name: /^save$/i }));

    await waitFor(() =>
      expect(updateGameConfig).toHaveBeenCalledWith('case-closed', { difficulty: 'easy' }),
    );
  });
});

describe('Admin Case Closed: the current published case', () => {
  it('renders the opening, suspects, leads and outcome', async () => {
    vi.mocked(getCaseClosedSessionOverview).mockResolvedValue(publishedOverview());
    renderConsole();

    expect(
      await screen.findByText('A mascot costume vanished from a locked auditorium.'),
    ).toBeInTheDocument();
    expect(screen.getByText(/suspects \(2\)/i)).toBeInTheDocument();
    expect(screen.getByText(/leads and choices \(1\)/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /inspect lead lead-a/i }));
    expect(screen.getByText('Entry at 6:18pm.')).toBeInTheDocument();

    // The solution is inspectable, and labelled as not yet revealed while the
    // event is still in progress.
    expect(screen.getByText(/not yet revealed to players/i)).toBeInTheDocument();
    expect(screen.getByText(/prank to delay the show/i)).toBeInTheDocument();
  });

  it('labels the solution as the case result once the event is finalized', async () => {
    const overview = publishedOverview();
    overview.session = { ...overview.session!, finalized: true };
    vi.mocked(getCaseClosedSessionOverview).mockResolvedValue(overview);
    renderConsole();

    expect(await screen.findByText(/case result/i)).toBeInTheDocument();
  });

  it('shows an empty state with the last generation error', async () => {
    vi.mocked(getCaseClosedSessionOverview).mockResolvedValue(
      emptyOverview(
        generation({
          status: 'failed',
          error_code: 'attempts_exhausted',
          last_error: 'The provider timed out three times.',
        }),
      ),
    );
    renderConsole();

    expect(await screen.findByText(/no case is active right now/i)).toBeInTheDocument();
    expect(screen.getByText(/timed out three times/i)).toBeInTheDocument();
  });
});

describe('Admin Case Closed: revoke and history', () => {
  it('revokes only after the confirmation click', async () => {
    let overview: CaseClosedSessionOverview = publishedOverview();
    vi.mocked(getCaseClosedSessionOverview).mockImplementation(async () => overview);
    renderConsole();

    // Wait for the live session so the revoke control is enabled.
    await screen.findByText('A mascot costume vanished from a locked auditorium.');
    fireEvent.click(screen.getByRole('button', { name: /revoke session/i }));
    expect(revokeCaseClosedSession).not.toHaveBeenCalled();

    overview = emptyOverview(generation({ status: 'cancelled' }));
    fireEvent.click(screen.getByRole('button', { name: /confirm revoke/i }));
    await waitFor(() => expect(revokeCaseClosedSession).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/no case is active right now/i)).toBeInTheDocument();
  });

  it('pages history through the backend and flags revoked sessions', async () => {
    const first = Array.from({ length: 10 }, (_, index) =>
      historyEntry(`hist-${index}`, { revoked: index === 0 }),
    );
    const second = [historyEntry('hist-10'), historyEntry('hist-11')];
    vi.mocked(getCaseClosedSessionHistory).mockImplementation(async (params) =>
      params?.skip ? historyPage(second, 12, params.skip ?? 0, 10) : historyPage(first, 12),
    );
    renderConsole();

    fireEvent.click(await screen.findByRole('button', { name: /show session history/i }));
    expect(screen.getByText('Case hist-0')).toBeInTheDocument();
    expect(screen.getByText('revoked')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /show more sessions/i }));
    await waitFor(() =>
      expect(getCaseClosedSessionHistory).toHaveBeenLastCalledWith({ skip: 10, limit: 10 }),
    );
    expect(await screen.findByText('Case hist-11')).toBeInTheDocument();
  });
});
