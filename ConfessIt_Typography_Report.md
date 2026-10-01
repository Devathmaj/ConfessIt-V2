# Improving ConfessIt's Typography

Oct 12, 2026

## The recommendation in one sentence

**Replace Montserrat-for-everything with three voices: Fraunces for display, Plus Jakarta Sans for interface text, and DM Mono for numbers and codes.**

Montserrat is wide, geometric and everywhere, so it reads as a template and fights the soft plum-and-coral / pink-and-berry direction. A warm, rounded serif for headlines gives ConfessIt the romantic, editorial voice that `font-dancing` was meant to supply but never delivered. A friendly, contemporary sans keeps buttons, chips and forms crisp. Fraunces italic replaces the broken script accent, so the roughly 50 existing `font-dancing` classes start working without being rewritten.

This report covers typefaces, sizes, weights, spacing and font loading only.

## 01 / Diagnosis: why the current fonts feel generic

The current setup is one family doing every job, plus a script accent that silently fails. That is why headings, buttons and body copy all read at the same "voice".

| Finding | Evidence in the current setup | Effect |
| --- | --- | --- |
| One voice everywhere | Montserrat on `body`, all `h1`-`h6` (bold), buttons and cards | No contrast between emotional headlines and functional UI text |
| Script accent is broken | `font-dancing` used about 50 times, never defined or imported; falls back to Montserrat | The romantic headings (About, Confessions, Login, Love Notes, Matchmaking, countdown) render as plain bold sans |
| Overused geometric sans | Montserrat is a very common default | Feels like a template; wide letterforms take up room on narrow cards and chips |
| Faux bold | `font-extrabold` (800) used, but only 300-700 are loaded | Browser synthesizes the weight; inconsistent rendering |
| Five static weights | 300, 400, 500, 600, 700 loaded as separate files | More requests than one variable font would need |
| Inconsistent mono | `font-mono` uses the OS default stack | Clock, score and code text looks different on every device |
| Stray default sans | `font-sans` used once (`UserDashboard`) | A second, unrelated body font in one place |
| Sub-legible sizes | Arbitrary `text-[10px]` and `text-[11px]` | Too small for comfortable reading, especially on pastel or dark gradients |
| Heavy label styling | `uppercase` with `tracking-widest` and `tracking-[0.2em]` | Very wide tracking on a wide font looks shouty and breaks words apart |
| Duplicate tokens | Font family declared in both `index.css` and `tailwind.config.ts` | The two can drift apart |

### Highest-priority fixes

1. Give headlines a distinct display typeface so hierarchy comes from type, not just size and color.
2. Make `font-dancing` resolve to a real font immediately; it is the quickest visible improvement.
3. Load only weights that are used, ideally as variable fonts, and stop relying on synthesized 800.
4. Set a 12px floor for all text and cap uppercase tracking at about 0.12em.
5. Keep one source of truth for font tokens.

## 02 / Font families

Four roles, three font families. All are free on Google Fonts, so the existing CSP (`fonts.googleapis.com` and `fonts.gstatic.com`) keeps working.

| Role | Font | Styles to load | Used for |
| --- | --- | --- | --- |
| Display | Fraunces | Roman 500-700, variable; optical size and SOFT axes | Page titles, section headings, wordmark, status titles |
| Accent (replaces `font-dancing`) | Fraunces Italic | Italic 400-600, same variable file | Taglines, love-note flourishes, short romantic phrases |
| UI and body | Plus Jakarta Sans | 400-800, variable | Body text, nav, buttons, chips, forms, card titles, captions |
| Numeric and code | DM Mono | 400 and 500 | Countdown, scores, dates, game tiles, codes |

### Why these three

- **Fraunces** is a soft, high-contrast serif with a variable "SOFT" axis. At SOFT 100 the terminals turn round and gentle, which suits a romantic product and sits well beside plum and berry without looking like a wedding invitation. Its optical-size axis keeps large headings sharp and small text sturdy.
- **Plus Jakarta Sans** has friendly, slightly narrower proportions than Montserrat, so more fits on a profile card, and it stays clean at 12-14px.
- **DM Mono** is rounded and light-feeling, so numerals do not look like a terminal. It also matches the geometry of the sans.
- **Fraunces Italic as the accent** removes the legibility problems of a connected script at small sizes (the countdown label is `text-lg`), and it needs only one family for both display and accent.

### Recommended variable-font settings for Fraunces

