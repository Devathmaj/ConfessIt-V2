import { describe, it, expect, vi, afterEach } from 'vitest';
import type { ContextType } from 'react';
import { render, screen, waitFor, act } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { Toaster, toast } from 'sonner';
import { MagicLinkVerification } from '@/services/MagicLinkVerification';
import { refreshAccessToken } from '@/services/api';
import { AuthContext, type VerifyTokenResult } from '@/contexts/AuthContext';

// The component's silent-refresh fallback must be hermetic: a real network call to the
// API origin has no server behind it here. Default to "no session", overridden per test.
vi.mock('@/services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/api')>();
  return { ...actual, refreshAccessToken: vi.fn(async () => null) };
});

type AuthContextValue = ContextType<typeof AuthContext>;

const authValue = (
  verifyMagicToken: (token: string) => Promise<VerifyTokenResult>
): AuthContextValue => ({
  user: null,
  isAuthenticated: false,
  isLoading: false,
  token: null,
  logout: vi.fn(),
  verifyMagicToken,
  fetchAndSetUser: vi.fn(),
  can: () => false,
  signInPath: '/login',
});

const renderVerify = (verifyMagicToken: AuthContextValue['verifyMagicToken']) =>
  render(
    <AuthContext.Provider value={authValue(verifyMagicToken)}>
      <MemoryRouter initialEntries={['/verify-login?token=abc123']}>
        <Toaster />
        <MagicLinkVerification />
      </MemoryRouter>
    </AuthContext.Provider>
  );

/** Seeds a pending sign-in for two doors, mirroring what the login pages persist. */
const seedPendingStates = () => {
  const pending = JSON.stringify({
    authenticationState: 'verification_pending',
    validatedRegno: 'MA9',
    tokenExpiresAt: Date.now() + 60_000,
    resendAvailableAt: Date.now() + 300_000,
  });
  window.localStorage.setItem('login:pending:users', pending);
  window.localStorage.setItem('login:pending:admin', pending);
};

const pendingKeys = () =>
  Object.keys(window.localStorage).filter((key) => key.startsWith('login:pending:'));

afterEach(() => {
  window.localStorage.clear();
  act(() => toast.dismiss());
});

describe('MagicLinkVerification: pending login state', () => {
  it('discards every pending login state once the magic link is redeemed', async () => {
    seedPendingStates();
    const verify = vi.fn(async (): Promise<VerifyTokenResult> => ({ success: true, redirectUrl: '/dashboard' }));
    renderVerify(verify);

    await waitFor(() => {
      expect(screen.getByText('Login successful! Welcome back.')).toBeInTheDocument();
    });

    expect(verify).toHaveBeenCalledWith('abc123');
    // The token is single-use server-side: redeeming it twice always fails the second
    // time with "already used" and bounces a fresh login back to /login, so the page
    // must fire exactly once even if its effect re-runs.
    expect(verify).toHaveBeenCalledTimes(1);
    expect(pendingKeys()).toHaveLength(0);
    expect(window.localStorage.getItem('login:pending:users')).toBeNull();
    expect(window.localStorage.getItem('login:pending:admin')).toBeNull();
  });

  it('keeps the pending state when a link is refused, so the person can still resend', async () => {
    seedPendingStates();
    renderVerify(vi.fn(async (): Promise<VerifyTokenResult> => ({ success: false })));

    await waitFor(() => {
      expect(screen.getByText('Your magic link is invalid or has expired.')).toBeInTheDocument();
    });

    expect(pendingKeys()).toHaveLength(2);
  });

  it('treats a failed redemption with a live session as a login, not an expiry', async () => {
    // The same-token race: this tab's redemption lost (already consumed elsewhere) but the
    // login succeeded, so the shared refresh cookie restores the session here.
    seedPendingStates();
    vi.mocked(refreshAccessToken).mockResolvedValueOnce('restored-token');
    const fetchAndSetUser = vi.fn(async (_token: string) => {});
    render(
      <AuthContext.Provider value={{ ...authValue(vi.fn(async (): Promise<VerifyTokenResult> => ({ success: false }))), fetchAndSetUser }}>
        <MemoryRouter initialEntries={['/verify-login?token=abc123']}>
          <Toaster />
          <Routes>
            <Route path="/verify-login" element={<MagicLinkVerification />} />
            <Route path="/" element={<div>DASHBOARD LANDING</div>} />
            <Route path="/login" element={<div>SIGN IN</div>} />
          </Routes>
        </MemoryRouter>
      </AuthContext.Provider>
    );

    await waitFor(() => {
      expect(screen.getByText('DASHBOARD LANDING')).toBeInTheDocument();
    });

    // No expiry toast for a person who is actually signed in, and the pending
    // sign-in state is discarded exactly as on the happy path.
    expect(screen.queryByText('Your magic link is invalid or has expired.')).not.toBeInTheDocument();
    expect(fetchAndSetUser).toHaveBeenCalledWith('restored-token');
    expect(pendingKeys()).toHaveLength(0);
  });
});