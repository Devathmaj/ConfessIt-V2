import * as React from 'react';

/**
 * The theme vocabulary and store shared by the provider and every consumer.
 *
 * This is the app's own replacement for next-themes. That library rendered an
 * inline `<script>` into the React tree on the client, which React 19 refuses
 * to execute and warns about ("Encountered a script tag while rendering React
 * component"). In a client-only Vite app the script was redundant anyway:
 * index.html already applies the saved theme before first paint. The provider
 * (`components/theme-provider`) owns the document side effects; this module
 * owns the values, so a consumer only ever imports a hook and a type.
 *
 * The storage key is the one index.html reads, and the attribute vocabulary
 * (`.dark` class, `data-theme`, `theme-color`) is exactly what that pre-paint
 * bootstrap sets.
 */

export type Theme = 'light' | 'dark' | 'system';
export type ResolvedTheme = 'light' | 'dark';

/** The storage key the pre-paint bootstrap in index.html reads. */
export const THEME_STORAGE_KEY = 'vite-ui-theme';

/** The `theme-color` meta value per resolved theme; index.html sets the same pair. */
export const THEME_COLORS: Record<ResolvedTheme, string> = {
  dark: '#181014',
  light: '#FFE5F2',
};

export interface ThemeContextValue {
  /** The stored preference: 'light', 'dark' or 'system'. */
  theme: Theme;
  /** What the preference currently resolves to. */
  resolvedTheme: ResolvedTheme;
  setTheme: (theme: Theme) => void;
}

export const ThemeContext = React.createContext<ThemeContextValue | undefined>(undefined);

export const isTheme = (value: string | null): value is Theme =>
  value === 'light' || value === 'dark' || value === 'system';

/** The stored preference, or `fallback` when nothing valid is stored. */
export const readStoredTheme = (fallback: Theme, storageKey: string): Theme => {
  try {
    const stored = window.localStorage.getItem(storageKey);
    return isTheme(stored) ? stored : fallback;
  } catch {
    // Storage can be blocked (private mode, policy): the default still applies.
    return fallback;
  }
};

export const systemPrefersDark = (): boolean =>
  typeof window !== 'undefined' &&
  typeof window.matchMedia === 'function' &&
  window.matchMedia('(prefers-color-scheme: dark)').matches;

const FALLBACK_THEME: ThemeContextValue = {
  theme: 'dark',
  resolvedTheme: 'dark',
  setTheme: () => undefined,
};

/**
 * The current theme everywhere. Falls back to the product default outside the
 * provider, so a component (or a unit test) rendering standalone still works.
 */
export function useTheme(): ThemeContextValue {
  return React.useContext(ThemeContext) ?? FALLBACK_THEME;
}
