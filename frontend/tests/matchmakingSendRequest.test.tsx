import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MatchmakingPage } from '@/pages/MatchmakingPage';

/**
 * "Send Message Request" spends the match's one request.
 *
 * The click is a write the server has to accept, so while it is in flight the button
 * carries the busy state and cannot be double-fired. Pinned here rather than in the
 * refusal-notice file because the setup is the opposite one: a live match waiting on
 * the viewer's side.
 */

vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn() },
}));

// The page renders chrome that is not what is under test.
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

import { checkMatchmakingStatus, requestConversation, getCurrentConversation } from '@/services/api';

const getConversation = getCurrentConversation as ReturnType<typeof vi.fn>;
const request = requestConversation as ReturnType<typeof vi.fn>;

/** The matchup the server is holding for this viewer in the ``pending`` stage. */
const PENDING_CONVERSATION = {
  status: 'success',
  match: {
    id: 'match-1',
    expires_at: '2099-01-01T00:00:00.000Z',
    created_at: '2026-01-01T00:00:00.000Z',
    is_expired: false,
  },
  conversation: {
    id: 'conv-1',
    status: 'pending',
    initiator_regno: 'CS22BT0001',
    receiver_regno: 'CS22BT0002',
    created_at: '2026-01-01T00:00:00.000Z',
  },
  other_user: {
    regno: 'CS22BT0002',
    name: 'Sam',
    profile_picture_url: '',
    year: 2,
    department: 'CSE',
    bio: '',
    interests: [],
  },
  is_initiator: true,
};

describe('MatchmakingPage: sending a message request', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (checkMatchmakingStatus as ReturnType<typeof vi.fn>).mockResolvedValue({ status: 'eligible' });
    // The first read is the pending matchup; the refresh after a successful request sees
    // the state the server moved it to.
    getConversation
      .mockResolvedValueOnce(PENDING_CONVERSATION)
      .mockResolvedValue({
        ...PENDING_CONVERSATION,
        conversation: { ...PENDING_CONVERSATION.conversation, status: 'requested' },
      });
  });

  it('shows the button busy until the server accepts, and one click sends one request', async () => {
    let resolveRequest!: (value: unknown) => void;
    request.mockImplementation(() => new Promise((resolve) => { resolveRequest = resolve; }));

    render(<MatchmakingPage />);
    const button = await screen.findByRole('button', { name: /send message request/i });

    fireEvent.click(button);

    // Busy: the label is replaced, and the disabled button swallows a second click.
    const busy = await screen.findByRole('button', { name: /sending request/i });
    expect(busy).toBeDisabled();
    fireEvent.click(busy);
    expect(request).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith('match-1');

    // The answer moves the card to the waiting state; the button is gone, not left busy.
    resolveRequest({ status: 'success' });
    await waitFor(() =>
      expect(screen.getByText(/waiting for response/i)).toBeInTheDocument(),
    );
    expect(screen.queryByRole('button', { name: /send message request/i })).not.toBeInTheDocument();
  });
});
