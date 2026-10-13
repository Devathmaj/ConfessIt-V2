import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, within, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AuthContext, type User } from '@/contexts/AuthContext';
import { ConfessionsPage } from '@/pages/ConfessionsPage';

/**
 * Clicking a confession opens it.
 *
 * The cases below are as much about what must *not* open it as about what does. A reaction, a
 * report flag and an edit control all live on the same card, so a body-wide click handler that
 * swallowed them would be a worse bug than the one it replaced.
 *
 * `fireEvent` rather than `user-event`, matching the other component tests here. The one thing
 * it cannot do is synthesize Enter activating a button -- jsdom does not implement the
 * default action -- so keyboard reachability is asserted structurally (it is a real `<button>`
 * and it takes focus) instead of being faked with a keydown that proves nothing.
 */

const api = vi.hoisted(() => ({
  getConfessions: vi.fn(),
  createConfession: vi.fn(),
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
  comments: [],
  heart_count: 0,
  haha_count: 0,
  whoa_count: 0,
  heartbreak_count: 0,
  comment_count: 0,
  user_reaction: null,
  report_count: 0,
  reported_by: [],
  ...over,
});

/** A comment on a confession, with its author's identity on the payload. */
const comment = (over: Record<string, unknown> = {}) => ({
  id: 'm1',
  user_info: { id: 'someone-else', name: 'Grace' },
  body: 'a reply',
  created_at: '2026-01-01T11:00:00Z',
  like_count: 0,
  dislike_count: 0,
  user_reaction: null,
  report_count: 0,
  reported_by: [],
  ...over,
});

/**
 * A signed-out visitor, which is what makes the feed's reaction and report affordances worth
 * testing: nothing on the card belongs to the author, so nothing on it should be swallowed by
 * the body click. Supplying the context directly rather than mounting `AuthProvider` is
 * deliberate -- the session lifecycle is `signOutDestination`'s subject, and driving it here
 * would only add a token fetch that has nothing to do with what is being checked.
 */
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

const renderFeed = async (
  items: ReturnType<typeof confession>[],
  { signedIn = false }: { signedIn?: boolean } = {}
) => {
  api.getConfessions.mockResolvedValue(items);

  render(
    <AuthContext.Provider
      value={{
        ...authValue,
        user: signedIn ? { id: 'me', name: 'Me', regno: 'CS-9', email: 'me@example.com' } : null,
        isAuthenticated: signedIn,
      }}
    >
      <MemoryRouter>
        <ConfessionsPage />
      </MemoryRouter>
    </AuthContext.Provider>
  );

  // The feed arrives over an effect, so no row exists until it lands.
  await waitFor(() => expect(screen.getByText(items[0].body)).toBeInTheDocument());
};

/** The body-as-button, as opposed to the comment-count button beside it. */
const bodyButton = (text: string) => screen.getByRole('button', { name: new RegExp(text) });

/**
 * The card a given confession is rendered in.
 *
 * Scoped rather than global because the reaction buttons carry no text of their own -- they are
 * an icon and a number -- so their accessible names are bare counts, and a count is not
 * something a global query can pin down.
 */
const cardFor = (text: string) =>
  screen.getByText(text).closest('.confession-card') as HTMLElement;

describe('clicking a confession in the feed', () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(() => vi.restoreAllMocks());

  it('opens the confession, showing the body in full', async () => {
    const body = 'I still think about you';
    await renderFeed([confession({ body })]);

    fireEvent.click(bodyButton(body));

    const dialog = await screen.findByRole('dialog');
    // The dialog is the only place the body is not clamped, so finding it there is what
    // "opens the confession" has to mean.
    expect(within(dialog).getByText(body)).toBeInTheDocument();
    expect(within(dialog).getByText('Confessing to:')).toBeInTheDocument();
  });

  it('opens a confession that has no recipient', async () => {
    const body = 'anonymous words';
    await renderFeed([confession({ body, recipient_name: null })]);

    fireEvent.click(bodyButton(body));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(body)).toBeInTheDocument();
    // The row announces a missing recipient to staff; the feed should not invent one.
    expect(within(dialog).queryByText('Confessing to:')).not.toBeInTheDocument();
  });

  it('is a real button, so it is reachable and activatable by keyboard', async () => {
    // The structural form of the keyboard guarantee. A div with onClick is skipped by the tab
    // order, announces nothing as interactive, and does not respond to Enter or Space.
    const body = 'I still think about you';
    await renderFeed([confession({ body })]);

    const button = bodyButton(body);
    expect(button.tagName).toBe('BUTTON');
    // Not decoration: the compose box is a <form>, and a bare "button" there submits it.
    expect(button).toHaveAttribute('type', 'button');

    button.focus();
    expect(button).toHaveFocus();
  });

  it('tells a screen reader what opening it leads to', async () => {
    const body = 'I still think about you';
    await renderFeed([confession({ body, comment_count: 2 })]);

    // Otherwise the button's accessible name is just the confession text, which reads as
    // "this is a button that says the thing you already read".
    expect(bodyButton(body)).toHaveAccessibleName(/read in full and see 2 comments/i);
  });

  it('does not open when a reaction is clicked', async () => {
    const body = 'I still think about you';
    // Distinct counts so each reaction button has its own accessible name, and signed in,
    // because a reaction is refused to an anonymous visitor and the write is what has to
    // happen for this to be a real conflict with the body handler.
    await renderFeed(
      [confession({ body, heart_count: 7, haha_count: 3, whoa_count: 1, heartbreak_count: 0 })],
      { signedIn: true }
    );
    api.reactToConfession.mockResolvedValue(undefined);

    fireEvent.click(within(cardFor(body)).getByRole('button', { name: '7' }));

    // A reaction is a write. If it also opened the confession, every tap on a reaction would
    // throw the reader into a dialog they did not ask for.
    expect(api.reactToConfession).toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('does not open when a reaction is refused to an anonymous visitor', async () => {
    const body = 'I still think about you';
    await renderFeed([confession({ body, heart_count: 7 })]);
    api.reactToConfession.mockResolvedValue(undefined);

    fireEvent.click(within(cardFor(body)).getByRole('button', { name: '7' }));

    expect(api.reactToConfession).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('does not open the confession when the report flag is clicked', async () => {
    const body = 'I still think about you';
    await renderFeed([confession({ body })], { signedIn: true });
    api.reportConfession.mockResolvedValue(undefined);

    // The flag is the header's only button when nobody is signed in as the author. It carries
    // no accessible name of its own -- an icon and no label -- so it is found structurally.
    const flag = within(cardFor(body)).getAllByRole('button')[0];
    expect(flag).not.toBe(bodyButton(body));

fireEvent.click(flag);

    // The click reached the report flow. Asserting only "no confession dialog" would also be
    // satisfied by a flag wired to nothing at all.
    //
    // Scoping to whichever dialog is open is what makes the second half meaningful: the
    // confession's own body would be inside that dialog if the body handler had fired too.
    // Note the regex -- getByText matches a node's entire text by default, and this
    // description continues "...This action cannot be undone.", so an exact-string lookup
    // silently finds nothing.
    const opened = await screen.findByRole('dialog');
    expect(within(opened).getByText(/Are you sure you want to report this confession\?/)).
      toBeInTheDocument();
    expect(within(opened).queryByText(body)).not.toBeInTheDocument();
  });

  it('still opens from the comment-count button, the way it always did', async () => {
    const body = 'I still think about you';
    await renderFeed([confession({ body, comment_count: 5 })]);

    fireEvent.click(within(cardFor(body)).getByRole('button', { name: '5' }));

    expect(await screen.findByRole('dialog')).toBeInTheDocument();
  });

  it('opens the right one when several confessions are on the page', async () => {
    // A shared handler closing over the wrong confession would open the first row every time.
    const first = 'the first confession';
    const second = 'the second confession';
    await renderFeed([
      confession({ id: 'c1', body: first }),
      confession({ id: 'c2', body: second }),
    ]);

    fireEvent.click(bodyButton(second));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(second)).toBeInTheDocument();
    expect(within(dialog).queryByText(first)).not.toBeInTheDocument();
  });

  it('does not open when the click was the end of a text selection', async () => {
    const body = 'I still think about you';
    await renderFeed([confession({ body })]);

    // Dragging across a confession to copy a line ends in a click on it. Opening the dialog
    // there would make the text uncopyable, so a non-collapsed selection suppresses it.
    vi.spyOn(window, 'getSelection').mockReturnValue({
      isCollapsed: false,
      toString: () => 'I still think',
    } as unknown as Selection);

    fireEvent.click(bodyButton(body));

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('opens on a plain click, where there is no selection', async () => {
    const body = 'I still think about you';
    await renderFeed([confession({ body })]);

    vi.spyOn(window, 'getSelection').mockReturnValue({
      isCollapsed: true,
      toString: () => '',
    } as unknown as Selection);

    fireEvent.click(bodyButton(body));

    expect(await screen.findByRole('dialog')).toBeInTheDocument();
  });
});

/**
 * Reporting is for other people's writing.
 *
 * A self-report has nothing for a moderator to rule on and would let someone move the
 * counters on their own account, so the server refuses it -- and the flag is not offered
 * on content the viewer wrote, on the post or on their comments. Everyone else's content
 * keeps the flag exactly as before.
 */
describe('who can be reported', () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(() => vi.restoreAllMocks());

  it('offers no report flag on a confession the viewer wrote, only the edit control', async () => {
    const body = 'my own words';
    await renderFeed([confession({ body, is_mine: true })], { signedIn: true });

    const card = cardFor(body);
    // The flag carries no accessible name of its own, so it is found structurally, the same
    // way the click tests above find it.
    expect(card.querySelector('.lucide-flag')).toBeNull();
    // The viewer's own action on their post is edit, which must still be there.
    expect(card.querySelector('.lucide-edit')).not.toBeNull();
  });

  it('offers no report flag on a comment the viewer wrote, and keeps it on everyone else\'s', async () => {
    const body = 'a discussed post';
    await renderFeed(
      [
        confession({
          body,
          is_mine: false,
          comment_count: 2,
          comments: [
            comment({ id: 'm-mine', body: 'my own reply', user_info: { id: 'me', name: 'Me' } }),
            comment({ id: 'm-theirs', body: 'their reply' }),
          ],
        }),
      ],
      { signedIn: true }
    );

    // The comment-count button is the way into the thread; reaction counts are all zero, so
    // "2" names only it.
    fireEvent.click(within(cardFor(body)).getByRole('button', { name: '2' }));
    const dialog = await screen.findByRole('dialog');

    // Each row is the body paragraph's parent.
    const ownRow = within(dialog).getByText('my own reply').closest('div') as HTMLElement;
    const theirRow = within(dialog).getByText('their reply').closest('div') as HTMLElement;
    expect(ownRow.querySelector('.lucide-flag')).toBeNull();
    expect(theirRow.querySelector('.lucide-flag')).not.toBeNull();
  });
});
