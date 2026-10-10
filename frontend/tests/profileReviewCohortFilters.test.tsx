import { fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { ProfileReview } from '@/pages/ProfileReview';

/**
 * Filtering and sorting the account cards on the admin user review screen.
 *
 * An operator looking for somebody on a platform with a few thousand accounts is looking for one
 * of three things: a name they half remember, a cohort ("the second-years in CSE"), or an ordering
 * that puts the people they need to look at next to each other. These tests hold that apart from
 * the grouping, which `profileReviewSections.test.tsx` already covers.
 *
 * Six properties are pinned here, and each fails in a way a test that only checked the happy path
 * would not notice:
 *
 * **Where the controls are.** They sit at the bottom of the page, hard against the ordinary
 * participants -- the list they exist to narrow and order -- and below the work queue, whose own
 * order is fixed and whose rows are still narrowed by them.
 *
 * **Each participant section searches for itself.** The queue's box narrows the work queue, the
 * Participants box narrows the list below it, and neither query reaches the other section -- nor
 * the staff sections, which have no search box at all.
 *
 * **The two cohort filters are independent.** "Every second-year" and "everyone in CSE" are both
 * real questions and a single combined cohort picker can only ask one of them.
 *
 * **Staff are not narrowed by cohort.** An admin and a moderator row has no year and no
 * department to match on. Refusing to compare them is right; dropping them is not, because a
 * cohort filter is a way of finding one participant and must not be able to hide the account class
 * an operator most needs to keep sight of.
 *
 * **The sort is the participants' alone.** It reorders the ordinary participants and nothing else:
 * the flagged section is a work queue that stays in removal order however the cards below it are
 * arranged, and a moderator list is not a list anybody is reading by year.
 *
 * **The offered options are the ones that can match something.** A year with nobody in it is an
 * option whose only possible result is an empty list, and an operator who picks it is then told
 * the platform has no such year.
 */

const api = vi.hoisted(() => ({
  getAdminUsers: vi.fn(),
  setAdminUserBlocked: vi.fn(),
  createAdminUser: vi.fn(),
  updateAdminUser: vi.fn(),
  uploadAdminUserPicture: vi.fn(),
}));

const toast = vi.hoisted(() => ({
  info: vi.fn(),
  error: vi.fn(),
  success: vi.fn(),
  warning: vi.fn(),
}));

vi.mock('@/services/api', () => api);
vi.mock('sonner', () => ({ toast, Toaster: () => null }));

let nextId = 0;

const anId = () => `00000000-0000-0000-0000-${String(++nextId).padStart(12, '0')}`;

/** A participant. Year and department are overridable because they are what is under test. */
const participant = (
  name: string,
  overrides: { year?: number | null; department?: string | null; removals?: number } = {}
) => {
  const removals = overrides.removals ?? 0;
  return {
    id: anId(),
    regno: `student-${name}`,
    name,
    email: `${name.toLowerCase()}@school.test`,
    year: 2,
    department: 'CSE',
    account_type: 'users',
    is_blocked: false,
    report_history: {
      confession: { reports: removals, removals },
      love_note: { reports: 0, removals: 0 },
      message: { reports: 0, removals: 0 },
    },
    ...(overrides.year !== undefined ? { year: overrides.year } : {}),
    ...(overrides.department !== undefined ? { department: overrides.department } : {}),
  };
};

/**
 * A staff account.
 *
 * Carries no year and no department at all, exactly as the server sends one: the columns do not
 * exist on the `admin` and `moderator` tables, so the keys are absent rather than null. Every test
 * here depends on that, because it is why the cohort filters have nothing to compare staff rows
 * against.
 */
const staff = (name: string, accountType: 'admin' | 'moderator') => ({
  id: anId(),
  regno: `${accountType}-${name}`,
  name,
  email: `${name.toLowerCase()}@school.test`,
  account_type: accountType,
  is_blocked: false,
});

const admin = (name = 'Ada Admin') => staff(name, 'admin');
const moderator = (name = 'Mo Moderator') => staff(name, 'moderator');

async function renderPage(rows: unknown[]) {
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
  // The header shell renders before the accounts arrive, so waiting on anything else would let an
  // assertion run against an empty list.
  await screen.findByText(/Total users:/);
}

const groupNamed = (heading: string) =>
  screen.getByRole('heading', { name: heading }).closest('section') as HTMLElement;

const ADMINS = 'Admins';
const MODERATORS = 'Moderators';
const REPEATED = 'Participants with more than 5 removals';
const PARTICIPANTS = 'Participants';

/**
 * The accounts a section is showing, in the order it is showing them.
 *
 * Read off the clickable area of each card rather than off the text, because a name query returns
 * the matches in document order but says nothing about which card each one belongs to -- and card
 * order is the entire claim being tested here. Only editable cards have that clickable area, so
 * admin cards -- which carry no editor, since only their own holder may edit them -- are asserted
 * by name text instead.
 */
const namesIn = (section: HTMLElement) =>
  within(section)
    .getAllByRole('button', { name: /^Edit profile for/ })
    .map((card) => (card.getAttribute('aria-label') ?? '').replace('Edit profile for ', ''));

/** Whether `first` is rendered before `second`. */
const precedes = (first: Element, second: Element) =>
  Boolean(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING);

/**
 * Picks `option` from the cohort/sort filter labelled `label`.
 *
 * A Radix trigger is a `div role="combobox"` holding no value, so `fireEvent.change` against it
 * would pass a click that went nowhere and then assert on a control that never moved. It opens on
 * pointer down, and the option is a separate element in a portal, so both steps are needed --
 * `pointerType` included, because Radix reads it to decide this is a mouse-like interaction and
 * without it the trigger simply stays shut.
 */
async function choose(label: string, option: string) {
  openFilter(label);
  fireEvent.click(await screen.findByRole('option', { name: option }));
}

function openFilter(label: string) {
  fireEvent.pointerDown(screen.getByLabelText(label), {
    button: 0,
    ctrlKey: false,
    pointerType: 'mouse',
  });
}

const filterByYear = (year: number | string) => choose('Year', String(year));
const filterByDepartment = (department: string) => choose('Department', department);
const sortBy = (order: string) => choose('Sort', order);

/**
 * Types into one participant section's own search box.
 *
 * There are two boxes now -- the queue's and the Participants' -- and each is scoped to the
 * section it narrows, because that scoping is the property under test: an unscoped placeholder
 * query would match whichever of the two the document order happened to put first, which is
 * exactly the coupling these tests exist to rule out. These suites answer with legacy bare
 * arrays, so the draft narrows its own section live; nothing is submitted here.
 */
const searchParticipants = (term: string) =>
  fireEvent.change(
    within(groupNamed(PARTICIPANTS)).getByPlaceholderText(/Search participants by name/),
    { target: { value: term } },
  );
const searchFlagged = (term: string) =>
  fireEvent.change(
    within(groupNamed(REPEATED)).getByPlaceholderText(/Search flagged participants by name/),
    { target: { value: term } },
  );

describe('ProfileReview: where the filters are', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

it('puts them directly above the ordinary participants and below everything else', async () => {
    await renderPage([admin(), moderator(), participant('Bravo', { removals: 9 }), participant('Alpha')]);

    // Last on the page, hard against the list they exist to narrow and order. Sitting at the top it
    // would have appeared to filter the admin list and the work queue above it as well, and it
    // filters neither -- the queue's order is fixed, and a staff row has no year to match on.
    const yearFilter = screen.getByLabelText('Year');
    expect(precedes(groupNamed(ADMINS), yearFilter)).toBe(true);
    expect(precedes(groupNamed(MODERATORS), yearFilter)).toBe(true);
    expect(precedes(groupNamed(REPEATED), yearFilter)).toBe(true);
    expect(precedes(yearFilter, groupNamed(PARTICIPANTS))).toBe(true);
  });

  it('leaves the four sections in the same order they were in', async () => {
    await renderPage([admin(), moderator(), participant('Alpha'), participant('Bravo', { removals: 9 })]);

    expect(screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent)).toEqual([
      ADMINS,
      MODERATORS,
      REPEATED,
      PARTICIPANTS,
    ]);
  });

  it('keeps the staff sections whole under a participant search, because nothing on them is searched', async () => {
    await renderPage([admin(), moderator(), participant('Alpha'), participant('Bravo')]);

    searchParticipants('Alpha');

    // The participant boxes narrow participant lists alone. Ada and Mo keep their accounts --
    // a participant's name is no reason for either section to lose one -- and neither section
    // ever claims a search it does not have hid somebody: "No admin accounts exist yet" beside
    // a visible admin was the reading the original note on this screen was written to prevent,
    // and a note about a search box that is not there would be the same mistake in reverse.
    expect(within(groupNamed(ADMINS)).getByText('Ada Admin')).toBeInTheDocument();
    expect(namesIn(groupNamed(MODERATORS))).toEqual(['Mo Moderator']);
    expect(
      screen.queryByText('No admin accounts match the current search.'),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText('No moderator accounts match the current search.'),
    ).not.toBeInTheDocument();

    // And the box narrowed the list it sits in: the one participant whose name answers it
    // remains, the other is gone from Participants and from nowhere else.
    expect(namesIn(groupNamed(PARTICIPANTS))).toEqual(['Alpha']);
  });
});

