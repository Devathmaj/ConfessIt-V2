import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act, waitFor } from '@testing-library/react';
import { toast } from 'sonner';
import {
  MatchmakingPage,
  readDrawRefusal,
  readCohortBlock,
  describeDrawRefusal,
  COHORT_NOT_CONFIGURED,
  NO_CANDIDATES,
} from '@/pages/MatchmakingPage';

vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn() },
}));

// The page renders a header, a background animation and a modal, none of which are what is under
// test, and all of which pull in auth state, timers and portal plumbing of their own.
vi.mock('@/components/Navigation', () => ({ Navigation: () => null }));
vi.mock('@/components/FloatingHearts', () => ({ FloatingHearts: () => null }));
vi.mock('@/components/CountdownTimer', () => ({ CountdownTimer: () => null }));
vi.mock('@/components/ConversationDialog', () => ({ ConversationDialog: () => null }));

vi.mock('@/contexts/AuthContext', () => ({
  useAuth: () => ({
    user: {
      id: 'me', regno: 'CS22BT0001', name: 'Me', email: 'me@example.com',
      year: 2, department: 'CSE',
    },
  }),
}));

vi.mock('@/services/api', () => ({
  findMatch: vi.fn(),
  checkMatchmakingStatus: vi.fn(),
  requestConversation: vi.fn(),
  getCurrentConversation: vi.fn(),
}));

import { findMatch, checkMatchmakingStatus, getCurrentConversation } from '@/services/api';

/** The server's answer when a wait is running. */
const COOLDOWN_RESPONSE = {
  status: 'cooldown',
  message: 'You are on a cooldown. Please try again in 4 hours.',
  remaining_hours: 4,
  remaining_minutes: 0,
};

const NO_COOLDOWN = { status: 'eligible' };

/** The server's answer for a cohort that no slot names. */
const BLOCKED_RESPONSE = {
  status: 'cohort_not_configured',
  message:
    'Your cohort (2 · CSE) is not in any matchmaking slot. An administrator needs to add it.',
  cohort: '2 · CSE',
};

/** Nobody was available. Luck, and another attempt can turn it around. */
const PROBABILISTIC =
  'No potential matches found. Your attempt has been used. Try again later!';

/** No slot names this cohort. Permanent until an operator fixes it. */
const UNCONFIGURED =
  'Your cohort (2 · CSE) is not configured for matchmaking. Your attempt has been used.';

/**
 * The 404 the draw endpoint raises when a cohort has no slot, or when nobody is left to match.
 *
 * `code` is optional because a server may answer with only a sentence, and the page has to cope
 * with that rather than showing nothing.
 */
type Refusal = Error & {
  response: { status: number; data: { detail: string; code?: string } };
};

const refusal = (detail: string, code?: string): Refusal =>
  Object.assign(new Error('Request failed with status code 404'), {
    response: { status: 404, data: { detail, ...(code ? { code } : {}) } },
  });

const errorCalls = () => vi.mocked(toast.error).mock.calls.map(call => String(call[0]));
const infoCalls = () => vi.mocked(toast.info).mock.calls.map(call => String(call[0]));

/**
 * Drive the page through a complete refused draw: heart, confirm, five seconds of animation, then
 * the refusal. The animation is a real `setInterval` in the component, so the clock is moved rather
 * than waited on.
 */
async function refuseADraw() {
  const { container } = render(<MatchmakingPage />);

  const heart = container.querySelector('.cursor-pointer') as HTMLElement;
  await act(async () => {
    heart.click();
  });

  await act(async () => {
    screen.getByText('Proceed').click();
  });

  // The draw is only attempted once the fill animation reaches the end.
  await act(async () => {
    vi.advanceTimersByTime(6000);
  });
}

describe('reading a refused draw', () => {
  it('reads the sentence and the code the server sent', () => {
    expect(readDrawRefusal(refusal(UNCONFIGURED, COHORT_NOT_CONFIGURED))).toEqual({
      message: UNCONFIGURED,
      code: COHORT_NOT_CONFIGURED,
    });
  });

  it('accepts a refusal that carries only a sentence', () => {
    // An older server, or one that lost the handler. The page still has to be able to say
    // something, so this is a value rather than a blank card.
    expect(readDrawRefusal(refusal(PROBABILISTIC))).toEqual({
      message: PROBABILISTIC,
      code: null,
    });
  });

  it('reads nothing out of a request that never arrived', () => {
    // "No match this time" would be a claim the page cannot support: a dropped request has not
    // established that there was nobody to match.
    expect(readDrawRefusal(new Error('Network Error'))).toBeNull();
    expect(readDrawRefusal(undefined)).toBeNull();
  });

  it('reads nothing out of a refusal with no sentence in it', () => {
    const blank = Object.assign(new Error('nope'), {
      response: { status: 404, data: { detail: '   ', code: NO_CANDIDATES } },
    });

    expect(readDrawRefusal(blank)).toBeNull();
  });
});

