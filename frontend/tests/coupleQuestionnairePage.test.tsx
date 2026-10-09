import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import type { ContextType } from 'react';
import { toast } from 'sonner';

import { CoupleQuestionnairePage } from '@/pages/CoupleQuestionnairePage';
import { AuthContext } from '@/contexts/AuthContext';
import type { Capability } from '@/lib/capabilities';
import type {
  CoupleCurrentState,
  CouplePartnerCandidate,
  CouplePartnersPage,
  CouplePredictionView,
  CoupleQuestionView,
} from '@/services/api';

/**
 * The Couple Questionnaire screen against its API boundary.
 *
 * The contract pinned here is the one the player experiences: the screen reads
 * its state on entry and after every write, and renders whichever phase the
 * server reports. Choosing a partner is a pending pick that a re-pick
 * replaces; answering and predicting submit one option per question and lock
 * the answered row; the settled score arrives only with the completed stage.
 * Discovery is server-side: the picker asks the server for one page at a time
 * (the gender mode, the search word and the year-and-division cohort all ride
 * the query), and "load more" appends the next page beneath the rows at hand.
 */

vi.mock('@/services/api', () => ({
  getCoupleState: vi.fn(),
  getCouplePartners: vi.fn(),
  getPartnerCohorts: vi.fn(),
  selectCouplePartner: vi.fn(),
  submitCoupleAnswer: vi.fn(),
  submitCouplePrediction: vi.fn(),
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
  getCoupleState,
  getCouplePartners,
  getPartnerCohorts,
  selectCouplePartner,
  submitCoupleAnswer,
  submitCouplePrediction,
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

/** One page of the partner endpoint: the rows plus the matching-row count. */
const partnerPage = (
  partners: CouplePartnerCandidate[],
  total: number = partners.length,
  skip = 0,
): CouplePartnersPage => ({ partners, total, skip, limit: 20 });

const question = (
  overrides: Partial<CoupleQuestionView> = {},
): CoupleQuestionView => ({
  session_question_id: 'sq-1',
  question_id: 'q-1',
  question_text: 'What would you prefer for a perfect date?',
  options: ['Dinner & movie', 'Long drive', 'Beach or outdoor trip', 'Staying in together'],
  shared: false,
  selected_option_index: null,
  ...overrides,
});

const prediction = (
  overrides: Partial<CouplePredictionView> = {},
): CouplePredictionView => ({
  session_question_id: 'sq-9',
  question_id: 'q-9',
  question_text: 'What would Sam prefer for a perfect date?',
  options: ['Dinner & movie', 'Long drive', 'Beach or outdoor trip', 'Staying in together'],
  shared: false,
  predicted_option_index: null,
  ...overrides,
});

const baseState = (overrides: Partial<CoupleCurrentState> = {}): CoupleCurrentState => ({
  stage: 'selecting_partner',
  game_id: 'game-1',
  game_date: '2026-10-08',
  my_selection: null,
  session_id: null,
  partner: null,
  my_answers_submitted: 0,
  my_answers_total: 15,
  partner_answers_submitted: 0,
  partner_answers_total: 15,
  my_predictions_submitted: 0,
  my_predictions_total: 15,
  waiting_for: null,
  questions: [],
  predictions: [],
  result: null,
  ...overrides,
});

const renderGame = (gender: 'male' | 'female' | 'other' = 'male') =>
  render(
    <AuthContext.Provider value={authValue(gender)}>
      <MemoryRouter initialEntries={['/games/couple-questionnaire']}>
        <Routes>
          <Route path="/games/couple-questionnaire" element={<CoupleQuestionnairePage />} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>,
  );

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getCoupleState).mockResolvedValue(baseState());
  vi.mocked(getCouplePartners).mockResolvedValue(partnerPage([]));
  vi.mocked(getPartnerCohorts).mockResolvedValue([]);
});