/**
 * Each participant section answers only its own search box.
 *
 * The queue and the Participants list are two views of one population, and each box sits in
 * the heading it narrows rather than in one toolbar for the page. That placement is
 * load-bearing: a query typed into the queue is a question about the queue, and a query typed
 * into Participants is a question about the population below the queue -- letting either one
 * reach the other list, or a staff section, would make one section's question change another
 * section's answer.
 */
describe('ProfileReview: each participant section searches for itself', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("narrows only the queue when the queue's own box is used, leaving Participants and staff whole", async () => {
    await renderPage([
      admin(),
      participant('Alpha', { removals: 9 }),
      participant('Bravo', { removals: 9 }),
      participant('Charlie'),
    ]);

    searchFlagged('Alpha');

    // The queue narrowed by its own query. Nothing below it moved: the flagged rows the
    // queue's box did not name are still participants, and a search typed above them is not
    // their search.
    expect(namesIn(groupNamed(REPEATED))).toEqual(['Alpha']);
    expect(namesIn(groupNamed(PARTICIPANTS))).toEqual(
      expect.arrayContaining(['Alpha', 'Bravo', 'Charlie']),
    );
    expect(within(groupNamed(ADMINS)).getByText('Ada Admin')).toBeInTheDocument();
  });

  it('narrows only Participants when its own box is used, leaving the queue whole', async () => {
    await renderPage([
      participant('Alpha', { removals: 9 }),
      participant('Bravo', { removals: 9 }),
      participant('Charlie'),
    ]);

    searchParticipants('Charlie');

    expect(namesIn(groupNamed(PARTICIPANTS))).toEqual(['Charlie']);
    // The queue is a work queue and its box was not the one used: rows above the threshold
    // stay on it whatever a search typed under it says about their names.
    expect(namesIn(groupNamed(REPEATED))).toEqual(['Alpha', 'Bravo']);
  });
});