describe('what the page says about a refused draw', () => {
  it('treats bad luck as something to retry', () => {
    const copy = describeDrawRefusal({ message: PROBABILISTIC, code: NO_CANDIDATES });

    expect(copy.headline).toBe('No match this time');
    expect(copy.guidance).toContain('four hours');
  });

  it('does not tell an unconfigured cohort to come back later', () => {
    // The mistake this whole distinction exists to prevent: promising a future draw that cannot
    // happen until somebody else does something.
    const copy = describeDrawRefusal({ message: UNCONFIGURED, code: COHORT_NOT_CONFIGURED });

    expect(copy.headline).toBe('Your cohort is not set up for matchmaking yet');
    expect(copy.guidance).toContain('will not help');
    expect(copy.guidance).not.toContain('four hours');
  });

  it('says only what it knows when the server sent no code', () => {
    // Neither branch is safe to assume here, so the copy claims nothing about whether retrying
    // would work.
    const copy = describeDrawRefusal({ message: PROBABILISTIC, code: null });

    expect(copy.headline).toBe('No match this time');
    expect(copy.guidance).not.toContain('four hours');
  });

  it('falls back rather than blanking on a code this build has never heard of', () => {
    const copy = describeDrawRefusal({ message: 'Something new.', code: 'invented_tomorrow' });

    expect(copy.headline).toBeTruthy();
    expect(copy.guidance).toBeTruthy();
  });
});

describe('reading a cohort that has no slot', () => {
  it('reads the message the server sent', () => {
    expect(readCohortBlock(BLOCKED_RESPONSE)).toEqual({ message: BLOCKED_RESPONSE.message });
  });

  it('reads nothing out of a cooldown', () => {
    // Folding this into the cooldown reader is the mistake: a four-hour wait and a missing
    // configuration are different problems, and the page says something different about each.
    expect(readCohortBlock(COOLDOWN_RESPONSE)).toBeNull();
    expect(readCohortBlock(NO_COOLDOWN)).toBeNull();
    expect(readCohortBlock(null)).toBeNull();
  });

  it('has something to say even without a message', () => {
    expect(readCohortBlock({ status: 'cohort_not_configured' })?.message).toBeTruthy();
  });
});

describe('a cohort that has no slot', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(checkMatchmakingStatus).mockResolvedValue(BLOCKED_RESPONSE);
    vi.mocked(getCurrentConversation).mockRejectedValue(new Error('no conversation'));
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  const clickHeart = async () => {
    const { container } = render(<MatchmakingPage />);
    await act(async () => {
      (container.querySelector('.cursor-pointer') as HTMLElement).click();
    });
  };

  it('is refused at the click, without the animation', async () => {
    // The point of the change. The user used to confirm, watch five seconds of matching, and be
    // turned away at the end of it by a draw that could never have succeeded.
    await clickHeart();

    await waitFor(() => expect(errorCalls()).toContain(BLOCKED_RESPONSE.message));
    expect(screen.queryByText('Confirm Matchmaking')).toBeNull();
    expect(findMatch).not.toHaveBeenCalled();
  });

  it('is not answered with a cooldown', async () => {
    // Nothing is waiting, so there is no wait to quote and no "try again in four hours" to give.
    await clickHeart();

    await waitFor(() => expect(errorCalls()).toContain(BLOCKED_RESPONSE.message));
    expect(infoCalls()).toEqual([]);
  });

  it('has its invitation to draw withdrawn', async () => {
    render(<MatchmakingPage />);

    await waitFor(() =>
      expect(screen.getByText('No matchmaking slot for your cohort')).toBeTruthy()
    );
    expect(screen.queryByText('Click the heart to find your match! 💕')).toBeNull();
  });

  it('is told the attempt was not spent', async () => {
    // The whole reason the backend refuses before it writes. Without this the user would still be
    // waiting out a four-hour lockout for a draw that could not succeed.
    render(<MatchmakingPage />);

    await waitFor(() =>
      expect(screen.getByText(/have not used an attempt/)).toBeTruthy()
    );
  });

  it('shows the same card copy as a draw refused for the same reason', async () => {
    // Same fact, discovered at a different moment, so one set of words.
    render(<MatchmakingPage />);

    await waitFor(() =>
      expect(screen.getByText('Your cohort is not set up for matchmaking yet')).toBeTruthy()
    );
    expect(screen.queryByText('Waiting for your match...')).toBeNull();
  });

  it('draws as soon as a slot is configured, without waiting out anything', async () => {
    // The condition is re-read on every click rather than latched for the life of the page, so an
    // operator adding the slot is noticed by the user's next attempt. There is nothing to wait out
    // because nothing was spent.
    const { container } = render(<MatchmakingPage />);
    await waitFor(() =>
      expect(screen.getByText('No matchmaking slot for your cohort')).toBeTruthy()
    );

    vi.mocked(checkMatchmakingStatus).mockResolvedValue(NO_COOLDOWN);
    await act(async () => {
      (container.querySelector('.cursor-pointer') as HTMLElement).click();
    });

    await waitFor(() => expect(screen.getByText('Confirm Matchmaking')).toBeTruthy());
  });
});