describe('partner selection', () => {
  /**
   * A gender-aware answer, as the server would give it: the default mode
   * hides the caller's own gender, the same-gender mode is only it.
   */
  const genderAnswer = (same: boolean) =>
    same
      ? partnerPage([candidate({ id: 'id-d', name: 'Max', gender: 'male' })])
      : partnerPage([
          candidate({ id: 'id-b', name: 'Sam', gender: 'female' }),
          candidate({ id: 'id-c', name: 'Jo', gender: 'other' }),
        ]);

  it('asks the server for everyone but the caller’s own gender by default', async () => {
    vi.mocked(getCouplePartners).mockImplementation(async (query) =>
      genderAnswer(query?.genderMode === 'same'),
    );
    renderGame('male');

    expect(await screen.findByText('Sam')).toBeInTheDocument();
    expect(screen.getByText('Jo')).toBeInTheDocument();
    expect(screen.queryByText('Max')).not.toBeInTheDocument();
    expect(getCouplePartners).toHaveBeenCalledWith(
      expect.objectContaining({ genderMode: 'different', skip: 0, limit: 20 }),
    );
  });

  it('switches to the same gender on request, in SQL', async () => {
    vi.mocked(getCouplePartners).mockImplementation(async (query) =>
      genderAnswer(query?.genderMode === 'same'),
    );
    renderGame('male');
    await screen.findByText('Sam');

    fireEvent.click(screen.getByRole('button', { name: /show same gender/i }));

    expect(await screen.findByText('Max')).toBeInTheDocument();
    expect(screen.queryByText('Sam')).not.toBeInTheDocument();
    expect(getCouplePartners).toHaveBeenLastCalledWith(
      expect.objectContaining({ genderMode: 'same' }),
    );
  });

  it('runs the typed search in SQL only when it is submitted', async () => {
    vi.mocked(getCouplePartners).mockResolvedValue(partnerPage([candidate()]));
    renderGame('male');
    await screen.findByText('Sam');

    // Typing alone issues no request: the box is a draft until submitted.
    fireEvent.change(screen.getByLabelText('Find someone'), { target: { value: 'sa' } });
    expect(getCouplePartners).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'Search' }));

    await waitFor(() =>
      expect(getCouplePartners).toHaveBeenLastCalledWith(
        expect.objectContaining({ q: 'sa', skip: 0 }),
      ),
    );
  });

  it('filters by the single year-and-division cohort in SQL', async () => {
    vi.mocked(getPartnerCohorts).mockResolvedValue([{ year: 2, department: 'CSE' }]);
    vi.mocked(getCouplePartners).mockResolvedValue(partnerPage([candidate()]));
    renderGame('male');
    await screen.findByText('Sam');

    // A Radix trigger holds no value a change event could set: open the
    // combobox and click the portalled option, as the reader would.
    fireEvent.pointerDown(screen.getByRole('combobox'), {
      button: 0,
      ctrlKey: false,
      pointerType: 'mouse',
    });
    fireEvent.click(await screen.findByRole('option', { name: 'CSE · 2' }));

    await waitFor(() =>
      expect(getCouplePartners).toHaveBeenLastCalledWith(
        expect.objectContaining({ year: 2, department: 'CSE', skip: 0 }),
      ),
    );
  });

  it('pages twenty at a time and appends the rest under the rows at hand', async () => {
    const first = Array.from({ length: 20 }, (_, index) =>
      candidate({ id: `p-${index + 1}`, name: `Person ${index + 1}`, gender: 'female' }),
    );
    const second = [candidate({ id: 'p-21', name: 'Person 21', gender: 'female' })];
    vi.mocked(getCouplePartners).mockImplementation(async (query) =>
      (query?.skip ?? 0) >= 20 ? partnerPage(second, 21, 20) : partnerPage(first, 21),
    );
    renderGame('male');

    expect(await screen.findByText('Person 1')).toBeInTheDocument();
    expect(screen.queryByText('Person 21')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Load more people' }));

    expect(await screen.findByText('Person 21')).toBeInTheDocument();
    expect(screen.getByText('Person 1')).toBeInTheDocument();
    expect(getCouplePartners).toHaveBeenLastCalledWith(
      expect.objectContaining({ skip: 20, limit: 20 }),
    );
  });

  it('saves the pick and advances on the response’s state', async () => {
    vi.mocked(getCouplePartners).mockResolvedValue(partnerPage([candidate()]));
    const answering = baseState({
      stage: 'answering',
      session_id: 's-1',
      partner: { id: 'id-b', name: 'Sam' },
      my_selection: { selected_user_id: 'id-b', selected_user_name: 'Sam' },
      questions: [question()],
    });
    vi.mocked(selectCouplePartner).mockResolvedValue(answering);
    renderGame('male');

    fireEvent.click(await screen.findByRole('button', { name: /choose sam as partner/i }));

    await waitFor(() => expect(selectCouplePartner).toHaveBeenCalledWith('id-b'));
    expect(await screen.findByText('What would you prefer for a perfect date?')).toBeInTheDocument();
    expect(toast.success).toHaveBeenCalledWith(
      'Choice saved. The game starts when they choose you back.',
    );
  });

  it('names the current pick so a re-pick replaces it', async () => {
    vi.mocked(getCoupleState).mockResolvedValue(
      baseState({ my_selection: { selected_user_id: 'id-b', selected_user_name: 'Sam' } }),
    );
    vi.mocked(getCouplePartners).mockResolvedValue(partnerPage([candidate()]));
    renderGame('male');

    expect(await screen.findByText('Sam')).toBeInTheDocument();
    // The pick banner rides a separate read from the candidate list: await it on its
    // own rather than assuming both commits landed together.
    expect(await screen.findByText('Your pick')).toBeInTheDocument();
  });
});

