import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AuthContext, type User } from '@/contexts/AuthContext';
import { ConfessionsPage } from '@/pages/ConfessionsPage';

/**
 * A reaction whose answer arrives after a newer one.
 *
 * Two reactions clicked in quick succession leave two requests in flight, and the
 * earlier one can answer after the later one has. Writing the earlier answer over
 * the later one leaves the screen showing a reaction the reader did not choose -- and
 * a count that disagrees with the server until the next read.
 *
 * The other half of the same defect: a request that *fails* used to restore a
 * snapshot taken before any of them, so a late failure undid a reaction that had
 * already been saved.
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

/** Distinct counts, so each reaction button has its own accessible name. */
const confession = (over: Record<string, unknown> = {}) => ({
  id: 'c1',
  body: 'I still think about you',
  recipient_name: 'Sam',
  anonymous: false,
  comments_enabled: true,
  is_mine: false,
  user_info: { id: 'u1', regno: 'CS-1', name: 'Ada', avatar: null },
  created_at: '2026-01-01T10:00:00Z',
  comments: [],
  heart_count: 70,
  haha_count: 30,
  whoa_count: 10,
  heartbreak_count: 5,
  comment_count: 0,
  user_reaction: null,
  report_count: 0,
  reported_by: [],
  ...over,
});

/** A promise whose settlement the test controls. */
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

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

/** The card a given confession is rendered in. */
const cardFor = (text: string) =>
  screen.getByText(text).closest('.confession-card') as HTMLElement;

describe('a reaction whose answer arrives late', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getConfessionCooldown.mockResolvedValue({ status: 'eligible', message: 'Proceed?' });
  });

  it('keeps the state of the last request when the earlier one answers after it', async () => {
    await renderFeed([confession()]);

    const first = deferred<Record<string, unknown>>();
    const second = deferred<Record<string, unknown>>();
    api.reactToConfession
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);

    const card = () => within(cardFor('I still think about you'));
    fireEvent.click(card().getByRole('button', { name: /^heart reaction/i }));   // heart
    fireEvent.click(card().getByRole('button', { name: /^haha reaction/i }));    // haha

    // The second request answers first, and its answer is the one that matters.
    await act(async () => {
      second.resolve(confession({ heart_count: 68, haha_count: 31, user_reaction: 'haha' }));
    });
    // Now the first one answers, with the state as it was before the second.
    await act(async () => {
      first.resolve(confession({ heart_count: 69, haha_count: 30, user_reaction: 'heart' }));
    });

    // The screen shows the last request, not the one that happened to answer last.
    expect(card().getByRole('button', { name: /^heart reaction \(68\)/i })).toBeInTheDocument();
    expect(card().getByRole('button', { name: /^haha reaction \(31\)/i })).toBeInTheDocument();
    expect(card().queryByRole('button', { name: /^heart reaction \(69\)/i })).not.toBeInTheDocument();
    expect(card().queryByRole('button', { name: /^haha reaction \(30\)/i })).not.toBeInTheDocument();
  });

  it('does not let a late failure undo a reaction that was already saved', async () => {
    await renderFeed([confession()]);

    const first = deferred<Record<string, unknown>>();
    const second = deferred<Record<string, unknown>>();
    api.reactToConfession
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);

    const card = () => within(cardFor('I still think about you'));
    fireEvent.click(card().getByRole('button', { name: /^heart reaction/i }));   // heart
    fireEvent.click(card().getByRole('button', { name: /^haha reaction/i }));    // haha

    await act(async () => {
      second.resolve(confession({ heart_count: 68, haha_count: 31, user_reaction: 'haha' }));
    });
    // The earlier request fails, after the later one was saved.
    await act(async () => {
      first.reject(new Error('the request failed'));
    });

    // The saved reaction is still on screen: the failure restored nothing.
    expect(card().getByRole('button', { name: /^heart reaction \(68\)/i })).toBeInTheDocument();
    expect(card().getByRole('button', { name: /^haha reaction \(31\)/i })).toBeInTheDocument();
  });
});
