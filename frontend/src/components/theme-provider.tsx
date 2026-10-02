import * as React from "react";
import { ThemeProvider as NextThemesProvider, useTheme } from "next-themes";
import type { ThemeProviderProps } from "next-themes";

/**
 * Mirrors the resolved theme onto `data-theme` (semantic color tokens) and the
 * mobile `theme-color` meta, matching what the pre-paint bootstrap in index.html
 * sets. next-themes owns the `.dark` class; this keeps the two mechanisms in step
 * when the user toggles, so one attribute switch re-skins the whole app with no
 * component-level theme code (color guide §3.2).
 */
function ThemeAttributeSync() {
  const { resolvedTheme } = useTheme();
  React.useEffect(() => {
    if (resolvedTheme !== "light" && resolvedTheme !== "dark") return;
    const root = document.documentElement;
    root.setAttribute("data-theme", resolvedTheme);
    document
      .querySelector('meta[name="theme-color"]')
      ?.setAttribute("content", resolvedTheme === "dark" ? "#181014" : "#FFE5F2");
  }, [resolvedTheme]);
  return null;
}

export function ThemeProvider({ children, ...props }: ThemeProviderProps) {
  return (
    <NextThemesProvider {...props}>
      <ThemeAttributeSync />
      {children}
    </NextThemesProvider>
  );
}