describe('answering', () => {
  it('submits one option per question and locks the answered row', async () => {
    const answered = baseState({
      stage: 'answering',
      session_id: 's-1',
      partner: { id: 'id-b', name: 'Sam' },
      my_answers_submitted: 1,
      questions: [question({ selected_option_index: 2 })],
    });
    vi.mocked(getCoupleState).mockResolvedValue(
      baseState({
        stage: 'answering',
        session_id: 's-1',
        partner: { id: 'id-b', name: 'Sam' },
        questions: [question()],
      }),
    );
    vi.mocked(submitCoupleAnswer).mockResolvedValue(answered);
    renderGame('male');

    const group = await screen.findByRole('group', {
      name: 'What would you prefer for a perfect date?',
    });
    fireEvent.click(within(group).getByRole('button', { name: 'Beach or outdoor trip' }));

    await waitFor(() =>
      expect(submitCoupleAnswer).toHaveBeenCalledWith('sq-1', 2),
    );
    await waitFor(() =>
      expect(screen.getByText('Your answers: 1 of 15')).toBeInTheDocument(),
    );
  });

  it('waits visibly once all fifteen are answered, and checks by itself', async () => {
    vi.mocked(getCoupleState).mockResolvedValue(
      baseState({
        stage: 'waiting_partner',
        waiting_for: 'answers',
        session_id: 's-1',
        partner: { id: 'id-b', name: 'Sam' },
        my_answers_submitted: 15,
        partner_answers_submitted: 8,
        questions: [],
      }),
    );
    // Fake timers before mount so the waiting-state interval is observable.
    vi.useFakeTimers();
    try {
      renderGame('male');
      await act(async () => {
        // Flush the mount fetch; the interval below is driven explicitly.
      });

      // The waiting state names the rule instead of offering a manual check.
      expect(screen.getByText(/You answered all 15/)).toBeInTheDocument();
      expect(screen.getByText(/both of you have finished/)).toBeInTheDocument();
      expect(
        screen.queryByRole('button', { name: /check for your partner/i }),
      ).not.toBeInTheDocument();

      // And the page re-reads the state on its own while waiting, so the next
      // stage opens without any action from the player.
      const calls = vi.mocked(getCoupleState).mock.calls.length;
      await act(async () => {
        vi.advanceTimersByTime(15000);
      });
      expect(vi.mocked(getCoupleState).mock.calls.length).toBeGreaterThan(calls);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('prediction and results', () => {
  it('predicts the partner’s answers by name and locks each guess', async () => {
    const after = baseState({
      stage: 'prediction',
      session_id: 's-1',
      partner: { id: 'id-b', name: 'Sam' },
      my_predictions_submitted: 1,
      predictions: [prediction({ predicted_option_index: 0 })],
    });
    vi.mocked(getCoupleState).mockResolvedValue(
      baseState({
        stage: 'prediction',
        session_id: 's-1',
        partner: { id: 'id-b', name: 'Sam' },
        predictions: [prediction()],
      }),
    );
    vi.mocked(submitCouplePrediction).mockResolvedValue(after);
    renderGame('male');

    const group = await screen.findByRole('group', {
      name: 'What would Sam prefer for a perfect date?',
    });
    fireEvent.click(within(group).getByRole('button', { name: 'Dinner & movie' }));

    await waitFor(() =>
      expect(submitCouplePrediction).toHaveBeenCalledWith('sq-9', 0),
    );
    await waitFor(() =>
      expect(screen.getByText('Your predictions: 1 of 15')).toBeInTheDocument(),
    );
  });

  it('shows both settled scores with the per-question review', async () => {
    vi.mocked(getCoupleState).mockResolvedValue(
      baseState({
        stage: 'completed',
        session_id: 's-1',
        partner: { id: 'id-b', name: 'Sam' },
        result: {
          my_score: 11,
          partner_score: 8,
          max_score: 15,
          my_correct_count: 11,
          my_prediction_count: 15,
          partner_correct_count: 8,
          partner_prediction_count: 15,
          completed_at: '2026-10-08T10:00:00Z',
          review: [
            {
              session_question_id: 'sq-9',
              question_id: 'q-9',
              question_text: 'What would Sam prefer for a perfect date?',
              options: ['Dinner & movie', 'Long drive'],
              shared: true,
              original_option_index: 1,
              predicted_option_index: 1,
              correct: true,
            },
          ],
        },
      }),
    );
    renderGame('male');

    expect(await screen.findByText('You both finished')).toBeInTheDocument();
    expect(screen.getByText('11')).toBeInTheDocument();
    expect(screen.getByText('8')).toBeInTheDocument();
    expect(screen.getByText('They answered:')).toBeInTheDocument();
  });

  it('explains a day with no featured game yet still offers the pick', async () => {
    vi.mocked(getCoupleState).mockResolvedValue(baseState({ stage: 'no_game', game_id: null, game_date: null }));
    vi.mocked(getCouplePartners).mockResolvedValue(partnerPage([candidate()]));
    renderGame('male');

    expect(await screen.findByText('No featured day yet')).toBeInTheDocument();
    // The candidate list loads independently of the banner above: await the name
    // itself instead of assuming it arrived in the same commit.
    expect(await screen.findByText('Sam')).toBeInTheDocument();
  });
});
