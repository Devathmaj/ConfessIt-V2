import { describe, it, expect, vi, afterEach } from 'vitest';
import type { ContextType } from 'react';
import { render, screen, waitFor, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { Toaster, toast } from 'sonner';
import { MagicLinkVerification } from '@/services/MagicLinkVerification';
import { AuthContext, type VerifyTokenResult } from '@/contexts/AuthContext';

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
});