| Setting | Value | Reason |
| --- | --- | --- |
| `SOFT` | 100 | Rounded, warm terminals |
| `WONK` | 0 | Avoids quirky leaning letterforms in headings |
| `opsz` | auto (`font-optical-sizing: auto`) | Browser picks the right optical size per font-size |
| Heading weight | 600 | Strong without feeling heavy |
| Accent weight | 400-500 italic | Light and expressive |

### Fallback stacks

| Role | Stack |
| --- | --- |
| Display and accent | `"Fraunces", "Iowan Old Style", "Palatino Linotype", Georgia, serif` |
| UI and body | `"Plus Jakarta Sans", ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif` |
| Numeric and code | `"DM Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace` |

### Alternates if you want a different feel

| If you want | Swap in | Trade-off |
| --- | --- | --- |
| A more fashion-editorial display | Instrument Serif (400 and italic only) | Single weight; headings cannot go bold, so use larger sizes |
| A more neutral UI sans | DM Sans or Manrope | Less personality than Plus Jakarta Sans |
| A handwritten signature moment | Caveat (400-700) | Use only for a sign-off line on love notes, never for headings or status text |
| A more technical mono | JetBrains Mono | Less soft; fine for admin screens |

## 03 / Type scale

The scale keeps Tailwind's default steps, so existing `text-*` classes stay valid. Roles tell you which font, weight, spacing and line height go with each step. Sizes are mobile first, then desktop (`lg:` and up).

| Role | Font | Size (mobile / desktop) | Tailwind steps | Weight | Line height | Tracking |
| --- | --- | --- | --- | --- | --- | --- |
| Display (hero title) | Fraunces | 36px / 60px | `text-4xl` / `text-6xl` | 600 | 1.05 | -0.02em |
| H1 (page title) | Fraunces | 30px / 48px | `text-3xl` / `text-5xl` | 600 | 1.1 | -0.015em |
| H2 (section) | Fraunces | 24px / 30px | `text-2xl` / `text-3xl` | 600 | 1.2 | -0.01em |
| H3 (card or status title) | Fraunces | 20px / 24px | `text-xl` / `text-2xl` | 600 | 1.25 | -0.005em |
| H4 (card name, list title) | Plus Jakarta Sans | 18px | `text-lg` | 700 | 1.35 | 0 |
| Accent (was `font-dancing`) | Fraunces Italic | 24px / 36px | `text-2xl` / `text-4xl` | 400-500 | 1.2 | 0 |
| Body large | Plus Jakarta Sans | 18px | `text-lg` | 400 | 1.6 | 0 |
| Body | Plus Jakarta Sans | 16px | `text-base` | 400 | 1.6 | 0 |
| Body small | Plus Jakarta Sans | 14px | `text-sm` | 400-500 | 1.5 | 0 |
| Button and nav label | Plus Jakarta Sans | 14px / 16px | `text-sm` / `text-base` | 600 | 1.25 | 0.01em |
| Caption and meta | Plus Jakarta Sans | 12px | `text-xs` | 500 | 1.4 | 0.01em |
| Chip | Plus Jakarta Sans | 12px | `text-xs` | 600 | 1.2 | 0.01em |
| Eyebrow (uppercase) | Plus Jakarta Sans | 12px | `text-xs` | 700 | 1.3 | 0.12em |
| Numeric (countdown, score) | DM Mono | 24px | `text-2xl` | 500 | 1 | 0.02em |
| Code and game tiles | DM Mono | 14px-24px | `text-sm` to `text-2xl` | 500-600 | 1.2 | 0.08em |

### Scale rules

- **Floor of 12px.** Replace every `text-[10px]` and `text-[11px]` with `text-xs`.
- **No serif below 20px.** Fraunces is for headings and accents; anything smaller stays in Plus Jakarta Sans, except confession bodies at 18px.
- **No weight below 400 for text, and no 300 below 18px.** Thin text on a gradient background gets lost.
- **Tighten big serif headlines.** Negative tracking at display sizes; none at body sizes.
- **Cap uppercase tracking at 0.12em.** Retire `tracking-widest` and `tracking-[0.2em]` on labels.
- **Fluid hero only.** Use `clamp(2.25rem, 1.4rem + 3.6vw, 3.75rem)` for Display; keep the other steps on fixed breakpoints so layouts stay predictable.
- **Measure.** Keep reading text to about 60-75 characters per line (`max-w-prose`).
- **Balanced headings.** Add `text-wrap: balance` to Display, H1-H3 and accent lines to avoid one-word last lines.

