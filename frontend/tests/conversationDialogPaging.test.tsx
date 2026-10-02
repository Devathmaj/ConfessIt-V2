import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { ConversationDialog } from '@/components/ConversationDialog';

/**
 * Paging a conversation thread upwards through the message dialog.
 *
 * Three properties are pinned here, each failing in a way the old load-everything
 * dialog could not have caught:
 *
 * * **The dialog opens on the newest window.** Latest message last, with the server
 *   total behind the pager -- never the whole transcript up front.
 * * **Older pages prepend above.** "Show older messages" sits at the top of the
 *   thread (not the bottom like every other list here) and names the oldest loaded
 *   message as its anchor, so concurrent sends cannot shift it.
 * * **Live arrivals still land at the bottom.** A socket message appends beneath and
 *   moves the total with it; the pager keeps offering exactly the older rows missing.
 */

const api = vi.hoisted(() => ({
  getCurrentConversation: vi.fn(),
  acceptConversation: vi.fn(),
  rejectConversation: vi.fn(),
  getConversationByMatch: vi.fn(),
  blockConversation: vi.fn(),
  unblockConversation: vi.fn(),
}));

const wsCallbacks = vi.hoisted(() => ({
  message: null as null | ((message: Record<string, unknown>) => void),
}));

const messaging = vi.hoisted(() => ({
  messagingClient: {
    connect: vi.fn(),
    disconnect: vi.fn(),
    onMessage: vi.fn(),
    onError: vi.fn(),
  },
  sendMessage: vi.fn(),
  fetchMessages: vi.fn(),
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

type ThreadRow = {
  id: string;
  body: string;
  sender_regno: string;
  receiver_regno: string;
  created_at: string;
  is_read: boolean;
};

/** The in-test stand-in for the thread table, oldest first, served newest-window-first. */
let db: ThreadRow[];

const stamp = (index: number) => `2026-05-01T10:${String(index).padStart(2, '0')}:00.000Z`;

const message = (index: number): ThreadRow => ({
  id: `m${index}`,
  body: `thread message ${index}`,
  sender_regno: index % 2 === 0 ? 'ALICE' : 'BOB',
  receiver_regno: index % 2 === 0 ? 'BOB' : 'ALICE',
  created_at: stamp(index),
  is_read: true,
});

const conversationData = () => ({
  match: {
    id: 'match-1',
    expires_at: '2026-05-01T14:00:00.000Z',
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
  // The transcript arrives over an effect, and the badge reads "0 msgs" from the
  // first paint -- so wait for a nonzero count, not merely the badge, or the wait
  // can resolve before the first window lands.
  await screen.findByText(/[1-9]\d* msgs/);
}

/** The message bodies on screen, top to bottom. */
const bodiesOnScreen = () =>
  screen
    .getAllByText(/thread message \d+/)
    .map((element) => element.textContent);

describe('ConversationDialog: paged thread', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    db = [];
    wsCallbacks.message = null;
    messaging.messagingClient.connect.mockResolvedValue(undefined);
    messaging.messagingClient.onMessage.mockImplementation((callback: never) => {
      wsCallbacks.message = callback as never;
      return () => {};
    });
    messaging.messagingClient.onError.mockImplementation(() => () => {});
    messaging.fetchMessages.mockImplementation(
      async (_conversationId: string, _token: string, limit = 200, before?: string) => {
        let rows = [...db];
        if (before) {
          const anchor = rows.findIndex((row) => row.id === before);
          rows = anchor === -1 ? [] : rows.slice(0, anchor);
        }
        const window = rows.slice(-limit);
        return { messages: window, total: rows.length, quota: null };
      },
    );
    api.getConversationByMatch.mockResolvedValue({ status: 'success', is_blocked: false });
  });

  it('opens on the newest window with the total behind the pager', async () => {
    db = Array.from({ length: 35 }, (_, index) => message(index));
    await renderDialog();
    await screen.findByText('thread message 34');

    // Thirty newest on screen, five still on the server, latest last.
    expect(bodiesOnScreen()).toHaveLength(30);
    expect(bodiesOnScreen()[29]).toBe('thread message 34');
    expect(screen.getByText(/35 msgs/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Show older messages' })).toHaveTextContent(
      '5 remaining',
    );
    expect(screen.queryByText('thread message 0')).not.toBeInTheDocument();
  });

  it('prepends the older page above without moving the rows already shown', async () => {
    db = Array.from({ length: 35 }, (_, index) => message(index));
    await renderDialog();
    await screen.findByText('thread message 34');

    fireEvent.click(screen.getByRole('button', { name: 'Show older messages' }));
    await screen.findByText('thread message 0');

    // The anchor went out as the oldest loaded id, not an offset.
    expect(messaging.fetchMessages).toHaveBeenCalledWith(
      'conv-1',
      'token',
      30,
      'm5',
    );
    expect(bodiesOnScreen()).toHaveLength(35);
    expect(bodiesOnScreen()[0]).toBe('thread message 0');
    expect(bodiesOnScreen()[34]).toBe('thread message 34');
    expect(
      screen.queryByRole('button', { name: 'Show older messages' }),
    ).not.toBeInTheDocument();
  });

  it('lands live arrivals at the bottom and keeps the pager honest', async () => {
    db = Array.from({ length: 31 }, (_, index) => message(index));
    await renderDialog();
    await screen.findByText('thread message 30');

    const live = { ...message(31), id: 'm-live', body: 'live arrival' };
    wsCallbacks.message?.({ ...live } as never);
    const liveBody = await screen.findByText('live arrival');

    // Beneath everything loaded: the socket appends at the bottom, never above.
    const previousLast = screen.getByText('thread message 30');
    expect(
      Boolean(liveBody.compareDocumentPosition(previousLast) & Node.DOCUMENT_POSITION_PRECEDING),
    ).toBe(true);
    // One arrived, one more exists: the total moved with it, so the pager still
    // offers exactly the older row missing.
    expect(screen.getByText(/32 msgs/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Show older messages' })).toHaveTextContent(
      '1 remaining',
    );
  });

  it('still renders answers without a total as one unpaged thread', async () => {
    messaging.fetchMessages.mockResolvedValue({
      messages: [message(0), message(1)],
      quota: null,
    });
    await renderDialog();

    expect(screen.getByText('thread message 0')).toBeInTheDocument();
    expect(screen.getByText('thread message 1')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Show older messages' })).not.toBeInTheDocument();
  });

  it('never opens a socket into a blocked conversation', async () => {
    db = [message(0), message(1)];
    // The setup payload carries no block flag, exactly as in the report, and the
    // authoritative answer lands on the block-status read: the dialog must wait
    // for it instead of initializing messaging on the default.
    api.getConversationByMatch.mockResolvedValue({
      status: 'success',
      is_blocked: true,
      blocked_by: 'ALICE',
    });
    await renderDialog();

    // History loads once, read-only -- and no socket is ever opened, so nothing
    // can fail, retry, or toast.
    await screen.findByText('thread message 1');
    expect(messaging.messagingClient.connect).not.toHaveBeenCalled();
    expect(messaging.fetchMessages).toHaveBeenCalledTimes(1);
    expect(toast.error).not.toHaveBeenCalled();
  });

  it('stays read-only when the setup data already says blocked', async () => {
    db = [message(0)];
    api.getConversationByMatch.mockResolvedValue({
      status: 'success',
      is_blocked: true,
      blocked_by: 'ALICE',
    });
    await renderDialog({ ...conversationData(), is_blocked: true, blocked_by: 'ALICE' });

    await screen.findByText('thread message 0');
    expect(messaging.messagingClient.connect).not.toHaveBeenCalled();
    expect(toast.error).not.toHaveBeenCalled();
  });
});
