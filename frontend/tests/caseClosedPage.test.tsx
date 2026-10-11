import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { ContextType } from 'react';

import { CaseClosedGamePage } from '@/pages/CaseClosedGamePage';
import { AuthContext } from '@/contexts/AuthContext';
import type { CaseClosedState } from '@/services/api';

vi.mock('@/services/api', () => ({
  getCaseClosedState: vi.fn(),
  getCaseClosedPartners: vi.fn(),
  getPartnerCohorts: vi.fn(),
  selectCaseClosedPartner: vi.fn(),
  startCaseClosedEvent: vi.fn(),
  investigateCaseClosedLead: vi.fn(),
  proposeCaseClosedAccusation: vi.fn(),
  confirmCaseClosedAccusation: vi.fn(),
  getCaseClosedResult: vi.fn(),
  getCaseClosedMessages: vi.fn(),
  sendCaseClosedMessage: vi.fn(),
  reportCaseClosedMessage: vi.fn(),
}));

vi.mock('@/lib/caseClosedMessaging', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/caseClosedMessaging')>();
  return {
    ...actual,
    caseClosedMessagingClient: { connect: vi.fn(), onMessage: vi.fn(() => () => {}), onError: vi.fn(() => () => {}), disconnect: vi.fn() },
  };
});

vi.mock('sonner', () => ({
  toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }),
  Toaster: () => null,
}));

const api = await import('@/services/api');
const getState = api.getCaseClosedState as ReturnType<typeof vi.fn>;
const investigate = api.investigateCaseClosedLead as ReturnType<typeof vi.fn>;
const propose = api.proposeCaseClosedAccusation as ReturnType<typeof vi.fn>;
const confirmFn = api.confirmCaseClosedAccusation as ReturnType<typeof vi.fn>;
const getResult = api.getCaseClosedResult as ReturnType<typeof vi.fn>;
const getMessages = api.getCaseClosedMessages as ReturnType<typeof vi.fn>;
const sendChat = api.sendCaseClosedMessage as ReturnType<typeof vi.fn>;
const reportCC = api.reportCaseClosedMessage as ReturnType<typeof vi.fn>;

type AuthValue = ContextType<typeof AuthContext>;
const authValue = (): AuthValue =>
  ({ user: { id: 'a', name: 'Alice', regno: 'T1', email: 'a@x.com', gender: 'female' }, isAuthenticated: true, isLoading: false }) as unknown as AuthValue;

const baseState = (over: Partial<CaseClosedState> = {}): CaseClosedState => ({
  stage: 'investigating',
  game_id: 'g1', session_id: 's1',
  case_title: 'The Missing Mascot', opening_narrative: 'A costume vanished.',
  suspects: [
    { id: 's1', name: 'Alex', background: 'Stagehand', relationship: 'Crew' },
    { id: 's2', name: 'Blake', background: 'Actor', relationship: 'Cast' },
  ],
  available_leads: [{ id: 'lead-a', title: 'Footage', description: 'Cameras', investigated: false, available: true, depth: 0 }],
  my_path: [], investigated_lead_ids: [], evidence_board: [],
  my_remaining: 5, partner_remaining: 5,
  accusation: { my_proposal: null, partner_proposal: null, my_confirmed: null, partner_confirmed: null, locked_suspect_id: null, locked_at: null },
  timer: { started: false, first_investigation_at: null, locked_at: null, elapsed_seconds: null },
  session_status: 'investigating', event_deadline: null, state_version: 0, finalized: false,
  ...over,
});

const renderPage = () =>
  render(<AuthContext.Provider value={authValue()}><MemoryRouter><CaseClosedGamePage /></MemoryRouter></AuthContext.Provider>);