describe('ProfileReview: cohort filters', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('narrows the participant cards to one year', async () => {
    await renderPage([
      participant('Alpha', { year: 2 }),
      participant('Bravo', { year: 2 }),
      participant('Charlie', { year: 3 }),
    ]);

    await filterByYear(2);

    expect(namesIn(groupNamed(PARTICIPANTS))).toEqual(['Alpha', 'Bravo']);
  });

  it('narrows the participant cards to one department', async () => {
    await renderPage([
      participant('Alpha', { department: 'CSE' }),
      participant('Bravo', { department: 'ECE' }),
      participant('Charlie', { department: 'CSE' }),
    ]);

    await filterByDepartment('ECE');

    expect(namesIn(groupNamed(PARTICIPANTS))).toEqual(['Bravo']);
  });

  it('narrows to the intersection when a year and a department are both chosen', async () => {
    // The two filters are independent axes rather than one combined cohort, which is what makes
    // "every second-year in the whole platform" reachable at all: choose a year and leave the
    // department on "all", or the reverse.
    await renderPage([
      participant('Alpha', { year: 2, department: 'CSE' }),
      participant('Bravo', { year: 2, department: 'ECE' }),
      participant('Charlie', { year: 3, department: 'CSE' }),
    ]);

    await filterByYear(2);
    await filterByDepartment('CSE');

    expect(namesIn(groupNamed(PARTICIPANTS))).toEqual(['Alpha']);
  });

  it('treats a department registered in another case as the same department', async () => {
    // Departments are free text belonging to the institution, so "CSE" and "cse" are one
    // department as far as an operator is concerned. Two options that each hid half the people in
    // it would be worse than no filter.
    await renderPage([
      participant('Alpha', { department: 'CSE' }),
      participant('Bravo', { department: 'cse' }),
    ]);

    await filterByDepartment('CSE');

    expect(namesIn(groupNamed(PARTICIPANTS))).toEqual(expect.arrayContaining(['Alpha', 'Bravo']));
  });

  it('does not narrow staff accounts, which have no cohort to match', async () => {
    await renderPage([
      admin(),
      moderator(),
      participant('Alpha', { year: 2, department: 'CSE' }),
      participant('Bravo', { year: 3, department: 'ECE' }),
    ]);

    await filterByYear(3);
    await filterByDepartment('ECE');

    // Bravo is the one participant the filters were aimed at. Ada and Mo have no year and no
    // department at all -- the server omits both keys on a staff row -- so there is nothing to
    // compare, and they stay. Hiding the only two accounts an operator can never afford to lose
    // behind a control set to find somebody else would be the wrong way round.
    expect(within(groupNamed(ADMINS)).getByText('Ada Admin')).toBeInTheDocument();
    expect(namesIn(groupNamed(MODERATORS))).toEqual(['Mo Moderator']);
    expect(namesIn(groupNamed(PARTICIPANTS))).toEqual(['Bravo']);
  });

  it('offers only the years and departments that have participants in them', async () => {
    // Nobody is a fourth-year in this deployment. An option for a cohort with no members can only
    // ever produce an empty list, and an operator who picked it would be told the platform has no
    // fourth-years when the truth is that the filter was narrower than they thought.
    await renderPage([
      participant('Alpha', { year: 2, department: 'CSE' }),
      participant('Bravo', { year: 3, department: 'ECE' }),
    ]);

    openFilter('Year');
    expect(await screen.findByRole('option', { name: '2' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: '3' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: '4' })).not.toBeInTheDocument();

    // Both departments, and neither of the two staff accounts' absent cohort has produced a blank
    // option in the list.
    fireEvent.keyDown(screen.getByLabelText('Year'), { key: 'Escape' });
    openFilter('Department');
    expect(await screen.findByRole('option', { name: 'CSE' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'ECE' })).toBeInTheDocument();
  });

  it('says so once when a filter matches nothing, and leaves the way back', async () => {
    // Two headings over two empty grids would be two separate false impressions that the platform
    // has no flagged participants and no participants at all.
    //
    // Reached with a combination rather than with an empty year: the option list only offers
    // cohorts somebody is in, so a year nobody is in cannot be selected in the first place. Two
    // real options whose intersection is empty is the way an operator actually gets here.
    await renderPage([
      participant('Alpha', { year: 2, department: 'CSE' }),
      participant('Bravo', { year: 3, department: 'ECE' }),
    ]);

    await filterByYear(2);
    await filterByDepartment('ECE');

    expect(screen.getByText('No accounts match')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: REPEATED })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: PARTICIPANTS })).not.toBeInTheDocument();

    // The controls are still on the page, so this is a dead end the operator can undo rather than
    // one they have to reload out of.
    expect(screen.getByLabelText('Year')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /show all accounts/i }));

    expect(screen.getByRole('heading', { name: PARTICIPANTS })).toBeInTheDocument();
    expect(namesIn(groupNamed(PARTICIPANTS))).toEqual(['Alpha', 'Bravo']);
  });

  it('still renders every group on a platform with nothing in it', async () => {
    // The no-match message is reachable only once something is narrowing the list. On a genuinely
    // empty platform the sections must render, or the create buttons living in them would be the
    // one thing missing from a page whose whole purpose is making accounts.
    await renderPage([]);

    expect(screen.queryByText('No accounts match')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: ADMINS })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: MODERATORS })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: PARTICIPANTS })).toBeInTheDocument();
  });

  it('puts every account back when the filters are cleared', async () => {
    await renderPage([
      participant('Alpha', { year: 2, department: 'CSE' }),
      participant('Bravo', { year: 3, department: 'ECE' }),
    ]);

    await filterByYear(3);
    expect(namesIn(groupNamed(PARTICIPANTS))).toEqual(['Bravo']);

    fireEvent.click(screen.getByRole('button', { name: /clear filters/i }));

    expect(namesIn(groupNamed(PARTICIPANTS))).toEqual(expect.arrayContaining(['Alpha', 'Bravo']));
  });
});

