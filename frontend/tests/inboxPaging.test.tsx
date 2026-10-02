import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { InboxPage } from '@/pages/InboxPage';

/**
 * Paging the inbox notice list through the paginated endpoint.
 *
 * Three properties are pinned here, each failing in a way the old capped list could
 * not have caught:
 *
 * * **Search is submitted, not typed.** The box is a draft; the Search button (or
 *   Enter) submits the query the database answers, so a half-typed word never costs a
 *   re-read. Typing alone fires nothing.
 * * **The list grows downwards.** The screen holds one contiguous window with the
 *   server total behind it, and "Show more" appends the next page beneath the notices
 *   already on screen -- the request carries the offset of what is shown.
 * * **Derived conversation cards never page.** They come from match state, complete
 *   on every read; only the system window grows, and a search narrows the cards in
 *   memory while the window re-reads from the database.
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

type NoticeRow = {
  id: string;
  heading: string;
  body: string;
  created_at: string;
  target_kind: null;
  target_id: null;
};

/** The in-test stand-in for the notice table, served newest-first like the endpoint. */
let db: NoticeRow[];

const stamp = (day: number) => `2026-06-${String(day).padStart(2, '0')}T10:00:00.000Z`;

const notice = (id: string, heading: string, day: number): NoticeRow => ({
  id,
  heading,
  body: `Body of ${heading.toLowerCase()}`,
  created_at: stamp(day),
  target_kind: null,
  target_id: null,
});

const noConversations = () => {
  api.getCurrentConversation.mockResolvedValue({ status: 'no_active_match' });
  api.getReceivedConversations.mockResolvedValue({
    status: 'no_received_conversations',
    conversations: [],
  });
};

