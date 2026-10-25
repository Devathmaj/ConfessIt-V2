import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { ThemeProvider } from '@/components/theme-provider';
import { useTheme } from '@/lib/theme';

/**
 * The theme provider is the app's own replacement for next-themes, which
 * rendered an inline `<script>` into the React tree -- React 19 refuses to
 * execute it and warns about it, and in this client-only Vite app the script
 * was redundant because index.html already bootstraps the theme before first
 * paint. These tests pin the two halves that matter: no script is ever
 * rendered, and the document attributes and persistence behave like that
 * bootstrap.
 */

const Probe = () => {
  const { theme, resolvedTheme, setTheme } = useTheme();
  return (
    <div>
      <span data-testid="theme">{theme}</span>
      <span data-testid="resolved">{resolvedTheme}</span>
      <button type="button" onClick={() => setTheme('light')}>
        set light
      </button>
      <button type="button" onClick={() => setTheme('dark')}>
        set dark
      </button>
      <button type="button" onClick={() => setTheme('system')}>
        set system
      </button>
    </div>
  );
};

const renderProvider = () =>
  render(
    <ThemeProvider defaultTheme="light" storageKey="vite-ui-theme">
      <Probe />
    </ThemeProvider>,
  );

const clearThemeState = () => {
  window.localStorage.clear();
  document.documentElement.className = '';
  document.documentElement.removeAttribute('data-theme');
  document.head.querySelectorAll('meta[name="theme-color"]').forEach((meta) => meta.remove());
};

beforeEach(() => {
  clearThemeState();
  const meta = document.createElement('meta');
  meta.setAttribute('name', 'theme-color');
  document.head.appendChild(meta);
});

afterEach(() => {
  clearThemeState();
});

describe('ThemeProvider', () => {
  it('renders no script tag -- the next-themes warning this replaced', () => {
    const { container } = renderProvider();

    expect(container.querySelector('script')).toBeNull();
  });

  it('applies the saved preference from the bootstrap storage key', () => {
    window.localStorage.setItem('vite-ui-theme', 'dark');
    renderProvider();

    expect(screen.getByTestId('theme')).toHaveTextContent('dark');
    expect(document.documentElement.classList.contains('dark')).toBe(true);
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');
  });

  it('defaults to light with an empty store, like index.html', () => {
    renderProvider();

    expect(screen.getByTestId('resolved')).toHaveTextContent('light');
    expect(document.documentElement.classList.contains('dark')).toBe(false);
  });

  it('applies and persists a toggle', () => {
    renderProvider();

    fireEvent.click(screen.getByRole('button', { name: 'set light' }));

    expect(document.documentElement.classList.contains('dark')).toBe(false);
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
    expect(
      document.querySelector('meta[name="theme-color"]')?.getAttribute('content'),
    ).toBe('#FFE5F2');
    expect(window.localStorage.getItem('vite-ui-theme')).toBe('light');
  });

  it('resolves system against prefers-color-scheme', () => {
    renderProvider();

    fireEvent.click(screen.getByRole('button', { name: 'set system' }));

    // The test environment's matchMedia stub reports light.
    expect(screen.getByTestId('theme')).toHaveTextContent('system');
    expect(screen.getByTestId('resolved')).toHaveTextContent('light');
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
  });

  it('falls back to the light default outside the provider', () => {
    render(<Probe />);

    expect(screen.getByTestId('resolved')).toHaveTextContent('light');
  });
});
