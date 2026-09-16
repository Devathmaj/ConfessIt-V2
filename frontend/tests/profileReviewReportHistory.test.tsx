import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { ProfileReview } from '@/pages/ProfileReview';

/**
 * The report tally on the admin user review screen.
 *
 * This screen used to render `Reports: {reported_count ?? 0}` against a field the API never
 * sent, so every participant read as having zero reports no matter how many times their
 * content had been taken down. It now renders the six stored counters, grouped by the kind of
 * content they are about, and these tests hold one distinction above all:
 *
 * `removals` is the authoritative figure and `reports` is not. `reports` counts complaints,
 * which includes every one a moderator dismissed as unfounded, so the two are shown side by
 * side rather than summed into a single number an operator might read as a verdict on somebody.
 * A test asserting only a total would pass against an implementation that had thrown the
 * distinction away.
 *
 * The panel renders inline on every row rather than only for a selected account, so each test
 * here loads exactly one row. That keeps every assertion unambiguous: a whole-document query on
 * a list holding both a participant and a moderator would find the participant's tally while
 * asserting something about the moderator's.
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

const participant = (overrides: Record<string, unknown> = {}) => ({
  id: '11111111-1111-1111-1111-111111111111',
  regno: 'student-1',
  name: 'Some Student',
  email: 'student@school.test',
  class_name: 'Class_9',
  gender: 'other',
  account_type: 'users',
  is_blocked: false,
  report_history: {
    confession: { reports: 0, removals: 0 },
    love_note: { reports: 0, removals: 0 },
    message: { reports: 0, removals: 0 },
  },
  ...overrides,
});

const moderator = () => ({
  id: '22222222-2222-2222-2222-222222222222',
  regno: 'mod-1',
  name: 'A Moderator',
  email: 'mod@school.test',
  account_type: 'moderator',
  is_blocked: false,
});

/** Load exactly the given accounts, so a query cannot match some other row. */
async function renderRows(rows: unknown[]) {
  api.getAdminUsers.mockResolvedValue(rows);
  render(
    <AuthContext.Provider
      value={{
        user: { regno: 'admin-1', account_type: 'admin', capabilities: [], is_blocked: false },
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
  // Waits for the list to arrive, which is also what guarantees the rows under test are present.
  await screen.findByText(/Total users:/);
}

describe('ProfileReview: report history', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows both figures for every kind of content', async () => {
    await renderRows([participant()]);

    // All three kinds are listed even at zero, so the shape of a person's history is the same
    // for everybody rather than appearing only once they have been complained about.
    expect(screen.getByText('Report history')).toBeInTheDocument();
    expect(screen.getByText('Confessions & comments')).toBeInTheDocument();
    expect(screen.getByText('Love notes')).toBeInTheDocument();
    expect(screen.getByText('Chat messages')).toBeInTheDocument();
    expect(screen.getAllByText('removed')).toHaveLength(3);
    expect(screen.getAllByText('0 reported')).toHaveLength(3);
  });

  it('shows the removals and the reports as two separate figures', async () => {
    await renderRows([
      participant({
        report_history: {
          confession: { reports: 4, removals: 2 },
          love_note: { reports: 1, removals: 0 },
          message: { reports: 0, removals: 0 },
        },
      }),
    ]);

    // Two upheld out of four complained about. These are different claims about the person,
    // and collapsing them into "4 reports" would overstate the case by the two that were
    // dismissed as unfounded.
    expect(screen.getByText('4 reported')).toBeInTheDocument();
    expect(screen.getByText('1 reported')).toBeInTheDocument();
    expect(screen.getByText('0 reported')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();
    expect(screen.getAllByText('0')).toHaveLength(2);
  });

  it('marks a participant with removals at the top of the panel', async () => {
    await renderRows([
      participant({
        report_history: {
          confession: { reports: 3, removals: 3 },
          love_note: { reports: 0, removals: 0 },
          message: { reports: 0, removals: 0 },
        },
      }),
    ]);

    expect(screen.getByText('3 removed')).toBeInTheDocument();
  });

  it('does not mark a participant whose reports were all dismissed', async () => {
    await renderRows([
      participant({
        report_history: {
          confession: { reports: 6, removals: 0 },
          love_note: { reports: 0, removals: 0 },
          message: { reports: 0, removals: 0 },
        },
      }),
    ]);

    // Six complaints and nothing upheld is a person who was reported six times and cleared six
    // times. Putting the headline badge on that would tell an operator the opposite of the
    // truth, so the headline tracks removals and only removals.
    expect(screen.getByText('6 reported')).toBeInTheDocument();
    expect(screen.queryByText(/^\d+ removed$/)).not.toBeInTheDocument();
  });

  it('says which of the two figures to act on', async () => {
    await renderRows([participant()]);

    // The panel has to make the authoritative one findable, not merely present.
    expect(screen.getByText(/Removals are the authoritative count/i)).toBeInTheDocument();
  });

  it('renders no tally at all for a staff account', async () => {
    await renderRows([moderator()]);

    // A moderator's row cannot say anything about reports: they have no reportable content.
    // Three rows of zeros would read as a clean record rather than as a field that does not
    // apply to them.
    expect(screen.getByText('A Moderator')).toBeInTheDocument();
    expect(screen.queryByText('Report history')).not.toBeInTheDocument();
    expect(screen.queryByText(/Removals are the authoritative count/i)).not.toBeInTheDocument();
    expect(screen.queryByText('Chat messages')).not.toBeInTheDocument();
  });

  it('reads zero rather than undefined for a participant row sent without a tally', async () => {
    await renderRows([{ ...participant(), report_history: undefined }]);

    // A row whose fields are absent must render "0 removed", not "undefined removed".
    expect(screen.getByText('Report history')).toBeInTheDocument();
    expect(screen.getAllByText('removed')).toHaveLength(3);
    expect(screen.getAllByText('0 reported')).toHaveLength(3);
    expect(screen.queryByText(/undefined/)).not.toBeInTheDocument();
  });

  it('no longer renders the single phantom total', async () => {
    await renderRows([participant({ report_history: undefined })]);

    // The old markup read `Reports: {reported_count ?? 0}`, a field the API never sent, so
    // every participant displayed a hardcoded zero. The bare "Reports:" with one number after
    // it must be gone.
    expect(screen.queryByText(/^Reports:/)).not.toBeInTheDocument();
  });
});