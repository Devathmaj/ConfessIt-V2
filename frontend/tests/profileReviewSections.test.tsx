import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { ProfileReview } from '@/pages/ProfileReview';
import {
  ADMIN_DEPARTMENT_MAX,
  ADMIN_NAME_MAX,
  ADMIN_REGNO_MAX,
} from '@/lib/limits';

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
 * **Which group an account lands in.** Staff land in exactly one group, because their sections
 * name a table. The flagged participant is the one deliberate duplicate: they are in the queue
 * above the threshold and in the Participants list, which is the whole population. An account
 * rendered nowhere would be invisible on the only screen an operator uses to find it. The
 * threshold is `> 5`, and the interesting case is the boundary and the sum, because the three
 * counters are stored per kind and the rule is about the total.
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

/**
 * Hoisted alongside the API so a rejected create can be asserted on by its message.
 *
 * `warning` is unused now that an admin cannot upload a picture: a profile photo is the owner's
 * own business and goes through `/profile/upload-picture`. It stays mocked because a toast the
 * component does not call should still be assertable as *not* called.
 */
const toast = vi.hoisted(() => ({
  info: vi.fn(),
  error: vi.fn(),
  success: vi.fn(),
  warning: vi.fn(),
}));

vi.mock('@/services/api', () => api);
vi.mock('sonner', () => ({ toast, Toaster: () => null }));

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

/** A participant, whose tally decides whether they also join the flagged queue. */
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
  year: 2,
  department: 'Class_9',
  account_type: 'users',
  is_blocked: false,
  report_history: tally(history),
  ...overrides,
});

/**
 * Load exactly the given accounts and wait for one of them to be on screen.
 *
 * `expected` cannot be omitted, because the page shell renders before the accounts arrive and
 * waiting on it would let an assertion run against an empty list. `findAllByText` rather than
 * `findByText`: a name above the threshold is rendered twice on purpose, in the queue and in
 * the Participants list, and a singular find would mistake the duplication for a failure.
 *
 * `rows` is either the literal answer every read should give, or a function answering each
 * read from its params. The function form is how the page-envelope shape is simulated: the
 * real server answers the four reads with different rows, and a single literal would put the
 * same accounts in every section.
 */
