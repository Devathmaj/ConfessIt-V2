import { render, screen, within, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { ReportsReview } from '@/pages/ReportsReview';

/**
 * How a report is presented before a moderator opens it, and what opening it reveals.
 *
 * Two separate properties are pinned here, and it is worth saying why they are not the same
 * property. Collapsing is a density decision: the queue is read as a list, and every report
 * rendering its full body turned a page of reports into a page of walls. Context is a
 * sufficiency decision: a reported comment is a sentence with the post it answers missing, and a
 * reported chat message is a sentence with the rest of the thread missing.
 *
 * The collapsed row therefore keeps a clipped copy of the reason and of the reported content.
 * The screen is still triageable without expanding anything, which is the whole reason the
 * detail is behind a toggle rather than the reason being behind one.
 *
 * The anonymity case is the load-bearing one. Showing a comment report's parent confession is new
 * surface area, and the failure mode is specific: a comment report points at a post, so if the
 * parent is rendered carelessly the author of an anonymous confession becomes readable to staff.
 * The server withholds it; these cases make sure the screen does not reconstruct it from anything
 * else that happens to be in the payload.
 */

const api = vi.hoisted(() => ({
  getAdminConfessionReports: vi.fn(),
  getAdminLoveNoteReports: vi.fn(),
  getAdminMessageReports: vi.fn(),
  updateAdminConfessionReportStatus: vi.fn(),
  updateAdminLoveNoteReportStatus: vi.fn(),
  updateAdminMessageReportStatus: vi.fn(),
}));

vi.mock('@/services/api', () => api);
vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn() },
  Toaster: () => null,
}));

const NOW = '2026-01-15T10:00:00Z';

const person = (regno: string, name: string) => ({ regno, name });

/** A comment report on a confession that was not anonymous. */
const commentReport = {
  id: 'c0000000-0000-0000-0000-000000000001',
  target_type: 'comment',
  target_id: 'c0000000-0000-0000-0000-00000000000c',
  confession_id: 'c0000000-0000-0000-0000-0000000000cc',
  parent_recipient_name: 'Grace',
  anonymous: false,
  content: 'a remark on the post',
  content_created_at: NOW,
  author: { id: 'u2', regno: 'CS-2', name: 'Alan Turing' },
  author_hidden: false,
  reporter: person('CS-9', 'Reporting Student'),
  reason: 'rude reply',
  reported_at: NOW,
  status: 'pending',
  reviewed_at: null,
  reviewed_by_regno: null,
  content_removed: false,
  parent_confession: {
    id: 'c0000000-0000-0000-0000-0000000000cc',
    body: 'the confession this comment answers',
    recipient_name: 'Grace',
    created_at: NOW,
    comment_count: 4,
    anonymous: false,
    content_removed: false,
    author: { id: 'u1', regno: 'CS-1', name: 'Ada Lovelace' },
  },
};

/** The same report against a confession the author asked to stay anonymous. */
const anonymousParentCommentReport = {
  ...commentReport,
  id: 'c0000000-0000-0000-0000-000000000002',
  parent_confession: {
    ...commentReport.parent_confession,
    anonymous: true,
    author: null,
  },
};

const messageReport = {
  id: 'm0000000-0000-0000-0000-000000000001',
  message_id: 'm0000000-0000-0000-0000-0000000000aa',
  body: 'you know what you did',
  message_created_at: NOW,
  conversation_id: 'm0000000-0000-0000-0000-0000000000dd',
  reporter: person('CS-9', 'Reporting Student'),
  reported_user: person('CS-1', 'Ada Lovelace'),
  reason: 'threatening tone',
  reported_at: NOW,
  status: 'pending',
  reviewed_at: null,
  reviewed_by_regno: null,
  content_removed: false,
  conversation: {
    id: 'm0000000-0000-0000-0000-0000000000dd',
    status: 'accepted',
    participants: [person('CS-1', 'Ada Lovelace'), person('CS-9', 'Reporting Student')],
    message_count: 12,
    removed_message_count: 2,
  },
};

const emptyQueue = { reports: [], total: 0, skip: 0, limit: 50 };

const queue = (reports: unknown[]) => ({ reports, total: reports.length, skip: 0, limit: 50 });

