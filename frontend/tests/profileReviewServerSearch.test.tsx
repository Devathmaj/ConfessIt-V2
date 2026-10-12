import { fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { ProfileReview } from '@/pages/ProfileReview';

/**
 * Search, filters and pagination against the real paginated server.
 *
 * The sibling suites mock `getAdminUsers` with a bare array -- one unfiltered list the
 * screen narrows itself -- and pin the legacy behaviour. These tests answer envelopes
 * (`{users, total, skip, limit}`) the way the paginated endpoint does, and pin the
 * server-driven contract instead:
 *
 * * typing in a section's search box fires nothing; that section's Search button (or Enter)
 *   submits the query its read is issued with, so a half-typed word never costs four reads
 *   and no section's question reaches another section's list;
 * * each participant section searches for itself: the queue's query travels on the flagged
 *   read alone and the Participants query on the ordinary read alone, while the staff reads
 *   carry no query at all -- the staff sections have no search box to ask one with, and a
 *   search submitted in one participant section leaves the other section's page untouched;
 * * the submitted query, the cohort filters and the sort travel as request params, and
 *   the page on screen is displayed as returned -- never narrowed a second time, which
 *   would hide rows the server correctly sent and break the pager totals beside them;
 * * turning the pager re-reads that same filtered question with a new offset, and brings
 *   the section heading back into view;
 * * the flagged queue and the Participants list overlap by design: the ordinary read asks
 *   for the whole participant population, so a flagged participant is in both sections and
 *   the screen must not treat the two reads as disjoint;
 * * the section badge reports the filtered group's server total -- fifty-two accounts
 *   still reads fifty-two while one page of fifty-one is on screen, because the badge
 *   answers how many accounts exist, not how many cards happen to be rendered.
 */

const api = vi.hoisted(() => ({
  getAdminUsers: vi.fn(),
  setAdminUserBlocked: vi.fn(),
  createAdminUser: vi.fn(),
  updateAdminUser: vi.fn(),
  uploadAdminUserPicture: vi.fn(),
  bulkUploadAdminUsers: vi.fn(),
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

const participant = (name: string, overrides: Record<string, unknown> = {}) => ({
  id: anId(),
  regno: `student-${name}`,
  name,
  email: `${name.toLowerCase()}@school.test`,
  year: 2,
  department: 'CSE',
  account_type: 'users',
  is_blocked: false,
  report_history: {
    confession: { reports: 0, removals: 0 },
    love_note: { reports: 0, removals: 0 },
    message: { reports: 0, removals: 0 },
  },
  ...overrides,
});

const pageOf = (rows: unknown[], total: number, skip = 0) => ({
  users: rows,
  total,
  skip,
  limit: 51,
});

const emptyPage = () => pageOf([], 0);

async function renderPage() {
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
  await screen.findByText(/Total users:/);
}

const groupNamed = (heading: string) =>
  screen.getByRole('heading', { name: heading }).closest('section') as HTMLElement;

const namesIn = (section: HTMLElement) =>
  within(section)
    .getAllByRole('button', { name: /^Edit profile for/ })
    .map((card) => (card.getAttribute('aria-label') ?? '').replace('Edit profile for ', ''));

const callsWith = () =>
  api.getAdminUsers.mock.calls.map((call) => call[0] as Record<string, unknown>);

describe('ProfileReview: server-driven search', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    nextId = 0;
  });

  it('fires nothing while typing; the Participants box sends its query on the Participants read alone', {
    // Four sequential waited reads: same long-test rationale as its sibling below.
    timeout: 15000,
  }, async () => {
    const alpha = participant('Alpha');
    api.getAdminUsers.mockImplementation(async (params: Record<string, unknown> = {}) => {
      if (params.account_type === 'admin' || params.account_type === 'moderator') {
        return emptyPage();
      }
      if (params.flagged === 'flagged') return emptyPage();
      if (params.q === 'Alpha') return pageOf([alpha], 1);
      return pageOf([alpha, participant('Bravo')], 2);
    });
    await renderPage();
    await screen.findByRole('button', { name: 'Edit profile for Alpha' });

    // Four reads for the unfiltered first page, and nothing else while the draft changes.
    expect(api.getAdminUsers).toHaveBeenCalledTimes(4);
    fireEvent.change(screen.getByPlaceholderText(/Search participants by name/), {
      target: { value: 'Alpha' },
    });
    expect(api.getAdminUsers).toHaveBeenCalledTimes(4);

    fireEvent.click(screen.getByRole('button', { name: 'Search participants' }));
    await screen.findByText(/Showing 1 of 1 profiles/);

    // The submitted query travelled on the ordinary participant read alone. The staff reads
    // carry no query -- their sections have no search box, and a participant's name is no
    // reason for either to lose an account -- and the queue answers its own box, not this one.
    expect(api.getAdminUsers).toHaveBeenCalledTimes(8);
    const submitted = callsWith().slice(4);
    const ordinaryRead = submitted.find(
      (params) => params.account_type === 'users' && params.flagged !== 'flagged',
    );
    expect(ordinaryRead?.q).toBe('Alpha');
    for (const params of submitted) {
      if (params.account_type === 'admin' || params.account_type === 'moderator') {
        expect(params.q).toBeUndefined();
      }
      if (params.flagged === 'flagged') expect(params.q).toBeUndefined();
    }
    expect(namesIn(groupNamed('Participants'))).toEqual(['Alpha']);
    // The badge names the filtered group, not the page: one match, singular.
    expect(within(groupNamed('Participants')).getByText('1 account')).toBeInTheDocument();
  });

  it('sends the queue search on the flagged read alone, leaving Participants as its own question', async () => {
    const flaggedRow = participant('Flagged', {
      report_history: {
        confession: { reports: 8, removals: 7 },
        love_note: { reports: 0, removals: 0 },
        message: { reports: 0, removals: 0 },
      },
    });
    const normal = participant('Normal');
    api.getAdminUsers.mockImplementation(async (params: Record<string, unknown> = {}) => {
      if (params.account_type === 'admin' || params.account_type === 'moderator') {
        return emptyPage();
      }
      if (params.flagged === 'flagged') {
        return params.q === 'Flagged' ? pageOf([flaggedRow], 1) : emptyPage();
      }
      return pageOf([flaggedRow, normal], 2);
    });
    await renderPage();
    await screen.findAllByRole('button', { name: 'Edit profile for Normal' });

    // The queue starts empty under this answer; typing into its box is still only a draft.
    expect(api.getAdminUsers).toHaveBeenCalledTimes(4);
    fireEvent.change(screen.getByPlaceholderText(/Search flagged participants by name/), {
      target: { value: 'Flagged' },
    });
    expect(api.getAdminUsers).toHaveBeenCalledTimes(4);

    fireEvent.click(screen.getByRole('button', { name: 'Search flagged participants' }));
    await within(groupNamed('Participants with more than 5 removals')).findByRole('button', {
      name: 'Edit profile for Flagged',
    });

    // The queue narrowed by its own query...
    expect(api.getAdminUsers).toHaveBeenCalledTimes(8);
    const submitted = callsWith().slice(4);
    expect(submitted.find((params) => params.flagged === 'flagged')?.q).toBe('Flagged');
    expect(namesIn(groupNamed('Participants with more than 5 removals'))).toEqual(['Flagged']);

    // ...and the list below it did not move: the ordinary read carried no query of the
    // queue's, so a row the queue's box has never heard of stays in the population, and no
    // staff read was issued a query at all.
    const ordinaryRead = submitted.find(
      (params) => params.account_type === 'users' && params.flagged !== 'flagged',
    );
    expect(ordinaryRead?.q).toBeUndefined();
    for (const params of submitted) {
      if (params.account_type === 'admin' || params.account_type === 'moderator') {
        expect(params.q).toBeUndefined();
      }
    }
    expect(namesIn(groupNamed('Participants'))).toEqual(['Flagged', 'Normal']);
  });

  it("keeps the queue's page when the Participants box is submitted", {
    // Fifty-one rows on the queue's first page: same budget rationale as the sibling above.
    timeout: 15000,
  }, async () => {
    const firstPage = Array.from({ length: 51 }, (_, index) => participant(`P${index}`));
    const lastOne = participant('P51');
    const alpha = participant('Alpha');
    api.getAdminUsers.mockImplementation(async (params: Record<string, unknown> = {}) => {
      if (params.account_type === 'admin' || params.account_type === 'moderator') {
        return emptyPage();
      }
      if (params.flagged === 'flagged') {
        return params.skip === 51 ? pageOf([lastOne], 52, 51) : pageOf(firstPage, 52);
      }
      return params.q === 'Alpha'
        ? pageOf([alpha], 1)
        : pageOf([alpha, participant('Bravo')], 2);
    });
    await renderPage();
    await screen.findByText(/Page 1 of 2/);

    fireEvent.click(screen.getByRole('button', { name: 'Next flagged page' }));
    await screen.findByText(/Page 2 of 2/);

    // A search submitted in Participants is that section's question: it re-reads its own
    // first page and leaves the queue exactly where the operator put it.
    fireEvent.change(screen.getByPlaceholderText(/Search participants by name/), {
      target: { value: 'Alpha' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Search participants' }));
    await screen.findByText(/Showing 1 of 1 profiles/);

    const submitted = callsWith().slice(-4);
    const flaggedRead = submitted.find((params) => params.flagged === 'flagged');
    expect(flaggedRead?.skip).toBe(51);
    const ordinaryRead = submitted.find(
      (params) => params.account_type === 'users' && params.flagged !== 'flagged',
    );
    expect(ordinaryRead?.skip).toBe(0);
    expect(ordinaryRead?.q).toBe('Alpha');

    // Still on the queue's second page, holding the row that page holds.
    expect(screen.getByText(/Page 2 of 2/)).toBeInTheDocument();
    expect(namesIn(groupNamed('Participants with more than 5 removals'))).toEqual(['P51']);
  });

  it('sends the cohort filters to the database and pages the filtered question', async () => {
    const secondYear = participant('SecondYear', { year: 2 });
    api.getAdminUsers.mockImplementation(async (params: Record<string, unknown> = {}) => {
      if (params.account_type === 'admin' || params.account_type === 'moderator') {
        return emptyPage();
      }
      if (params.flagged === 'flagged') return emptyPage();
      if (params.year === 2) return pageOf([secondYear], 1);
      return pageOf([secondYear, participant('ThirdYear', { year: 3 })], 2);
    });
    await renderPage();
    await screen.findByRole('button', { name: 'Edit profile for SecondYear' });

    fireEvent.pointerDown(screen.getByLabelText('Year'), {
      button: 0,
      ctrlKey: false,
      pointerType: 'mouse',
    });
    fireEvent.click(await screen.findByRole('option', { name: '2' }));
    await screen.findByText(/Showing 1 of 1 profiles/);

    // The year travelled as a request param, and the page on screen is the filtered page:
    // one matching participant, total one, no second client-side narrowing.
    expect(callsWith().some((params) => params.year === 2)).toBe(true);
    expect(namesIn(groupNamed('Participants'))).toEqual(['SecondYear']);
  });

  it('shows a flagged participant in the queue and in the Participants list', async () => {
    const flagged = participant('Flagged', {
      report_history: {
        confession: { reports: 8, removals: 7 },
        love_note: { reports: 0, removals: 0 },
        message: { reports: 0, removals: 0 },
      },
    });
    const normal = participant('Normal');
    api.getAdminUsers.mockImplementation(async (params: Record<string, unknown> = {}) => {
      if (params.account_type === 'admin' || params.account_type === 'moderator') {
        return emptyPage();
      }
      if (params.flagged === 'flagged') return pageOf([flagged], 1);
      return pageOf([flagged, normal], 2);
    });
    await renderPage();
    await screen.findAllByRole('button', { name: 'Edit profile for Normal' });

    // In the queue and in the population: the queue is a view of rows the ordinary read
    // also returns, and the screen must show the same row in both sections.
    expect(namesIn(groupNamed('Participants with more than 5 removals'))).toEqual(['Flagged']);
    expect(namesIn(groupNamed('Participants'))).toEqual(['Flagged', 'Normal']);
    expect(within(groupNamed('Participants')).getByText('2 accounts')).toBeInTheDocument();

    // Read one: no read may ask the server to exclude the queue from the population.
    // `flagged: 'ordinary'` is what used to hide flagged participants from Participants.
    expect(callsWith().every((params) => params.flagged !== 'ordinary')).toBe(true);
  });

  it('re-reads the filtered question with a new offset on Next and returns to the section top', {
    // Two full page renders with fifty-one rows each: under a loaded parallel run this
    // legitimately exceeds the 5s default, so the budget -- not the assertions -- grows.
    timeout: 15000,
  }, async () => {
    const scrollSpy = vi.fn();
    window.HTMLElement.prototype.scrollIntoView = scrollSpy as never;
    try {
      const firstPage = Array.from({ length: 51 }, (_, index) => participant(`P${index}`));
      const lastOne = [participant('P51')];
      api.getAdminUsers.mockImplementation(async (params: Record<string, unknown> = {}) => {
        if (params.account_type === 'admin' || params.account_type === 'moderator') {
          return emptyPage();
        }
        if (params.flagged === 'flagged') return emptyPage();
        if (params.skip === 51) return pageOf(lastOne, 52, 51);
        return pageOf(firstPage, 52);
      });
      await renderPage();
      await screen.findByText(/Page 1 of 2/);

      // Fifty-two accounts in the filtered group, fifty-one of them rendered: the badge
      // must count the group, not the cards.
      expect(within(groupNamed('Participants')).getByText('52 accounts')).toBeInTheDocument();
      expect(namesIn(groupNamed('Participants'))).toHaveLength(51);

      fireEvent.click(screen.getByRole('button', { name: 'Next participants page' }));
      await screen.findByText(/Page 2 of 2/);

      // Same question, next slice: the offset went out and only the new slice came back.
      expect(callsWith().some((params) => params.skip === 51 && params.limit === 51)).toBe(true);
      expect(namesIn(groupNamed('Participants'))).toEqual(['P51']);
      expect(within(groupNamed('Participants')).getByText('52 accounts')).toBeInTheDocument();

      // And the operator is looking at the section heading again, not at the pager that
      // carried them here.
      expect(scrollSpy).toHaveBeenCalled();
      const scrolledTo = (scrollSpy.mock as unknown as { instances: Element[] }).instances[0];
      expect(scrolledTo?.getAttribute('id')).toBe('profile-section-participants');
    } finally {
      // jsdom ships without scrollIntoView; leave the prototype as found.
      delete (window.HTMLElement.prototype as unknown as Record<string, unknown>).scrollIntoView;
    }
  });
});
