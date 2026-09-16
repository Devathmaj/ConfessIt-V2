import { fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { ProfileReview } from '@/pages/ProfileReview';

/**
 * How accounts are grouped and laid out on the admin user review screen.
 *
 * The screen used to render one full-width row per account, all of them in a single list in
 * whatever order the server sent, which made it a page to scroll rather than a page to read. It is
 * now four groups in a deliberate order -- admins, moderators, participants above the removal
 * threshold, everyone else -- and each account is a card laid out in a grid.
 *
 * Two properties are pinned here, and they fail differently:
 *
 * **Which group an account lands in.** Every row must appear in exactly one group. An account
 * rendered twice would double its block button and double every count; an account rendered
 * nowhere would be invisible on the only screen an operator uses to find it. The threshold is
 * `> 5`, and the interesting case is the boundary and the sum, because the three counters are
 * stored per kind and the rule is about the total.
 *
 * **What the grouping is *not*.** Only upheld removals count. A participant reported many times
 * and cleared every time has still had nothing taken down, and putting them in the group above
 * the threshold would tell an operator the opposite of the truth.
 */

const api = vi.hoisted(() => ({
  getAdminUsers: vi.fn(),
  setAdminUserBlocked: vi.fn(),
  createAdminUser: vi.fn(),
  updateAdminUser: vi.fn(),
}));

vi.mock('@/services/api', () => api);
vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn() },
  Toaster: () => null,
}));

const ADMIN = '11111111-1111-1111-1111-111111111111';
const MODERATOR = '22222222-2222-2222-2222-222222222222';
const REPEAT = '33333333-3333-3333-3333-333333333333';
const ORDINARY = '44444444-4444-4444-4444-444444444444';

interface TallyOverrides {
  confession?: { reports: number; removals: number };
  love_note?: { reports: number; removals: number };
  message?: { reports: number; removals: number };
}

const tally = (overrides: TallyOverrides = {}) => ({
  confession: overrides.confession ?? { reports: 0, removals: 0 },
  love_note: overrides.love_note ?? { reports: 0, removals: 0 },
  message: overrides.message ?? { reports: 0, removals: 0 },
});

const admin = (overrides: Record<string, unknown> = {}) => ({
  id: ADMIN,
  regno: 'staff-1',
  name: 'Ada Admin',
  email: 'ada@school.test',
  account_type: 'admin',
  is_blocked: false,
  ...overrides,
});

const moderator = (overrides: Record<string, unknown> = {}) => ({
  id: MODERATOR,
  regno: 'staff-2',
  name: 'Mo Moderator',
  email: 'mo@school.test',
  account_type: 'moderator',
  is_blocked: false,
  ...overrides,
});

/** A participant, whose tally is what decides which of the two participant groups they join. */
const participant = (
  id: string,
  name: string,
  history: TallyOverrides = {},
  overrides: Record<string, unknown> = {},
) => ({
  id,
  regno: `student-${name}`,
  name,
  email: `${name.toLowerCase()}@school.test`,
  class_name: 'Class_9',
  account_type: 'users',
  is_blocked: false,
  report_history: tally(history),
  ...overrides,
});

/**
 * Load exactly the given accounts and wait for one of them to be on screen.
 *
 * `expected` cannot be omitted, because the page shell renders before the accounts arrive and
 * waiting on it would let an assertion run against an empty list.
 */
