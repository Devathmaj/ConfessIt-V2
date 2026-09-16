import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
  uploadAdminUserPicture: vi.fn(),
}));

/** Hoisted alongside the API so a rejected create can be asserted on by its message. */
const toast = vi.hoisted(() => ({
  info: vi.fn(),
  error: vi.fn(),
  success: vi.fn(),
  // `warning` is the one the create flow uses to say "the account is there, the picture is not".
  // Leaving it out of the mock made that toast throw, which aborted the handler before the new
  // account was added to the list -- so the row silently vanished from the screen.
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

/**
 * Picks `option` from the gender <Select> labelled `label`.
 *
 * Gender stopped being a text box when the vocabulary was closed, and the old
 * `fireEvent.change(trigger, { value: 'female' })` has no meaning against a Radix trigger: a
 * <div role="combobox"> holds no value to change, so the test would pass a click that went
 * nowhere and then assert against a control that never moved. The trigger opens on pointer down
 * and the option is a separate element in a portal, so both steps are needed.
 */
async function chooseGender(dialog: HTMLElement, option: string, label = 'Gender *') {
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

    // The `moderator` table has no class, bio, interests, emoji, matchmaking or love-note
    // columns. Offering those inputs asked for values the server silently dropped.
    expect(
      within(dialog).getByRole('heading', { name: 'Create moderator account' }),
    ).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Full name *')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Register number *')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Gender *')).toBeInTheDocument();
    expect(within(dialog).queryByLabelText('Class / department *')).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText('Emoji tag')).not.toBeInTheDocument();
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
    await chooseGender(dialog, 'Female');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create moderator account' }));

    await waitFor(() => expect(api.createAdminUser).toHaveBeenCalledTimes(1));
    const payload = api.createAdminUser.mock.calls[0][0] as Record<string, unknown>;
    expect(payload).toMatchObject({
      name: 'New Mod',
      regno: 'staff-9',
      // Email is required of a staff account too. It used to be omitted here, which is how a
      // moderator ended up with no address anybody could reach.
      email: 'newmod@school.test',
      gender: 'female',
      account_type: 'moderator',
    });
    // Nothing participant-only was sent, because nothing participant-only was collected. Sending
    // empty values would rely on the server dropping them, which is how they got sent at all.
    for (const key of [
      'class_name',
      'bio',
      'emoji',
      'interests',
      'matchmaking_enabled',
      'love_notes_receive_enabled',
      'love_notes_send_enabled',
    ]) {
      expect(payload).not.toHaveProperty(key);
    }
  });

  it('still requires a class from a participant, and names the field when it is missing', async () => {
    await renderRows([participant(ORDINARY, 'Ordinary')], 'Ordinary');

    fireEvent.click(screen.getByRole('button', { name: 'Create participant' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByLabelText('Email *')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Class / department *')).toBeInTheDocument();
    expect(within(dialog).getByText('Allow matchmaking')).toBeInTheDocument();

    fireEvent.change(within(dialog).getByLabelText('Full name *'), {
      target: { value: 'New Student' },
    });
    fireEvent.change(within(dialog).getByLabelText('Register number *'), {
      target: { value: '21CS999' },
    });
    fireEvent.change(within(dialog).getByLabelText('Gender *'), { target: { value: 'other' } });
    fireEvent.change(within(dialog).getByLabelText('Email *'), {
      target: { value: 'new@school.test' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create participant account' }));

    // Class is required of a participant and there is no column for it on a moderator, so the
    // same form cannot ask for both. "Please fill in all required fields" was true and useless:
    // it could not say which of the five fields was the one missing.
    expect(api.createAdminUser).not.toHaveBeenCalled();
    expect(toast.error).toHaveBeenCalledWith(
      'Class / department is required for this account type.',
    );
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
    fireEvent.change(within(dialog).getByLabelText('Gender *'), { target: { value: 'other' } });
    fireEvent.change(within(dialog).getByLabelText('Email *'), {
      target: { value: 'new@school.test' },
    });
    fireEvent.change(within(dialog).getByLabelText('Class / department *'), {
      target: { value: 'CSE' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create participant account' }));

    await waitFor(() => expect(api.createAdminUser).toHaveBeenCalledTimes(1));
    expect(api.createAdminUser.mock.calls[0][0]).toMatchObject({
      name: 'New Student',
      regno: '21CS999',
      gender: 'other',
      email: 'new@school.test',
      class_name: 'CSE',
      account_type: 'users',
    });
  });
});

describe('ProfileReview: editing a staff account', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('offers only the columns a moderator actually has', async () => {
    await renderRows([moderator()], 'Mo Moderator');

    fireEvent.click(screen.getByRole('button', { name: 'View profile for Mo Moderator' }));
    const dialog = await screen.findByRole('dialog');

    // The `moderator` table has five columns this form can write. The seven below live on
    // `users` and nowhere else, so offering them asked the operator to type something that had no
    // destination.
    expect(within(dialog).getByLabelText('Full name')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Register number')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Email')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Gender')).toBeInTheDocument();
    // A file, not a text box holding a Cloudinary identifier. The URL is produced by the server
    // from an upload now, so an editable box here could only ever hold something it would refuse.
    expect(within(dialog).getByLabelText('Profile picture')).toHaveAttribute('type', 'file');
    expect(within(dialog).getByText('Email notifications')).toBeInTheDocument();

    expect(within(dialog).queryByLabelText('Class / department')).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText('Emoji tag')).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText('Bio')).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText('Interests')).not.toBeInTheDocument();
    expect(within(dialog).queryByText('Allow matchmaking')).not.toBeInTheDocument();
    expect(within(dialog).queryByText('Receive love notes')).not.toBeInTheDocument();
    expect(within(dialog).queryByText('Send love notes')).not.toBeInTheDocument();
  });

  it('says why the missing fields are missing rather than leaving a silent gap', async () => {
    await renderRows([moderator()], 'Mo Moderator');

    // A shorter form reads as the screen having less to say, not as the account having less on it.
    fireEvent.click(screen.getByRole('button', { name: 'View profile for Mo Moderator' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/A moderator account has no class, bio, interests/i))
      .toBeInTheDocument();
  });

  it('sends no participant-only key when a moderator is saved', async () => {
    api.updateAdminUser.mockResolvedValue({
      id: MODERATOR,
      name: 'Mo Renamed',
      account_type: 'moderator',
    });
    await renderRows([moderator()], 'Mo Moderator');

    fireEvent.click(screen.getByRole('button', { name: 'View profile for Mo Moderator' }));
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

  it('still saves the shared toggles for a moderator', async () => {
    api.updateAdminUser.mockResolvedValue({
      id: MODERATOR,
      name: 'A Moderator',
      account_type: 'moderator',
      notifications_enabled: false,
    });
    await renderRows([moderator({ notifications_enabled: true })], 'Mo Moderator');

    fireEvent.click(screen.getByRole('button', { name: 'View profile for Mo Moderator' }));
    const dialog = await screen.findByRole('dialog');
    // `notifications_enabled` is on all three account tables, so it is not a participant field.
    // It is also the only switch on the form: the others have no column to be written to.
    expect(within(dialog).getAllByRole('switch')).toHaveLength(1);
    fireEvent.click(within(dialog).getByRole('switch'));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(api.updateAdminUser).toHaveBeenCalledTimes(1));
    expect(api.updateAdminUser.mock.calls[0][1]).toEqual({ notifications_enabled: false });
  });

  it('offers every participant field when a participant is edited', async () => {
    await renderRows(
      [participant(ORDINARY, 'Ordinary', {}, { bio: 'hello', interests: ['Music', 'Bogus'] })],
      'Ordinary',
    );

    // The fix must not narrow the participant form by accident: these are the fields the whole
    // reason a participant row carries a class.
    fireEvent.click(screen.getByRole('button', { name: 'View profile for Ordinary' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByLabelText('Class / department')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Emoji tag')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Bio')).toHaveValue('hello');
    // Interests are twelve tickable boxes, not a comma-separated string the operator has to know
    // the spelling of. `Bogus` is what a row from before the vocabulary was closed holds: there is
    // no box for it, rather than an unticked one that looks like a choice the operator could make.
    expect(within(dialog).getByRole('checkbox', { name: 'Music' })).toBeChecked();
    expect(within(dialog).queryByRole('checkbox', { name: 'Bogus' })).not.toBeInTheDocument();
    expect(within(dialog).getAllByRole('checkbox')).toHaveLength(12);
    expect(within(dialog).getByText('Allow matchmaking')).toBeInTheDocument();
    expect(within(dialog).getByText('Receive love notes')).toBeInTheDocument();
    expect(within(dialog).getByText('Send love notes')).toBeInTheDocument();
  });

  it('still saves a participant edit', async () => {
    api.updateAdminUser.mockResolvedValue({
      id: ORDINARY,
      name: 'Renamed',
      account_type: 'users',
    });
    await renderRows([participant(ORDINARY, 'Ordinary')], 'Ordinary');

    fireEvent.click(screen.getByRole('button', { name: 'View profile for Ordinary' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Bio'), {
      target: { value: 'a short bio' },
    });
    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Music' }));
    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Traveling' }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(api.updateAdminUser).toHaveBeenCalledTimes(1));
    expect(api.updateAdminUser.mock.calls[0][1]).toEqual({
      bio: 'a short bio',
      interests: ['Music', 'Traveling'],
    });
  });

  it('drops a legacy interest from the payload the first time the row is edited', async () => {
    api.updateAdminUser.mockResolvedValue({
      id: ORDINARY,
      name: 'Ordinary',
      account_type: 'users',
    });
    // Untouched interests. The stored list holds a value the server no longer accepts, and the
    // operator has changed nothing about interests -- only their name.
    await renderRows(
      [participant(ORDINARY, 'Ordinary', {}, { interests: ['Bogus', 'Music'] })],
      'Ordinary',
    );

    fireEvent.click(screen.getByRole('button', { name: 'View profile for Ordinary' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Full name'), {
      target: { value: 'Renamed' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(api.updateAdminUser).toHaveBeenCalledTimes(1));
    // `interests` is absent, not `['Music']`. Sending it would be a silent edit of a field nobody
    // looked at -- but the next save that does touch interests will send `['Music']` on its own,
    // which is why this is a no-op here rather than a correction.
    expect(api.updateAdminUser.mock.calls[0][1]).toEqual({ name: 'Renamed' });
  });

  it('sends a changed gender once, and not on every later save', async () => {
    api.updateAdminUser.mockImplementation((_id: string, payload: Record<string, unknown>) =>
      Promise.resolve({
        id: MODERATOR,
        name: 'Mo Moderator',
        account_type: 'moderator',
        gender: 'male',
        ...payload,
      }),
    );
    await renderRows([moderator({ gender: 'male' })], 'Mo Moderator');

    fireEvent.click(screen.getByRole('button', { name: 'View profile for Mo Moderator' }));
    const dialog = await screen.findByRole('dialog');
    await chooseGender(dialog, 'Female', 'Gender');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(api.updateAdminUser).toHaveBeenCalledTimes(1));
    expect(api.updateAdminUser.mock.calls[0][1]).toEqual({ gender: 'female' });

    // Reopening on the value just written must report no change. If the form kept comparing the
    // displayed value against the row it was fetched with, a moderator edited for any other reason
    // would have their gender rewritten on that save instead.
    fireEvent.click(screen.getByRole('button', { name: 'View profile for Mo Moderator' }));
    const reopened = await screen.findByRole('dialog');
    await chooseGender(reopened, 'Female', 'Gender');
    fireEvent.click(within(reopened).getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(toast.success).toHaveBeenCalled());
    expect(api.updateAdminUser).toHaveBeenCalledTimes(1);
  });

  it('never offers an interest outside the twelve the server accepts', async () => {
    await renderRows([participant(ORDINARY, 'Ordinary')], 'Ordinary');

    fireEvent.click(screen.getByRole('button', { name: 'View profile for Ordinary' }));
    const dialog = await screen.findByRole('dialog');
    const labels = within(dialog)
      .getAllByRole('checkbox')
      .map((box) => box.getAttribute('aria-label') ?? document.querySelector(`label[for="${box.id}"]`)?.textContent?.trim());
    expect(labels).toEqual([
      'Music',
      'Movies',
      'Reading',
      'Gaming',
      'Traveling',
      'Cooking',
      'Sports',
      'Art',
      'Photography',
      'Dancing',
      'Writing',
      'Coding',
    ]);
  });
});

describe('ProfileReview: profile pictures', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('uploads a chosen file, and skips the JSON call when nothing else changed', async () => {
    // An explicit log rather than `invocationCallOrder`, which this vitest build does not expose.
    // The order is the point of the test, so it is recorded where it can be asserted.
    const calls: string[] = [];
    api.updateAdminUser.mockImplementation(() => {
      calls.push('update');
      return Promise.resolve({ id: ORDINARY, name: 'Ordinary', account_type: 'users' });
    });
    api.uploadAdminUserPicture.mockImplementation(() => {
      calls.push('upload');
      return Promise.resolve({ profile_picture_url: '/media/profile/ordinary.png' });
    });
    await renderRows([participant(ORDINARY, 'Ordinary')], 'Ordinary');

    fireEvent.click(screen.getByRole('button', { name: 'View profile for Ordinary' }));
    const dialog = await screen.findByRole('dialog');
    const file = new File(['bytes'], 'me.png', { type: 'image/png' });
    fireEvent.change(within(dialog).getByLabelText('Profile picture'), {
      target: { files: [file] },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(api.uploadAdminUserPicture).toHaveBeenCalledTimes(1));
    expect(api.uploadAdminUserPicture).toHaveBeenCalledWith(ORDINARY, file);
    // No field changed, so there is no JSON request to make. The picture is a multipart upload and
    // cannot ride along in an update body, so it is the only call here.
    expect(calls).toEqual(['upload']);
  });

  it('keeps both halves of the result when a rename and a picture are saved together', async () => {
    api.updateAdminUser.mockResolvedValue({
      id: ORDINARY,
      name: 'Renamed',
      account_type: 'users',
    });
    api.uploadAdminUserPicture.mockResolvedValue({
      profile_picture_url: '/media/profile/ordinary.png',
    });
    await renderRows([participant(ORDINARY, 'Ordinary')], 'Ordinary');

    fireEvent.click(screen.getByRole('button', { name: 'View profile for Ordinary' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Full name'), {
      target: { value: 'Renamed' },
    });
    fireEvent.change(within(dialog).getByLabelText('Profile picture'), {
      target: { files: [new File(['bytes'], 'me.png', { type: 'image/png' })] },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(toast.success).toHaveBeenCalled());
    // The upload response carries only the new URL. Merging it over the stored profile would have
    // reverted the name this save had just changed, while still reporting success.
    expect(await screen.findByText('Renamed')).toBeInTheDocument();
  });

  it('keeps the saved edit when the picture upload is rejected', async () => {
    api.updateAdminUser.mockResolvedValue({
      id: ORDINARY,
      name: 'Ordinary',
      account_type: 'users',
    });
    api.uploadAdminUserPicture.mockRejectedValue(new Error('not an image'));
    await renderRows([participant(ORDINARY, 'Ordinary')], 'Ordinary');

    fireEvent.click(screen.getByRole('button', { name: 'View profile for Ordinary' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Bio'), {
      target: { value: 'a short bio' },
    });
    fireEvent.change(within(dialog).getByLabelText('Profile picture'), {
      target: { files: [new File(['x'], 'me.txt', { type: 'text/plain' })] },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));

    // The dialog closes and the edit stands. A wrong file is a mistake about one field, not a
    // reason to discard the bio the operator wrote in the same sitting.
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(api.updateAdminUser.mock.calls[0][1]).toEqual({ bio: 'a short bio' });
  });

  it('creates the account first and only then uploads, so a rejected picture leaves a usable row', async () => {
    api.createAdminUser.mockResolvedValue({
      id: '77777777-7777-7777-7777-777777777777',
      regno: '21CS777',
      name: 'New Student',
      email: 'new@school.test',
      gender: 'other',
      class_name: 'CSE',
      account_type: 'users',
    });
    api.uploadAdminUserPicture.mockRejectedValue(new Error('too large'));
    await renderRows([participant(ORDINARY, 'Ordinary')], 'Ordinary');

    fireEvent.click(screen.getByRole('button', { name: 'Create participant' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Full name *'), {
      target: { value: 'New Student' },
    });
    fireEvent.change(within(dialog).getByLabelText('Register number *'), {
      target: { value: '21CS777' },
    });
    fireEvent.change(within(dialog).getByLabelText('Email *'), {
      target: { value: 'new@school.test' },
    });
    fireEvent.change(within(dialog).getByLabelText('Class / department *'), {
      target: { value: 'CSE' },
    });
    fireEvent.change(within(dialog).getByLabelText('Profile picture'), {
      target: { files: [new File(['x'], 'huge.png', { type: 'image/png' })] },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create participant account' }));

    // `warning`, not `error`: the account was created. The operator needs to know the picture is
    // missing without being told the whole thing failed.
    await waitFor(() => expect(toast.warning).toHaveBeenCalled());
    expect(api.createAdminUser).toHaveBeenCalledTimes(1);
    expect(api.uploadAdminUserPicture).toHaveBeenCalledWith(
      '77777777-7777-7777-7777-777777777777',
      expect.any(File),
    );
    // The new participant is still listed. There is no id to upload against until the row exists,
    // so the other order is not available.
    expect(await screen.findByText('New Student')).toBeInTheDocument();
    // Only the warning. A success toast alongside it would tell the operator the picture is done.
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('confirms the create when the chosen picture uploads cleanly', async () => {
    api.createAdminUser.mockResolvedValue({
      id: '88888888-8888-8888-8888-888888888888',
      regno: 'staff-8',
      name: 'New Mod',
      email: 'newmod@school.test',
      gender: 'other',
      account_type: 'moderator',
    });
    api.uploadAdminUserPicture.mockResolvedValue({
      id: '88888888-8888-8888-8888-888888888888',
      profile_picture_url: '/media/profile/newmod.png',
    });
    await renderRows([moderator()], 'Mo Moderator');

    fireEvent.click(screen.getByRole('button', { name: 'Create moderator' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Full name *'), {
      target: { value: 'New Mod' },
    });
    fireEvent.change(within(dialog).getByLabelText('Register number *'), {
      target: { value: 'staff-8' },
    });
    fireEvent.change(within(dialog).getByLabelText('Email *'), {
      target: { value: 'newmod@school.test' },
    });
    fireEvent.change(within(dialog).getByLabelText('Profile picture'), {
      target: { files: [new File(['bytes'], 'me.png', { type: 'image/png' })] },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create moderator account' }));

    // The upload succeeded, so this is a plain success. Skipping it because a file had been chosen
    // left a create that did everything right reporting nothing at all.
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith('Moderator account created successfully.'),
    );
    expect(toast.warning).not.toHaveBeenCalled();
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
    // a name, and nobody noticed because nothing on the way to the database complained.
    expect(api.createAdminUser).not.toHaveBeenCalled();
    expect(toast.error).toHaveBeenCalledWith('Email is required for this account type.');
  });
});