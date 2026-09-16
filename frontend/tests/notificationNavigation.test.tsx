import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
import { AuthContext, type User } from '@/contexts/AuthContext';
import { InboxPage } from '@/pages/InboxPage';

/**
 * Which notification cards go somewhere, and which do not.
 *
 * The server decides this by sending a target pair or by sending neither; these tests pin the
 * other half, that a card with a target behaves like a link and a card without one behaves like
 * text. Both halves matter, and the second is the one that is easy to break: a card that shows
 * a pointer cursor and takes focus but navigates nowhere is a lie the reader has to discover by
 * clicking it.
 *
 * The path is asserted rather than the destination component. Rendering the real Message Box
 * would drag in a second page's data fetching, and what is under test here is which URL the
 * card produces -- `notificationTargets.test.ts` covers the path itself.
 */

const api = vi.hoisted(() => ({
  getNotifications: vi.fn(),
  getCurrentConversation: vi.fn(),
  getReceivedConversations: vi.fn(),
  acceptConversation: vi.fn(),
  rejectConversation: vi.fn(),
}));

vi.mock('@/services/api', () => api);
vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn() },
  Toaster: () => null,
}));

const authValue = {
  user: { id: 'me', name: 'Me', regno: 'CS-9' } as User,
  logout: vi.fn(),
  isAuthenticated: true,
  isLoading: false,
  token: 't' as string | null,
  verifyMagicToken: vi.fn(),
  fetchAndSetUser: vi.fn(),
  can: () => false,
  signInPath: '/login',
};

const notice = (over: Record<string, unknown> = {}) => ({
  id: 'n1',
  heading: 'You received a Love Note!',
  body: 'Ada sent you a love note!',
  created_at: '2026-03-01T12:00:00Z',
  target_kind: null,
  target_id: null,
  ...over,
});

/**
 * Renders the notifications list with one row in it, and waits for that row's heading.
 *
 * The two conversation endpoints are stubbed empty so nothing is derived from conversation
 * state; the rows below are the only thing on screen, which is what makes "this card came from
 * this row" checkable rather than merely likely.
 */
const renderInbox = async (row: Record<string, unknown>) => {
  api.getNotifications.mockResolvedValue({ notifications: [row] });
  api.getCurrentConversation.mockResolvedValue({ status: 'no_active_match' });
  api.getReceivedConversations.mockResolvedValue({
    status: 'no_received_conversations',
    conversations: [],
  });

  render(
    <AuthContext.Provider value={authValue}>
      <MemoryRouter initialEntries={['/notifications']}>
        <Routes>
          <Route path="/notifications" element={<InboxPage />} />
        </Routes>
        {/* Outside <Routes> on purpose: the probe has to be mounted while the page is still
            at /notifications, which is exactly the moment a click is asserted not to have
            navigated anywhere. */}
        <LocationProbe />
      </MemoryRouter>
    </AuthContext.Provider>
  );

  // The feed arrives over an effect, so no card exists until it lands.
  await waitFor(() => expect(screen.getByText(row.heading)).toBeInTheDocument());
};

/**
 * Reports where the router actually went. Rendering it is the only way to observe a navigation
 * without asserting on the mock that performed it.
 */
const LocationProbe = () => {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>;
};

const here = () => screen.getByTestId('location').textContent;

describe('notification cards with a target', () => {
  beforeEach(() => vi.clearAllMocks());

  it('sends a love note notification to the note', async () => {
    await renderInbox(
      notice({ target_kind: 'love_note', target_id: 'note-7' })
    );

    fireEvent.click(screen.getByRole('button', { name: /You received a Love Note/ }));

    await waitFor(() => expect(here()).toBe('/love-notes?tab=inbox&note=note-7'));
  });

  it('sends a conversation notification to that match in the Message Box', async () => {
    await renderInbox(
      notice({
        heading: 'A conversation is waiting',
        target_kind: 'conversation',
        target_id: 'm-123',
      })
    );

    fireEvent.click(screen.getByRole('button', { name: /A conversation is waiting/ }));

    await waitFor(() => expect(here()).toBe('/inbox?match=m-123'));
  });

  it('is reachable by keyboard, and Enter activates it', async () => {
    // jsdom does not implement the browser's default action for Enter on a non-button
    // element, so the handler is asserted directly rather than through synthesised activation.
    await renderInbox(
      notice({ target_kind: 'love_note', target_id: 'note-7' })
    );

    const card = screen.getByRole('button', { name: /You received a Love Note/ });
    expect(card).toHaveAttribute('tabindex', '0');
    card.focus();
    expect(card).toHaveFocus();

    fireEvent.keyDown(card, { key: 'Enter' });

    await waitFor(() => expect(here()).toContain('note=note-7'));
  });
});

describe('notification cards without a target', () => {
  beforeEach(() => vi.clearAllMocks());

  it('leaves the matchmaking notice unclickable rather than guessing a route', async () => {
    // The whole point: a match id is a fact about two people, not a page. There is no list of
    // your matches to put it in, so there is nowhere correct for this to go.
    await renderInbox(notice({ heading: 'You appeared in matchmaking', body: 'You were matched with Ada' }));

    expect(screen.queryByRole('button', { name: /You appeared in matchmaking/ })).toBeNull();

    fireEvent.click(screen.getByText('You appeared in matchmaking'));
    expect(here()).toBe('/notifications');
  });

  it('keeps a removal notice out of the tab order', async () => {
    await renderInbox(notice({ heading: 'Content Removed', body: 'Your content was removed.' }));

    // No `role` and no `tabindex`: an inert card that still takes focus is worse than one that
    // is visibly not a control, because it promises an action it cannot perform.
    const card = screen.getByText('Content Removed').closest('.rounded-lg');
    expect(card).not.toHaveAttribute('tabindex');
    expect(card).not.toHaveAttribute('role');
  });

  it('does not navigate when an informational notice is clicked', async () => {
    await renderInbox(notice({ heading: 'We reviewed your report', body: 'It was not removed.' }));

    fireEvent.click(screen.getByText('We reviewed your report'));
    expect(here()).toBe('/notifications');
  });
});