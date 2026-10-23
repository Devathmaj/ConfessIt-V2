import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { CaseClosedChatDialog } from '@/components/CaseClosedChatDialog';

/**
 * The Case Closed thread's count.
 *
 * A turn reaches the thread twice: once from the send, which the server has
 * already stored, and once from the live echo of that same turn. The echo
 * de-duplicated the *row* but incremented the count anyway, so the count ran one
 * ahead of the thread for every turn the reader had sent.
 *
 * The count is what the "load older" affordance is derived from -- the rows on
 * screen are subtracted from it -- so the affordance offered a page that did not
 * exist, for the rest of the thread.
 */

const api = vi.hoisted(() => ({
  getCaseClosedMessages: vi.fn(),
  sendCaseClosedMessage: vi.fn(),
  reportCaseClosedMessage: vi.fn(),
}));

const messaging = vi.hoisted(() => ({
  caseClosedMessagingClient: {
    connect: vi.fn(async () => true),
    disconnect: vi.fn(),
    onMessage: vi.fn((_callback: (message: Record<string, unknown>) => void) => () => {}),
    onError: vi.fn(() => () => {}),
    onReconnect: vi.fn(() => () => {}),
  },
  /** The callbacks the dialog registered, so a test can deliver a live turn. */
  messageCallbacks: [] as ((message: Record<string, unknown>) => void)[],
}));

vi.mock('@/services/api', () => api);
vi.mock('@/lib/caseClosedMessaging', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/caseClosedMessaging')>();
  return { ...actual, caseClosedMessagingClient: messaging.caseClosedMessagingClient };
});
vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn(), warning: vi.fn() },
  Toaster: () => null,
}));

const quota = {
  conversation_limit: 400,
  participant_limit: 200,
  conversation_count: 0,
  participant_count: 0,
  conversation_remaining: 400,
  participant_remaining: 200,
  can_send: true,
  limit_reached: null,
};

const turn = (id: string, body: string, overrides: Record<string, unknown> = {}) => ({
  id,
  conversation_id: 'conv-1',
  sender_user_id: 'detective-1',
  body,
  created_at: '2026-05-01T10:05:00.000Z',
  is_read: false,
  ...overrides,
});

beforeEach(() => {
  vi.clearAllMocks();
  messaging.messageCallbacks = [];
  messaging.caseClosedMessagingClient.onMessage.mockImplementation((callback) => {
    messaging.messageCallbacks.push(callback);
    return () => {};
  });
  messaging.caseClosedMessagingClient.onError.mockImplementation(() => () => {});
  messaging.caseClosedMessagingClient.connect.mockResolvedValue(true);
  api.getCaseClosedMessages.mockResolvedValue({
    messages: [],
    total: 0,
    quota,
    chat_open: true,
    closed_reason: null,
  });
});

async function renderThread() {
  render(
    <AuthContext.Provider
      value={{ user: { id: 'detective-1', name: 'Detective' }, token: 'token', isAuthenticated: true, loading: false } as never}
    >
      <CaseClosedChatDialog sessionId="session-1" />
    </AuthContext.Provider>,
  );
  await waitFor(() => expect(api.getCaseClosedMessages).toHaveBeenCalled());
}

/** The count the thread shows beside the "load older" affordance. */
const shownCount = () => {
  const match = document.body.textContent?.match(/Show older \((\d+) more\)/);
  return match ? Number(match[1]) : 0;
};

describe('the Case Closed thread count', () => {
  it('moves by exactly one for a turn the reader sent', async () => {
    api.sendCaseClosedMessage.mockResolvedValue({
      ...turn('turn-1', 'my own turn'),
      quota: { ...quota, conversation_count: 1, participant_count: 1, conversation_remaining: 399, participant_remaining: 199 },
    });
    await renderThread();

    fireEvent.change(screen.getByPlaceholderText(/discuss leads/i), {
      target: { value: 'my own turn' },
    });
    fireEvent.click(screen.getByRole('button', { name: /send/i }));

    await waitFor(() => expect(screen.getByText('my own turn')).toBeInTheDocument());
    // One turn on screen, and the count agrees: one, not two.
    expect(screen.getAllByText('my own turn')).toHaveLength(1);
    // One turn in the thread, so nothing older is on offer.
    expect(shownCount()).toBe(0);
  });

  it('does not move again when the live echo of that same turn arrives', async () => {
    api.sendCaseClosedMessage.mockResolvedValue({
      ...turn('turn-1', 'my own turn'),
      quota: { ...quota, conversation_count: 1, participant_count: 1, conversation_remaining: 399, participant_remaining: 199 },
    });
    await renderThread();

    fireEvent.change(screen.getByPlaceholderText(/discuss leads/i), {
      target: { value: 'my own turn' },
    });
    fireEvent.click(screen.getByRole('button', { name: /send/i }));
    await waitFor(() => expect(screen.getByText('my own turn')).toBeInTheDocument());

    // The echo of the turn the reader just sent: the same turn, arriving live.
    messaging.messageCallbacks.forEach((callback) =>
      callback(turn('turn-1', 'my own turn') as never),
    );

    await waitFor(() => expect(screen.getAllByText('my own turn')).toHaveLength(1));
    // The thread holds one turn and the count agrees, so there is nothing older
    // to offer. Before the fix the count ran one ahead of the thread for every
    // turn the reader sent, and the "Show older" affordance promised a page that
    // did not exist -- for the rest of the thread.
    expect(screen.queryByRole('button', { name: /show older/i })).not.toBeInTheDocument();
  });

  it('does move for a turn the partner sent', async () => {
    await renderThread();

    messaging.messageCallbacks.forEach((callback) =>
      callback(
        turn('turn-2', 'a partner turn', { sender_user_id: 'detective-2' }) as never,
      ),
    );

    await waitFor(() => expect(screen.getByText('a partner turn')).toBeInTheDocument());
    expect(screen.getAllByText('a partner turn')).toHaveLength(1);
  });
});
