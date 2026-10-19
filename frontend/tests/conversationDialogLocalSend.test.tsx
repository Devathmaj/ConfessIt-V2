import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { ConversationDialog } from '@/components/ConversationDialog';

/**
 * The sender's own message, and the one live stream a thread may have.
 *
 * Two defects, one dialog:
 *
 * * the send used to put nothing on screen and wait for the WebSocket echo. If
 *   the socket was not connected, or the publish did not go out, or the server
 *   reaped the socket, the sender looked at a thread without their own message
 *   -- and the composer had already been cleared, so the natural thing to do
 *   was send it again. The server counts that second attempt, so the allowance
 *   was spent twice;
 * * unblocking called the initialiser directly *and* flipped the block flag the
 *   initialiser effect depends on, so the thread was initialised twice: the
 *   history was loaded twice, a second socket opened (orphaning the first,
 *   which kept pinging and kept holding a Redis connection) and a second
 *   message subscription registered, so every live message arrived twice.
 *
 * These tests drive the real dialog against a stand-in for the chat client.
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
    onMessage: vi.fn((_callback: (message: Record<string, unknown>) => void) => () => {}),
    onError: vi.fn(() => () => {}),
  },
  sendMessage: vi.fn(),
  fetchMessages: vi.fn(async () => ({ messages: [], quota: null, total: 0 })),
  reportMessage: vi.fn(),
  /** The callbacks the dialog registered, so a test can deliver a live message. */
  messageCallbacks: [] as ((message: Record<string, unknown>) => void)[],
}));

const toast = vi.hoisted(() => ({
  info: vi.fn(),
  error: vi.fn(),
  success: vi.fn(),
  warning: vi.fn(),
}));

vi.mock('@/services/api', () => api);
vi.mock('@/lib/messaging', () => messaging);
vi.mock('sonner', () => ({ toast, Toaster: () => null }));

const conversationData = (overrides: Record<string, unknown> = {}) => ({
  match: {
    id: 'match-1',
    // An hour from now, not a fixed date: the countdown reads the match's expiry and
    // locks the composer when it passes, so a hard-coded date stops being a live
    // match the day it goes by.
    expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
    created_at: new Date(Date.now() - 60 * 60 * 1000).toISOString(),
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

const sentMessage = {
  id: 'sent-1',
  conversation_id: 'conv-1',
  sender_regno: 'BOB',
  receiver_regno: 'ALICE',
  body: 'my own message',
  created_at: '2026-05-01T10:05:00.000Z',
  is_read: false,
  quota: {
    conversation_limit: 100,
    participant_limit: 50,
    conversation_count: 0,
    participant_count: 1,
    conversation_remaining: 100,
    participant_remaining: 49,
    can_send: true,
    limit_reached: null,
  },
};

async function renderDialog(data: Record<string, unknown> = conversationData()) {
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
        <ConversationDialog
          onClose={() => {}}
          onRefresh={() => {}}
          conversationData={data as never}
        />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
  // The dialog is ready when its block controls are on screen: a blocked thread
  // has no composer, so the composer is not a readiness signal for every case.
  await screen.findByRole('button', { name: /block user|unblock/i });
  // A blocked thread has no composer at all, so there is nothing to wait for.
  if (data.is_blocked) return;
  // The composer is disabled until the socket is up, and a value typed into a
  // disabled input is not a value at all. So the wait is for the composer to be
  // usable -- the precondition of sending, stated rather than assumed.
  const composer = await screen.findByPlaceholderText(/type a message/i);
  await waitFor(() => expect(composer).toBeEnabled());
}

/** How many times the text is on screen. */
const occurrences = (text: string) => screen.getAllByText(text).length;

beforeEach(() => {
  vi.clearAllMocks();
  messaging.messageCallbacks = [];
  messaging.messagingClient.onMessage.mockImplementation((callback) => {
    messaging.messageCallbacks.push(callback);
    return () => {};
  });
  messaging.messagingClient.onError.mockImplementation(() => () => {});
  messaging.messagingClient.connect.mockResolvedValue(true);
  messaging.fetchMessages.mockResolvedValue({ messages: [], quota: null, total: 0 } as never);
  api.getConversationByMatch.mockResolvedValue({ status: 'success', is_blocked: false });
});

describe("the sender's own message", () => {
  it('appears as soon as the send is answered, with no live echo at all', async () => {
    // The socket is not connected (or the publish did not go out, or the server
    // reaped the socket): the thread must still show what was sent.
    messaging.sendMessage.mockResolvedValue(sentMessage as never);
    await renderDialog();

    fireEvent.change(screen.getByPlaceholderText(/type a message/i), {
      target: { value: 'my own message' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    await waitFor(() => expect(occurrences('my own message')).toBe(1));
    // Nothing was delivered live: the message is there because the send
    // answered, not because an echo arrived.
    expect(messaging.messageCallbacks).toHaveLength(1);
  });

  it('is not shown twice when the live echo arrives afterwards', async () => {
    messaging.sendMessage.mockResolvedValue(sentMessage as never);
    await renderDialog();

    fireEvent.change(screen.getByPlaceholderText(/type a message/i), {
      target: { value: 'my own message' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await waitFor(() => expect(occurrences('my own message')).toBe(1));

    // The echo lands afterwards: the same message, once.
    messaging.messageCallbacks.forEach((callback) => callback(sentMessage as never));
    await waitFor(() => expect(occurrences('my own message')).toBe(1));
  });
});

describe('unblocking a partner', () => {
  it('opens one live stream, and one delivered message arrives once', async () => {
    // The thread starts blocked, by us.
    api.getConversationByMatch.mockResolvedValue({
      status: 'success',
      is_blocked: true,
      blocked_by: 'BOB',
    });
    api.unblockConversation.mockResolvedValue({} as never);
    await renderDialog(
      conversationData({ is_blocked: true, blocked_by: 'BOB' }) as never,
    );

    // The block answer lands: the thread is read-only, so no socket yet.
    await waitFor(() => expect(api.getConversationByMatch).toHaveBeenCalled());
    expect(messaging.messagingClient.connect).not.toHaveBeenCalled();

    // The unblock: the server accepts, and the block flag flips.
    api.getConversationByMatch.mockResolvedValue({
      status: 'success',
      is_blocked: false,
      blocked_by: null,
    });
    fireEvent.click(screen.getByRole('button', { name: /unblock/i }));

    // Exactly one live stream: the initialiser ran once, not twice.
    await waitFor(() => expect(messaging.messagingClient.connect).toHaveBeenCalledTimes(1));
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(messaging.messagingClient.connect).toHaveBeenCalledTimes(1);
    expect(messaging.messageCallbacks).toHaveLength(1);

    // And one delivered message is shown once, not twice.
    messaging.messageCallbacks.forEach((callback) =>
      callback({
        id: 'live-1',
        conversation_id: 'conv-1',
        sender_regno: 'ALICE',
        body: 'a live message',
        created_at: '2026-05-01T10:06:00.000Z',
        is_read: false,
      } as never),
    );
    await waitFor(() => expect(occurrences('a live message')).toBe(1));
  });
});
