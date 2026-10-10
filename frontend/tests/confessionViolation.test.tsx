import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AuthContext } from '@/contexts/AuthContext';
import { ConfessionsPage } from '@/pages/ConfessionsPage';

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
  searchConfessions: vi.fn(),
}));

vi.mock('@/services/api', () => api);
vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn() },
  Toaster: () => null,
}));

const post = (id: string, body: string) => ({
  id,
  body,
  recipient_name: null,
  anonymous: false,
  comments_enabled: true,
  created_at: '2026-04-11T10:00:00.000Z',
  author_id: 'u9',
  user_info: { id: 'u9', name: 'Author' },
  report_count: 0,
  reported_by: [],
  reactions: {},
  comments: [],
  heart_count: 0,
  haha_count: 0,
  whoa_count: 0,
  heartbreak_count: 0,
  comment_count: 0,
  user_reaction: null,
});

const violationError = (message: string) => {
  const err: any = new Error(message);
  err.response = { status: 422, data: { detail: message } };
  return err;
};

/** The comment row the server hands back after a successful write. */
const commentPayload = {
  id: 'm1',
  user_info: { id: 'u1', name: 'Tester' },
  body: 'a slow reply',
  created_at: '2026-04-11T10:05:00.000Z',
  like_count: 0,
  dislike_count: 0,
  user_reaction: null,
  report_count: 0,
  reported_by: [],
};

async function renderFeed() {
  render(
    <AuthContext.Provider
      value={{
        user: { id: 'u1', name: 'Tester', regno: 'CS-1', email: 't@example.com' },
        isAuthenticated: true,
        loading: false,
        can: () => true,
      } as never}
    >
      <MemoryRouter>
        <ConfessionsPage />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
  await screen.findByText('first post');
}

describe('ConfessionsPage: violation popup', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.getConfessions as any).mockResolvedValue({ confessions: [post('c1', 'first post')], total: 1, skip: 0, limit: 50 });
    (api.getConfessionCooldown as any).mockResolvedValue({ status: 'eligible', message: '', cooldown_hours: 2, remaining_minutes: 0, remaining_seconds: 0 });
    (api.searchConfessions as any).mockResolvedValue({ keyword: { items: [], total: 0 }, semantic: { items: [], status: 'ok' } });
  });

  it('shows violation popup and does not render a flagged confession', async () => {
    (api.createConfession as any).mockRejectedValue(
      violationError('Your confession was removed because it violated our content policies.')
    );
    await renderFeed();

    fireEvent.click(screen.getByRole('button', { name: /share your heart/i }));
    fireEvent.change(screen.getByPlaceholderText(/Share what's in your heart/), {
      target: { value: 'violating confession' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^share confession$/i }));

    await screen.findByText('Content removed');
    expect(screen.getByText('Your confession was removed because it violated our content policies.')).toBeInTheDocument();
    expect(screen.queryByText('Your confession has been shared!')).not.toBeInTheDocument();
    expect(screen.queryByText('violating confession')).not.toBeInTheDocument();
  });

  it('shows violation popup and does not render a flagged comment', async () => {
    (api.createComment as any).mockRejectedValue(
      violationError('Your comment was removed because it violated our content policies.')
    );
    (api.getConfessionComments as any).mockResolvedValue({ comments: [], total: 0, skip: 0, limit: 10 });
    await renderFeed();

    fireEvent.click(screen.getByRole('button', { name: /read in full/i }));
    const dialog = await screen.findByRole('dialog');
    const { within } = await import('@testing-library/react');
    const box = within(dialog).getByPlaceholderText(/Write a thoughtful comment/i);
    fireEvent.change(box, { target: { value: 'violating comment' } });
    fireEvent.click(within(dialog).getByRole('button', { name: /send comment/i }));

    await screen.findByText('Content removed');
    expect(screen.getByText('Your comment was removed because it violated our content policies.')).toBeInTheDocument();
    expect(screen.queryByText('violating comment')).not.toBeInTheDocument();
  });
});

/**
 * Moderation and embedding run inside the write, so both composer buttons are slow enough
 * to be double-clicked. While the server is processing a submission the button is the busy
 * one: a second click is swallowed, and the label is replaced with a spinner.
 */
describe('ConfessionsPage: submissions are busy until the server answers', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.getConfessions as any).mockResolvedValue({ confessions: [post('c1', 'first post')], total: 1, skip: 0, limit: 50 });
    (api.getConfessionCooldown as any).mockResolvedValue({ status: 'eligible', message: '', cooldown_hours: 2, remaining_minutes: 0, remaining_seconds: 0 });
    (api.searchConfessions as any).mockResolvedValue({ keyword: { items: [], total: 0 }, semantic: { items: [], status: 'ok' } });
  });

  it('a second click while the confession is processing cannot post twice, and a failure re-enables the button', async () => {
    let rejectCreate!: (reason: unknown) => void;
    (api.createConfession as any).mockImplementation(
      () => new Promise((_resolve, reject) => { rejectCreate = reject; }),
    );
    await renderFeed();

    fireEvent.click(screen.getByRole('button', { name: /share your heart/i }));
    fireEvent.change(screen.getByPlaceholderText(/Share what's in your heart/), {
      target: { value: 'a slow confession' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^share confession$/i }));

    // Busy: the label is gone, and the disabled button swallows a second click.
    const busy = await screen.findByRole('button', { name: /sharing/i });
    expect(busy).toBeDisabled();
    fireEvent.click(busy);
    expect(api.createConfession).toHaveBeenCalledTimes(1);

    // A failed attempt must not leave the button dead.
    rejectCreate(new Error('network down'));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /^share confession$/i })).toBeEnabled(),
    );
  });

  it('a second click while the comment is processing cannot post it twice', async () => {
    let resolveComment!: (value: unknown) => void;
    (api.createComment as any).mockImplementation(
      () => new Promise((resolve) => { resolveComment = resolve; }),
    );
    (api.getConfessionComments as any).mockResolvedValue({ comments: [], total: 0, skip: 0, limit: 10 });
    await renderFeed();

    fireEvent.click(screen.getByRole('button', { name: /read in full/i }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByPlaceholderText(/Write a thoughtful comment/i), {
      target: { value: 'a slow reply' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: /send comment/i }));

    const busy = within(dialog).getByRole('button', { name: /send comment/i });
    expect(busy).toBeDisabled();
    fireEvent.click(busy);
    expect(api.createComment).toHaveBeenCalledTimes(1);

    // When the answer lands the composer is free again, with the box cleared.
    resolveComment(commentPayload);
    await waitFor(() =>
      expect(within(dialog).getByPlaceholderText(/Write a thoughtful comment/i)).toHaveValue(''),
    );
    expect(within(dialog).getByRole('button', { name: /send comment/i })).toBeEnabled();
  });
});
