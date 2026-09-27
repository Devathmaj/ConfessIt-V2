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
 * * typing in the search box fires nothing; the Search button (or Enter) submits the
 *   query the database answers, so a half-typed word never costs four reads;
 * * the submitted query, the cohort filters and the sort travel as request params, and
 *   the page on screen is displayed as returned -- never narrowed a second time, which
 *   would hide rows the server correctly sent and break the pager totals beside them;
 * * turning the pager re-reads that same filtered question with a new offset, and brings
 *   the section heading back into view.
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
  limit: 50,
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
    .getAllByRole('button', { name: /^View profile for/ })
    .map((card) => (card.getAttribute('aria-label') ?? '').replace('View profile for ', ''));

const callsWith = () =>
  api.getAdminUsers.mock.calls.map((call) => call[0] as Record<string, unknown>);

describe('ProfileReview: server-driven search', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    nextId = 0;
  });

  it('fires nothing while typing; the Search button submits the query the database answers', async () => {
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
    await screen.findByRole('button', { name: 'View profile for Alpha' });

    // Four reads for the unfiltered first page, and nothing else while the draft changes.
    expect(api.getAdminUsers).toHaveBeenCalledTimes(4);
    fireEvent.change(screen.getByPlaceholderText(/Search by name/), { target: { value: 'Alpha' } });
    expect(api.getAdminUsers).toHaveBeenCalledTimes(4);

    fireEvent.click(screen.getByRole('button', { name: 'Search profiles' }));
    await screen.findByText(/Showing 1 of 1 profiles/);

    // The submitted query went out on every read, staff included.
    expect(api.getAdminUsers).toHaveBeenCalledTimes(8);
    for (const params of callsWith().slice(4)) {
      expect(params.q).toBe('Alpha');
    }
    expect(namesIn(groupNamed('Participants'))).toEqual(['Alpha']);
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
    await screen.findByRole('button', { name: 'View profile for SecondYear' });

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

  it('re-reads the filtered question with a new offset on Next and returns to the section top', async () => {
    const scrollSpy = vi.fn();
    window.HTMLElement.prototype.scrollIntoView = scrollSpy as never;
    try {
      const firstFifty = Array.from({ length: 50 }, (_, index) => participant(`P${index}`));
      const lastOne = [participant('P50')];
      api.getAdminUsers.mockImplementation(async (params: Record<string, unknown> = {}) => {
        if (params.account_type === 'admin' || params.account_type === 'moderator') {
          return emptyPage();
        }
        if (params.flagged === 'flagged') return emptyPage();
        if (params.skip === 50) return pageOf(lastOne, 51, 50);
        return pageOf(firstFifty, 51);
      });
      await renderPage();
      await screen.findByText(/Page 1 of 2/);

      fireEvent.click(screen.getByRole('button', { name: 'Next participants page' }));
      await screen.findByText(/Page 2 of 2/);

      // Same question, next slice: the offset went out and only the new slice came back.
      expect(callsWith().some((params) => params.skip === 50 && params.limit === 50)).toBe(true);
      expect(namesIn(groupNamed('Participants'))).toEqual(['P50']);

      // And the operator is looking at the section heading again, not at the pager that
      // carried them here.
      expect(scrollSpy).toHaveBeenCalled();
      const scrolledTo = (scrollSpy.mock as unknown as { instances: Element[] }).instances[0];
      expect(scrolledTo?.getAttribute('id')).toBe('profile-section-participants');
    } finally {
      // jsdom ships without scrollIntoView; leave the prototype as found.
      delete (window.HTMLElement.prototype as Record<string, unknown>).scrollIntoView;
    }
  });
});