## 04 / Component mapping

Apply roles by function, not by page, so the same element looks the same everywhere.

| Component | Role | Notes |
| --- | --- | --- |
| Wordmark ("ConfessIt") | Fraunces 600, 20px, -0.01em | Keep it the only place Fraunces appears in the navbar |
| Page heading ("Random Matchmaking") | Display or H1 | Drop the extra `font-dancing` wrapper; the base heading style now does the work |
| Tagline ("Find your perfect Valentine match") | Accent, 18-24px | Fraunces italic, one line, never in a button or chip |
| Navbar links | Button and nav label, 14px 600 | Sentence case; no uppercase |
| Profile card name | H4, 18px 700 | Plus Jakarta Sans so long names stay compact |
| Profile meta (age, gender) | Caption, 12px 500 | Pair label and value at the same size; bold the value only |
| Interest chips | Chip, 12px 600 | One line, no wrapping inside a chip |
| Primary and secondary buttons (`.btn-romantic`, `.btn-love`) | Button label, 16px 600 | Sentence case by default; uppercase only for tiny tertiary actions |
| Waiting and status titles | H3, Fraunces 600, 20-24px | Supporting line below in Body small |
| Countdown and clocks | Numeric, DM Mono 500, 24px | Label above in Eyebrow; numerals never wrap |
| Confession body | Fraunces 400 at 18px, line height 1.6 | Long text stays upright, not italic. Love-note bodies are not part of this change: they keep their current font and color |
| Love-note flourish or sign-off | Accent, Fraunces italic | Short phrases only, under about 12 words |
| Forms and inputs | Body 16px | Keep inputs at 16px minimum so mobile browsers do not zoom on focus |
| Admin dashboards | Plus Jakarta Sans for headings (700) and body | Utility screens do not need the romantic serif |
| Wordle and anagram tiles, codes | Code and game tiles, DM Mono 600 | Uppercase with 0.08em tracking |
| Tables and charts | Body small, DM Mono for figures | Numbers align in columns |

## 05 / Light and dark mode type behavior

The typefaces, sizes and line heights stay identical in both modes so the layout never shifts when the theme toggles. Only fine rendering details change, because light text on a dark background looks heavier and slightly bolder than the same text on a light background.

| Property | Light (P1) | Dark (D5) |
| --- | --- | --- |
| Heading weight (Fraunces) | 600 | 560 (the variable axis allows in-between weights) |
| Body weight | 400 | 400 |
| Secondary text weight | 500 | 450 |
| Body letter spacing | 0 | +0.005em |
| Body line height | 1.6 | 1.65 |
| Font smoothing | Browser default | `-webkit-font-smoothing: antialiased` (reduces blooming on macOS and iOS) |
| Fraunces SOFT axis | 100 | 100 |
| Italic accent weight | 400 | 400 |

### Implementation tip

Expose these as tokens so a single theme attribute switches them: `--heading-weight`, `--text-secondary-weight`, `--body-tracking` and `--body-leading`. Components read the tokens; they never branch on theme themselves.

### Rules that hold in both modes

- Do not change font family or font size between themes.
- Do not use italic or script for any text that carries meaning on its own (a status, a warning, an error).
- Keep weights of 500 or higher for small secondary text that sits directly on a gradient background.

## 06 / Implementation spec

A starter contract for Tailwind v4, matching the structure you described (`frontend/src/index.css` and `frontend/tailwind.config.ts`). Treat it as a starting point to reconcile with your existing stylesheet, not a drop-in replacement.

### Step 1: Replace the font import (`index.css`, line 1)

```css
@import url('https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght,SOFT@0,9..144,500..700,0..100;1,9..144,400..600,0..100&family=Plus+Jakarta+Sans:wght@400..800&family=DM+Mono:wght@400;500&display=swap');
```

If the request returns an error, remove the `SOFT` axis from the Fraunces part first, then retest. Fraunces will still load, just without the rounded terminals.

### Step 2: Define the font tokens (replaces the `--font-montserrat` token)

```css
@theme {
  --font-display: "Fraunces", "Iowan Old Style", "Palatino Linotype", Georgia, serif;
  --font-sans: "Plus Jakarta Sans", ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --font-mono: "DM Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;

  /* Compatibility alias so existing classes keep working. Remove after migration. */
  --font-montserrat: "Plus Jakarta Sans", ui-sans-serif, system-ui, sans-serif;
}

/* Fixes the ~50 existing font-dancing usages: serif italic accent instead of a missing script. */
@utility font-dancing {
  font-family: var(--font-display);
  font-style: italic;
  font-weight: 400;
  font-variation-settings: "SOFT" 100, "WONK" 0;
}

@utility font-accent {
  font-family: var(--font-display);
  font-style: italic;
  font-weight: 400;
  font-variation-settings: "SOFT" 100, "WONK" 0;
}
```