describe('ProfileReview: card order', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('orders the participants by name in either direction', async () => {
    await renderPage([participant('Charlie'), participant('Alpha'), participant('Bravo')]);

    await sortBy('Name (A-Z)');
    expect(namesIn(groupNamed(PARTICIPANTS))).toEqual(['Alpha', 'Bravo', 'Charlie']);

    await sortBy('Name (Z-A)');
    expect(namesIn(groupNamed(PARTICIPANTS))).toEqual(['Charlie', 'Bravo', 'Alpha']);
  });

  it('orders the participants by year in either direction', async () => {
    // A year is a number, so "10" has to sort after "9". Ordering these as the strings the wire
    // format renders them would put every tenth-year before every ninth-year.
    await renderPage([
      participant('Ninth', { year: 9 }),
      participant('Twelfth', { year: 12 }),
      participant('Tenth', { year: 10 }),
    ]);

    await sortBy('Year (low to high)');
    expect(namesIn(groupNamed(PARTICIPANTS))).toEqual(['Ninth', 'Tenth', 'Twelfth']);

    await sortBy('Year (high to low)');
    expect(namesIn(groupNamed(PARTICIPANTS))).toEqual(['Twelfth', 'Tenth', 'Ninth']);
  });

  it('leaves the flagged section in removal order whatever the sort says', async () => {
    // This section is a work queue: the person with the most content taken down is the one a
    // moderator opens first. Offering removal order as one option among several would let an
    // operator sort the queue away from the order they are meant to work through it in, so the
    // picker does not offer it and cannot override it.
    await renderPage([
      participant('Alpha', { removals: 9 }),
      participant('Bravo', { removals: 6 }),
      participant('Charlie', { removals: 7 }),
    ]);

    expect(namesIn(groupNamed(REPEATED))).toEqual(['Alpha', 'Charlie', 'Bravo']);

    for (const order of ['Name (A-Z)', 'Name (Z-A)', 'Year (low to high)', 'Year (high to low)']) {
      await sortBy(order);
      expect(namesIn(groupNamed(REPEATED))).toEqual(['Alpha', 'Charlie', 'Bravo']);
    }
  });

  it('offers no removal order to pick', async () => {
    await renderPage([participant('Alpha')]);

    openFilter('Sort');

    expect(await screen.findByRole('option', { name: 'Name (A-Z)' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /removal/i })).not.toBeInTheDocument();
  });

  it('leaves the staff sections in the order the server sent them', async () => {
    // Deliberately not alphabetical. A moderator list is not a list anybody is reading by year or
    // by surname, and a staff row has no year to order by in the first place.
    await renderPage([
      admin('Zoe Admin'),
      admin('Amy Admin'),
      moderator('Yves Moderator'),
      moderator('Bea Moderator'),
    ]);

    await sortBy('Name (Z-A)');

    // Admins are read by name text: their cards carry no editor for this viewer, so the
    // `namesIn` button sweep would find nothing at all on them.
    const admins = groupNamed(ADMINS);
    expect(
      precedes(within(admins).getByText('Zoe Admin'), within(admins).getByText('Amy Admin')),
    ).toBe(true);
    expect(namesIn(groupNamed(MODERATORS))).toEqual(['Yves Moderator', 'Bea Moderator']);
  });

  it('narrows the flagged queue by cohort while Participants keeps the whole filtered population', async () => {
    // Filtering by cohort narrows each section; it never moves an account between them. The
    // queue keeps only the matching flagged rows, and Participants keeps everyone who matches
    // -- the flagged ones included, because they are part of the population that list names.
    await renderPage([
      participant('Alpha', { year: 2, removals: 9 }),
      participant('Bravo', { year: 3, removals: 9 }),
      participant('Charlie', { year: 2 }),
    ]);

    await filterByYear(2);

    expect(namesIn(groupNamed(REPEATED))).toEqual(['Alpha']);
    expect(namesIn(groupNamed(PARTICIPANTS))).toEqual(['Alpha', 'Charlie']);
  });

  it('puts an account with no name last whichever direction is chosen', async () => {
    // An unnamed row is not the alphabetically first name, it is a row an operator is reading
    // because of something else -- so it belongs at the end of the list either way round.
    await renderPage([participant('Alpha'), { ...participant('Bravo'), name: null }]);

    await sortBy('Name (A-Z)');
    expect(namesIn(groupNamed(PARTICIPANTS))).toEqual(['Alpha', 'user']);

    await sortBy('Name (Z-A)');
    expect(namesIn(groupNamed(PARTICIPANTS))).toEqual(['Alpha', 'user']);
  });
});