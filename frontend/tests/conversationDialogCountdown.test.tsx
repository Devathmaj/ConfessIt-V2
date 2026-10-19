import { act, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { ConversationDialog } from '@/components/ConversationDialog';

/**
 * The countdown in the conversation dialog.
 *
 * The matchmaking page opens the dialog with no conversation data at all -- the
 * dialog fetches its own. The countdown runs on a timer that was set up on the
 * first render, and a timer's callback closes over the values of the render that
 * created it, so on that path the timer was holding no data: the countdown
 * returned on every tick and the header stayed empty for the whole four hours.
 *
 * The same staleness meant the expiry never fired either -- the flag that locks
 * the composer when the match runs out -- so the composer stayed open and only
 * the server refused the send.
 */

const api = vi.hoisted(() => ({
  getCurrentConversation: vi.fn(),
  acceptConversation: vi.fn(),
  rejectConversation: vi.fn(),
  getConversationByMatch: vi.fn(),
  blockConversation: vi.fn(),
  unblockConversation: vi.fn(),
}));

const messaging = vi.hoisted(() => ({
  messagingClient: {
    connect: vi.fn(async () => true),
    disconnect: vi.fn(),
    onMessage: vi.fn(() => () => {}),
    onError: vi.fn(() => () => {}),
  },
  sendMessage: vi.fn(),
  fetchMessages: vi.fn(async () => ({ messages: [], quota: null, total: 0 })),
  reportMessage: vi.fn(),
}));

vi.mock('@/services/api', () => api);
vi.mock('@/lib/messaging', () => messaging);
vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn(), warning: vi.fn() },
  Toaster: () => null,
}));

/** The match runs out four hours after it was drawn. */
const EXPIRES_AT = '2026-05-01T14:00:00.000Z';

const conversationData = (overrides: Record<string, unknown> = {}) => ({
  match: {
    id: 'match-1',
    expires_at: EXPIRES_AT,
    created_at: '2026-05-01T10:00:00.000Z',
    is_expired: false,
  },
  conversation: {
    id: 'conv-1',
    status: 'accepted',
    initiator_regno: 'ALICE',
    receiver_regno: 'BOB',
    created_at: '2026-05-01T10:00:00.000Z',
    accepted_at: '2026-05-01T10:00:00.000Z',
  },
  other_user: {
    regno: 'ALICE',
    name: 'Alice',
    profile_picture_url: null,
    year: 2,
    department: 'CSE',
    bio: '',
    interests: [],
  },
  is_initiator: false,
  ...overrides,
});

/** Mount the dialog the way the matchmaking page does: with no data at all. */
async function renderWithoutData() {
  render(
    <AuthContext.Provider
      value={{
        user: { regno: 'BOB', name: 'Bob' },
        token: 'token',
        isAuthenticated: true,
        loading: false,
      } as never}
    >
      <MemoryRouter>
        <ConversationDialog onClose={() => {}} onRefresh={() => {}} />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
  // The dialog fetches its own conversation, so let that promise settle before
  // anything is asserted. (The timer has to be advanced for the promise to be
  // flushed: the fetch is awaited inside an effect.)
  await act(async () => {
    await vi.advanceTimersByTimeAsync(100);
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers({ shouldAdvanceTime: true });
  // One minute before the match runs out.
  vi.setSystemTime(new Date('2026-05-01T13:59:00.000Z'));
  api.getCurrentConversation.mockResolvedValue({ status: 'success', ...conversationData() });
  api.getConversationByMatch.mockResolvedValue({ status: 'success', is_blocked: false });
  messaging.messagingClient.connect.mockResolvedValue(true);
});

afterEach(() => {
  vi.useRealTimers();
});

describe('the conversation countdown', () => {
  it('ticks when the dialog fetched its own conversation', async () => {
    await renderWithoutData();

    const shown = () => screen.getByText(/^\d+h \d+m \d+s$/).textContent;
    const before = shown();

    // The clock moves on, and nothing else does: no new data, no re-render of
    // the conversation. The countdown is on a timer, so it has to move by itself.
    await act(async () => {
      vi.setSystemTime(new Date('2026-05-01T13:59:30.000Z'));
      await vi.advanceTimersByTimeAsync(1500);
    });

    expect(shown()).not.toBe(before);
  });

  it('locks the composer when the match runs out', async () => {
    await renderWithoutData();

    // The composer is open while the match is live.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });
    expect(screen.getByPlaceholderText(/type a message/i)).toBeInTheDocument();

    // Now the match runs out.
    await act(async () => {
      vi.setSystemTime(new Date(EXPIRES_AT));
      await vi.advanceTimersByTimeAsync(1500);
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(screen.getAllByText('Expired').length).toBeGreaterThan(0);
    expect(screen.getByText(/this conversation has expired/i)).toBeInTheDocument();
    // The composer is gone: the client locks it, rather than leaving it open for
    // the server to refuse.
    expect(screen.queryByPlaceholderText(/type a message/i)).not.toBeInTheDocument();
  });
});