function renderRows(rows: unknown[], expected: string) {
  api.getAdminUsers.mockResolvedValue(rows);
  render(
    <AuthContext.Provider
      value={{
        user: { regno: 'viewer', account_type: 'admin', capabilities: [], is_blocked: false },
        isAuthenticated: true,
        loading: false,
        can: () => true,
      } as never}
    >
      <MemoryRouter>
        <ProfileReview />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
  return screen.findByText(expected);
}

/** The group an account is rendered in, found through the heading that labels the section. */
const groupNamed = (heading: string) =>
  screen.getByRole('heading', { name: heading }).closest('section') as HTMLElement;

const ADMINS = 'Admins';
const MODERATORS = 'Moderators';
const REPEATED = 'Participants with more than 5 removals';
const PARTICIPANTS = 'Participants';

describe('ProfileReview: account groups', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows the groups in the order an operator should meet them', async () => {
    await renderRows(
      [participant(ORDINARY, 'Ordinary'), moderator(), admin()],
      'Ordinary',
    );

    // The order is the point of the change, not incidental: a staff account with the wrong
    // privileges is the most consequential mistake on the page, and the participants who need a
    // decision come last.
    expect(screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent)).toEqual([
      ADMINS,
      MODERATORS,
      REPEATED,
      PARTICIPANTS,
    ]);
  });

  it('files each account into exactly one group', async () => {
    await renderRows(
      [
        admin(),
        moderator(),
        participant(REPEAT, 'Repeat', { confession: { reports: 9, removals: 6 } }),
        participant(ORDINARY, 'Ordinary'),
      ],
      'Ordinary',
    );

    expect(within(groupNamed(ADMINS)).getByText('Ada Admin')).toBeInTheDocument();
    expect(within(groupNamed(MODERATORS)).getByText('Mo Moderator')).toBeInTheDocument();
    expect(within(groupNamed(REPEATED)).getByText('Repeat')).toBeInTheDocument();
    expect(within(groupNamed(PARTICIPANTS)).getByText('Ordinary')).toBeInTheDocument();

    // And nowhere else. A row shown twice would double its block button and its counts.
    expect(within(groupNamed(ADMINS)).queryByText('Ordinary')).not.toBeInTheDocument();
    expect(within(groupNamed(MODERATORS)).queryByText('Ada Admin')).not.toBeInTheDocument();
    expect(within(groupNamed(REPEATED)).queryByText('Ordinary')).not.toBeInTheDocument();
    expect(within(groupNamed(PARTICIPANTS)).queryByText('Repeat')).not.toBeInTheDocument();

    for (const name of ['Ada Admin', 'Mo Moderator', 'Repeat', 'Ordinary']) {
      expect(screen.getAllByText(name)).toHaveLength(1);
    }
  });

  it('sums removals across every kind of content to decide the group', async () => {
    // Three confessions, two love notes and one chat message: six pieces of content taken down,
    // and not one of them six of the same kind. Reading a single counter would have put this
    // participant in the ordinary group and buried them among hundreds of accounts.
    await renderRows(
      [
        participant(REPEAT, 'Spread', {
          confession: { reports: 4, removals: 3 },
          love_note: { reports: 2, removals: 2 },
          message: { reports: 1, removals: 1 },
        }),
      ],
      'Spread',
    );

    expect(within(groupNamed(REPEATED)).getByText('Spread')).toBeInTheDocument();
    expect(within(groupNamed(PARTICIPANTS)).queryByText('Spread')).not.toBeInTheDocument();
  });

  it('leaves a participant exactly at the threshold in the ordinary group', async () => {
    // The rule is "more than", so the boundary belongs to the ordinary group. Nothing is riding on
    // the choice either way, which is exactly why a threshold needs a test: the off-by-one would
    // otherwise be found by an operator rather than by a suite.
    await renderRows(
      [participant(ORDINARY, 'Boundary', { confession: { reports: 5, removals: 5 } })],
      'Boundary',
    );

    expect(within(groupNamed(PARTICIPANTS)).getByText('Boundary')).toBeInTheDocument();
    expect(within(groupNamed(REPEATED)).queryByText('Boundary')).not.toBeInTheDocument();
  });

  it('does not promote a participant whose reports were all dismissed', async () => {
    await renderRows(
      [participant(ORDINARY, 'Cleared', { confession: { reports: 40, removals: 0 } })],
      'Cleared',
    );

    // Forty complaints and nothing upheld is a person who was cleared forty times. Grouping them
    // with the accounts above the threshold would assert something about them that no moderator
    // ever did.
    expect(within(groupNamed(PARTICIPANTS)).getByText('Cleared')).toBeInTheDocument();
    expect(within(groupNamed(REPEATED)).queryByText('Cleared')).not.toBeInTheDocument();
  });

  it('never offers the removal tally to a staff account', async () => {
    await renderRows([admin(), moderator()], 'Ada Admin');

    // Staff hold no reportable content. The group above the threshold is defined by removals, so
    // a moderator cannot be in it even if the server were to send a tally with their row.
    expect(within(groupNamed(MODERATORS)).queryByText('Report history')).not.toBeInTheDocument();
    expect(within(groupNamed(REPEATED)).queryByText('Mo Moderator')).not.toBeInTheDocument();
  });

  it('reports how many accounts are above the threshold in the header', async () => {
    await renderRows(
      [
        participant(REPEAT, 'Repeat', { confession: { reports: 6, removals: 6 } }),
        participant(ORDINARY, 'Ordinary'),
      ],
      'Ordinary',
    );

    // The count is on the header as well as on the section, so an operator knows the size of the
    // problem without scrolling to the group that holds it.
    expect(screen.getByText(/Over 5 removals: 1/)).toBeInTheDocument();
  });
});

describe('ProfileReview: accounts as cards', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('lays the accounts out as a grid of cards', async () => {
    await renderRows([participant(ORDINARY, 'One'), participant(REPEAT, 'Two')], 'One');

    const cards = ['One', 'Two'].map((name) =>
      screen.getByRole('button', { name: `View profile for ${name}` }),
    );

    // Both accounts share one multi-column container. Two full-width rows would each be the only
    // child of its own parent, which is what the grid replaced.
    for (const card of cards) {
      const grid = card.closest('.grid');
      expect(grid).not.toBeNull();
      expect(grid?.className).toMatch(/sm:grid-cols-2/);
    }
    expect(cards[0].closest('.grid')).toBe(cards[1].closest('.grid'));
  });

  it('opens the editor from anywhere on the card', async () => {
    await renderRows([participant(ORDINARY, 'One')], 'One');

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'View profile for One' }));
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    expect(screen.getByText('Edit profile')).toBeInTheDocument();
  });

  it('does not open the editor when the block button is pressed', async () => {
    api.setAdminUserBlocked.mockResolvedValue({ id: ORDINARY, is_blocked: true });
    await renderRows([participant(ORDINARY, 'One')], 'One');

    // The card body is the click target and the block button is outside it. When the button lived
    // inside the clickable row, one press both suspended the account and opened its editor.
    fireEvent.click(screen.getByRole('button', { name: /Block user/ }));

    expect(api.setAdminUserBlocked).toHaveBeenCalledWith(ORDINARY, true);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(await screen.findByText('Blocked')).toBeInTheDocument();
  });

  it('marks a card above the threshold on the card itself', async () => {
    await renderRows(
      [participant(REPEAT, 'Repeat', { confession: { reports: 7, removals: 6 } })],
      'Repeat',
    );

    // The group heading scrolls out of view; the card has to still say why it is on the page.
    const card = screen.getByRole('button', { name: 'View profile for Repeat' });
    expect(within(card).getByText('Repeated removals')).toBeInTheDocument();
    expect(within(card).getByText('6 removed')).toBeInTheDocument();
  });

  it('leaves an ordinary card unmarked', async () => {
    await renderRows([participant(ORDINARY, 'Ordinary')], 'Ordinary');

    const card = screen.getByRole('button', { name: 'View profile for Ordinary' });
    expect(within(card).queryByText('Repeated removals')).not.toBeInTheDocument();
  });
});