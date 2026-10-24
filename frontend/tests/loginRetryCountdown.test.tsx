import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { Toaster, toast } from 'sonner';
import { Login } from '@/pages/Login';

/**
 * Signing in is two steps and only a minted link moves between them.
 *
 * The first screen asks for a registration number and submits it. The server answers
 * every well-formed request identically (200) whether the number is registered or not,
 * so a 200 always opens the verification-pending screen: the submitted number is shown,
 * the panel confirms the email was sent, and the resend button is held until the account
 * cooldown that answer charged has passed -- a live "Resend available in" countdown shows
 * how long. The expiry itself is stated in the email, though an expired token still shows
 * its expired state on the panel. A refusal -- 429, 400 -- never leaves the first screen,
 * because nothing was minted and there is no token to verify. The cooldown and token
 * deadlines are stored as timestamps under the door's own localStorage key, so a refresh
 * restores the cooldown running instead of restarting it.
 *
 * These tests drive the real component against a scripted fetch.
 */

const LOGIN_SENT = 'If this registration number is registered, a login link has been generated. Check the backend console -- and double-check the number for typos.';
const PENDING_KEY = 'login:pending:users';

interface StubResponse {
  ok: boolean;
  status: number;
  body: unknown;
  retryAfter?: number;
}

/**
 * Answers each request in order, capturing the URL and the regno the request carried.
 */
const stubFetch = (responses: StubResponse[]) => {
  const calls: { url: string; regno: string }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    let regno = '';
    if (init?.body) {
      regno = (JSON.parse(String(init.body)) as { regno?: string }).regno ?? '';
    }
    calls.push({ url, regno });
    const next = responses.shift()!;
    return {
      ok: next.ok,
      status: next.status,
      headers: {
        get: (name: string) =>
          name.toLowerCase() === 'retry-after' && next.retryAfter ? String(next.retryAfter) : null,
      },
      json: async () => next.body,
    } as Response;
  });
  vi.stubGlobal('fetch', fetchMock);
  return calls;
};

const successBody = (expiresAt?: string) => ({
  message: LOGIN_SENT,
  expires_at: expiresAt ?? new Date(Date.now() + 15 * 60_000).toISOString(),
});

const refusalBody = (detail: string) => ({ detail });

/** Plants a pending state for the participant door, as the page would have left one. */
const seedPending = (overrides: {
  tokenExpiresAt?: number;
  resendAvailableAt?: number;
} = {}) => {
  const now = Date.now();
  window.localStorage.setItem(
    PENDING_KEY,
    JSON.stringify({
      authenticationState: 'verification_pending',
      validatedRegno: 'shared-reg',
      tokenExpiresAt: overrides.tokenExpiresAt ?? now + 90_000,
      resendAvailableAt: overrides.resendAvailableAt ?? now + 900_000,
    }),
  );
};

const renderPage = () =>
  render(
    <MemoryRouter>
      <Toaster />
      <Login />
    </MemoryRouter>,
  );

const typeAndSubmit = async (regno: string) => {
  const { fireEvent } = await import('@testing-library/react');
  fireEvent.change(screen.getByPlaceholderText(/registration number/i), {
    target: { value: regno },
  });
  fireEvent.click(screen.getByRole('button', { name: /enter confessit|generating link/i }));
};

const resendButton = () => screen.getByRole('button', { name: /resend your magic link/i });

afterEach(() => {
  vi.unstubAllGlobals();
  act(() => toast.dismiss());
  window.localStorage.clear();
});