describe('a draw that is refused', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    // The wait begins *with* the refusal, which is the state under test: eligible while the user
    // is choosing to draw, on cooldown once the spent attempt has started one. Keyed off whether a
    // draw was attempted rather than off a call count, so it stays honest if the page checks once
    // more or once less on mount.
    vi.mocked(checkMatchmakingStatus).mockImplementation(async () =>
      vi.mocked(findMatch).mock.calls.length > 0 ? COOLDOWN_RESPONSE : NO_COOLDOWN
    );
    vi.mocked(getCurrentConversation).mockRejectedValue(new Error('no conversation'));
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  it('says why there was no match, in the server\'s words', async () => {
    vi.mocked(findMatch).mockRejectedValue(refusal(PROBABILISTIC, NO_CANDIDATES));

    await refuseADraw();

    await waitFor(() => expect(errorCalls()).toContain(PROBABILISTIC));
  });

  it('names the cohort when it is the cohort that has no slot', async () => {
    vi.mocked(findMatch).mockRejectedValue(refusal(UNCONFIGURED, COHORT_NOT_CONFIGURED));

    await refuseADraw();

    await waitFor(() => expect(errorCalls()).toContain(UNCONFIGURED));
  });

  it('does not answer a refusal with a cooldown notice', async () => {
    // The regression. The attempt was spent, so a wait genuinely started, and the page used to
    // announce it as well -- producing a second toast that outranked the reason the draw failed.
    // For a cohort with no slot that second toast is not merely redundant: it tells the user to
    // come back later for a draw that cannot succeed until an operator configures one.
    vi.mocked(findMatch).mockRejectedValue(refusal(PROBABILISTIC, NO_CANDIDATES));

    await refuseADraw();

    await waitFor(() => expect(errorCalls()).toContain(PROBABILISTIC));
    expect(infoCalls()).not.toContain(COOLDOWN_RESPONSE.message);
  });

  it('still withdraws the heart, because the wait is real', async () => {
    // The counterpart to the test above, and the reason silence is the right answer rather than
    // merely a quiet one: the spent attempt did start a cooldown, and if the page ignored it the
    // heart would still be there inviting a draw that the server would refuse.
    vi.mocked(findMatch).mockRejectedValue(refusal(PROBABILISTIC, NO_CANDIDATES));

    await refuseADraw();

    await waitFor(() => expect(screen.getByText('Matchmaking on Cooldown')).toBeTruthy());
  });

  it('answers a click made during a cooldown with the cooldown', async () => {
    // The one place a cooldown notice belongs, and the reason the refusal above must not raise one:
    // here the wait is the whole reason the click did nothing, so saying nothing would read as a
    // broken button rather than as an answer.
    vi.mocked(checkMatchmakingStatus).mockResolvedValue(COOLDOWN_RESPONSE);

    const { container } = render(<MatchmakingPage />);
    await waitFor(() => expect(screen.getByText('Matchmaking on Cooldown')).toBeTruthy());

    await act(async () => {
      (container.querySelector('.cursor-pointer') as HTMLElement).click();
    });

    expect(infoCalls()).toContain(COOLDOWN_RESPONSE.message);
    expect(findMatch).not.toHaveBeenCalled();
  });
});

describe('the card after a refused draw', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(checkMatchmakingStatus).mockImplementation(async () =>
      vi.mocked(findMatch).mock.calls.length > 0 ? COOLDOWN_RESPONSE : NO_COOLDOWN
    );
    vi.mocked(getCurrentConversation).mockRejectedValue(new Error('no conversation'));
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  it('stops promising a match that is not coming', async () => {
    // The card used to fall through to "Waiting for your match..." and stay there. Nothing was in
    // progress, the attempt was spent, and the heart was withdrawn, so it was a promise the page
    // would not let the user act on.
    vi.mocked(findMatch).mockRejectedValue(refusal(PROBABILISTIC, NO_CANDIDATES));

    await refuseADraw();

    await waitFor(() => expect(screen.getByText('No match this time')).toBeTruthy());
    expect(screen.queryByText('Waiting for your match...')).toBeNull();
    expect(screen.getByText(PROBABILISTIC)).toBeTruthy();
  });

  it('says the cohort needs configuring, rather than that there was no match', async () => {
    vi.mocked(findMatch).mockRejectedValue(refusal(UNCONFIGURED, COHORT_NOT_CONFIGURED));

    await refuseADraw();

    await waitFor(() =>
      expect(screen.getByText('Your cohort is not set up for matchmaking yet')).toBeTruthy()
    );
    expect(screen.queryByText('No match this time')).toBeNull();
    expect(screen.queryByText('Waiting for your match...')).toBeNull();
  });

  it('says something when the server sent no code at all', async () => {
    vi.mocked(findMatch).mockRejectedValue(refusal(PROBABILISTIC));

    await refuseADraw();

    await waitFor(() => expect(screen.getByText('No match this time')).toBeTruthy());
    expect(screen.queryByText('Waiting for your match...')).toBeNull();
  });

  it('is still waiting when nothing has been refused', async () => {
    // The honest reading of the empty card: no attempt has been made, so nothing has come of one.
    render(<MatchmakingPage />);

    await waitFor(() => expect(screen.getByText('Waiting for your match...')).toBeTruthy());
  });
});