function renderRows(
  rows: unknown[] | ((params: Record<string, unknown>) => unknown),
  expected: string,
  viewerId?: string,
) {
  if (typeof rows === 'function') {
    api.getAdminUsers.mockImplementation(rows as never);
  } else {
    api.getAdminUsers.mockResolvedValue(rows);
  }
  render(
    <AuthContext.Provider
      value={{
        user: {
          // The viewer's id decides whether their own admin card carries an editor. Omitted
          // by default, which is the fail-closed reading the page takes: no id, no match, no
          // admin card editable (see `canEditProfile`).
          ...(viewerId ? { id: viewerId } : {}),
          regno: 'viewer',
          account_type: 'admin',
          capabilities: [],
          is_blocked: false,
        },
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
  return screen.findAllByText(expected);
}

/** The group an account is rendered in, found through the heading that labels the section. */
const groupNamed = (heading: string) =>
  screen.getByRole('heading', { name: heading }).closest('section') as HTMLElement;

/**
 * Picks `option` from the <Select> labelled `label`.
 *
 * Gender and year both stopped being text boxes when their vocabularies were closed, and the old
 * `fireEvent.change(trigger, { value: 'female' })` has no meaning against a Radix trigger: a
 * <div role="combobox"> holds no value to change, so the test would pass a click that went
 * nowhere and then assert against a control that never moved. The trigger opens on pointer down
 * and the option is a separate element in a portal, so both steps are needed.
 *
 * The gender calls that skipped this did not fail, which was the trap: gender used to default to
 * 'other', so a change that never happened was indistinguishable from the one the test meant to
 * make. It now defaults to nothing, so the same mistake fails loudly -- the form stays invalid
 * and the request is never sent.
 */
async function chooseOption(dialog: HTMLElement, option: string, label = 'Gender *') {
  // `pointerType` is not decoration. Radix reads it to decide whether this is a mouse-like
  // interaction, and without it the trigger stays shut -- leaving "unable to find option" as the
  // only symptom, with no hint that the first click never reached the control.
  fireEvent.pointerDown(within(dialog).getByLabelText(label), {
    button: 0,
    ctrlKey: false,
    pointerType: 'mouse',
  });
  fireEvent.click(await screen.findByRole('option', { name: option }));
}

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

  it('files staff once and a flagged participant into both participant groups', async () => {
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
    // Above the threshold puts a participant in the queue, not out of the population:
    // they belong in Participants as well, by name like everybody else.
    expect(within(groupNamed(PARTICIPANTS)).getByText('Repeat')).toBeInTheDocument();

    // Each badge counts its own list. The legacy answers repeat the full list to every
    // read, so an unguarded server total would print "4 accounts" on all four; the
    // Participants badge holds two because the flagged participant is counted there too.
    for (const heading of [ADMINS, MODERATORS, REPEATED]) {
      expect(within(groupNamed(heading)).getByText(/^1 account$/)).toBeInTheDocument();
    }
    expect(within(groupNamed(PARTICIPANTS)).getByText('2 accounts')).toBeInTheDocument();

    // And nowhere they do not belong. Staff are table-bound, so each appears once; the
    // flagged participant is the one deliberate duplicate.
    expect(within(groupNamed(ADMINS)).queryByText('Ordinary')).not.toBeInTheDocument();
    expect(within(groupNamed(MODERATORS)).queryByText('Ada Admin')).not.toBeInTheDocument();
    expect(within(groupNamed(REPEATED)).queryByText('Ordinary')).not.toBeInTheDocument();
    expect(within(groupNamed(PARTICIPANTS)).queryByText('Ada Admin')).not.toBeInTheDocument();

    for (const name of ['Ada Admin', 'Mo Moderator', 'Ordinary']) {
      expect(screen.getAllByText(name)).toHaveLength(1);
    }
    expect(screen.getAllByText('Repeat')).toHaveLength(2);
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
    // In Participants too: the queue is a view of the same population, so the row is
    // never moved out of the list an operator scans for somebody.
    expect(within(groupNamed(PARTICIPANTS)).getByText('Spread')).toBeInTheDocument();
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

    // Scoped to Participants because the flagged row is rendered in both participant groups.
    const cards = ['One', 'Two'].map((name) =>
      within(groupNamed(PARTICIPANTS)).getByRole('button', {
        name: `Edit profile for ${name}`,
      }),
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

  it('opens the editor from the explicit edit button and nowhere else', async () => {
    await renderRows([participant(ORDINARY, 'One')], 'One');

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    // The card body is read-only: clicking the name to read or copy it must go nowhere.
    fireEvent.click(screen.getByText('One'));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Edit profile for One' }));
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    expect(screen.getByText('Edit profile')).toBeInTheDocument();
  });

  it('opens the editor with no field focused or selected', async () => {
    await renderRows([participant(ORDINARY, 'One')], 'One');

    fireEvent.click(screen.getByRole('button', { name: 'Edit profile for One' }));
    const dialog = await screen.findByRole('dialog');

    // Radix focuses the first tabbable element when a dialog opens, which here was the Full
    // name box: opening the editor put a cursor in the field an operator is usually not there
    // to change. Focus now lands on the dialog itself -- inside the modal, no field active,
    // nothing selected -- and the operator reaches for the field they came for.
    expect(dialog).toHaveFocus();
    expect(within(dialog).getByLabelText('Full name')).not.toHaveFocus();
    expect(within(dialog).getByLabelText('Register number')).not.toHaveFocus();
    expect(within(dialog).getByLabelText('Email')).not.toHaveFocus();
  });

  it('does not open the editor when the block button is pressed', async () => {
    api.setAdminUserBlocked.mockResolvedValue({ id: ORDINARY, is_blocked: true });
    await renderRows([participant(ORDINARY, 'One')], 'One');

    // The block button and the editor are separate controls, and one press must only ever do
    // the thing it says. When the block button lived inside a clickable card body, one press
    // both suspended the account and opened its editor.
    fireEvent.click(screen.getByRole('button', { name: /Block user/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Block account' }));

    await waitFor(() =>
      expect(api.setAdminUserBlocked).toHaveBeenCalledWith(ORDINARY, true),
    );
    expect(await screen.findByText('Blocked')).toBeInTheDocument();
  });

  it('asks before blocking, and does nothing until the warning is confirmed', async () => {
    api.setAdminUserBlocked.mockResolvedValue({ id: ORDINARY, is_blocked: true });
    await renderRows([participant(ORDINARY, 'One')], 'One');

    fireEvent.click(screen.getByRole('button', { name: /Block user/ }));

    // The whole point: one click warns and stops. Nothing has been sent yet, so a mis-click on a
    // list being read through quickly cannot end somebody's access to the platform.
    expect(await screen.findByText('Block this account?')).toBeInTheDocument();
    expect(api.setAdminUserBlocked).not.toHaveBeenCalled();

    // The warning has to say what actually happens, not just ask. Blocking revokes a live
    // session immediately, which is the part an operator is most likely not to expect.
    const warning = screen.getByText(/signed out immediately/i);
    expect(warning).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Block account' }));

    await waitFor(() =>
      expect(api.setAdminUserBlocked).toHaveBeenCalledWith(ORDINARY, true),
    );
  });

  it('sends nothing at all when the block warning is cancelled', async () => {
    api.setAdminUserBlocked.mockResolvedValue({ id: ORDINARY, is_blocked: true });
    await renderRows([participant(ORDINARY, 'One')], 'One');

    fireEvent.click(screen.getByRole('button', { name: /Block user/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }));

    await waitFor(() =>
      expect(screen.queryByText('Block this account?')).not.toBeInTheDocument(),
    );
    expect(api.setAdminUserBlocked).not.toHaveBeenCalled();
    // Still unblocked on screen, so a cancelled warning leaves nothing behind.
    expect(screen.queryByText('Blocked')).not.toBeInTheDocument();
  });

  it('confirms unblocking separately, and says it restores access', async () => {
    api.setAdminUserBlocked.mockResolvedValue({ id: ORDINARY, is_blocked: false });
    await renderRows(
      [participant(ORDINARY, 'One', {}, { is_blocked: true })],
      'One',
    );

    fireEvent.click(screen.getByRole('button', { name: /Unblock user/ }));

    // Wording is not shared with blocking: the two are opposite outcomes, and an operator who
    // misread which one they were about to confirm should have been told first.
    expect(await screen.findByText('Unblock this account?')).toBeInTheDocument();
    expect(screen.queryByText('Block this account?')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Unblock account' })).toBeInTheDocument();
    expect(api.setAdminUserBlocked).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Unblock account' }));

    await waitFor(() =>
      expect(api.setAdminUserBlocked).toHaveBeenCalledWith(ORDINARY, false),
    );
  });

  it('flips a participant card between Block and Unblock with no refetch when the server pages', async () => {
    // Against the real server every read is a page envelope, so the two participant sections
    // render from the per-read rows rather than the merged list. The block flip has to reach
    // those copies too: updating only the merged list left the card offering the old action
    // until a page change or a refresh refetched it.
    const envelope = (users: unknown[]) => ({ users, total: users.length, skip: 0, limit: 51 });
    await renderRows(
      (params) =>
        params?.account_type === 'users' && params.flagged !== 'flagged'
          ? envelope([participant(ORDINARY, 'One')])
          : envelope([]),
      'One',
    );
    api.setAdminUserBlocked.mockResolvedValue({ id: ORDINARY, is_blocked: true });
    const reads = api.getAdminUsers.mock.calls.length;

    fireEvent.click(screen.getByRole('button', { name: /Block user/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Block account' }));

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Unblock user/ })).toBeInTheDocument(),
    );
    expect(screen.queryByRole('button', { name: /Block user/ })).not.toBeInTheDocument();
    expect(await screen.findByText('Blocked')).toBeInTheDocument();
    // The flip is local state, not a refetch: the four reads that loaded the page are all
    // there has ever been.
    expect(api.getAdminUsers).toHaveBeenCalledTimes(reads);

    // And back again, in the same sitting: the toggle is two-way without a reload.
    api.setAdminUserBlocked.mockResolvedValue({ id: ORDINARY, is_blocked: false });
    fireEvent.click(screen.getByRole('button', { name: /Unblock user/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Unblock account' }));

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Block user/ })).toBeInTheDocument(),
    );
    expect(screen.queryByText('Blocked')).not.toBeInTheDocument();
    expect(api.getAdminUsers).toHaveBeenCalledTimes(reads);
  });

  it('offers no block button for an admin, because the database refuses it', async () => {
    await renderRows([admin()], 'Ada Admin');

    // Not merely disabled: absent, with the reason stated. The rule is a CHECK constraint on the
    // admin table, so there is no call this page could make that would succeed.
    expect(screen.queryByRole('button', { name: /Block user/ })).not.toBeInTheDocument();
    expect(screen.getByText(/Admins cannot be blocked/i)).toBeInTheDocument();
    expect(api.setAdminUserBlocked).not.toHaveBeenCalled();
  });

  it("offers the editor on the viewer's own admin card and not a colleague's", async () => {
    const colleague = admin({
      id: '99999999-9999-9999-9999-999999999999',
      regno: 'staff-2',
      name: 'Otto Admin',
      email: 'otto@school.test',
    });
    // The viewer is Ada. Only the target admin's identity decides -- the caller is an admin
    // either way -- so Ada's card is editable and Otto's is not, in the same render.
    await renderRows([admin(), colleague], 'Ada Admin', ADMIN);

    expect(
      within(groupNamed(ADMINS)).getByRole('button', { name: 'Edit profile for Ada Admin' }),
    ).toBeInTheDocument();

    // Absent rather than disabled, with the reason in its place -- the treatment the block
    // button gets, for the same reason: a control that can only come back a 403 is worse than
    // none. The server enforces the rule either way (see the backend suite).
    expect(
      screen.queryByRole('button', { name: 'Edit profile for Otto Admin' }),
    ).not.toBeInTheDocument();
    const colleagueCard = screen.getByText('Otto Admin').closest('[data-slot="card"]') as HTMLElement;
    expect(
      within(colleagueCard).getByText(/Admins can edit only their own profile/i),
    ).toBeInTheDocument();
  });

  it('leaves every admin card uneditable when the viewer is not named', async () => {
    // The page cannot know whether an unnamed viewer is the holder, and "might be somebody
    // else's" reads as not theirs: the server would refuse the write anyway, so the screen
    // does not offer it.
    await renderRows([admin()], 'Ada Admin');

    expect(screen.queryByRole('button', { name: /Edit profile for/ })).not.toBeInTheDocument();
    expect(screen.getByText(/Admins can edit only their own profile/i)).toBeInTheDocument();
  });

  it('still offers unblock for an admin, since a legacy blocked admin has to be recoverable', async () => {
    api.setAdminUserBlocked.mockResolvedValue({ id: ADMIN, is_blocked: false });
    await renderRows([admin({ is_blocked: true })], 'Ada Admin');

    fireEvent.click(screen.getByRole('button', { name: /Unblock user/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Unblock account' }));

    await waitFor(() =>
      expect(api.setAdminUserBlocked).toHaveBeenCalledWith(ADMIN, false),
    );
  });

  it('still offers block for a moderator, who is not an admin', async () => {
    api.setAdminUserBlocked.mockResolvedValue({ id: MODERATOR, is_blocked: true });
    await renderRows([moderator()], 'Mo Moderator');

    // Folding moderators in with admins would hide a control that the server does allow.
    fireEvent.click(screen.getByRole('button', { name: /Block user/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Block account' }));

    await waitFor(() =>
      expect(api.setAdminUserBlocked).toHaveBeenCalledWith(MODERATOR, true),
    );
  });

  it('marks a card above the threshold on the card itself', async () => {
    await renderRows(
      [participant(REPEAT, 'Repeat', { confession: { reports: 7, removals: 6 } })],
      'Repeat',
    );

    // The group heading scrolls out of view; the card has to still say why it is on the page.
    // The edit button names the card, and the card root is what carries the marks.
    const card = within(groupNamed(REPEATED))
      .getByRole('button', { name: 'Edit profile for Repeat' })
      .closest('[data-slot="card"]') as HTMLElement;
    expect(within(card).getByText('Repeated removals')).toBeInTheDocument();
    expect(within(card).getByText('6 removed')).toBeInTheDocument();

    // The mark follows the account, not the section: the same row in Participants carries
    // it too, because the tally is what is worth a decision wherever the card is read.
    const listed = within(groupNamed(PARTICIPANTS))
      .getByRole('button', { name: 'Edit profile for Repeat' })
      .closest('[data-slot="card"]') as HTMLElement;
    expect(within(listed).getByText('Repeated removals')).toBeInTheDocument();
  });

  it('leaves an ordinary card unmarked', async () => {
    await renderRows([participant(ORDINARY, 'Ordinary')], 'Ordinary');

    const card = screen
      .getByRole('button', { name: 'Edit profile for Ordinary' })
      .closest('[data-slot="card"]') as HTMLElement;
    expect(within(card).queryByText('Repeated removals')).not.toBeInTheDocument();
  });
});

describe('ProfileReview: creating an account from its own group', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('offers no single create button above the groups', async () => {
    await renderRows([admin(), moderator()], 'Ada Admin');

    // One button used to open one form that asked for a participant's class and a moderator's
    // regno in the same breath. The operator had to remember which of the three kinds they were
    // making before they could fill it in.
    expect(screen.queryByRole('button', { name: 'New account' })).not.toBeInTheDocument();
  });

  it('puts each create button in the group it creates into', async () => {
    await renderRows([admin(), moderator(), participant(ORDINARY, 'Ordinary')], 'Ordinary');

    expect(within(groupNamed(ADMINS)).queryByRole('button', { name: /Create/ })).not.toBeInTheDocument();
    expect(
      within(groupNamed(MODERATORS)).getByRole('button', { name: 'Create moderator' }),
    ).toBeInTheDocument();
    expect(
      within(groupNamed(PARTICIPANTS)).getByRole('button', { name: 'Create participant' }),
    ).toBeInTheDocument();
  });

  it('keeps a create button reachable when its group is empty', async () => {
    await renderRows([participant(ORDINARY, 'Ordinary')], 'Ordinary');

    // The most likely moment to need the button is before any moderator exists. A control that
    // lives inside the grid it fills would be unreachable exactly then.
    expect(within(groupNamed(MODERATORS)).getByText('No moderator accounts exist yet.'))
      .toBeInTheDocument();
    expect(
      within(groupNamed(MODERATORS)).getByRole('button', { name: 'Create moderator' }),
    ).toBeInTheDocument();
  });

  it('asks a moderator for nothing a moderator has no column for', async () => {
    await renderRows([moderator()], 'Mo Moderator');

    fireEvent.click(screen.getByRole('button', { name: 'Create moderator' }));
    const dialog = await screen.findByRole('dialog');

    // The `moderator` table has no year, department, gender, bio, interests, matchmaking or
    // love-note columns. Offering those inputs asked for values the server silently dropped.
    expect(
      within(dialog).getByRole('heading', { name: 'Create moderator account' }),
    ).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Full name *')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Register number *')).toBeInTheDocument();
    expect(within(dialog).queryByLabelText('Gender *')).not.toBeInTheDocument();
    // Both halves of the split, not just the one that used to be asked for: a moderator needs
    // neither, and offering the department alone would invite an answer with nowhere to go.
    expect(within(dialog).queryByLabelText('Year *')).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText('Department/Division *')).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText('Bio')).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText('Interests')).not.toBeInTheDocument();
    expect(within(dialog).queryByText('Allow matchmaking')).not.toBeInTheDocument();
    expect(within(dialog).queryByText('Receive love notes')).not.toBeInTheDocument();
  });

  it('offers no account-type choice inside the form', async () => {
    await renderRows([moderator()], 'Mo Moderator');

    // Which kind of account is being made is decided by the button that opened the form, so the
    // form cannot contradict it. This is also why an admin cannot be created from here.
    fireEvent.click(screen.getByRole('button', { name: 'Create moderator' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).queryByLabelText(/Account type/)).not.toBeInTheDocument();
  });

  it('creates a moderator from the shared fields alone', async () => {
    api.createAdminUser.mockResolvedValue({
      id: '55555555-5555-5555-5555-555555555555',
      regno: 'staff-9',
      name: 'New Mod',
      account_type: 'moderator',
    });
    await renderRows([moderator()], 'Mo Moderator');

    fireEvent.click(screen.getByRole('button', { name: 'Create moderator' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Full name *'), {
      target: { value: 'New Mod' },
    });
    fireEvent.change(within(dialog).getByLabelText('Register number *'), {
      target: { value: 'staff-9' },
    });
    fireEvent.change(within(dialog).getByLabelText('Email *'), {
      target: { value: 'newmod@school.test' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create moderator account' }));

    await waitFor(() => expect(api.createAdminUser).toHaveBeenCalledTimes(1));
    const payload = api.createAdminUser.mock.calls[0][0] as Record<string, unknown>;
    expect(payload).toMatchObject({
      name: 'New Mod',
      regno: 'staff-9',
      // Email is required of a staff account too. It used to be omitted here, which is how a
      // moderator ended up with no address anybody could reach.
      email: 'newmod@school.test',
      account_type: 'moderator',
    });
    // Nothing participant-only was sent, because nothing participant-only was collected. Sending
    // empty values would rely on the server dropping them, which is how they got sent at all.
    // `gender` is in this list: the create form no longer offers it for a staff account, because
    // neither staff table has the column to write it into.
    for (const key of [
      'gender',
      'year',
      'department',
      'bio',
      'interests',
      'matchmaking_enabled',
      'love_notes_receive_enabled',
      'love_notes_send_enabled',
    ]) {
      expect(payload).not.toHaveProperty(key);
    }
  });

  it('still requires a year and a department from a participant, and names the field when it is missing', async () => {
    await renderRows([participant(ORDINARY, 'Ordinary')], 'Ordinary');

    fireEvent.click(screen.getByRole('button', { name: 'Create participant' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByLabelText('Email *')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Year *')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Department/Division *')).toBeInTheDocument();
    // Matching is the participant's own setting, so it is not asked for here even on a
    // participant create.
    expect(within(dialog).queryByText('Allow matchmaking')).not.toBeInTheDocument();

    fireEvent.change(within(dialog).getByLabelText('Full name *'), {
      target: { value: 'New Student' },
    });
    fireEvent.change(within(dialog).getByLabelText('Register number *'), {
      target: { value: '21CS999' },
    });
    await chooseOption(dialog, 'Other');
    fireEvent.change(within(dialog).getByLabelText('Email *'), {
      target: { value: 'new@school.test' },
    });
    // Year left blank on purpose. It has no default, so this is the state an operator who filled
    // in everything else but forgot the year is actually in.
    fireEvent.change(within(dialog).getByLabelText('Department/Division *'), {
      target: { value: 'CSE' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create participant account' }));

    // Year and department are required of a participant and there is no column for either on a
    // moderator, so the same form cannot ask for both. "Please fill in all required fields" was
    // true and useless: it could not say which of the six fields was the one missing. The message
    // now sits under the field instead of in a toast, so it travels with the control it is about.
    expect(api.createAdminUser).not.toHaveBeenCalled();
    const yearMessage = await within(dialog).findByText('Choose a year of study.');
    expect(yearMessage).toBeInTheDocument();
    // It is the year that is flagged, not the department filled in three lines above, and the
    // message is announced rather than only coloured red.
    expect(within(dialog).getByLabelText('Year *')).toHaveAttribute('aria-invalid', 'true');
    expect(within(dialog).getByLabelText('Department/Division *')).not.toHaveAttribute(
      'aria-invalid',
      'true',
    );
    expect(yearMessage).toHaveAttribute('role', 'alert');
  });

  it('leaves gender unselected on a new account instead of guessing one', async () => {
    await renderRows([participant(ORDINARY, 'Ordinary')], 'Ordinary');

    fireEvent.click(screen.getByRole('button', { name: 'Create participant' }));
    const dialog = await screen.findByRole('dialog');

    // Nothing about the person is known yet, so the honest control is an empty one. "Other" was
    // both a real stored value and what a pre-filled select showed, so the two were
    // indistinguishable on screen and an operator could not tell a guess from a fact.
    expect(within(dialog).getByLabelText('Gender *')).toHaveTextContent('Select a gender');
  });

  it('reports every empty field of a new account at once, each under its own control', async () => {
    await renderRows([participant(ORDINARY, 'Ordinary')], 'Ordinary');

    fireEvent.click(screen.getByRole('button', { name: 'Create participant' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create participant account' }));

    // One round-trip, all the missing fields named. Previously this surfaced as a single toast
    // that dismissed itself while the operator was still reading the list.
    const alerts = within(dialog).getAllByRole('alert').map((node) => node.textContent);
    expect(alerts).toEqual(
      expect.arrayContaining([
        'Enter the full name.',
        'Enter the register number.',
        'Choose a gender.',
        'Enter an email address, for example name@example.com.',
        'Choose a year of study.',
        'Enter the department or division.',
      ]),
    );
  });

  it('clears a field error as soon as the operator fixes that field', async () => {
    await renderRows([moderator()], 'Mo Moderator');

    fireEvent.click(screen.getByRole('button', { name: 'Create moderator' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create moderator account' }));
    await within(dialog).findByText('Enter an email address, for example name@example.com.');

    fireEvent.change(within(dialog).getByLabelText('Email *'), {
      target: { value: 'new@school.test' },
    });

    // The stale error must go when it stops being true, or it reads as a fresh verdict on a value
    // that is now fine.
    await waitFor(() =>
      expect(
        within(dialog).queryByText('Enter an email address, for example name@example.com.'),
      ).not.toBeInTheDocument(),
    );
    expect(within(dialog).getByLabelText('Email *')).not.toHaveAttribute('aria-invalid', 'true');
  });

  it('re-judges an address that was valid and has been backspaced into', async () => {
    await renderRows([moderator()], 'Mo Moderator');

    fireEvent.click(screen.getByRole('button', { name: 'Create moderator' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Email *'), {
      target: { value: 'new@school.test' },
    });
    // One submit to establish that the rest of the form is now out of the way.
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create moderator account' }));
    expect(
      within(dialog).queryByText('Enter an email address, for example name@example.com.'),
    ).not.toBeInTheDocument();

    // Backspacing out of a valid address. Editing clears the stale verdict, and the next submit
    // replaces it with the specific one -- rather than the field going unflagged indefinitely.
    fireEvent.change(within(dialog).getByLabelText('Email *'), { target: { value: 'new@' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create moderator account' }));

    await within(dialog).findByText('Add the domain after the @, for example example.com.');
    expect(api.createAdminUser).not.toHaveBeenCalled();
  });

  it('refuses to send an address the browser accepts but the mail server would reject', async () => {
    await renderRows([moderator()], 'Mo Moderator');

    fireEvent.click(screen.getByRole('button', { name: 'Create moderator' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Full name *'), {
      target: { value: 'New Mod' },
    });
    fireEvent.change(within(dialog).getByLabelText('Register number *'), {
      target: { value: 'staff-9' },
    });
    // No gender step here: a moderator create has no gender control to fill in on the way to a
    // malformed address.
    // No @ at all, so <input type="email"> would have raised a native tooltip and the form would
    // have submitted nothing at all -- with no message in the dialog and none in the app either.
    fireEvent.change(within(dialog).getByLabelText('Email *'), {
      target: { value: 'not-an-address' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create moderator account' }));

    expect(api.createAdminUser).not.toHaveBeenCalled();
    expect(
      await within(dialog).findByText('An email address needs an @, for example name@example.com.'),
    ).toBeInTheDocument();
  });

  it('tells the difference between a blank email and a malformed one', async () => {
    await renderRows([moderator()], 'Mo Moderator');

    fireEvent.click(screen.getByRole('button', { name: 'Create moderator' }));
    const dialog = await screen.findByRole('dialog');
    // The dialog is opened without touching any field, so every message on screen is the empty
    // case rather than the typo case.
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create moderator account' }));

    const blank = within(dialog).getAllByRole('alert').map((node) => node.textContent);
    expect(blank).toContain('Enter an email address, for example name@example.com.');

    // "Enter an email address" for "name@" would be true but useless: the operator did enter one.
    // They need to hear which part is wrong.
    fireEvent.change(within(dialog).getByLabelText('Email *'), {
      target: { value: 'name@' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create moderator account' }));

    expect(
      await within(dialog).findByText('Add the domain after the @, for example example.com.'),
    ).toBeInTheDocument();
    expect(api.createAdminUser).not.toHaveBeenCalled();
  });

  it('creates a participant once its own fields are filled in', async () => {
    api.createAdminUser.mockResolvedValue({
      id: '66666666-6666-6666-6666-666666666666',
      regno: '21CS999',
      name: 'New Student',
      account_type: 'users',
    });
    await renderRows([participant(ORDINARY, 'Ordinary')], 'Ordinary');

    fireEvent.click(screen.getByRole('button', { name: 'Create participant' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Full name *'), {
      target: { value: 'New Student' },
    });
    fireEvent.change(within(dialog).getByLabelText('Register number *'), {
      target: { value: '21CS999' },
    });
    await chooseOption(dialog, 'Other');
    fireEvent.change(within(dialog).getByLabelText('Email *'), {
      target: { value: 'new@school.test' },
    });
    // The two halves sent as two keys. A single joined "2nd Year / CSE" string would have needed
    // splitting again on the server to fill the two NOT NULL columns it has.
    await chooseOption(dialog, '3', 'Year *');
    fireEvent.change(within(dialog).getByLabelText('Department/Division *'), {
      target: { value: 'CSE' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create participant account' }));

    await waitFor(() => expect(api.createAdminUser).toHaveBeenCalledTimes(1));
    expect(api.createAdminUser.mock.calls[0][0]).toMatchObject({
      name: 'New Student',
      regno: '21CS999',
      gender: 'other',
      email: 'new@school.test',
      year: 3,
      department: 'CSE',
      account_type: 'users',
    });
    expect(api.createAdminUser.mock.calls[0][0]).not.toHaveProperty('class_name');
  });
});

describe('ProfileReview: editing a staff account', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('offers only the columns a moderator actually has', async () => {
    await renderRows([moderator()], 'Mo Moderator');

    fireEvent.click(screen.getByRole('button', { name: 'Edit profile for Mo Moderator' }));
    const dialog = await screen.findByRole('dialog');

    // The `moderator` table has these three columns and no others. Everything asserted absent
    // below lives on `users` and nowhere else, so offering it asked the operator to type
    // something with no destination.
    expect(within(dialog).getByLabelText('Full name')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Register number')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Email')).toBeInTheDocument();
    expect(within(dialog).queryByLabelText('Gender')).not.toBeInTheDocument();

    expect(within(dialog).queryByLabelText('Year')).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText('Department/Division')).not.toBeInTheDocument();
    // A picture is the row's own business. An admin could once open this dialog, choose a file,
    // and overwrite someone's face without them being in the room or able to refuse.
    expect(within(dialog).queryByLabelText('Profile picture')).not.toBeInTheDocument();
    expect(within(dialog).queryByText('Email notifications')).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText('Bio')).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText('Interests')).not.toBeInTheDocument();
    expect(within(dialog).queryByText('Allow matchmaking')).not.toBeInTheDocument();
    expect(within(dialog).queryByText('Receive love notes')).not.toBeInTheDocument();
    expect(within(dialog).queryByText('Send love notes')).not.toBeInTheDocument();
  });

  it('says why the missing fields are missing rather than leaving a silent gap', async () => {
    await renderRows([moderator()], 'Mo Moderator');

    // A shorter form reads as the screen having less to say, not as the account having less on it.
    fireEvent.click(screen.getByRole('button', { name: 'Edit profile for Mo Moderator' }));
    const dialog = await screen.findByRole('dialog');
    expect(
      within(dialog).getByText(/A moderator account has no gender, year or department/i),
    ).toBeInTheDocument();
  });

  it('sends no participant-only key when a moderator is saved', async () => {
    api.updateAdminUser.mockResolvedValue({
      id: MODERATOR,
      name: 'Mo Renamed',
      account_type: 'moderator',
    });
    await renderRows([moderator()], 'Mo Moderator');

    fireEvent.click(screen.getByRole('button', { name: 'Edit profile for Mo Moderator' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Full name'), {
      target: { value: 'Mo Renamed' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(api.updateAdminUser).toHaveBeenCalledTimes(1));
    const [id, payload] = api.updateAdminUser.mock.calls[0];
    expect(id).toBe(MODERATOR);
    // The server strips these keys from a staff update, so sending them was a value that could
    // only be ignored. It also meant a toggle on the form could report a change that no amount of
    // saving would ever keep.
    expect(payload).toEqual({ name: 'Mo Renamed' });
  });

  it('offers no toggle of any kind for a moderator', async () => {
    await renderRows([moderator({ notifications_enabled: true })], 'Mo Moderator');

    fireEvent.click(screen.getByRole('button', { name: 'Edit profile for Mo Moderator' }));
    const dialog = await screen.findByRole('dialog');
    // `notifications_enabled` has a column on all three account tables, so it is the one switch
    // that would never have been ignored. It is gone anyway: this dialog writes identity, and a
    // preference is a decision the account owner makes. Switching off someone's mail from an
    // admin screen is not an identity correction.
    expect(within(dialog).queryAllByRole('switch')).toHaveLength(0);
    expect(within(dialog).queryAllByRole('checkbox')).toHaveLength(0);
  });

  it('offers a participant the cohort fields too, and shows what is stored', async () => {
    await renderRows(
      [participant(ORDINARY, 'Ordinary', {}, { bio: 'hello', interests: ['Music', 'Bogus'] })],
      'Ordinary',
    );

    // The identity-only change must not narrow the participant form by accident: year and
    // department are the whole reason a participant row carries a cohort at all.
    fireEvent.click(screen.getByRole('button', { name: 'Edit profile for Ordinary' }));
    const dialog = await screen.findByRole('dialog');
    // Both halves present, and the stored year is shown rather than dropped: `2nd Year` came from
    // the fixture, so a select sitting empty here would mean the editor refused a valid stored
    // value.
    expect(within(dialog).getByLabelText('Year')).toHaveTextContent('2');
    expect(within(dialog).getByLabelText('Department/Division')).toHaveValue('Class_9');
    // Everything below is the owner's, so it is not on an admin's form at all -- not even read-only.
    expect(within(dialog).queryByLabelText('Bio')).not.toBeInTheDocument();
    expect(within(dialog).queryAllByRole('checkbox')).toHaveLength(0);
    expect(within(dialog).queryByText('Allow matchmaking')).not.toBeInTheDocument();
  });

  it('saves a participant cohort change and nothing else', async () => {
    api.updateAdminUser.mockResolvedValue({
      id: ORDINARY,
      name: 'Ordinary',
      department: 'ECE',
      account_type: 'users',
    });
    await renderRows([participant(ORDINARY, 'Ordinary')], 'Ordinary');

    fireEvent.click(screen.getByRole('button', { name: 'Edit profile for Ordinary' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Department/Division'), {
      target: { value: 'ECE' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(api.updateAdminUser).toHaveBeenCalledTimes(1));
    expect(api.updateAdminUser.mock.calls[0][1]).toEqual({ department: 'ECE' });
  });

  it('leaves a stored interest out of the payload when the name alone changes', async () => {
    api.updateAdminUser.mockResolvedValue({
      id: ORDINARY,
      name: 'Renamed',
      account_type: 'users',
    });
    // Untouched interests, including a value the server no longer accepts. The operator changed
    // nothing but a name.
    await renderRows(
      [participant(ORDINARY, 'Ordinary', {}, { interests: ['Bogus', 'Music'] })],
      'Ordinary',
    );

    fireEvent.click(screen.getByRole('button', { name: 'Edit profile for Ordinary' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Full name'), {
      target: { value: 'Renamed' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(api.updateAdminUser).toHaveBeenCalledTimes(1));
    // `interests` is not in the payload at all -- the form does not carry it, so it cannot report
    // a stale value as a deliberate choice.
    expect(api.updateAdminUser.mock.calls[0][1]).toEqual({ name: 'Renamed' });
  });

  it('sends a changed gender once, and not on every later save', async () => {
    // A participant, because gender is a `users` column now: the same comparison on a moderator
    // would have nothing to compare, since the editor never seeds a gender it cannot store.
    api.updateAdminUser.mockImplementation((_id: string, payload: Record<string, unknown>) =>
      Promise.resolve({
        id: ORDINARY,
        name: 'Ordinary',
        account_type: 'users',
        gender: 'male',
        ...payload,
      }),
    );
    await renderRows(
      [participant(ORDINARY, 'Ordinary', {}, { gender: 'male' })],
      'Ordinary',
    );

    fireEvent.click(screen.getByRole('button', { name: 'Edit profile for Ordinary' }));
    const dialog = await screen.findByRole('dialog');
    await chooseOption(dialog, 'Female', 'Gender');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(api.updateAdminUser).toHaveBeenCalledTimes(1));
    expect(api.updateAdminUser.mock.calls[0][1]).toEqual({ gender: 'female' });

    // Reopening on the value just written must report no change. If the form kept comparing the
    // displayed value against the row it was fetched with, a participant edited for any other
    // reason would have their gender rewritten on that save instead.
    fireEvent.click(screen.getByRole('button', { name: 'Edit profile for Ordinary' }));
    const reopened = await screen.findByRole('dialog');
    await chooseOption(reopened, 'Female', 'Gender');
    fireEvent.click(within(reopened).getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(toast.success).toHaveBeenCalled());
    expect(api.updateAdminUser).toHaveBeenCalledTimes(1);
  });

  it('offers no way to write a picture, for either account type', async () => {
    await renderRows(
      [participant(ORDINARY, 'Ordinary'), moderator()],
      'Ordinary',
    );

    // The admin client has no picture call at all any more, so there is nothing behind a control
    // to remove later: a profile photo belongs to the account, who uploads it themselves through
    // `/profile/upload-picture`. A missing control here would be a real regression -- it would be
    // the admin overwriting someone's face without them being able to refuse -- so this is pinned
    // for the participant form too, not only the staff one.
    for (const label of ['Ordinary', 'Mo Moderator']) {
      fireEvent.click(screen.getByRole('button', { name: `Edit profile for ${label}` }));
      const dialog = await screen.findByRole('dialog');
      expect(within(dialog).queryByLabelText('Profile picture')).not.toBeInTheDocument();
      expect(within(dialog).queryByRole('textbox', { name: /picture|photo|image|url/i })).toBeNull();
      fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    }
  });
});

describe('ProfileReview: the fields an admin does not get', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('sends only the identity and cohort fields when a participant is created', async () => {
    api.createAdminUser.mockResolvedValue({
      id: '44444444-4444-4444-4444-444444444488',
      name: 'New Student',
      regno: '21CS999',
      email: 'new@school.test',
      gender: 'other',
      year: 3,
      department: 'CSE',
      account_type: 'users',
    });
    await renderRows([participant(ORDINARY, 'Ordinary')], 'Ordinary');

    fireEvent.click(screen.getByRole('button', { name: 'Create participant' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Full name *'), {
      target: { value: 'New Student' },
    });
    fireEvent.change(within(dialog).getByLabelText('Register number *'), {
      target: { value: '21CS999' },
    });
    await chooseOption(dialog, 'Other');
    fireEvent.change(within(dialog).getByLabelText('Email *'), {
      target: { value: 'new@school.test' },
    });
    await chooseOption(dialog, '3', 'Year *');
    fireEvent.change(within(dialog).getByLabelText('Department/Division *'), {
      target: { value: 'CSE' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create participant account' }));

    await waitFor(() => expect(api.createAdminUser).toHaveBeenCalledTimes(1));
    // `toEqual` on the whole object, not a per-key check. A per-key check would pass over an
    // extra key, which is the failure mode: the create used to carry a bio and interests the
    // server then had to decide what to do with.
    expect(api.createAdminUser.mock.calls[0][0]).toEqual({
      account_type: 'users',
      name: 'New Student',
      regno: '21CS999',
      gender: 'other',
      email: 'new@school.test',
      year: 3,
      department: 'CSE',
    });
  });

  it('sends only the identity fields when a staff account is edited', async () => {
    api.updateAdminUser.mockResolvedValue({
      id: MODERATOR,
      name: 'Mo Renamed',
      account_type: 'moderator',
    });
    await renderRows([moderator()], 'Mo Moderator');

    fireEvent.click(screen.getByRole('button', { name: 'Edit profile for Mo Moderator' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Full name'), {
      target: { value: 'Mo Renamed' },
    });
    fireEvent.change(within(dialog).getByLabelText('Register number'), {
      target: { value: 'staff-77' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(api.updateAdminUser).toHaveBeenCalledTimes(1));
    // No `gender`, no `year` and no `department`: the `moderator` table has none of those columns,
    // so a key there would be a value with nowhere to land. `extra="forbid"` on the server turns a
    // stray key into a 422, which is why an over-broad payload would fail the request rather than
    // be silently dropped.
    expect(api.updateAdminUser.mock.calls[0][1]).toEqual({
      name: 'Mo Renamed',
      regno: 'staff-77',
    });
  });

  it('shows a 422 as readable text instead of crashing on the error body', async () => {
    // FastAPI sends `detail` as an array of objects, so this used to reach Sonner as an array and
    // blew up on render -- which took the whole page down rather than showing a failed save.
    api.createAdminUser.mockRejectedValue({
      response: {
        data: {
          detail: [
            {
              type: 'value_error',
              loc: ['body', 'regno'],
              msg: 'Register number already exists.',
              input: '21CS999',
              ctx: { error: {} },
            },
          ],
        },
        status: 422,
      },
    });
    await renderRows([participant(ORDINARY, 'Ordinary')], 'Ordinary');

    fireEvent.click(screen.getByRole('button', { name: 'Create participant' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Full name *'), {
      target: { value: 'New Student' },
    });
    fireEvent.change(within(dialog).getByLabelText('Register number *'), {
      target: { value: '21CS999' },
    });
    await chooseOption(dialog, 'Other');
    fireEvent.change(within(dialog).getByLabelText('Email *'), {
      target: { value: 'new@school.test' },
    });
    await chooseOption(dialog, '3', 'Year *');
    fireEvent.change(within(dialog).getByLabelText('Department/Division *'), {
      target: { value: 'CSE' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create participant account' }));

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    // The server's own words, under the field path it refused, rather than a rendering of its
    // internal shape. The `regno: ` prefix is what tells the operator which of six fields to fix.
    expect(toast.error).toHaveBeenCalledWith('regno: Register number already exists.');
    // And the dialog is still open with the operator's input intact, so nothing has to be retyped.
    expect(within(dialog).getByLabelText('Register number *')).toHaveValue('21CS999');
  });

  it('falls back to a message the server did not send', async () => {
    // An empty 422 says nothing useful, and an empty toast is worse than no toast: it looks like
    // the operator pressed the wrong button.
    api.createAdminUser.mockRejectedValue({
      response: { data: { detail: [] }, status: 422 },
    });
    await renderRows([participant(ORDINARY, 'Ordinary')], 'Ordinary');

    fireEvent.click(screen.getByRole('button', { name: 'Create participant' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Full name *'), {
      target: { value: 'New Student' },
    });
    fireEvent.change(within(dialog).getByLabelText('Register number *'), {
      target: { value: '21CS999' },
    });
    await chooseOption(dialog, 'Other');
    fireEvent.change(within(dialog).getByLabelText('Email *'), {
      target: { value: 'new@school.test' },
    });
    await chooseOption(dialog, '3', 'Year *');
    fireEvent.change(within(dialog).getByLabelText('Department/Division *'), {
      target: { value: 'CSE' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create participant account' }));

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(toast.error).toHaveBeenCalledWith('Unable to create this account.');
  });

  it('caps each identity field at the length the server accepts', async () => {
    await renderRows([participant(ORDINARY, 'Ordinary')], 'Ordinary');

    fireEvent.click(screen.getByRole('button', { name: 'Create participant' }));
    const dialog = await screen.findByRole('dialog');

    // The server's `max_length` is the rule; these attributes are the form telling the operator
    // before the round-trip. A mismatch in either direction is the bug: too high and the save
    // fails on submit, too low and a legitimate register number cannot be typed.
    expect(within(dialog).getByLabelText('Full name *')).toHaveAttribute(
      'maxlength',
      String(ADMIN_NAME_MAX),
    );
    expect(within(dialog).getByLabelText('Register number *')).toHaveAttribute(
      'maxlength',
      String(ADMIN_REGNO_MAX),
    );
    expect(within(dialog).getByLabelText('Department/Division *')).toHaveAttribute(
      'maxlength',
      String(ADMIN_DEPARTMENT_MAX),
    );
    // No cap on an address: the server's limit on `email` is 320 and the widest realistic address
    // is nowhere near it, so a cap here would only be a guess.
    expect(within(dialog).getByLabelText('Email *')).not.toHaveAttribute('maxlength');
  });

  it('refuses a staff account with no email, and names the field', async () => {
    await renderRows([moderator()], 'Mo Moderator');

    fireEvent.click(screen.getByRole('button', { name: 'Create moderator' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Full name *'), {
      target: { value: 'New Mod' },
    });
    fireEvent.change(within(dialog).getByLabelText('Register number *'), {
      target: { value: 'staff-9' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create moderator account' }));

    // Email is required of every account type. A moderator could once be created with a regno and
    // a name and nobody noticed, because nothing on the way to the database complained. The
    // message now lives with the field and is named on its own; gender is not among the messages,
    // because a staff create never offers the control that would need one.
    expect(api.createAdminUser).not.toHaveBeenCalled();
    const alerts = within(dialog).getAllByRole('alert').map((node) => node.textContent);
    expect(alerts).toEqual(
      expect.arrayContaining(['Enter an email address, for example name@example.com.']),
    );
    expect(alerts).not.toContain('Choose a gender.');
    expect(within(dialog).getByLabelText('Email *')).toHaveAttribute('aria-invalid', 'true');
  });
});
