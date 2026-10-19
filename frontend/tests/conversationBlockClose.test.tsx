import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { ConversationDialog } from '@/components/ConversationDialog';

/**
 * Blocking a conversation is closing it.
 *
 * The mechanism, in one sentence: **the participant who closes a chat is the one who may
 * reopen it, and until then the chat is shut for both of them.** The thread stays
 * readable, and there is one closer at a time -- a chat is not something two participants
 * each hold shut, so the screen never offers the second participant a way to take that
 * right away.
 *
 * These tests pin the half of that the client is responsible for. The server's half --
 * one closer, and a second close refused -- is in
 * `backend/tests/test_conversation_block_close.py`.
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
  // The block answer is read on its own effect, so the controls settle after it.
  await waitFor(() => expect(api.getConversationByMatch).toHaveBeenCalled());
}

beforeEach(() => {
  vi.clearAllMocks();
  messaging.messagingClient.onMessage.mockImplementation(() => () => {});
  messaging.messagingClient.onError.mockImplementation(() => () => {});
  messaging.messagingClient.connect.mockResolvedValue(true);
  messaging.fetchMessages.mockResolvedValue({ messages: [], quota: null, total: 0 } as never);
});

describe('the close-the-chat controls', () => {
  it('offers the reopen to the participant who closed the chat', async () => {
    api.getConversationByMatch.mockResolvedValue({
      status: 'success',
      is_blocked: true,
      blocked_by: 'BOB',
    });
    await renderDialog(conversationData({ is_blocked: true, blocked_by: 'BOB' }));

    expect(await screen.findByRole('button', { name: /unblock/i })).toBeInTheDocument();
  });

  it('does not offer the reopen to the participant who did not close it', async () => {
    // Alice closed it. Bob can read the thread, but the reopen is hers, not his.
    api.getConversationByMatch.mockResolvedValue({
      status: 'success',
      is_blocked: true,
      blocked_by: 'ALICE',
    });
    await renderDialog(conversationData({ is_blocked: true, blocked_by: 'ALICE' }));

    await waitFor(() => expect(screen.getByText('Blocked')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /unblock/i })).not.toBeInTheDocument();
  });

  it('offers no close button on a chat that is already closed', async () => {
    // The chat is shut, so there is nothing to close. This is why the second close --
    // the one that could take the reopen right away from the participant who closed the
    // chat -- is only reachable through the API.
    api.getConversationByMatch.mockResolvedValue({
      status: 'success',
      is_blocked: true,
      blocked_by: 'ALICE',
    });
    await renderDialog(conversationData({ is_blocked: true, blocked_by: 'ALICE' }));

    await waitFor(() => expect(screen.getByText('Blocked')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /block user/i })).not.toBeInTheDocument();
  });

  it('offers the close on a chat that is open', async () => {
    api.getConversationByMatch.mockResolvedValue({
      status: 'success',
      is_blocked: false,
      blocked_by: null,
    });
    await renderDialog();

    expect(await screen.findByRole('button', { name: /block user/i })).toBeInTheDocument();
  });

  it('says what closing the chat does, and promises nothing it does not do', async () => {
    api.getConversationByMatch.mockResolvedValue({
      status: 'success',
      is_blocked: false,
      blocked_by: null,
    });
    await renderDialog();

    fireEvent.click(await screen.findByRole('button', { name: /block user/i }));

    // The chat closes for both of them, nothing is deleted, and only the closer can
    // reopen it. It is *not* a notification: the other participant is not told, so the
    // copy must not say they are.
    expect(await screen.findByText(/closes for both of you/i)).toBeInTheDocument();
    expect(screen.getByText(/nothing is deleted/i)).toBeInTheDocument();
    expect(screen.getByText(/only you can reopen it/i)).toBeInTheDocument();
    expect(screen.queryByText(/will be notified/i)).not.toBeInTheDocument();
  });
});