describe('CaseClosedGamePage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getMessages.mockResolvedValue({
      game_id: 'g1', session_id: 's1', messages: [], count: 0, total: 0,
      quota: { conversation_limit: 400, participant_limit: 200, conversation_count: 0, participant_count: 0, conversation_remaining: 400, participant_remaining: 200, can_send: true, limit_reached: null },
    });
  });

  it('pairs through the shared server-side picker before the case opens', async () => {
    getState.mockResolvedValue(baseState({ stage: 'selecting_partner', session_id: null }));
    (api.getCaseClosedPartners as ReturnType<typeof vi.fn>).mockResolvedValue({
      partners: [{ id: 'id-b', name: 'Sam', gender: 'female', year: 2, department: 'Class' }],
      total: 1,
      skip: 0,
      limit: 20,
    });
    (api.getPartnerCohorts as ReturnType<typeof vi.fn>).mockResolvedValue([]);
    (api.selectCaseClosedPartner as ReturnType<typeof vi.fn>).mockResolvedValue(baseState());
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: 'Choose Sam as partner' }));

    await waitFor(() => expect(api.selectCaseClosedPartner).toHaveBeenCalledWith('id-b'));
    expect(await screen.findByText('The Missing Mascot')).toBeTruthy();
  });

  it('shows leads, budgets, evidence board and timer placeholder', async () => {
    getState.mockResolvedValue(baseState());
    renderPage();
    expect(await screen.findByText('The Missing Mascot')).toBeTruthy();
    expect(screen.getByText(/You: 5 left/)).toBeTruthy();
    expect(screen.getByText('Footage')).toBeTruthy();
    expect(screen.getByText(/Timer starts on first investigation/)).toBeTruthy();
  });

  it('investigates a lead and shows the discovery + evidence', async () => {
    const after = baseState({
      my_path: ['lead-a'], investigated_lead_ids: ['lead-a'],
      evidence_board: [{ id: 'e1', title: 'Entry', description: 'At 6:18', discovered_at: new Date().toISOString(), discovered_by: 'a', source_lead_id: 'lead-a', suspect_ids: [], kind: 'establishes_fact' }],
      my_remaining: 4, state_version: 1,
      timer: { started: true, first_investigation_at: new Date().toISOString(), locked_at: null, elapsed_seconds: 12 },
    });
    getState.mockResolvedValue(baseState());
    investigate.mockResolvedValue({ state: after, lead_id: 'lead-a', discovery_text: 'Entry at 6:18', already_known: false, replay: false });
    renderPage();
    fireEvent.click(await screen.findByText('Investigate'));
    await waitFor(() => expect(investigate).toHaveBeenCalledWith('lead-a', 0));
    expect(await screen.findByText('Entry')).toBeTruthy();
  });

  it('proposes then confirms with an irreversible warning', async () => {
    getState.mockResolvedValue(baseState());
    propose.mockResolvedValue(baseState({ accusation: { my_proposal: 's1', partner_proposal: null, my_confirmed: null, partner_confirmed: null, locked_suspect_id: null, locked_at: null } }));
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    confirmFn.mockResolvedValue(baseState({ stage: 'accusation_locked', accusation: { my_proposal: 's1', partner_proposal: 's1', my_confirmed: 's1', partner_confirmed: 's1', locked_suspect_id: 's1', locked_at: new Date().toISOString() }, timer: { started: true, first_investigation_at: new Date().toISOString(), locked_at: new Date().toISOString(), elapsed_seconds: 60 } }));
    getResult.mockResolvedValue({ locked_suspect_id: 's1', correct: null, score: null, elapsed_seconds: 60, finalized: false, culprit_id: null, solution_title: null, solution_narrative: null, why_others_ruled_out: null });
    renderPage();
    const alexButtons = await screen.findAllByText('Alex');
    const alexBtn = alexButtons.find((el) => el.tagName === 'BUTTON') ?? alexButtons[0];
    fireEvent.click(alexBtn);
    fireEvent.click(screen.getByText('Propose'));
    await waitFor(() => expect(propose).toHaveBeenCalledWith('s1'));
    fireEvent.click(screen.getByText(/Confirm \(irreversible\)/));
    await waitFor(() => expect(confirmSpy).toHaveBeenCalled());
    await waitFor(() => expect(confirmFn).toHaveBeenCalledWith('s1'));
    confirmSpy.mockRestore();
  });

  it('hides correctness until finalization and reveals after', async () => {
    getState.mockResolvedValue(baseState({ stage: 'accusation_locked', accusation: { my_proposal: 's1', partner_proposal: 's1', my_confirmed: 's1', partner_confirmed: 's1', locked_suspect_id: 's1', locked_at: new Date().toISOString() }, timer: { started: true, first_investigation_at: new Date().toISOString(), locked_at: new Date().toISOString(), elapsed_seconds: 60 } }));
    getResult.mockResolvedValue({ locked_suspect_id: 's1', correct: null, score: null, elapsed_seconds: 60, finalized: false, culprit_id: null, solution_title: null, solution_narrative: null, why_others_ruled_out: null });
    renderPage();
    await waitFor(() => expect(getResult).toHaveBeenCalled());
    expect(screen.queryByText(/Solution reveal/)).toBeNull();
  });

  it('reveals the culprit and the accusation by name once the event finalizes', async () => {
    getState.mockResolvedValue(baseState({ stage: 'completed', finalized: true }));
    getResult.mockResolvedValue({
      locked_suspect_id: 's2', correct: false, score: 0, elapsed_seconds: 60,
      finalized: true, culprit_id: 's1', solution_title: 'Alex did it',
      solution_narrative: 'The key log proves it.', why_others_ruled_out: 'Blake was on stage.',
    });
    renderPage();

    expect(await screen.findByText(/the culprit was Alex/i)).toBeTruthy();
    expect(screen.getByText(/alex did it/i)).toBeTruthy();
    expect(screen.getByText(/your accusation: Blake/i)).toBeTruthy();
    expect(screen.getByText(/incorrect · 0 pts/i)).toBeTruthy();
  });

  it('embeds the detective thread with 400/200 quotas inside the game window', async () => {
    getState.mockResolvedValue(baseState());
    getMessages.mockResolvedValue({
      game_id: 'g1', session_id: 's1',
      messages: [{ id: 'm1', game_id: 'g1', session_id: 's1', sender_user_id: 'b', body: 'Check the key log', created_at: new Date().toISOString(), is_read: true }],
      count: 1, total: 1,
      quota: { conversation_limit: 400, participant_limit: 200, conversation_count: 1, participant_count: 0, conversation_remaining: 399, participant_remaining: 200, can_send: true, limit_reached: null },
    });
    renderPage();
    expect(await screen.findByText('Detective thread')).toBeTruthy();
    expect(await screen.findByText('Check the key log')).toBeTruthy();
    expect(screen.getByText('0/200 sent')).toBeTruthy();
  });

  it('sends a detective-thread message without touching the investigation budget', async () => {
    getState.mockResolvedValue(baseState());
    sendChat.mockResolvedValue({
      id: 'm2', game_id: 'g1', session_id: 's1', sender_user_id: 'a', body: 'On it',
      created_at: new Date().toISOString(), is_read: false,
      quota: { conversation_limit: 400, participant_limit: 200, conversation_count: 2, participant_count: 1, conversation_remaining: 398, participant_remaining: 199, can_send: true, limit_reached: null },
    });
    renderPage();
    await screen.findByText('Detective thread');
    fireEvent.change(await screen.findByPlaceholderText(/Discuss leads/), { target: { value: 'On it' } });
    fireEvent.click(screen.getByText('Send'));
    await waitFor(() => expect(sendChat).toHaveBeenCalledWith('On it'));
    expect(await screen.findByText('On it')).toBeTruthy();
    expect(investigate).not.toHaveBeenCalled();
  });

  it('shows the send button busy until the server accepts the turn', async () => {
    getState.mockResolvedValue(baseState());
    let resolveSend!: (value: unknown) => void;
    sendChat.mockImplementation(() => new Promise((resolve) => { resolveSend = resolve; }));
    renderPage();
    await screen.findByText('Detective thread');
    fireEvent.change(await screen.findByPlaceholderText(/Discuss leads/), { target: { value: 'On it' } });

    fireEvent.click(screen.getByRole('button', { name: /^send$/i }));

    // Busy while moderation runs: the label is replaced, and a second click is swallowed.
    const busy = await screen.findByRole('button', { name: /sending/i });
    expect(busy).toBeDisabled();
    fireEvent.click(busy);
    expect(sendChat).toHaveBeenCalledTimes(1);

    resolveSend({
      id: 'm2', game_id: 'g1', session_id: 's1', sender_user_id: 'a', body: 'On it',
      created_at: new Date().toISOString(), is_read: false,
      quota: { conversation_limit: 400, participant_limit: 200, conversation_count: 2, participant_count: 1, conversation_remaining: 398, participant_remaining: 199, can_send: true, limit_reached: null },
    });
    await screen.findByText('On it');
    // Once the turn lands the composer is free again (the empty draft keeps the button
    // disabled on its own, so a fresh draft is what shows the busy state has cleared).
    fireEvent.change(screen.getByPlaceholderText(/Discuss leads/), { target: { value: 'Next lead' } });
    expect(screen.getByRole('button', { name: /^send$/i })).toBeEnabled();
  });

  it('offers a report action on a received turn and files it through the Case Closed endpoint', async () => {
    getState.mockResolvedValue(baseState());
    reportCC.mockResolvedValue({ id: 'r1' });
    getMessages.mockResolvedValue({
      game_id: 'g1', session_id: 's1',
      messages: [{ id: 'm1', game_id: 'g1', session_id: 's1', sender_user_id: 'b', body: 'That alibi is a lie', created_at: new Date().toISOString(), is_read: true }],
      count: 1, total: 1,
      quota: { conversation_limit: 400, participant_limit: 200, conversation_count: 1, participant_count: 0, conversation_remaining: 399, participant_remaining: 200, can_send: true, limit_reached: null },
    });
    renderPage();
    await screen.findByText('That alibi is a lie');

    // Only a turn the viewer received can be reported; their own turns carry no such action.
    fireEvent.click(screen.getByRole('button', { name: /report this message/i }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getAllByRole('radio')[0]);
    fireEvent.click(within(dialog).getByRole('button', { name: /report message/i }));

    await waitFor(() => expect(reportCC).toHaveBeenCalledWith('m1', 'harassment'));
  });
});
