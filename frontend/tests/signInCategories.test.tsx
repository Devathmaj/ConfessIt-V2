import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { Login, AdminLogin, ModeratorLogin } from '@/pages/Login';
import {
  SIGN_IN_CATEGORIES,
  magicLinkEndpoint,
} from '@/lib/signInCategories';
import { ACCOUNT_TYPES } from '@/lib/capabilities';

/**
 * The category is chosen by the door, not by anything typed, so these tests check the two
 * things that make that true in the browser: each page posts to the endpoint for its own
 * account table, and no page can be talked into posting for another. The enforcement itself
 * is server-side (`/auth/login/<table>/magic` reads exactly one table) -- this is the client
 * half of that contract.
 */

/** Captures the URL the form posts to, and answers as the API would. */
const stubFetch = (ok = true) => {
  const calls: string[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    calls.push(String(input));
    return {
      ok,
      status: ok ? 200 : 404,
      json: async () => ({ detail: 'No participant account with that registration number.' }),
    } as Response;
  });
  vi.stubGlobal('fetch', fetchMock);
  return calls;
};

const renderPage = (element: JSX.Element, path: string) =>
  render(<MemoryRouter initialEntries={[path]}>{element}</MemoryRouter>);

beforeEach(() => {
  // A successful send persists a verification-pending state under the door's own key; the
  // page restores it on mount. Tests here would otherwise inherit the previous test's
  // pending sign-in and render the verification screen instead of the door they assert.
  window.localStorage.clear();
});

const typeAndSubmit = async () => {
  const input = screen.getByPlaceholderText(/registration number/i);
  // React needs a change event, not just a value, for the controlled input to update.
  const { fireEvent } = await import('@testing-library/react');
  fireEvent.change(input, { target: { value: 'shared-reg' } });
  fireEvent.click(screen.getByRole('button'));
};

describe('sign-in categories', () => {
  it('has one door per account table, and the table is its own path', () => {
    expect(Object.keys(SIGN_IN_CATEGORIES).sort()).toEqual([...ACCOUNT_TYPES].sort());
    for (const category of Object.values(SIGN_IN_CATEGORIES)) {
      expect(magicLinkEndpoint(category.accountType)).toBe(
        `http://localhost:8001/auth/login/${category.accountType}/magic`
      );
    }
  });

  it('gives every door a distinct path', () => {
    const paths = Object.values(SIGN_IN_CATEGORIES).map((category) => category.path);
    expect(new Set(paths).size).toBe(paths.length);
  });

  it('posts the participant page to the users endpoint', async () => {
    const calls = stubFetch();
    renderPage(<Login />, '/login');
    await typeAndSubmit();

    await waitFor(() => {
      expect(calls).toEqual(['http://localhost:8001/auth/login/users/magic']);
    });
  });

  it('posts the admin page to the admin endpoint', async () => {
    const calls = stubFetch();
    renderPage(<AdminLogin />, '/login/admin');
    await typeAndSubmit();

    await waitFor(() => {
      expect(calls).toEqual(['http://localhost:8001/auth/login/admin/magic']);
    });
  });

  it('posts the moderator page to the moderator endpoint', async () => {
    const calls = stubFetch();
    renderPage(<ModeratorLogin />, '/login/moderator');
    await typeAndSubmit();

    await waitFor(() => {
      expect(calls).toEqual(['http://localhost:8001/auth/login/moderator/magic']);
    });
  });

  it('titles each door so a person can tell which one they are on', () => {
    // The server's own message names the category it looked in, so a regno typed on the
    // wrong door points at the right one. What the page adds is being visibly the right door
    // before anything is submitted.
    stubFetch();
    const { unmount } = renderPage(<ModeratorLogin />, '/login/moderator');
    expect(screen.getByRole('heading', { name: 'Moderator sign-in' })).toBeInTheDocument();
    unmount();

    const admin = renderPage(<AdminLogin />, '/login/admin');
    expect(screen.getByRole('heading', { name: 'Admin sign-in' })).toBeInTheDocument();
    admin.unmount();

    renderPage(<Login />, '/login');
    expect(screen.getByRole('heading', { name: 'ConfessIt' })).toBeInTheDocument();
  });

  it('gives the participant door the themed treatment and the staff doors a plain one', () => {
    // Landing in a moderation console should not look like the participant page. This is
    // presentation only and grants nothing, but it is the visible signal that a person is on
    // a different door.
    expect(SIGN_IN_CATEGORIES.users.themed).toBe(true);
    expect(SIGN_IN_CATEGORIES.admin.themed).toBe(false);
    expect(SIGN_IN_CATEGORIES.moderator.themed).toBe(false);
  });

  it('offers no navigation from one door to another', async () => {
    // The staff doors are not advertised on the participant door. A participant who learns a
    // staff regno gains nothing by looking for another page, and a person who is staff already
    // has the URL. The separation does not depend on this -- the endpoint decides the table,
    // not the links -- so hiding them is a choice about who the page is for, not a control.
    stubFetch();
    const { unmount } = renderPage(<Login />, '/login');
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.queryByText('Admin sign-in')).not.toBeInTheDocument();
    expect(screen.queryByText('Moderator sign-in')).not.toBeInTheDocument();
    unmount();

    renderPage(<AdminLogin />, '/login/admin');
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.queryByText('Moderator sign-in')).not.toBeInTheDocument();
  });
});
