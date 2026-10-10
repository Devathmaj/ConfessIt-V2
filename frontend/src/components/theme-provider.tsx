import * as React from "react";

import {
  THEME_COLORS,
  THEME_STORAGE_KEY,
  ThemeContext,
  readStoredTheme,
  systemPrefersDark,
  type ResolvedTheme,
  type Theme,
} from "@/lib/theme";

/**
 * The theme store for the whole app. One provider, one `useTheme` (from
 * `@/lib/theme`).
 *
 * A small in-house replacement for next-themes, whose provider renders an
 * inline `<script>` into the React tree -- React 19 refuses to execute it and
 * warns about it, and in a client-only Vite app the script is redundant
 * because index.html already applies the saved theme before first paint.
 * This provider keeps the same behaviour without the script:
 *
 * - the saved preference lives under `vite-ui-theme` (the key index.html reads);
 * - the resolved theme toggles the `.dark` class, sets `data-theme`, sets
 *   `color-scheme` and updates the `theme-color` meta -- exactly the attributes
 *   the pre-paint bootstrap sets;
 * - `system` follows `prefers-color-scheme` and re-resolves when it changes;
 * - another tab's preference is picked up through the `storage` event.
 */

interface ThemeProviderProps {
  children: React.ReactNode;
  /** Used until a stored preference exists. The product default is dark. */
  defaultTheme?: Theme;
  /** Where the preference is persisted; index.html reads the same key. */
  storageKey?: string;
}

export function ThemeProvider({
  children,
  defaultTheme = "dark",
  storageKey = THEME_STORAGE_KEY,
}: ThemeProviderProps) {
  const [theme, setThemeState] = React.useState<Theme>(() =>
    readStoredTheme(defaultTheme, storageKey),
  );
  const [systemDark, setSystemDark] = React.useState<boolean>(systemPrefersDark);

  const resolvedTheme: ResolvedTheme =
    theme === "system" ? (systemDark ? "dark" : "light") : theme;

  // Follow the OS while the preference is "system". One listener for the
  // provider's lifetime; `systemDark` only feeds resolution, so an explicit
  // light/dark preference ignores its updates.
  React.useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (event: MediaQueryListEvent) => setSystemDark(event.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  // The one place a resolved theme reaches the document. The pre-paint
  // bootstrap already ran, so the first pass here is a no-op in practice --
  // it exists so a toggle (or a system flip) re-skins the app immediately.
  React.useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle("dark", resolvedTheme === "dark");
    root.setAttribute("data-theme", resolvedTheme);
    root.style.colorScheme = resolvedTheme;
    document
      .querySelector('meta[name="theme-color"]')
      ?.setAttribute("content", THEME_COLORS[resolvedTheme]);
  }, [resolvedTheme]);

  const setTheme = React.useCallback(
    (next: Theme) => {
      setThemeState(next);
      try {
        window.localStorage.setItem(storageKey, next);
      } catch {
        // A blocked store still applies for this session.
      }
    },
    [storageKey],
  );

  // Another tab's preference wins here too, the way next-themes behaved.
  React.useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key !== storageKey) return;
      setThemeState(readStoredTheme(defaultTheme, storageKey));
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [defaultTheme, storageKey]);

  const value = React.useMemo(
    () => ({ theme, resolvedTheme, setTheme }),
    [theme, resolvedTheme, setTheme],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}
