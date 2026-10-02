"use client"

import * as React from "react"
import { Moon, Sun } from "lucide-react"
import { useTheme } from "next-themes"

import { Button } from "@/components/ui/button"

export function ModeToggle() {
  // Driven by theme state, not the CSS dark: variant: inside dashboard navbars
  // the theme is force-scoped to dark, so variant-driven icons would stick on
  // the moon forever. Rendering from resolvedTheme keeps the sun/moon swap
  // working everywhere without touching the navbar's own colors.
  const { theme, setTheme, resolvedTheme } = useTheme()
  const active = resolvedTheme ?? theme ?? "dark"
  const next = active === "light" ? "dark" : "light"

  return (
    <Button
      variant="outline"
      size="icon"
      onClick={() => setTheme(next)}
      aria-label={`Switch to ${next} mode`}
      title={`Switch to ${next} mode`}
      className="min-h-[44px] min-w-[44px] border-[var(--border-strong)]"
    >
      {active === "dark" ? (
        <Moon className="h-[1.2rem] w-[1.2rem]" />
      ) : (
        <Sun className="h-[1.2rem] w-[1.2rem]" />
      )}
      <span className="sr-only">Switch to {next} mode</span>
    </Button>
  )
}