async function renderInbox() {
  render(
    <AuthContext.Provider
      value={
        {
          user: { id: 'me', name: 'Me', regno: 'CS-9' },
          logout: vi.fn(),
          isAuthenticated: true,
          isLoading: false,
          token: 't',
          verifyMagicToken: vi.fn(),
          fetchAndSetUser: vi.fn(),
          can: () => false,
          signInPath: '/login',
        } as never
      }
    >
      <MemoryRouter initialEntries={['/notifications']}>
        <InboxPage />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
  // The feed arrives over an effect, so no card exists until the first page lands.
  await screen.findByText(/Notifications/);
  await waitFor(() => {
    expect(api.getNotifications).toHaveBeenCalled();
  });
}

describe('InboxPage: paged notices', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    db = [];
    api.getNotifications.mockImplementation(async (params: Record<string, unknown> = {}) => {
      const q = ((params.q as string | undefined) ?? '').trim().toLowerCase();
      const skip = (params.skip as number | undefined) ?? 0;
      const limit = (params.limit as number | undefined) ?? 20;
      const rows = db
        .filter(
          (row) =>
            !q ||
            row.heading.toLowerCase().includes(q) ||
            row.body.toLowerCase().includes(q),
        )
        .sort((a, b) => b.created_at.localeCompare(a.created_at));
      return { notifications: rows.slice(skip, skip + limit), total: rows.length, skip, limit };
    });
    noConversations();
  });

  it('fires nothing while typing; the Search button submits the query the database answers', async () => {
    db = [notice('n1', 'You were matched!', 3), notice('n2', 'Content Removed', 2)];
    await renderInbox();
    await screen.findByText('You were matched!');

    // One read for the unfiltered first page, and nothing else while the draft changes.
    expect(api.getNotifications).toHaveBeenCalledTimes(1);
    fireEvent.change(screen.getByPlaceholderText(/Search notifications/), {
      target: { value: 'matched' },
    });
    expect(api.getNotifications).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'Search notifications' }));
    await waitFor(() => {
      expect(screen.queryByText('Content Removed')).not.toBeInTheDocument();
    });

    expect(api.getNotifications).toHaveBeenCalledWith(
      expect.objectContaining({ q: 'matched', skip: 0 }),
    );
    expect(screen.getByText('You were matched!')).toBeInTheDocument();
  });

  it('grows the list downwards on Show more without moving the notices already shown', async () => {
    db = Array.from({ length: 21 }, (_, index) =>
      notice(`n${index}`, `Notice number ${index}`, index + 1),
    );
    await renderInbox();
    await screen.findByText('Notice number 20');

    expect(screen.getByRole('button', { name: 'Show more notifications' })).toHaveTextContent(
      'Show more (1 remaining)',
    );

    fireEvent.click(screen.getByRole('button', { name: 'Show more notifications' }));
    await screen.findByText('Notice number 0');

    // The second read carried the offset of what was shown; nothing was re-read.
    const skips = api.getNotifications.mock.calls.map((call) => call[0]?.skip ?? 0);
    expect(skips).toContain(20);
    expect(screen.getByText('Notice number 20')).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Show more notifications' }),
    ).not.toBeInTheDocument();
  });

  it('clears back to the full first page', async () => {
    db = [notice('n1', 'You were matched!', 3), notice('n2', 'Content Removed', 2)];
    await renderInbox();
    await screen.findByText('You were matched!');

    fireEvent.change(screen.getByPlaceholderText(/Search notifications/), {
      target: { value: 'matched' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Search notifications' }));
    await waitFor(() => {
      expect(screen.queryByText('Content Removed')).not.toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
    await screen.findByText('Content Removed');
    expect(screen.getByPlaceholderText(/Search notifications/)).toHaveValue('');
  });

  it('still renders legacy bare-array answers with no pager', async () => {
    api.getNotifications.mockResolvedValue([
      notice('n1', 'You were matched!', 3),
      notice('n2', 'Content Removed', 2),
    ]);
    await renderInbox();

    expect(screen.getByText('You were matched!')).toBeInTheDocument();
    expect(screen.getByText('Content Removed')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /show more/i })).not.toBeInTheDocument();
  });

  it('ends the pager on a tail of only filtered-out conversation rows', async () => {
    // Twenty plain notices, then one conversation-family row the screen derives
    // from match state instead of rendering: the oldest row, so it is the whole
    // of the second page and contributes no card.
    db = Array.from({ length: 20 }, (_, index) =>
      notice(`n${index}`, `Notice number ${index}`, index + 2),
    );
    db.push(notice('cold', 'Zed accepted your message request', 1));
    await renderInbox();
    await screen.findByText('Notice number 19');

    expect(screen.getByRole('button', { name: 'Show more notifications' })).toHaveTextContent(
      'Show more (1 remaining)',
    );

    fireEvent.click(screen.getByRole('button', { name: 'Show more notifications' }));

    // The last page held nothing renderable, so nothing new appears -- but the
    // pager is consumed and the button goes away instead of stalling forever.
    await waitFor(() => {
      expect(
        screen.queryByRole('button', { name: 'Show more notifications' }),
      ).not.toBeInTheDocument();
    });
    expect(screen.getByText('Notice number 19')).toBeInTheDocument();
    expect(api.getNotifications.mock.calls.length).toBe(2);
  });

  it('never re-reads rows a filtered-out conversation row displaced', async () => {
    // The newest row is conversation-family, so the first page holds twenty server
    // rows but only nineteen cards; the next read must start past all twenty.
    db = Array.from({ length: 20 }, (_, index) =>
      notice(`n${index}`, `Notice number ${index}`, index + 1),
    );
    db.push(notice('cold', 'Zed accepted your message request', 21));
    await renderInbox();
    await screen.findByText('Notice number 19');

    fireEvent.click(screen.getByRole('button', { name: 'Show more notifications' }));
    await screen.findByText('Notice number 0');

    // Twenty cards, each exactly once: the row beneath the filtered one was not
    // read twice, and the pager is consumed.
    for (let index = 0; index < 20; index += 1) {
      expect(screen.getAllByText(`Notice number ${index}`).length).toBe(1);
    }
    expect(
      screen.queryByRole('button', { name: 'Show more notifications' }),
    ).not.toBeInTheDocument();
  });
});