In `tailwind.config.ts`, either delete the duplicate `fontFamily.montserrat` entry or mirror the three new families there. Keep only one source of truth.

### Step 3: Base styles (replaces `index.css` lines 205 and 221)

```css
@layer base {
  :root {
    --heading-weight: 600;
    --text-secondary-weight: 500;
    --body-tracking: 0;
    --body-leading: 1.6;
  }
  [data-theme="dark"] {
    --heading-weight: 560;
    --text-secondary-weight: 450;
    --body-tracking: 0.005em;
    --body-leading: 1.65;
    -webkit-font-smoothing: antialiased;
  }

  html { font-optical-sizing: auto; text-rendering: optimizeLegibility; }

  body {
    @apply bg-background text-foreground font-sans;
    font-size: 1rem;
    line-height: var(--body-leading);
    letter-spacing: var(--body-tracking);
  }

  h1, h2, h3 {
    font-family: var(--font-display);
    font-weight: var(--heading-weight);
    font-variation-settings: "SOFT" 100, "WONK" 0;
    letter-spacing: -0.015em;
    line-height: 1.15;
    text-wrap: balance;
  }
  h4, h5, h6 {
    font-family: var(--font-sans);
    font-weight: 700;
    line-height: 1.35;
  }
}
```

### Step 4: Role utilities (optional but recommended)

These bundle font, size, weight, line height and tracking so components pick a role instead of five separate classes.

```css
@utility type-display {
  font-family: var(--font-display);
  font-weight: var(--heading-weight);
  font-size: clamp(2.25rem, 1.4rem + 3.6vw, 3.75rem);
  line-height: 1.05;
  letter-spacing: -0.02em;
  text-wrap: balance;
}
@utility type-h1 { font-family: var(--font-display); font-weight: var(--heading-weight); font-size: clamp(1.875rem, 1.3rem + 2.4vw, 3rem); line-height: 1.1; letter-spacing: -0.015em; }
@utility type-h2 { font-family: var(--font-display); font-weight: var(--heading-weight); font-size: clamp(1.5rem, 1.3rem + 0.8vw, 1.875rem); line-height: 1.2; letter-spacing: -0.01em; }
@utility type-h3 { font-family: var(--font-display); font-weight: var(--heading-weight); font-size: clamp(1.25rem, 1.15rem + 0.4vw, 1.5rem); line-height: 1.25; letter-spacing: -0.005em; }
@utility type-accent { font-family: var(--font-display); font-style: italic; font-weight: 400; font-size: clamp(1.5rem, 1.2rem + 1.2vw, 2.25rem); line-height: 1.2; }
@utility type-label { font-family: var(--font-sans); font-weight: 600; font-size: 0.875rem; line-height: 1.25; letter-spacing: 0.01em; }
@utility type-caption { font-family: var(--font-sans); font-weight: var(--text-secondary-weight); font-size: 0.75rem; line-height: 1.4; letter-spacing: 0.01em; }
@utility type-eyebrow { font-family: var(--font-sans); font-weight: 700; font-size: 0.75rem; line-height: 1.3; letter-spacing: 0.12em; text-transform: uppercase; }
@utility type-numeric { font-family: var(--font-mono); font-weight: 500; font-size: 1.5rem; line-height: 1; letter-spacing: 0.02em; font-variant-numeric: tabular-nums; }
```

### Step 5: Migrate existing classes

| Existing | Replace with | Note |
| --- | --- | --- |
| `font-montserrat` | `font-sans` | The alias keeps old classes working until you finish |
| `font-dancing` | Leave as is, then rename to `font-accent` or `type-accent` | Alias already renders Fraunces italic |
| `font-bold` on `h1`-`h6` | Remove | Base layer sets weight per heading |
| `font-extrabold` | `font-bold` or `font-extrabold` | 800 is now genuinely loaded for the sans; use sparingly |
| `text-[10px]`, `text-[11px]` | `text-xs` | 12px floor |
| `tracking-widest`, `tracking-[0.2em]` on labels | `tracking-[0.12em]` or `type-eyebrow` | |
| `font-mono text-2xl font-semibold uppercase tracking-widest` | `type-numeric` or `font-mono text-2xl font-medium tracking-[0.08em]` | Applies to anagram and wordle config screens |
| `font-dancing text-lg font-semibold` (countdown) | `type-numeric` with an Eyebrow label | Numbers should not be italic |
| `text-white font-sans` (UserDashboard) | `text-white` | Sans is now the default |
| `.btn-romantic`, `.btn-love` (`font-weight: 600`) | Keep 600, set `letter-spacing: 0.01em` | |