function renderPage() {
  return render(
    <AuthContext.Provider
      value={
        {
          user: {
            id: 'admin-1',
            regno: 'ADMIN-1',
            name: 'Admin',
            account_type: 'admin',
            capabilities: [],
            is_blocked: false,
          },
          isAuthenticated: true,
          loading: false,
          can: () => true,
        } as never
      }
    >
      <MemoryRouter>
        <ReportsReview />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

/** The card header, which is the toggle. */
const cardToggle = (name: RegExp) => screen.getByRole('button', { name });

/** Switches queue tab.
 *
 * `mouseDown` rather than `click`: the Radix Tabs trigger activates on mouse down, so a click
 * event alone changes nothing and the tab silently stays where it was.
 */
const openTab = async (label: RegExp) => {
  const trigger = await screen.findByRole('tab', { name: label });
  fireEvent.mouseDown(trigger);
  return trigger;
};

/** The context block introduced by its heading, so assertions do not also match the collapsed row. */
const contextBlock = (heading: string): HTMLElement => {
  const element = screen.getByText(heading).closest('div');
  if (!element) throw new Error(`no block found for ${heading}`);
  return element as HTMLElement;
};

describe('ReportsReview: collapsed report cards', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getAdminConfessionReports.mockResolvedValue(queue([commentReport]));
    api.getAdminLoveNoteReports.mockResolvedValue(emptyQueue);
    api.getAdminMessageReports.mockResolvedValue(queue([messageReport]));
  });

  it('starts collapsed, with no ruling buttons and no detail on screen', async () => {
    renderPage();

    const toggle = await screen.findByRole('button', { name: /comment reported by/i });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');

    // The reason is clipped onto the row so the queue is triageable, but the reported content
    // and the parent confession are not on the page at all until the card is opened.
    expect(screen.getByText('rude reply')).toBeInTheDocument();
    expect(screen.queryByText('the confession this comment answers')).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /remove content/i }),
    ).not.toBeInTheDocument();
  });

  it('reveals the detail and the ruling buttons when opened', async () => {
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: /comment reported by/i }));

    expect(screen.getByText('the confession this comment answers')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /remove content/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /dismiss/i })).toBeInTheDocument();
  });

  it('collapses again on a second click', async () => {
    renderPage();
    const toggle = await screen.findByRole('button', { name: /comment reported by/i });

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('the confession this comment answers')).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /remove content/i }),
    ).not.toBeInTheDocument();
  });

  it('holds one card open at a time', async () => {
    api.getAdminConfessionReports.mockResolvedValue(
      queue([
        commentReport,
        { ...commentReport, id: 'c0000000-0000-0000-0000-000000000009', reason: 'a different complaint' },
      ]),
    );
    renderPage();

    const [first, second] = await screen.findAllByRole('button', { name: /comment reported by/i });
    fireEvent.click(first);
    expect(first).toHaveAttribute('aria-expanded', 'true');

    fireEvent.click(second);
    // Opening a second report closes the first: a moderator working a queue wants the next card,
    // not an ever-growing stack of open ones competing for the same space.
    expect(second).toHaveAttribute('aria-expanded', 'true');
    expect(first).toHaveAttribute('aria-expanded', 'false');
  });

  it('clips a long reason on the collapsed row and shows it whole when opened', async () => {
    const longReason = `harassment ${'and more of the same '.repeat(20)}`.trim();
    api.getAdminConfessionReports.mockResolvedValue(queue([{ ...commentReport, reason: longReason }]));
    renderPage();

    const toggle = await screen.findByRole('button', { name: /comment reported by/i });
    // The full string is genuinely absent, not merely scrolled out of view, so it cannot be
    // matched or read while the card is closed.
    expect(screen.queryByText(longReason)).not.toBeInTheDocument();
    expect(toggle.textContent).toContain('…');

    fireEvent.click(toggle);
    expect(screen.getByText(longReason)).toBeInTheDocument();
  });

  it('shows the reason when there is none to clip', async () => {
    api.getAdminConfessionReports.mockResolvedValue(queue([{ ...commentReport, reason: null }]));
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: /comment reported by/i }));
    expect(screen.getByText('No reason was given.')).toBeInTheDocument();
  });
});

describe('ReportsReview: context on a comment report', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getAdminConfessionReports.mockResolvedValue(queue([commentReport]));
    api.getAdminLoveNoteReports.mockResolvedValue(emptyQueue);
    api.getAdminMessageReports.mockResolvedValue(emptyQueue);
  });

  it('names the confession the comment was left on', async () => {
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: /comment reported by/i }));

    expect(
      screen.getByText('The confession this comment was left on'),
    ).toBeInTheDocument();

    // Scoped to the block: the author's name also appears on the collapsed row, and the point
    // here is that the *block* identifies the parent, not that a name is somewhere on the page.
    const parent = contextBlock('The confession this comment was left on');
    expect(within(parent).getByText(/the confession this comment answers/)).toBeInTheDocument();
    expect(within(parent).getByText(/Written by Ada Lovelace/)).toBeInTheDocument();
    expect(within(parent).getByText(/To Grace/)).toBeInTheDocument();
    expect(within(parent).getByText(/4 comments in total/)).toBeInTheDocument();
  });

  it('summarises the parent on the collapsed row', async () => {
    renderPage();

    const toggle = await screen.findByRole('button', { name: /comment reported by/i });
    // Enough to recognise the context without opening anything.
    expect(toggle.textContent).toContain('On a confession from Ada Lovelace');
    expect(toggle.textContent).toContain('4 comments on it');
  });

  it('does not name an anonymous parent author, and says it is anonymous instead', async () => {
    api.getAdminConfessionReports.mockResolvedValue(queue([anonymousParentCommentReport]));
    renderPage();

    const toggle = await screen.findByRole('button', { name: /comment reported by/i });
    expect(toggle.textContent).toContain('On a confession from an anonymous author');
    expect(toggle.textContent).not.toContain('Ada Lovelace');

    fireEvent.click(toggle);
    // The body is readable -- staff review means reading the post -- but the author is not, and
    // the screen states that as a fact rather than printing a blank where a name would be.
    expect(screen.getByText('the confession this comment answers')).toBeInTheDocument();
    expect(screen.getByText(/Written anonymously/)).toBeInTheDocument();
    expect(screen.queryByText(/Ada Lovelace/)).not.toBeInTheDocument();
  });

  it('says when the parent confession has already been removed', async () => {
    api.getAdminConfessionReports.mockResolvedValue(
      queue([
        {
          ...commentReport,
          parent_confession: { ...commentReport.parent_confession, content_removed: true },
        },
      ]),
    );
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: /comment reported by/i }));

    expect(screen.getByText(/parent confession has already been removed/i)).toBeInTheDocument();
  });

  it('renders a comment report whose parent context is absent', async () => {
    api.getAdminConfessionReports.mockResolvedValue(
      queue([{ ...commentReport, parent_confession: null, parent_recipient_name: null }]),
    );
    renderPage();
    const toggle = await screen.findByRole('button', { name: /comment reported by/i });

    fireEvent.click(toggle);
    expect(
      screen.queryByText('The confession this comment was left on'),
    ).not.toBeInTheDocument();
    // The report itself is still fully reviewable: a missing context block must not cost the
    // moderator the thing they came for.
    expect(screen.getByText('a remark on the post')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /remove content/i })).toBeInTheDocument();
  });
});

