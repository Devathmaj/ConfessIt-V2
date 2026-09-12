import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { PublicOnlyRoute } from './App';
import { AuthContext } from './contexts/AuthContext';

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation(query => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});

const authValue = (user: any, isAuthenticated: boolean) =>
  ({
    user,
    isAuthenticated,
    isLoading: false,
    token: 'token',
    logout: vi.fn(),
    verifyMagicToken: vi.fn(),
    fetchAndSetUser: vi.fn(),
  }) as any;

const renderAtLogin = (value: any) =>
  render(
    <AuthContext.Provider value={value}>
      <MemoryRouter initialEntries={['/login']}>
        <Routes>
          <Route
            path="/login"
            element={
              <PublicOnlyRoute>
                <div>LOGIN SCREEN</div>
              </PublicOnlyRoute>
            }
          />
          <Route path="/dashboard" element={<div>USER DASHBOARD</div>} />
          <Route path="/admin" element={<div>ADMIN DASHBOARD</div>} />
        </Routes>
      </MemoryRouter>
    </AuthContext.Provider>
  );

describe('PublicOnlyRoute', () => {
  it('redirects a signed-in user away from the login screen to their dashboard', async () => {
    renderAtLogin(authValue({ id: '1', name: 'Test' }, true));

    await waitFor(() => {
      expect(screen.getByText('USER DASHBOARD')).toBeInTheDocument();
    });
    expect(screen.queryByText('LOGIN SCREEN')).not.toBeInTheDocument();
  });

  it('sends an admin to the admin dashboard rather than the user one', async () => {
    renderAtLogin(authValue({ id: '1', name: 'Admin', role: 'admin' }, true));

    await waitFor(() => {
      expect(screen.getByText('ADMIN DASHBOARD')).toBeInTheDocument();
    });
  });

  it('still shows the login screen to a signed-out visitor', async () => {
    renderAtLogin(authValue(null, false));

    await waitFor(() => {
      expect(screen.getByText('LOGIN SCREEN')).toBeInTheDocument();
    });
    expect(screen.queryByText('USER DASHBOARD')).not.toBeInTheDocument();
  });
});
