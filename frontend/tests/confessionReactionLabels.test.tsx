import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AuthContext, type User } from '@/contexts/AuthContext';
import { ConfessionsPage } from '@/pages/ConfessionsPage';

/**
 * The reaction buttons have to say what they are.
 *
 * A reaction button is an icon and a count, and the count was the whole of its
 * accessible name: a screen reader announced "70" for a button whose job is to
 * say whether *you* have hearted a post. The count is also a sibling of the
 * comment's like and dislike buttons rather than part of them, so those had no
 * name at all.
 *
 * So the names are stated -- what the button is for, and how many people have
 * used it -- and the pressed state is stated too, because a button that toggles
 * your own reaction has to say whether yours is the one that is set.
 */

const api = vi.hoisted(() => ({
  getConfessions: vi.fn(),
  getConfessionComments: vi.fn(),
  createConfession: vi.fn(),
  getConfessionCooldown: vi.fn(),
  reactToConfession: vi.fn(),
  createComment: vi.fn(),
  likeComment: vi.fn(),
  dislikeComment: vi.fn(),
  reportComment: vi.fn(),
  reportConfession: vi.fn(),
  updateConfession: vi.fn(),
}));

vi.mock('@/services/api', () => api);
vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn() },
  Toaster: () => null,
}));

const confession = (over: Record<string, unknown> = {}) => ({
  id: 'c1',
  body: 'I still think about you',
  recipient_name: 'Sam',
  anonymous: false,
  comments_enabled: true,
  is_mine: false,
  user_info: { id: 'u1', regno: 'CS-1', name: 'Ada', avatar: null },
  created_at: '2026-01-01T10:00:00Z',
  comments: [
    {
      id: 'm1',
      user_info: { id: 'u2', regno: 'CS-2', name: 'Grace' },
      body: 'a reply',
      created_at: '2026-01-01T11:00:00Z',
      like_count: 4,
      dislike_count: 2,
      user_reaction: null,
      report_count: 0,
      reported_by: [],
    },
  ],
  heart_count: 70,
  haha_count: 30,
  whoa_count: 10,
  heartbreak_count: 5,
  comment_count: 1,
  user_reaction: null,
  report_count: 0,
  reported_by: [],
  ...over,
});

const authValue = {
  user: null as User | null,
  logout: vi.fn(),
  isAuthenticated: false,
  isLoading: false,
  token: null as string | null,
  verifyMagicToken: vi.fn(),
  fetchAndSetUser: vi.fn(),
  can: () => false,
  signInPath: '/login',
};

const renderFeed = async (items: ReturnType<typeof confession>[]) => {
  api.getConfessions.mockResolvedValue(items);
  render(
    <AuthContext.Provider
      value={{
        ...authValue,
        user: { id: 'me', name: 'Me', regno: 'CS-9', email: 'me@example.com' },
        isAuthenticated: true,
        token: 'token',
      }}
    >
      <MemoryRouter>
        <ConfessionsPage />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
  await waitFor(() => expect(screen.getByText(items[0].body)).toBeInTheDocument());
};

const cardFor = (text: string) =>
  screen.getByText(text).closest('.confession-card') as HTMLElement;

describe('the reaction buttons', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getConfessionCooldown.mockResolvedValue({ status: 'eligible', message: 'Proceed?' });
  });

  it('say what they are, and how many people have used them', async () => {
    await renderFeed([confession()]);
    const card = within(cardFor('I still think about you'));

    // Not "70". A name that says what the button does, with the count it
    // carries -- the count is the other half of what the button is for.
    expect(card.getByRole('button', { name: 'Heart reaction (70)' })).toBeInTheDocument();
    expect(card.getByRole('button', { name: 'Haha reaction (30)' })).toBeInTheDocument();
    expect(card.getByRole('button', { name: 'Whoa reaction (10)' })).toBeInTheDocument();
    expect(card.getByRole('button', { name: 'Heartbreak reaction (5)' })).toBeInTheDocument();
  });

  it('say whether the reader is the one who reacted', async () => {
    await renderFeed([confession()]);
    const card = within(cardFor('I still think about you'));

    // A toggle has to say which way it is set, or the reader cannot tell their
    // own reaction from everybody else's.
    expect(card.getByRole('button', { name: 'Heart reaction (70)' })).toHaveAttribute(
      'aria-pressed',
      'false',
    );
  });

  it('mark the reader\'s own reaction as pressed', async () => {
    await renderFeed([confession({ user_reaction: 'heart' })]);
    const card = within(cardFor('I still think about you'));

    expect(card.getByRole('button', { name: 'Heart reaction (70)' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(card.getByRole('button', { name: 'Haha reaction (30)' })).toHaveAttribute(
      'aria-pressed',
      'false',
    );
  });

  it('give the comment\'s like and dislike buttons a name too', async () => {
    await renderFeed([confession()]);
    const card = within(cardFor('I still think about you'));

    // The thread opens from the comment-count button on the card.
    fireEvent.click(card.getByRole('button', { name: '1' }));

    // These are icon-only buttons whose count sits beside them rather than in
    // them, so without a name they had none at all.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Like (4)' })).toBeInTheDocument(),
    );
    expect(screen.getByRole('button', { name: 'Dislike (2)' })).toBeInTheDocument();
  });
});