describe('ReportsReview: context on a message report', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getAdminConfessionReports.mockResolvedValue(emptyQueue);
    api.getAdminLoveNoteReports.mockResolvedValue(emptyQueue);
    api.getAdminMessageReports.mockResolvedValue(queue([messageReport]));
  });

  it('names the conversation and its other participant on the collapsed row', async () => {
    renderPage();
    await openTab(/chat messages/i);

    const toggle = await screen.findByRole('button', { name: /message reported by/i });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle.textContent).toContain('In a conversation with Reporting Student');
    expect(toggle.textContent).toContain('12 messages in it');
  });

  it('flags messages already removed from the conversation', async () => {
    renderPage();
    await openTab(/chat messages/i);
    fireEvent.click(await screen.findByRole('button', { name: /message reported by/i }));

    expect(
      screen.getByText('The conversation it was sent in'),
    ).toBeInTheDocument();

    // Scoped to the block, because "2 already removed" also appears on the collapsed row -- the
    // collapsed summary is meant to carry it, and that is asserted on its own case above.
    const conversation = contextBlock('The conversation it was sent in');
    // Two of twelve already gone reads as a pattern rather than a single lapse, and that is
    // the difference between dismissing a report and taking a thread down.
    expect(within(conversation).getByText(/2 already removed/)).toBeInTheDocument();
    expect(
      within(conversation).getByText(/Between Ada Lovelace and Reporting Student/),
    ).toBeInTheDocument();
  });

  it('shows when the message was sent, not only when it was reported', async () => {
    renderPage();
    await openTab(/chat messages/i);
    fireEvent.click(await screen.findByRole('button', { name: /message reported by/i }));

    // The collapsed row leads with when it was *reported* and follows with when it was *sent*;
    // the queue used to print only the first, so a message could look minutes old however long
    // ago it actually arrived.
    const toggle = screen.getByRole('button', { name: /message reported by/i });
    expect(toggle.textContent).toMatch(/Reported .* · Sent \d{2}\/\d{2}\/\d{4}/);

    const body = contextBlock('The message as it was sent');
    expect(within(body).getByText(/Sent \d{2}\/\d{2}\/\d{4}/)).toBeInTheDocument();
  });

  it('survives a report whose message row is gone', async () => {
    api.getAdminMessageReports.mockResolvedValue(
      queue([
        {
          ...messageReport,
          body: null,
          message_created_at: null,
          conversation: null,
          conversation_id: null,
        },
      ]),
    );
    renderPage();
    await openTab(/chat messages/i);

    const toggle = await screen.findByRole('button', { name: /message reported by/i });
    fireEvent.click(toggle);
    expect(screen.getByText('[The message no longer exists]')).toBeInTheDocument();
    expect(
      screen.queryByText('The conversation it was sent in'),
    ).not.toBeInTheDocument();
  });

  it('keeps the ruling buttons on the message queue reachable only when opened', async () => {
    renderPage();
    await openTab(/chat messages/i);

    const toggle = await screen.findByRole('button', { name: /message reported by/i });
    expect(
      screen.queryByRole('button', { name: /remove content/i }),
    ).not.toBeInTheDocument();

    fireEvent.click(toggle);
    const remove = screen.getByRole('button', { name: /remove content/i });
    expect(within(remove).queryByText(/threatening tone/)).not.toBeInTheDocument();
    expect(screen.getByText('threatening tone')).toBeInTheDocument();
  });
});