## 07 / Loading and performance

The new system loads fewer font files than the current five static Montserrat weights, but Fraunces with two extra axes is the heaviest piece. Keep it in check.

| Topic | Recommendation |
| --- | --- |
| Variable fonts | Use the variable versions of all three families so one file per style covers every weight in range |
| Weight ranges | Request only what is used: Fraunces roman 500-700 and italic 400-600, Plus Jakarta Sans 400-800, DM Mono 400 and 500 |
| `font-display` | Keep `display=swap` so text shows immediately in the fallback font |
| Skip `WONK` | Leaving the axis out keeps the Fraunces file smaller |
| Replace CSS `@import` | `@import` inside CSS delays discovery of the font request. Move the Google Fonts `<link>` into `index.html`, with `preconnect` hints for `fonts.googleapis.com` and `fonts.gstatic.com` (add `crossorigin` on the second) |
| Self-hosting option | Packages such as Fontsource serve the same variable fonts from your own domain. If you switch, the CSP can drop the Google hosts and use `font-src 'self'` |
| Layout shift | Add size-adjusted fallback faces (tools such as Capsize or Fontaine generate the values) so swapping from Georgia to Fraunces does not move text |
| Preload | Preload only the Plus Jakarta Sans and Fraunces roman files used above the fold, and only if you self-host |
| Unicode coverage | These families cover Latin scripts. If users write confessions in other scripts (for example Devanagari or Malayalam), those characters fall back to the system font; add a matching Noto Sans family to the stack if that matters |

Suggested budget: keep first-load font transfer to roughly 150 KB of WOFF2 or less, and confirm the real number in the browser's Network panel after the swap. This is a target, not a measurement.

## 08 / Delivery plan and acceptance checklist

| Stage | Work | Acceptance check |
| --- | --- | --- |
| T0: Load and alias | Swap the import, add the `@theme` tokens, `font-dancing` and `font-accent` utilities | In DevTools, Computed > Rendered Fonts shows Fraunces on an `h1` and Plus Jakarta Sans on `body` |
| T1: Base styles | Replace the `body` and `h1`-`h6` rules; add weight and spacing tokens | No heading still renders in Montserrat; theme toggle changes weight but not size |
| T2: Roles | Add the role utilities; migrate hero titles, navbar, buttons, chips, card text | Every component maps to one role in the type scale |
| T3: Clean-up | Remove sizes below 12px, `font-extrabold` where not needed, wide uppercase tracking, old aliases | Search finds no `text-[10px]`, `text-[11px]` or `tracking-[0.2em]` |
| T4: Mono and numbers | Move countdown, clocks and scores to DM Mono | Numerals do not shift width as they tick |
| T5: Regression | Check mobile, long names, zoom, both themes, slow network | Items below all ticked |

### Acceptance checklist

- [ ] `font-dancing` text renders in Fraunces italic everywhere, not Montserrat.
- [ ] Headings (`h1`-`h3`) render in Fraunces; card titles, buttons, nav and chips render in Plus Jakarta Sans.
- [ ] No text below 12px; form inputs are 16px.
- [ ] No serif text below 20px, except confession bodies at 18px.
- [ ] Countdown and clock numerals use DM Mono and do not jitter.
- [ ] Switching light and dark does not change line breaks or layout height.
- [ ] Text at 200% browser zoom and on a 360px-wide screen does not clip or overflow.
- [ ] The page is readable with web fonts blocked (fallback stacks look acceptable).
- [ ] First load shows no visible jump when fonts swap in.
- [ ] Only one place defines font tokens; `tailwind.config.ts` does not conflict.

### Final recommendation

**Let Fraunces carry the emotion and Plus Jakarta Sans carry the interface.** Keep italic for short flourishes, keep mono for numbers, and keep the scale small and predictable.

Weight ranges and axes here reflect the Google Fonts catalogue as I know it, and I did not check them live for this report. Confirm them when you load the import URL.
