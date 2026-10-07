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