describe('verification-pending sign-in', () => {
  it('opens a verification screen, holding the resend button behind its cooldown', async () => {
    stubFetch([{ ok: true, status: 200, body: successBody() }]);
    renderPage();

    await typeAndSubmit('shared-reg');

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Verification Pending' })).toBeInTheDocument();
    });
    expect(screen.getByText('shared-reg')).toBeInTheDocument();
    expect(screen.getByText('Magic link sent to your email.')).toBeInTheDocument();
    // The email carries the expiry; the panel stays on the essentials.
    expect(screen.queryByText(/only valid for 15 minutes/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Expires in:/)).not.toBeInTheDocument();
    // The mint answered this registration number, which charged the account cooldown:
    // the resend button is locked and counts the wait down live.
    expect(resendButton()).toBeDisabled();
    expect(screen.getByText(/Resend available in 1[45]:\d{2}\./)).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /change registration number/i }),
    ).toBeInTheDocument();
    // The input screen is gone; its submit button no longer exists.
    expect(screen.queryByRole('button', { name: /enter confessit/i })).not.toBeInTheDocument();
  });

  it('keeps a 429 refusal on the input screen, with no token to verify', async () => {
    // A refusal mints nothing, so it must not open the verification screen or start any timer.
    // The toast is a fixed sentence, never the server's detail: rendering that verbatim
    // would let callers distinguish refusal causes per registration number.
    stubFetch([
      {
        ok: false,
        status: 429,
        body: refusalBody(
          'A login link for this registration number can be requested only once every few minutes.',
        ),
        retryAfter: 120,
      },
    ]);
    renderPage();

    await typeAndSubmit('shared-reg');

    await waitFor(() => {
      expect(screen.getByText(/Failed to generate magic link/i)).toBeInTheDocument();
    });
    expect(screen.queryByText(/only once every few minutes/i)).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'ConfessIt' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Verification Pending' })).not.toBeInTheDocument();
    expect(screen.queryByText(/Resend available in/)).not.toBeInTheDocument();
  });

  it('opens the same pending screen for an unregistered number -- no oracle', async () => {
    // The server answers every well-formed request identically, so the page must not
    // branch either: an unknown number opens the same verification screen with the same
    // timers as a real one. Only a working link distinguishes them, and that arrives
    // out of band.
    stubFetch([{ ok: true, status: 200, body: successBody() }]);
    renderPage();

    await typeAndSubmit('never-registered');

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Verification Pending' })).toBeInTheDocument();
    });
    expect(screen.getByText('never-registered')).toBeInTheDocument();
    expect(screen.getByText(/Resend available in 1[45]:\d{2}\./)).toBeInTheDocument();
    expect(resendButton()).toBeDisabled();
    expect(window.localStorage.getItem(PENDING_KEY)).not.toBeNull();
  });

  it('restores a pending sign-in after refresh, keeping the resend cooldown running', async () => {
    // The cooldown deadline is a timestamp, so a fresh render two seconds later shows
    // ~2s less of the resend wait -- never a restarted fifteen minutes.
    const expiresAt = new Date(Date.now() + 90_000).toISOString();
    const calls = stubFetch([{ ok: true, status: 200, body: successBody(expiresAt) }]);
    const { unmount } = renderPage();

    await typeAndSubmit('shared-reg');

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Verification Pending' })).toBeInTheDocument();
    });
    const firstResendWait = screen.getByText(/Resend available in/).textContent ?? '';

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2000));
    });
    const beforeRefreshResendWait = screen.getByText(/Resend available in/).textContent ?? '';
    unmount();

    // "Refresh": a brand-new component reads the same stored key from this door.
    renderPage();
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Verification Pending' })).toBeInTheDocument();
    });
    const restoredResendWait = screen.getByText(/Resend available in/).textContent ?? '';

    expect(calls).toHaveLength(1); // the refresh performed no new request
    expect(screen.getByText('shared-reg')).toBeInTheDocument();
    // The resend cooldown survived the refresh too: the button is still locked.
    expect(resendButton()).toBeDisabled();
    // Real time elapsed between the first reading and the restored one; the restored
    // countdown shows less time remaining, not the same fresh minutes.
    expect(restoredResendWait).toMatch(/Resend available in \d{2}:\d{2}/);
    expect(restoredResendWait).not.toBe(firstResendWait);
    // And between the two pre-refresh readings too.
    expect(beforeRefreshResendWait).not.toBe(firstResendWait);
  });

  it('shows an expired token as expired while the resend is still cooling down', async () => {
    const expiresAt = new Date(Date.now() + 2000).toISOString();
    stubFetch([{ ok: true, status: 200, body: successBody(expiresAt) }]);
    renderPage();

    await typeAndSubmit('shared-reg');

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Verification Pending' })).toBeInTheDocument();
    });

    await waitFor(
      () => {
        expect(screen.getByText('The link has expired.')).toBeInTheDocument();
      },
      { timeout: 4000 },
    );
    // The cooldown (900s) outlives the token here, so no "request a new one now" claim and
    // the button stays locked under its own countdown.
    expect(screen.queryByText(/You can request a new one now/)).not.toBeInTheDocument();
    expect(resendButton()).toBeDisabled();
    expect(screen.getByText(/Resend available in/)).toBeInTheDocument();
  });

  it('enables resend once the cooldown passes, and a resend restarts the wait', async () => {
    // A pending sign-in whose cooldown has elapsed and whose token is still alive: the
    // resend button must be usable, and using it runs the whole dance again.
    seedPending({ resendAvailableAt: Date.now() - 1000 });
    const calls = stubFetch([{ ok: true, status: 200, body: successBody() }]);
    renderPage();

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Verification Pending' })).toBeInTheDocument();
    });
    expect(resendButton()).toBeEnabled();

    await act(async () => {
      resendButton().click();
    });

    await waitFor(() => {
      // The resend minted a fresh token and recharged the cooldown.
      expect(screen.getByText(/Resend available in 1[45]:\d{2}\./)).toBeInTheDocument();
    });
    expect(resendButton()).toBeDisabled();

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe('http://localhost:8001/auth/login/users/magic');
    expect(calls[0].regno).toBe('shared-reg');
  });

  it('adopts a 429 Retry-After when a resend is refused, relocking the button', async () => {
    seedPending({ resendAvailableAt: Date.now() - 1000 }); // cooldown done
    const calls = stubFetch([
      { ok: false, status: 429, body: refusalBody('Too many attempts. Please wait.'), retryAfter: 45 },
    ]);
    renderPage();

    await waitFor(() => {
      expect(resendButton()).toBeEnabled();
    });

    await act(async () => {
      resendButton().click();
    });

    // The server names the exact wait its window has left; the countdown adopts it.
    await waitFor(() => {
      expect(screen.getByText(/Resend available in 00:4[45]./)).toBeInTheDocument();
    });
    expect(resendButton()).toBeDisabled();
    expect(calls).toHaveLength(1);
    expect(calls[0].regno).toBe('shared-reg');
  });

  it('holds the login form on the exterior while the lid is opening', async () => {
    stubFetch([{ ok: true, status: 200, body: successBody() }]);
    renderPage();

    await typeAndSubmit('shared-reg');

    await waitFor(() => {
      expect(document.querySelector('.heart-locket-stage')).toHaveAttribute(
        'data-locket-state',
        'opening',
      );
    });
    // The form is still painted on the exterior while the hinge moves, though the
    // inaccessible face is made inert so its controls cannot submit a second request.
    expect(screen.getByText(/enter confessit|generating link/i)).toBeInTheDocument();

    await waitFor(() => {
      expect(document.querySelector('.heart-locket-stage')).toHaveAttribute(
        'data-locket-state',
        'open',
      );
    });
    expect(screen.queryByRole('button', { name: /enter confessit/i })).not.toBeInTheDocument();
  });

  it('settles the locket immediately when reduced motion is preferred', async () => {
    const originalMatchMedia = window.matchMedia;
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: query.includes('prefers-reduced-motion'),
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));

    try {
      stubFetch([{ ok: true, status: 200, body: successBody() }]);
      renderPage();

      await typeAndSubmit('shared-reg');

      await waitFor(() => {
        expect(document.querySelector('.heart-locket-stage')).toHaveAttribute(
          'data-locket-state',
          'open',
        );
      });

      await act(async () => {
        screen.getByRole('button', { name: /change registration number/i }).click();
      });

      await waitFor(() => {
        expect(document.querySelector('.heart-locket-stage')).toHaveAttribute(
          'data-locket-state',
          'closed',
        );
      });
      expect(screen.getByRole('heading', { name: 'ConfessIt' })).toBeInTheDocument();
    } finally {
      window.matchMedia = originalMatchMedia;
    }
  });

  it('changes the registration number: closes first, then clears the pending state', async () => {
    stubFetch([{ ok: true, status: 200, body: successBody() }]);
    renderPage();

    await typeAndSubmit('shared-reg');

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Verification Pending' })).toBeInTheDocument();
      expect(document.querySelector('.heart-locket-stage')).toHaveAttribute(
        'data-locket-state',
        'open',
      );
    });
    expect(window.localStorage.getItem(PENDING_KEY)).not.toBeNull();

    await act(async () => {
      screen.getByRole('button', { name: /change registration number/i }).click();
    });

    // The lid is still closing: verification content stays mounted, and the login screen
    // has not been restored yet.
    expect(document.querySelector('.heart-locket-stage')).toHaveAttribute(
      'data-locket-state',
      'closing',
    );
    expect(screen.getByText('Verification Pending')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'ConfessIt' })).not.toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'ConfessIt' })).toBeInTheDocument();
    });
    expect(screen.queryByRole('heading', { name: 'Verification Pending' })).not.toBeInTheDocument();
    expect(screen.queryByText(/Resend available in/)).not.toBeInTheDocument();
    expect(window.localStorage.getItem(PENDING_KEY)).toBeNull();
    expect(document.querySelector('.heart-locket-stage')).toHaveAttribute(
      'data-locket-state',
      'closed',
    );
  });
});
