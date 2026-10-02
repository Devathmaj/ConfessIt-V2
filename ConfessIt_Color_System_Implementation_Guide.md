# ConfessIt — Color System Implementation Guide

**Scope:** Light mode (P1 — Pink & Berry) and Dark mode (D5 — Plum & Coral)
**Source:** *ConfessIt Color System Improvement Report* (prepared 12 October 2026)
**Purpose:** An engineering-ready, step-by-step plan to implement the report: tokens, gradients, components, states, accessibility checks, migration, testing and rollout.

> **How to read this document.** Sections 1–3 tell you *what is decided and what changed from the report*. Sections 4–8 are the *build spec* (copy-paste CSS). Sections 9–12 are the *process* (migration, phases, tests, acceptance). Appendices contain the contrast data and scripts.
>
> **Selectors are placeholders.** The report was written from screenshots, not source. Class names such as `.app-shell`, `.navbar`, `.profile-card` are illustrative — map them to your real selectors in the audit step (§9). Hex values come from the report unless marked **NEW**.

---

## Table of contents

1. [Summary and decisions](#1-summary-and-decisions)
2. [Audit corrections to the report (read before building)](#2-audit-corrections-to-the-report-read-before-building)
3. [Architecture: how theming should work](#3-architecture-how-theming-should-work)
4. [Token sheet (copy-paste)](#4-token-sheet-copy-paste)
5. [App shell, gradients and decorative layer](#5-app-shell-gradients-and-decorative-layer)
6. [Component specifications](#6-component-specifications)
7. [Interaction state matrix](#7-interaction-state-matrix)
8. [Accessibility rules and verification method](#8-accessibility-rules-and-verification-method)
9. [Migration procedure](#9-migration-procedure)
10. [Phased delivery plan (P0–P5)](#10-phased-delivery-plan-p0p5)
11. [Testing plan](#11-testing-plan)
12. [Master acceptance checklist](#12-master-acceptance-checklist)
13. [Risks and open questions](#13-risks-and-open-questions)
- [Appendix A — Contrast reference table](#appendix-a--contrast-reference-table)
- [Appendix B — Contrast checker script](#appendix-b--contrast-checker-script)
- [Appendix C — Theme bootstrap (no flash)](#appendix-c--theme-bootstrap-no-flash)
- [Appendix D — Playwright visual and a11y tests](#appendix-d--playwright-visual-and-a11y-tests)
- [Appendix E — Stylelint guardrails](#appendix-e--stylelint-guardrails)

---

## 1. Summary and decisions

### 1.1 The one-sentence goal

**Keep the existing gradients as the page atmosphere; put predictable, semantic, theme-aware surfaces, text and action colors on top of them.**

### 1.2 Locked decisions (from the report)

| # | Decision | Dark (D5) | Light (P1) |
|---|---|---|---|
| 1 | Page canvas | Plum gradient, darkest top/right, plum glow lower-left | Pale-pink wash with rose-pink center glow |
| 2 | Primary action accent | Coral `#FF7A7A` | Berry `#AC326E` |
| 3 | Text on primary button | Deep plum `#2A1122` (never white) | White `#FFFFFF` |
| 4 | Primary text | Warm ivory `#FFE7E4` | Deep plum `#4A1733` |
| 5 | Secondary text | Blush `#FFC1B6` | Muted berry `#7E4665` (**see §2.1 — needs a companion token**) |
| 6 | Standard card surface | `rgba(50,27,41,.94)` | `rgba(255,247,251,.90)` |
| 7 | Focus ring | `#FFC1B6` | `#8E2357` |
| 8 | Gradient location | **Once**, on the app shell | **Once**, on the app shell |
| 9 | Theme mechanism | Single `data-theme` attribute on the root | same |
| 10 | Brand pink | Confined to logo / identity marks; **not** a third CTA color | same |
| 11 | Dashed outer frames | Removed as default decoration; reserved for a meaningful state | same |
| 12 | Decorative hearts | Behind content, low opacity, `pointer-events:none` | same |

### 1.3 What "done" looks like

- Switching the `data-theme` attribute re-skins the whole app with **no layout shift** and **no component-level theme code**.
- No component contains a raw color literal; every color is a token (§4) enforced by lint (Appendix E).
- Every text/icon/control pair passes WCAG AA at the **worst visible point of the gradient** (§8, Appendix A/B).
- The light-mode countdown, waiting title and hero title are readable at a glance (the report's #1 priority).

---

## 2. Audit corrections to the report (read before building)

I re-computed every contrast ratio in the report (all reproduce exactly) and then tested additional pairs the report's proposed tokens will actually hit in production, including **translucent cards composited over every gradient stop** and **text placed directly on the gradient's brightest glow**. Most of the report holds up. The items below are places where following the report literally would reintroduce a failure, plus a few internal inconsistencies. Each has a concrete fix that is already baked into the token sheet in §4.

### 2.1 `--text-secondary` (light) fails on the brightest part of the canvas

The report gives `#7E4665` for "supporting labels on pink/white surfaces" and notes it passes at 4.77:1 on `#FEC2E6` with "limited headroom." On the real gradient it goes lower:

| Muted berry `#7E4665` on… | Ratio | AA (4.5:1) |
|---|---|---|
| `#FFF7FB` (card) | 6.76 | Pass |
| `#FFE5F2` (base) | 6.01 | Pass |
| `#FEC2E6` (petal) | 4.77 | Pass (thin) |
| `#F9C9DC` | 4.88 | Pass |
| `#FBBFE1` (center glow + pink region composite) | 4.62 | Pass (very thin) |
| `#FAB6D4` (light center glow over `#F9C9DC`) | **4.31** | **Fail** |

**Fix (NEW token):** add `--text-on-canvas` for any secondary text that sits **directly on the gradient** (not on a card). Use `#66304F` (≥ 6.05:1 on every sampled canvas point). Keep `--text-secondary: #7E4665` for text on **cards**.

### 2.2 Hot-pink brand color fails in light mode and is borderline on dark cards

| Pair | Ratio | Note |
|---|---|---|
| `#F43F8E` on `#181014` (dark canvas) | 5.30 | OK (report) |
| `#F43F8E` on `#321B29` (dark **card**) | **4.49** | Just under 4.5 for small text |
| `#F43F8E` on `#FFF7FB` (light card) | **3.35** | Fails for text; OK only for large graphics |
| `#F43F8E` on `#FEC2E6` (light canvas) | **2.37** | Fails |
| `#F43F8E` on `#F9C9DC` | **2.42** | Fails |

The report already warns that "a hot pink legible on dark plum may be weak on pale pink" — the numbers confirm it. **Fix (NEW token):** make `--brand` theme-aware: dark `#F43F8E`, light `#C4005A` (5.71:1 on `#FFF7FB`; 4.03:1 on `#FEC2E6` — acceptable for a logo mark/large graphic, not for small text).

### 2.3 Avatar initial contrast is "barely passing"

White on `#E90062` = **4.52:1** (report flags this). Antialiasing on small initials can effectively dip below threshold.
**Fix (NEW token):** `--avatar-bg: #D6006B` (white = **5.16:1**). If the signature fuchsia must be preserved exactly, keep `#E90062` but render initials at ≥ 18.66px bold (counts as "large text," 3:1 required) and verify rendered pixels.

### 2.4 The report's `--border` is decorative-only (≈1.4:1) — inputs need a stronger border

`rgba(172,50,110,.22)` over `#FFF7FB` ≈ `#EDCCDC`, only **1.4:1** against the card. That is fine for quiet card edges. It is **not** fine for **input fields, toggles, and checkboxes**: WCAG 1.4.11 requires UI component boundaries to reach **3:1**.
**Fix (NEW token):** `--border-strong` — light `#A35A80` (4.61:1 on `#FFF7FB`), dark `#9C6C88` (3.74:1 on `#321A28`). Use it on form controls and anything whose boundary is the only affordance.

### 2.5 Coral text on raised surfaces

Report already flags coral `#FF7A7A` on mauve `#573047` = 4.33:1 (fail). Also note coral on `--surface-raised #4A1B3B` = 5.50:1 (OK) but only because that surface is darker than mauve. **Rule:** coral = fill / icon / marker; use **blush** for small labels on any raised surface.

### 2.5.1 Error / success / warning have no tokens in the report

Notifications, form validation and "matched / unavailable" states need them. **NEW tokens** with verified ratios on their intended surfaces are in §4 (e.g., light error `#B42318` on `#FFF7FB` = 6.25:1; dark error `#FFA8A8` on `#321B29` = 8.62:1). Note: light error **fails on the pink canvas** (4.41:1), so error text must sit on a card/solid surface, not directly on the gradient.

### 2.6 Internal inconsistencies to resolve with the designer

| # | Inconsistency | My default in this guide |
|---|---|---|
| a | Light gradient swatch lists `#FFF5FA`, but the light gradient **CSS** uses `#FFF3FA` as the first stop. | Use the CSS value `#FFF3FA` (it's what the report's code ships). Confirm with design. |
| b | Dark swatches list `#321020` and `#3C132E`, but the dark CSS builds the glow from `rgba(112,17,77,.38)` / `rgba(81,25,55,.20)` and never references those swatches. | Use the CSS. Treat `#321020`/`#3C132E` as *reference* colors; the composite glow center lands near `#3F122D`, which I use as the worst-case dark canvas sample. |
| c | `--surface-solid` for dark is not in the "recommended tokens" table but appears in the implementation spec (`#321B29`). | Include it (it equals the opaque form of `--surface`). |
| d | The dark profile-card gradient and light profile-card gradient are described qualitatively only. | I propose concrete gradients in §6.5 — **mark as design-review items** and run the contrast script on them. |
| e | The report says `--page-bg` "Layered plum gradient"; no token form is given. | Expose it as a custom property **and** apply it on `.app-shell` (§5). |

---

## 3. Architecture: how theming should work

### 3.1 Three token layers

```
Layer 1  PRIMITIVES     raw palette, named by color, never used in components
Layer 2  SEMANTIC       role-based tokens, theme-specific values  (--text-primary, --surface, --accent …)
Layer 3  COMPONENT      optional per-component aliases that point at semantic tokens (--btn-bg: var(--accent))
```

Rules:

1. **Components may only reference Layer 2 or Layer 3.** Never a hex, `rgb()`, `hsl()`, or a primitive.
2. **Themes change only Layer 2 values.** Layer 3 aliases and component CSS are identical in both themes.
3. **Gradients are tokens too** (`--page-bg`, `--card-gradient`, `--heading-gradient`) so themes can swap them without touching components.

### 3.2 Switching mechanism

- A single attribute on the root element: `<html data-theme="light|dark">`.
- Light is the default in `:root` (per the report's contract) so unstyled/JS-less renders are readable.
- Set `color-scheme` per theme so native form controls, scrollbars and autofill follow the theme.
- Persist the choice and apply it **before first paint** with the bootstrap in Appendix C (prevents a flash of the wrong theme).
- **Do not** swap stylesheets or duplicate component CSS per theme.
- Theme toggle must not change layout: fixed control size, no theme-dependent padding/border-width changes.

### 3.3 Suggested file layout

Adapt to your stack. The point is separation of concerns.

```
src/styles/
  tokens.primitives.css     # Layer 1 (raw palette)
  tokens.light.css          # Layer 2, :root,[data-theme="light"]
  tokens.dark.css           # Layer 2, [data-theme="dark"]
  base.css                  # html/body, focus ring, forced-colors, reduced-motion
  app-shell.css             # gradient + decorative layer ownership
  components/
    navbar.css
    buttons.css
    card.css
    profile-card.css
    avatar.css
    chips.css
    status.css
    inputs.css
    overlays.css            # menus, popovers, dialogs
    hearts.css
```

If you use Tailwind, expose the semantic tokens through the theme config instead (e.g., `colors: { surface: 'var(--surface)', accent: 'var(--accent)' … }`) so utilities such as `bg-surface text-primary` resolve to variables, and **ban** palette utilities like `bg-pink-500` in shared components (see Appendix E). If you use CSS-in-JS, put the same tokens on a global style and reference `var(--…)` inside styled components.

---

## 4. Token sheet (copy-paste)

> **Legend:** *(report)* = from the PDF; **NEW** = added in this guide for the reasons in §2.

### 4.1 Layer 1 — Primitives

```css
/* tokens.primitives.css */
:root {
  /* Plum / wine family (dark canvas + surfaces) */
  --plum-950: #181014;   /* near-black plum, top/right of dark canvas */
  --plum-925: #1B1016;
  --plum-900: #211219;   /* dark base */
  --plum-850: #2A1122;   /* deep plum, brand anchor, text on coral */
  --plum-800: #321B29;   /* neutral card surface (opaque) */
  --wine-800: #3C132E;   /* reference: deep plum accent region */
  --plum-750: #4A1B3B;   /* raised surface */
  --mauve-700: #573047;  /* muted mauve: chips, raised surfaces */

  /* Coral / blush / ivory (dark foreground family) */
  --coral-400: #FF7A7A;  /* dark action accent */
  --coral-300: #FF8C89;  /* dark hover */
  --coral-500: #E86A6A;  /* dark pressed (NEW) */
  --blush-300: #FFC1B6;  /* dark secondary text, focus */
  --ivory-100: #FFE7E4;  /* dark primary text */

  /* Pink / berry family (light canvas + foreground) */
  --pink-50:  #FFF7FB;   /* warm white card */
  --pink-75:  #FFF3FA;   /* light canvas first stop (CSS value) */
  --pink-100: #FFE5F2;   /* light base */
  --pink-125: #FFE0F0;   /* active nav pill / pale fills (NEW) */
  --pink-200: #FEC2E6;   /* petal pink */
  --pink-250: #F9C9DC;   /* light gradient end */
  --pink-300: #F48BC8;   /* soft pink, decorative only */
  --berry-600: #AC326E;  /* light primary action */
  --berry-700: #92204F;  /* light hover */
  --berry-750: #8E2357;  /* light focus / berry text on canvas */
  --berry-800: #7A1A42;  /* light pressed (NEW) */
  --berry-500: #7E4665;  /* muted berry (secondary text on cards) */
  --berry-650: #66304F;  /* secondary text directly on canvas (NEW) */
  --plum-text: #4A1733;  /* light primary text */

  /* Brand / identity (shared fuchsia family) */
  --fuchsia-brand-dark:  #F43F8E;  /* logo on dark */
  --fuchsia-brand-light: #C4005A;  /* logo on light (NEW) */
  --fuchsia-avatar:      #D6006B;  /* avatar fill, white initials = 5.16:1 (NEW) */

  /* Status (NEW) */
  --red-700: #B42318;  --red-300: #FFA8A8;
  --green-700: #14683A; --green-300: #8CE0AE;
  --amber-800: #7A4A00; --amber-300: #FFD28A;
}
```

### 4.2 Layer 2 — Light theme (P1)

```css
/* tokens.light.css */
:root,
[data-theme="light"] {
  color-scheme: light;

  /* Text */
  --text-primary:    #4A1733;                 /* (report) */
  --text-secondary:  #7E4665;                 /* (report) on CARDS only */
  --text-on-canvas:  #66304F;                 /* NEW: secondary text directly on gradient */
  --text-placeholder:#7E4665;                 /* 7.12:1 on #FFFFFF input */
  --text-on-accent:  #FFFFFF;                 /* alias of --accent-text */

  /* Surfaces */
  --surface:         rgba(255, 247, 251, .90); /* (report) standard cards/panels */
  --surface-solid:   #FFF7FB;                  /* (report) profile cards, forms, dialogs */
  --surface-raised:  #FFFFFF;                  /* (report) menus, popovers */
  --input-bg:        #FFFFFF;                  /* NEW */
  --frame-bg:        rgba(255, 255, 255, .28); /* NEW: outer matchmaking wrapper tint */
  --scrim:           rgba(74, 23, 51, .45);    /* NEW: modal backdrop */

  /* Action */
  --accent:          #AC326E;                  /* (report) */
  --accent-hover:    #92204F;                  /* (report) */
  --accent-pressed:  #7A1A42;                  /* NEW */
  --accent-text:     #FFFFFF;                  /* (report spec) */
  --accent-outline-text: #8E2357;              /* NEW: text on secondary/outline buttons (7.89:1 on card) */

  /* Lines / focus */
  --border:          rgba(172, 50, 110, .22);  /* (report) decorative edges */
  --border-strong:   #A35A80;                  /* NEW: inputs/toggles ≥3:1 */
  --focus:           #8E2357;                  /* (report) */

  /* Brand + avatar */
  --brand:           #C4005A;                  /* NEW: light-mode logo/identity pink */
  --avatar-bg:       #D6006B;                  /* NEW */
  --avatar-text:     #FFFFFF;
  --avatar-ring:     #FFF7FB;                  /* (report: clear light ring in light mode) */

  /* Nav */
  --nav-bg:          rgba(255, 247, 251, .92); /* near-white surface */
  --nav-border:      rgba(172, 50, 110, .22);
  --nav-active-bg:   #FFE0F0;                  /* pale pink pill; 6.79:1 with #8E2357 */
  --nav-active-text: #8E2357;
  --nav-active-marker: #AC326E;

  /* Chips */
  --chip-bg:         rgba(172, 50, 110, .12);  /* ≈ #F5DFEA on card */
  --chip-text:       #4A1733;                  /* 11.38:1 on chip */
  --chip-selected-bg:   #AC326E;
  --chip-selected-text: #FFFFFF;

  /* Status badge (countdown / waiting) */
  --status-bg:       #8E2357;                  /* solid berry */
  --status-text:     #FFFFFF;                  /* 8.31:1 */
  --status-tint-bg:  rgba(255, 247, 251, .92); /* alt: tinted badge */
  --status-tint-text:#4A1733;

  /* Feedback (use on cards/solid surfaces, not directly on canvas) */
  --danger:  #B42318;   /* 6.25:1 on #FFF7FB */
  --success: #14683A;   /* 6.50:1 on #FFF7FB */
  --warning: #7A4A00;   /* 7.11:1 on #FFF7FB */

  /* Decoration */
  --heart-color:     rgba(172, 50, 110, .14);
  --shadow-card:     0 1px 2px rgba(74, 23, 51, .06), 0 8px 24px rgba(172, 50, 110, .10);
  --shadow-nav:      0 1px 0 rgba(172, 50, 110, .10), 0 6px 20px rgba(172, 50, 110, .10);

  /* Gradients (see §5, §6) */
  --page-bg:
    radial-gradient(ellipse 72% 62% at 48% 54%, rgba(255, 120, 185, .23) 0%, transparent 72%),
    radial-gradient(ellipse 70% 65% at 5% 95%,  rgba(255, 177, 232, .40) 0%, transparent 72%),
    linear-gradient(115deg, #FFF3FA 0%, #FFE5F2 46%, #F9C9DC 100%);
  --card-gradient: linear-gradient(160deg, #FFF7FB 0%, #FDE3F0 100%);   /* proposal — verify */
  --heading-gradient: linear-gradient(90deg, #8E2357 0%, #C4005A 100%); /* proposal — verify */
  --heading-fallback: #8E2357;

  /* Disabled (inactive controls are exempt from WCAG contrast, but keep them perceivable) */
  --disabled-bg:   #EBD3DF;                    /* starting value — verify visually */
  --disabled-text: #86556E;                    /* starting value — verify visually */
}
```

### 4.3 Layer 2 — Dark theme (D5)

```css
/* tokens.dark.css */
[data-theme="dark"] {
  color-scheme: dark;

  /* Text */
  --text-primary:    #FFE7E4;                  /* (report) */
  --text-secondary:  #FFC1B6;                  /* (report) */
  --text-on-canvas:  #FFC1B6;                  /* same as secondary: ≥10:1 on every canvas stop */
  --text-placeholder:#FFC1B6;                  /* 11.28:1 on #2A1122 input */
  --text-on-accent:  #2A1122;

  /* Surfaces */
  --surface:         rgba(50, 27, 41, .94);    /* (report) */
  --surface-solid:   #321B29;                  /* (report spec) */
  --surface-raised:  #4A1B3B;                  /* (report) */
  --input-bg:        #2A1122;                  /* NEW */
  --frame-bg:        rgba(255, 255, 255, .02); /* NEW: barely-there tint */
  --scrim:           rgba(0, 0, 0, .60);       /* NEW */

  /* Action */
  --accent:          #FF7A7A;                  /* (report) */
  --accent-hover:    #FF8C89;                  /* (report spec) */
  --accent-pressed:  #E86A6A;                  /* NEW (plum text = 5.58:1) */
  --accent-text:     #2A1122;                  /* (report) */
  --accent-outline-text: #FFC1B6;              /* NEW */

  /* Lines / focus */
  --border:          rgba(255, 193, 182, .22); /* (report) */
  --border-strong:   #9C6C88;                  /* NEW: inputs/toggles ≥3:1 */
  --focus:           #FFC1B6;                  /* (report) */

  /* Brand + avatar */
  --brand:           #F43F8E;                  /* 5.30:1 on canvas; use for logo/large marks only */
  --avatar-bg:       #D6006B;                  /* NEW */
  --avatar-text:     #FFFFFF;
  --avatar-ring:     #2A1122;                  /* (report: dark plum ring in dark mode) */

  /* Nav */
  --nav-bg:          rgba(24, 16, 20, .88);    /* near-black plum, opaque enough to hide glow */
  --nav-border:      rgba(255, 193, 182, .18);
  --nav-active-bg:   #4A1B3B;                  /* restrained plum surface */
  --nav-active-text: #FFC1B6;                  /* 8.97:1 */
  --nav-active-marker: #FF7A7A;                /* icon/marker only (5.50:1) */

  /* Chips */
  --chip-bg:         #573047;                  /* muted mauve */
  --chip-text:       #FFC1B6;                  /* 7.06:1 */
  --chip-selected-bg:   #FF7A7A;
  --chip-selected-text: #2A1122;               /* 6.92:1 */

  /* Status badge */
  --status-bg:       #4A1B3B;
  --status-text:     #FFC1B6;                  /* 8.97:1; pair with a coral icon */
  --status-tint-bg:  rgba(50, 27, 41, .94);
  --status-tint-text:#FFE7E4;

  /* Feedback */
  --danger:  #FFA8A8;   /* 8.62:1 on #321B29 */
  --success: #8CE0AE;   /* 10.11:1 */
  --warning: #FFD28A;   /* 11.20:1 */

  /* Decoration */
  --heart-color:     rgba(255, 122, 122, .16);
  --shadow-card:     0 1px 2px rgba(0, 0, 0, .40), 0 10px 28px rgba(0, 0, 0, .35);
  --shadow-nav:      0 1px 0 rgba(255, 193, 182, .08), 0 8px 24px rgba(0, 0, 0, .40);

  /* Gradients */
  --page-bg:
    radial-gradient(ellipse 70% 60% at 12% 88%, rgba(112, 17, 77, .38) 0%, transparent 72%),
    radial-gradient(ellipse 55% 45% at 52% 55%, rgba(81, 25, 55, .20) 0%, transparent 75%),
    linear-gradient(115deg, #181014 0%, #211219 48%, #1B1016 100%);
  --card-gradient: linear-gradient(160deg, #3A1530 0%, #321B29 100%);  /* proposal — verify */
  --heading-gradient: linear-gradient(90deg, #FF7A7A 0%, #FFC1B6 100%);
  --heading-fallback: #FFC1B6;

  --disabled-bg:   #3A2433;                    /* starting value — verify visually */
  --disabled-text: #A08592;                    /* starting value — verify visually */
}
```

### 4.4 Shared (theme-independent) tokens

```css
:root {
  --radius-card: 16px;      /* (report .card) */
  --radius-control: 12px;
  --radius-pill: 999px;
  --focus-width: 3px;       /* (report: 2–3 px) */
  --focus-offset: 3px;
  --control-height: 44px;   /* ≥44px touch target */
  --theme-transition: 150ms ease;
}
```

---

## 5. App shell, gradients and decorative layer

### 5.1 Principles (from the report)

1. The page gradient is applied **once**, to the app shell. Never on cards.
2. Maximum **two** radial glows on top of the linear base.
3. Shell must cover **at least** the viewport height.
4. Decorative items sit **below** content, are non-interactive, and never overlap text.
5. Don't depend on `background-attachment: fixed` on mobile.

### 5.2 Base styles

```css
/* base.css */
html { background: var(--surface-solid); }          /* fallback behind overscroll */
body { margin: 0; color: var(--text-primary); }

.app-shell {
  position: relative;
  isolation: isolate;                  /* own stacking context so z-index:-1 layers stay inside */
  min-height: 100vh;
  min-height: 100dvh;                  /* mobile browser UI-safe */
  color: var(--text-primary);
}
```

### 5.3 Gradient — Option A (matches the report literally; simplest)

```css
.app-shell {
  background: var(--page-bg);
  background-repeat: no-repeat;
  background-size: 100% 100%;
}
```

> Caveat: when page content is taller than the viewport the gradient **stretches over the full document height**, so the glow positions scale with page length. That is acceptable if pages are roughly one screen. If long pages make the glow feel "lost," use Option B.

### 5.4 Gradient — Option B (recommended if you scroll; mobile-safe "fixed" look without `background-attachment`)

```css
.app-shell::before {
  content: "";
  position: fixed;          /* pins to the viewport; works on iOS where background-attachment:fixed does not */
  inset: 0;
  z-index: -1;
  background: var(--page-bg);
  pointer-events: none;
}
```

Pick **one** option and document the choice. Test iOS Safari and Android Chrome with the URL bar showing/hiding.

### 5.5 Decorative hearts layer

```css
/* hearts.css */
.hearts-layer {
  position: fixed;
  inset: 0;
  z-index: 0;                 /* above gradient (-1), below content (1) */
  overflow: hidden;
  pointer-events: none;       /* never intercept clicks */
  user-select: none;
}
.hearts-layer .heart {
  position: absolute;
  color: var(--heart-color);  /* low-opacity brand tint, theme-aware */
  /* keep opacity in the color token; do NOT also lower element opacity (double-fading) */
}

.app-content {                 /* wrapper around routed page content */
  position: relative;
  z-index: 1;
}

@media (prefers-reduced-motion: reduce) {
  .hearts-layer .heart { animation: none !important; }
}
```

Markup: `<div class="hearts-layer" aria-hidden="true">…</div>` (hearts are decorative; hide from assistive tech).

**Overlap prevention (the report observed a heart near content in light mode):**

- Primary protection: z-index + low-opacity token (above) so overlap is harmless.
- Secondary: if hearts are generated procedurally, bias spawn X-positions toward the left/right gutters (e.g., 0–12% and 88–100% of width) and cap hearts on screens narrower than 480px (e.g., ≤ 4).
- Tertiary: never place an interactive element inside `.hearts-layer`.

### 5.6 Mobile and reduced-motion checklist (shell level)

- [ ] No `background-attachment: fixed` anywhere.
- [ ] Scrolling long content doesn't "restart" or band the gradient.
- [ ] `prefers-reduced-motion: reduce` disables heart float animations and any gradient animation.
- [ ] `min-height: 100dvh` fallback behaves when the mobile URL bar collapses.
- [ ] Rotating the device doesn't reveal a flat `html` background (the `html` fallback in §5.2 should be close in tone to the canvas edge).

---

## 6. Component specifications

> For each component: **role → tokens → CSS → rules/watch-outs → acceptance**. CSS is a starting contract; adapt selectors to your codebase.

### 6.1 Brand / wordmark

- **Role:** One identity treatment in both modes; the *only* place the saturated brand pink appears at full strength (plus the avatar).
- **Tokens:** `--brand` (theme-aware).

```css
.brand-mark { color: var(--brand); }            /* logo glyph / heart icon */
.brand-wordmark { color: var(--text-primary); } /* "Confessit" text stays neutral for legibility */
```

- **Watch-out:** Don't let `--brand`, `--accent`, and the heading gradient all appear at full saturation in one cluster. If the logo's current hot-pink must stay in both modes, verify it separately over each canvas (see §2.2).
- **Acceptance:** logo mark ≥ 3:1 against the navbar background in both themes (graphic object).

### 6.2 Navbar

- **Role:** Persistent top bar; quiet and legible regardless of glow underneath.
- **Dark:** near-black plum, subtle plum border. **Light:** near-white, fine berry-tinted border, soft shadow.

```css
.navbar {
  background: var(--nav-bg);
  border: 1px solid var(--nav-border);
  box-shadow: var(--shadow-nav);
  color: var(--text-primary);
  -webkit-backdrop-filter: blur(10px);
  backdrop-filter: blur(10px);          /* optional; keep nav-bg opacity high enough that blur isn't load-bearing */
}

.nav-link {
  color: var(--text-secondary);
  border-radius: var(--radius-pill);
  min-height: var(--control-height);
  padding-inline: 14px;
  border: 1px solid transparent;
  transition: background var(--theme-transition), color var(--theme-transition);
}
.nav-link:hover { background: color-mix(in srgb, var(--accent) 10%, transparent); }

.nav-link[aria-current="page"],
.nav-link.is-active {
  background: var(--nav-active-bg);
  color: var(--nav-active-text);
  border-color: var(--border);
  font-weight: 600;                                    /* non-color cue */
}
.nav-link.is-active .nav-icon { color: var(--nav-active-marker); }
```

- **Rules:** Active state = **visible surface + readable text/icon + weight change** (not color alone). In dark mode coral is an *icon/marker* color on the plum pill; label stays blush.
- **Acceptance:** Active label `#8E2357` on `#FFE0F0` = 6.79:1 (light); `#FFC1B6` on `#4A1B3B` = 8.97:1 (dark).
- **Note on `color-mix()`:** supported in current evergreen browsers; if you must support older ones, precompute a `--hover-tint` token per theme instead.

### 6.3 Theme toggle

```css
.theme-toggle {
  min-width: var(--control-height);
  min-height: var(--control-height);
  background: var(--surface-solid);
  color: var(--text-primary);
  border: 1px solid var(--border-strong);              /* the only affordance → needs ≥3:1 */
  border-radius: var(--radius-control);
}
.theme-toggle:hover { background: var(--surface-raised); }
.theme-toggle[aria-pressed="true"] { border-color: var(--accent); }
```

- **Report issue addressed:** "theme-toggle and control outlines compete in light mode" → one consistent border token (`--border-strong`) for form-like controls, `--border` for passive containers.
- Fixed dimensions so switching icons (sun/moon) never shifts layout.
- `aria-label` must describe the action ("Switch to dark mode"), not the state color.

### 6.4 Page heading (hero title "Random Matchmaking")

- **Dark:** coral→blush gradient text. **Light:** solid deep berry or darker berry→magenta gradient. **Never** pale-pink gradient text on the pink canvas.

```css
.page-heading {
  color: var(--heading-fallback);                      /* fallback when background-clip unsupported */
  background-image: var(--heading-gradient);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
}
@media (forced-colors: active) {
  .page-heading { background: none; -webkit-text-fill-color: currentColor; color: CanvasText; }
}
```

- **Rules:** The heading is large text (≥24px, or ≥18.66px bold) → 3:1 minimum, but target ≥4.5:1 anyway for the pale-gradient light canvas.
- **Measured:** dark coral `#FF7A7A` on the plum glow `#3F122D` = 6.23:1, blush higher. Light `#8E2357` on the brightest glow `#FAB6D4` = 5.03:1; the magenta end `#C4005A` is 4.03:1 on `#FEC2E6` and lower on the glow — acceptable only because the heading is large; **verify visually and with the sampler (§8.3)**. If the magenta end looks weak, shorten the gradient range or fall back to solid `#8E2357`.
- **Subtitle ("Find your perfect Valentine match"):** use `--text-on-canvas` (light) / `--text-secondary` (dark) — it sits directly on the gradient.

### 6.5 Matchmaking layout: outer frame + inner profile card

This is where the report's "surface hierarchy" finding lives. **Outer frame = calm. Inner profile card = carries the surface.**

```css
/* Outer matchmaking wrapper — replaces the large white dashed frames */
.match-frame {
  background: var(--frame-bg);
  border: 1px solid var(--border);                     /* solid, thin, low-contrast */
  border-radius: calc(var(--radius-card) + 4px);
  padding: 12px;
}
.match-frame:focus-within,
.match-frame.is-selected { border-color: var(--accent); }   /* stronger border ONLY for focus/selected */

/* Dashed is reserved for a meaningful state, e.g. an empty "waiting for partner" slot */
.match-frame[data-state="empty"] { border-style: dashed; border-color: var(--border-strong); }

/* Inner profile card */
.profile-card {
  background: var(--card-gradient);
  color: var(--text-primary);
  border: 1px solid var(--border);
  border-radius: var(--radius-card);
  box-shadow: var(--shadow-card);
}
.profile-card .meta   { color: var(--text-secondary); }
```

- **Light:** near-white → pale blush (`#FFF7FB → #FDE3F0`). Reduce the saturated pink end-stop wherever text sits.
- **Dark:** restrained wine (`#3A1530 → #321B29`) — *less saturated than the current strongest magenta area*; the avatar and actions are the focal points.
- **Rules:** The card must be calmer than the hero title and avatar. If the profile card contains the interest chips and the Interests label, all text must read from the card gradient's **worst stop**, not the average (run the script on both gradient endpoints).
- **Acceptance:** No pure-white dashed block remains; dashed appears only in explicit state variants.

### 6.6 Avatar

```css
.avatar {
  background: var(--avatar-bg);
  color: var(--avatar-text);
  font-weight: 700;
  border-radius: 50%;
  box-shadow: 0 0 0 3px var(--avatar-ring);            /* ring = separation mechanism */
  /* do NOT rely on a decorative black border for separation */
}
```

- Light: ring `#FFF7FB`; dark: ring `#2A1122`.
- White initial on `#D6006B` = 5.16:1. If you retain `#E90062`, you are at 4.52:1 — verify rendered pixels (§2.3).

### 6.7 Interest chips

| State | Light | Dark |
|---|---|---|
| Default | `--chip-bg` pale pink + `--chip-text` plum (11.38:1) | `--chip-bg` mauve + `--chip-text` blush (7.06:1) |
| Selected | `--chip-selected-bg` berry + white (6.11:1) | `--chip-selected-bg` coral + plum (6.92:1) |

```css
.chip {
  background: var(--chip-bg);
  color: var(--chip-text);
  border: 1px solid var(--border);
  border-radius: var(--radius-pill);
  padding: 4px 10px;
}
.chip[aria-pressed="true"],
.chip.is-selected {
  background: var(--chip-selected-bg);
  color: var(--chip-selected-text);
  border-color: transparent;
}
.chip.is-selected::before { content: "✓"; margin-inline-end: 6px; }   /* non-color selection cue */
```

- **Never** soft-pink text on a soft-pink chip.
- Selection must not rely on color alone → checkmark/icon or weight change.

### 6.8 Buttons

```css
.btn {
  min-height: var(--control-height);
  padding-inline: 18px;
  border-radius: var(--radius-control);
  font-weight: 600;
  border: 1px solid transparent;
  transition: background var(--theme-transition), border-color var(--theme-transition);
}

/* Primary */
.btn-primary { background: var(--accent); color: var(--accent-text); }
.btn-primary:hover:not(:disabled) { background: var(--accent-hover); }
.btn-primary:active:not(:disabled) { background: var(--accent-pressed); }   /* no layout jump, no transform that shifts box */

/* Secondary (outline) */
.btn-secondary {
  background: transparent;
  color: var(--accent-outline-text);
  border-color: var(--border-strong);
}
.btn-secondary:hover:not(:disabled) { background: color-mix(in srgb, var(--accent) 10%, transparent); }

/* Disabled — do NOT lower opacity on the whole control */
.btn:disabled,
.btn[aria-disabled="true"] {
  background: var(--disabled-bg);
  color: var(--disabled-text);
  border-color: transparent;
  cursor: not-allowed;
}
```

- Dark primary: **plum text on coral** (6.92:1). Never white on `#FF7A7A`.
- Light primary: **white on berry** (6.11:1); hover `#92204F` (8.26:1).
- Light outline button text uses `#8E2357` (7.89:1 on card) — **not** `#AC326E` on the gradient (4.10:1 fail).

### 6.9 Status: countdown and "Waiting for Response" (report's top priority)

**The problem:** white countdown and bright-pink waiting title directly on the pink canvas.

**Solution — pick one pattern per element and apply consistently:**

*Pattern A: solid badge (most robust; recommended for the countdown)*

```css
.status-badge {
  display: inline-flex; align-items: center; gap: 8px;
  background: var(--status-bg);
  color: var(--status-text);
  border-radius: var(--radius-pill);
  padding: 6px 14px;
  font-weight: 600;
}
```
Light: white on `#8E2357` = **8.31:1**. Dark: blush on `#4A1B3B` = **8.97:1**.

*Pattern B: text directly on the gradient (acceptable for the waiting title)*

```css
.status-title { color: var(--text-primary); }      /* light #4A1733: ≥8.69:1 on worst glow; dark ivory */
.status-hint  { color: var(--text-on-canvas); }
```

**Rules:**

- Include an icon *and* text for state (hourglass + "Waiting for Response") — never color alone.
- Avoid pure white or pale-pink text directly on the light gradient (ever).
- If the badge sits over the profile card, use `--status-tint-bg/-text` instead.
- Use `aria-live="polite"` on the waiting message if it updates dynamically.

### 6.10 Inputs, selects, toggles

```css
.input {
  background: var(--input-bg);
  color: var(--text-primary);
  border: 1px solid var(--border-strong);      /* ≥3:1 boundary */
  border-radius: var(--radius-control);
  min-height: var(--control-height);
}
.input::placeholder { color: var(--text-placeholder); }
.input:hover { border-color: var(--accent); }
.input[aria-invalid="true"] { border-color: var(--danger); }
.input:disabled { background: var(--disabled-bg); color: var(--disabled-text); }
```

- Error text must **not** rely on color alone: icon + message text, linked via `aria-describedby`.
- Use `accent-color: var(--accent)` for native checkboxes/radios/range to inherit the theme.

### 6.11 Overlays: menus, popovers, dialogs, toasts

- **Always solid** (`--surface-raised` for menus/popovers, `--surface-solid` for dialogs) — never translucent over the gradient, because contrast would vary with scroll position.
- Backdrop: `--scrim`.
- Border: `1px solid var(--border)`; shadow `--shadow-card`.
- Toasts: solid surface + semantic icon (`--danger` / `--success` / `--warning`) + text; don't tint the entire toast with the semantic color unless you re-verify contrast.

### 6.12 Focus ring (global)

```css
:where(a, button, input, select, textarea, [tabindex], [role="button"]):focus-visible {
  outline: var(--focus-width) solid var(--focus);
  outline-offset: var(--focus-offset);
}
@media (forced-colors: active) {
  :where(a, button, input, select, textarea, [tabindex], [role="button"]):focus-visible {
    outline: 3px solid Highlight;
  }
}
```

- Must **not** depend on a subtle `box-shadow` alone.
- Test the ring against **both** the card surface and the page background (the offset gap means the ring sits on whichever is behind it). Measured: light `#8E2357` ≥ 5.03:1 on every canvas stop and 7.89:1 on card; dark `#FFC1B6` ≥ 10:1 on every dark canvas/card.
- For elements that clip overflow (e.g., chips in a scroll row), increase padding so the outline isn't cut off.

---

## 7. Interaction state matrix

Applies to buttons, chips, nav links, toggles and any clickable surface.

| State | Visual rule | Dark (D5) | Light (P1) | Not color-only cue |
|---|---|---|---|---|
| **Default** | Theme's standard CTA / neutral surface | coral fill + plum text | berry fill + white text | Label text |
| **Hover** | Darken/lighten action ~8–12% **or** subtle elevation; don't shift hue | `#FF8C89` | `#92204F` | Cursor + elevation |
| **Pressed** | One more step; no layout jump | `#E86A6A` | `#7A1A42` | Slight inset shadow OK |
| **Selected** | Tinted surface + strong text/icon (+ optional border) | `--nav-active-bg` / coral chip | `--nav-active-bg` / berry chip | ✓ icon, weight, `aria-pressed/current` |
| **Disabled** | Dedicated disabled tokens; **not** whole-control `opacity` | `--disabled-*` | `--disabled-*` | `cursor:not-allowed`, `disabled` attr |
| **Focus-visible** | 3px outline + 3px offset in `--focus` | `#FFC1B6` | `#8E2357` | Shape (outline) — not subtle shadow |
| **Loading** | Keep label dimensions; spinner/skeleton | same | same | Spinner + `aria-busy="true"` |
| **Error** | Border `--danger` + icon + message | `#FFA8A8` | `#B42318` | Icon + text |
| **Success** | `--success` + icon + message | `#8CE0AE` | `#14683A` | Icon + text |

> Hover/pressed values for pressed states (`#E86A6A`, `#7A1A42`) and all disabled values are **NEW** starting points. Dark pressed with plum text measures 5.58:1; light pressed with white will be higher than the 8.26:1 of `#92204F`, since it is darker. Confirm visually.

---

## 8. Accessibility rules and verification method

### 8.1 Targets

| Item | Minimum |
|---|---|
| Normal text | **4.5:1** |
| Large text (≥24px, or ≥18.66px bold) | **3:1** |
| UI component boundaries / meaningful icons | **3:1** against adjacent colors |
| Focus indicator | ≥3:1 vs adjacent colors, and visible (3px, offset) |

### 8.2 Why the nominal hex isn't enough

1. **Translucent surfaces** (`--surface`, `--nav-bg`) take on the color of whatever is behind them. Composite the surface over each canvas stop (the script in Appendix B does this).
2. **Text directly on a gradient** must be tested against the **worst** (lowest-contrast) background point under any glyph.
3. **Glow regions** are the worst case: the light center glow composite lands near `#FAB6D4`; the dark lower-left glow near `#3F122D`.

Worst-case sample points used throughout this guide:

```
Light canvas: #FFF3FA  #FFE5F2  #F9C9DC  #FEC2E6  #FFCCE5  #FAB6D4  #FBBFE1
Dark canvas:  #181014  #211219  #1B1016  #3C132E  #3F122D
```

### 8.3 How to measure the real worst point (do this for P0 and P5)

1. Render each theme at 1440×900 and 390×844.
2. For each text element, take a screenshot of the element's bounding box region **with the text hidden** (`color: transparent` injected via DevTools or Playwright `addStyleTag`).
3. Sample the min- and max-luminance pixels in that region.
4. Compute contrast of the text token against the **lowest-contrast** sample (script: sample → `ratio(textColor, sample)`).
5. Record results in the audit table (§9.2). Anything < threshold: change the token, add a solid backing, or move the element onto a card.

Automated checkers (axe, Lighthouse) typically report **"incomplete / needs review"** for text over gradients rather than pass/fail — so §8.3 is the real gate for gradient-backed text.

### 8.4 Additional checks

- Zoom to 200% and 400%: no clipped text, no focus rings cut off.
- `prefers-reduced-motion: reduce`: hearts static; no animated gradient.
- `forced-colors: active` (Windows High Contrast): headings and focus rings fall back to system colors (see §6.4, §6.12).
- Don't encode state by color alone: waiting / matched / unavailable / selected / error all need an icon or text.
- Keyboard-only run-through of the whole matchmaking flow in both themes.

---

## 9. Migration procedure

### 9.1 Step 1 — Inventory every color source

Run from the repo root (adjust paths/extensions):

```bash
# Raw color literals in styles and components
rg -n -i --glob '!node_modules' --glob '!dist' --glob '!build' \
   -e '#[0-9a-f]{3,8}\b' -e 'rgba?\(' -e 'hsla?\(' -e 'oklch\(' -e 'color-mix\(' \
   src > color-literals.txt

# Gradients (should end up only in tokens + shell)
rg -n -i --glob '!node_modules' -e 'linear-gradient|radial-gradient|conic-gradient' src

# Tailwind palette utilities (if applicable)
rg -n --glob '!node_modules' -e '\b(bg|text|border|ring|from|via|to|fill|stroke|shadow|outline|divide|placeholder)-(pink|rose|fuchsia|purple|red|slate|gray|zinc|neutral)-[0-9]{2,3}\b' src

# Inline styles and CSS-in-JS
rg -n --glob '!node_modules' -e 'style=\{\{' -e 'styled\.' -e 'css`' src

# Theme conditionals in component code (these should disappear)
rg -n --glob '!node_modules' -e "theme\s*===\s*['\"]dark['\"]" -e 'isDark' -e 'dark:' src

# Pointer-events on decorative items; z-index sprawl
rg -n --glob '!node_modules' -e 'pointer-events' -e 'z-index' src
```

### 9.2 Step 2 — Build the audit table

Create a spreadsheet/markdown table with one row per color usage:

| File:line | Element/role | Current value | Theme(s) | Target token | Status | Contrast verified (worst point) |
|---|---|---|---|---|---|---|
| `Navbar.css:12` | nav background | `#...` | both | `--nav-bg` | ☐ | n/a |
| `Countdown.tsx:34` | countdown text | `#fff` | light | `--status-text` on `--status-bg` | ☐ | ☐ |

### 9.3 Step 3 — Role-based replacement map

Because the current CSS isn't in the report, map by **role**, not by old hex:

| If the current usage is… | Replace with |
|---|---|
| Page/body background, section backgrounds that duplicate the gradient | Remove; the shell owns `--page-bg` |
| Body/heading text | `--text-primary` |
| Muted label on a card | `--text-secondary` |
| Muted label **directly on the canvas** | `--text-on-canvas` |
| Card background | `--surface` (translucent ok) or `--surface-solid` (when text contrast must be guaranteed) |
| Menus/popovers | `--surface-raised` (solid) |
| Card border, divider | `--border` |
| Input/toggle border | `--border-strong` |
| Primary button bg / text | `--accent` / `--accent-text` |
| Hover / pressed | `--accent-hover` / `--accent-pressed` |
| Hot-pink logo | `--brand` |
| Hot-pink avatar | `--avatar-bg` + `--avatar-text` + `--avatar-ring` |
| Hot-pink active nav label | `--nav-active-text` on `--nav-active-bg`; coral/berry marker via `--nav-active-marker` |
| Hot-pink hero title | `--heading-gradient` (with `--heading-fallback`) |
| Dashed outer frame | `--frame-bg` + `1px solid var(--border)`; dashed only for `[data-state="empty"]` |
| White countdown / pink waiting title (light) | `.status-badge` (solid) or `--text-primary` |
| Floating hearts color | `--heart-color` |
| Box shadows | `--shadow-card` / `--shadow-nav` |
| Modal overlay | `--scrim` |

### 9.4 Step 4 — Theme-conditional logic

Remove component-level `isDark ? '#…' : '#…'` branches. If an element needs a different treatment per theme, add a **new semantic token** (e.g., `--status-bg`), don't branch in the component.

### 9.5 Step 5 — Enforce with lint

Add the Stylelint/ESLint guardrails from Appendix E in the same PR that finishes tokenization, so regressions fail CI.

---

## 10. Phased delivery plan (P0–P5)

Each phase should be its own PR (or a small stack) with before/after screenshots in both themes at desktop and mobile widths.

### P0 — Contrast fixes (highest priority; small and shippable alone)

**Goal:** Fix the readability failures the report flagged, immediately, even before tokenization is complete.

**Tasks**
1. Light-mode countdown → solid status badge (`#8E2357` bg, white text) **or** `#4A1733` text on gradient.
2. Light-mode "Waiting for Response" title → `#4A1733` or `#8E2357` (5.03:1 on worst glow).
3. Light-mode hero title → `#8E2357` solid (or `--heading-gradient`).
4. Avatar initials → `--avatar-bg: #D6006B`, white text (or large bold if keeping `#E90062`).
5. Active nav (light) → `#8E2357` text on `#FFE0F0` pill; (dark) → blush on `#4A1B3B`.
6. Light-mode secondary text directly on canvas → `#66304F`.
7. Hearts: `pointer-events:none`, `z-index` below content, lower opacity.

**Acceptance**
- Both modes pass the §8.3 worst-point measurement for every element above.
- Light-mode countdown + waiting status readable at a glance (squint test + 100% zoom screenshot).
- No change to layout; no new colors beyond those in §4.

**Estimate:** Small (≈ 0.5–1 day).

### P1 — Tokenize

**Goal:** All colors flow through semantic tokens; theme switch = one attribute.

**Tasks**
1. Add `tokens.primitives.css`, `tokens.light.css`, `tokens.dark.css` (§4).
2. Add `base.css` (focus ring, reduced-motion, forced-colors).
3. Run the inventory (§9.1); build the audit table (§9.2).
4. Replace color literals in **shared** components first (button, card, navbar, input, chip, avatar, status).
5. Replace theme conditionals (§9.4).
6. Add Stylelint rules (Appendix E) in warn mode, then error mode once clean.
7. Add the no-flash theme bootstrap (Appendix C) if not present.

**Acceptance**
- `rg` for hex/rgb/hsl outside `tokens.*.css` returns **zero** hits in shared components.
- Toggling `data-theme` changes every surface/text/border color with no component CSS differences.
- No visual regression vs. P0 except intentional diffs.

**Estimate:** Medium (≈ 2–4 days depending on component count).

### P2 — Tune surfaces

**Goal:** Clear surface hierarchy: canvas → frame → card → raised/overlay.

**Tasks**
1. Move the page gradient to `.app-shell` only (§5); remove any gradient restarts on sections.
2. Replace outer dashed frames with `.match-frame` (§6.5).
3. Apply `--card-gradient` to profile cards; verify worst-stop contrast.
4. Navbar surface/border/shadow (§6.2).
5. Menus/popovers/dialogs → solid surfaces + scrim (§6.11).
6. Chips (§6.7).

**Acceptance**
- Gradient visibly continuous across the page, no banding at card boundaries.
- Navbar, profile cards, outer frames, chips, modals and primary buttons are visually distinct roles.
- No large pure-white blocks in light mode.

**Estimate:** Medium (≈ 2–3 days).

### P3 — Tune accents

**Goal:** Disciplined accent usage: coral (dark) / berry (light) for actions; brand pink confined.

**Tasks**
1. Primary buttons → `--accent`/`--accent-text`.
2. Selected states (nav, chips, toggles) → tokenized selected treatments.
3. Brand pink → `--brand` for logo only; remove from CTAs/links.
4. Hero heading → `--heading-gradient`.
5. Review each component for "two or three equally saturated colors" (checklist item).

**Acceptance**
- Dark primary = plum text on coral; light primary = white on berry.
- No component shows brand pink + coral/berry + heading gradient at full strength together.

**Estimate:** Small–Medium (≈ 1–2 days).

### P4 — Interaction states

**Tasks**
1. Implement the full state matrix (§7) via tokens.
2. Global focus-visible (§6.12).
3. Disabled treatment without whole-control opacity.
4. Loading/error/success patterns with icons + text.

**Acceptance**
- Keyboard run-through: every interactive element shows a visible focus ring in both themes.
- Each state is distinguishable without color alone.

**Estimate:** Medium (≈ 2 days).

### P5 — Regression and hardening

**Tasks**
1. Visual regression suite across theme × viewport (Appendix D).
2. Contrast script in CI (Appendix B) against token values.
3. Full-page + modal-layer walkthrough, long text, small screens, scrolling, theme switching mid-session.
4. `reduced-motion`, 200%/400% zoom, forced-colors checks.
5. Capture before/after screenshots and re-run §8.3 worst-point sampling.

**Acceptance:** The full checklist in §12 is green.

**Estimate:** Medium (≈ 2–3 days).

### Suggested merge order & rollback

`P0 → P1 → P2 → P3 → P4 → P5`. P0 is independently shippable. P1 is the foundation for everything else. If a phase regresses production, revert that PR only; tokens (P1) are backward-compatible as long as old selectors still resolve.

---

## 11. Testing plan

### 11.1 Automated

| Test | Tool | Where | Notes |
|---|---|---|---|
| Token contrast (incl. composites) | `contrast-check.mjs` (Appendix B) | CI | Fails build on any pair below its threshold |
| Color literal ban | Stylelint (Appendix E) | CI / pre-commit | Allows literals only in `tokens.*.css` |
| Visual regression | Playwright screenshots (Appendix D) | CI | Theme × viewport; update baselines intentionally |
| Axe a11y scan | `@axe-core/playwright` | CI | Gradient-backed text shows as "incomplete" — covered manually in §8.3 |
| Theme toggle layout shift | Playwright bounding-box compare | CI | Assert key elements keep identical `boundingBox()` across themes |

### 11.2 Manual matrix

| Axis | Values |
|---|---|
| Theme | light, dark |
| Viewport | 390×844, 768×1024, 1440×900 |
| Browser | Chrome, Safari (iOS + macOS), Firefox, Android Chrome |
| Input | Mouse, touch, keyboard-only |
| Settings | default, reduced-motion, 200% zoom, forced-colors (Windows) |
| Content | short names, long names, many interest chips, empty state, error state |

### 11.3 Scenarios to walk through (both themes)

1. Land on Random Matchmaking → heading, subtitle, nav, frames, cards visible and legible.
2. Waiting for Response → countdown and waiting title readable; hourglass icon + text present.
3. Hover/press/focus every button, chip, nav link, toggle.
4. Open every menu, popover, dialog, toast → surfaces solid and legible over any scroll position.
5. Scroll long content → gradient doesn't band or restart; hearts stay behind content.
6. Toggle the theme repeatedly → no layout shift, no flash, no stale colors (check for any element still using an old literal).
7. Reload with each theme persisted → no flash of the wrong theme.
8. Resize across breakpoints → hearts count reduced on small screens, no overlap with content.

### 11.4 Definition of "contrast verified"

An element is verified when: (a) its token pair passes via the script, **and** (b) the §8.3 worst-point sample for its actual on-screen position passes. Record both in the audit table.

---

## 12. Master acceptance checklist

**Gradients**
- [ ] Both modes keep their original gradient character; no unintended flat background replaces it.
- [ ] Gradient lives on the app shell only; it does not restart on cards or sections.
- [ ] ≤ 2 radial glows over the linear base.
- [ ] No `background-attachment: fixed`; mobile scroll tested.

**Roles & surfaces**
- [ ] Navbar, profile cards, outer frames, chips, modals and primary buttons have distinct roles.
- [ ] Forms, dialogs, popovers and status blocks use solid surfaces.
- [ ] No large pure-white dashed frames remain; dashed is state-only.

**Accent discipline**
- [ ] Dark primary buttons: plum text on coral (never white on coral).
- [ ] Light primary buttons: white text on berry; berry *text* on gradient uses `#8E2357`, not `#AC326E`.
- [ ] Brand pink confined to logo/identity; not a CTA color.
- [ ] No component shows 2–3 equally saturated hues at once.

**Contrast**
- [ ] Every text color tested against the real composite surface at the worst gradient point.
- [ ] Light-mode countdown and waiting status readable at a glance.
- [ ] Avatar initials pass (≥ 4.5:1, or large-bold ≥ 3:1) on the production fill.
- [ ] Input/toggle boundaries ≥ 3:1 (`--border-strong`).

**Interaction & a11y**
- [ ] Keyboard focus visible in both themes; not represented by color or subtle shadow alone.
- [ ] All states (hover, pressed, selected, disabled, loading, error, success) implemented; none color-only.
- [ ] Reduced-motion, 200%/400% zoom, forced-colors verified.

**Decoration**
- [ ] Hearts: behind content, `pointer-events:none`, low opacity, `aria-hidden`, never overlap text/controls.

**Engineering**
- [ ] Theme switching toggles one root attribute; no stylesheet swapping.
- [ ] No hex/rgb/hsl outside `tokens.*.css` (lint enforced).
- [ ] No theme-conditional color logic in components.
- [ ] No theme-toggle layout shift; no flash of wrong theme on load.
- [ ] Before/after screenshots captured at desktop and mobile widths.

---

## 13. Risks and open questions

| # | Item | Risk | Suggested resolution |
|---|---|---|---|
| 1 | Source CSS not seen; hex values were sampled from screenshots | Tokens may not match existing variables | Reconcile in P1 audit; keep the report's names; alias old variables to new ones during migration |
| 2 | Proposed `--card-gradient`, `--heading-gradient`, disabled colors, pressed colors, status/feedback colors are **my** additions | Not signed off by design | Get designer review at the end of P2/P3; all are isolated tokens so changing them is a one-line edit |
| 3 | `#FFF5FA` vs `#FFF3FA` mismatch (§2.6a) | Minor tonal difference | Confirm; one-token change |
| 4 | Hot-pink logo exact value unknown | May fail on light canvas | Measure; use `--brand` light variant `#C4005A` if needed |
| 5 | Gradient stretching on long pages (Option A) | Glow drifts | Use Option B (fixed pseudo-element) |
| 6 | `color-mix()` and `backdrop-filter` support | Older browsers | Precompute tint tokens; keep nav opaque enough to not need blur |
| 7 | Third-party widgets/embeds with their own colors | Off-palette islands | Wrap in a solid surface; theme via their API or iframe `color-scheme` |
| 8 | Images/avatars with transparency | Unexpected backgrounds | Give avatar containers an explicit `--avatar-bg` |
| 9 | Theme persistence key and SSR | Flash of wrong theme | Appendix C bootstrap placed in `<head>` before CSS |
| 10 | Real user content (long names, many chips) | Overflow/contrast surprises | Test content matrix in §11.2 |

**Questions to confirm with design:** (1) Is a hot-pink wordmark mandatory in both themes? (2) Are the new status colors acceptable? (3) Should the avatar keep `#E90062` exactly? (4) Does the dark profile card want the proposed `#3A1530 → #321B29` gradient or something closer to the current look?

---

## Appendix A — Contrast reference table

All ratios computed with the WCAG 2.x relative-luminance formula. Rows marked **(report)** reproduce the PDF; others are additional pairs checked for this guide.

### A.1 Dark mode

| Foreground | Background | Ratio | Use | Result |
|---|---|---|---|---|
| `#FFE7E4` | `#2A1122` | 14.81 | Body text on deep plum **(report)** | AAA |
| `#FFE7E4` | `#321B29` | 13.44 | Primary text on card | AAA |
| `#FFE7E4` | `#4A1B3B` | 11.78 | Primary text on raised | AAA |
| `#FFE7E4` | `#181014` | 15.85 | Primary text on darkest canvas | AAA |
| `#FFE7E4` | `#3F122D` | 13.33 | Primary text on dark glow | AAA |
| `#FFC1B6` | `#573047` | 7.06 | Secondary on mauve **(report)** | AAA |
| `#FFC1B6` | `#321B29` | 10.24 | Secondary on card | AAA |
| `#FFC1B6` | `#4A1B3B` | 8.97 | Secondary on raised / nav-active / status | AAA |
| `#FFC1B6` | `#181014` | 12.07 | Secondary on canvas | AAA |
| `#FFC1B6` | `#3F122D` | 10.15 | Secondary on glow | AAA |
| `#FFC1B6` | `#2A1122` | 11.28 | Placeholder on input | AAA |
| `#2A1122` | `#FF7A7A` | 6.92 | Button text on coral **(report)** | AA |
| `#2A1122` | `#FF8C89` | 7.79 | Button hover | AA |
| `#2A1122` | `#E86A6A` | 5.58 | Button pressed | AA |
| `#FF7A7A` | `#573047` | 4.33 | Coral text on mauve **(report)** | **Fail** (fill/icon only) |
| `#FF7A7A` | `#4A1B3B` | 5.50 | Coral on raised | AA |
| `#FF7A7A` | `#321B29` | 6.28 | Coral on card | AA |
| `#FF7A7A` | `#3F122D` | 6.23 | Heading start on glow | AA |
| `#F43F8E` | `#181014` | 5.30 | Brand on canvas **(report)** | AA |
| `#F43F8E` | `#321B29` | 4.49 | Brand on card | **Fail by 0.01** (logo/large only) |
| `#9C6C88` | `#321A28` | 3.74 | Input border | UI ≥ 3 |
| `#FFA8A8` | `#321B29` | 8.62 | Danger | AAA |
| `#8CE0AE` | `#321B29` | 10.11 | Success | AAA |
| `#FFD28A` | `#321B29` | 11.20 | Warning | AAA |
| `#FFFFFF` | `#D6006B` | 5.16 | Avatar initials | AA |

### A.2 Light mode

| Foreground | Background | Ratio | Use | Result |
|---|---|---|---|---|
| `#4A1733` | `#FEC2E6` | 9.63 | Body on petal **(report)** | AAA |
| `#4A1733` | `#FFF7FB` | 13.65 | Body on card | AAA |
| `#4A1733` | `#F9C9DC` | 9.86 | Body on gradient end | AAA |
| `#4A1733` | `#FAB6D4` | 8.69 | Body on brightest glow | AAA |
| `#7E4665` | `#FFF7FB` | 6.76 | Secondary on card | AA |
| `#7E4665` | `#FEC2E6` | 4.77 | Secondary on petal **(report)** | AA (thin) |
| `#7E4665` | `#FAB6D4` | **4.31** | Secondary on brightest glow | **Fail → use `#66304F`** |
| `#66304F` | `#FAB6D4` | 6.05 | Secondary on canvas (NEW) | AA |
| `#66304F` | `#FEC2E6` | 6.70 | Secondary on petal | AA |
| `#FFFFFF` | `#AC326E` | 6.11 | Button **(report)** | AA |
| `#FFFFFF` | `#92204F` | 8.26 | Button hover | AAA |
| `#AC326E` | `#FFF7FB` | 5.81 | Berry text on card **(report)** | AA |
| `#AC326E` | `#FEC2E6` | 4.10 | Berry on canvas **(report)** | **Fail** |
| `#8E2357` | `#FFF7FB` | 7.89 | Focus / outline-button text on card | AAA |
| `#8E2357` | `#FEC2E6` | 5.57 | Berry-dark on petal | AA |
| `#8E2357` | `#F9C9DC` | 5.70 | Berry-dark on gradient end | AA |
| `#8E2357` | `#FAB6D4` | 5.03 | Berry-dark on brightest glow | AA |
| `#8E2357` | `#FFE0F0` | 6.79 | Active nav on pill | AA |
| `#FFFFFF` | `#8E2357` | 8.31 | Status badge | AAA |
| `#4A1733` | `#F5DFEA` | 11.38 | Chip text on chip | AAA |
| `#7E4665` | `#FFFFFF` | 7.12 | Placeholder on input | AAA |
| `#A35A80` | `#FFF7FB` | 4.61 | Input border | UI ≥ 3 |
| `#C4005A` | `#FFF7FB` | 5.71 | Brand on card | AA |
| `#C4005A` | `#FEC2E6` | 4.03 | Brand on canvas | Large/graphic only |
| `#F43F8E` | `#FEC2E6` | 2.37 | Old brand on canvas | **Fail** |
| `#B42318` | `#FFF7FB` | 6.25 | Danger (card) | AA |
| `#B42318` | `#FEC2E6` | 4.41 | Danger on canvas | **Fail → keep on cards** |
| `#14683A` | `#FFF7FB` | 6.50 | Success | AA |
| `#7A4A00` | `#FFF7FB` | 7.11 | Warning | AAA |
| `#FFFFFF` | `#D6006B` | 5.16 | Avatar initials | AA |
| `#FFFFFF` | `#E90062` | 4.52 | Avatar initials (original) **(report)** | AA (barely) |

### A.3 Translucent card composites (checked in the script)

| Surface | Over canvas stop | Composite | Primary text | Secondary text |
|---|---|---|---|---|
| Light `--surface` (.90) | `#FFF3FA` | `#FFF7FB` | 13.65 | 6.76 |
| | `#FFE5F2` | `#FFF5FA` | 13.47 | 6.67 |
| | `#F9C9DC` | `#FEF2F8` | 13.19 | 6.53 |
| | `#FEBDE2` | `#FFF1F8` | 13.13 | 6.50 |
| Dark `--surface` (.94) | `#181014` | `#301A28` | 13.65 | 10.39 |
| | `#211219` | `#311A28` | 13.60 | 10.36 |
| | `#3C132E` | `#331B29` | 13.39 | 10.20 |

---

## Appendix B — Contrast checker script

Save as `scripts/contrast-check.mjs`. Run `node scripts/contrast-check.mjs`. Exits non-zero on any failure, so it can gate CI. **Update the `LIGHT` / `DARK` objects whenever tokens change** (or generate them from your token files).

```js
// contrast-check.mjs — run: node contrast-check.mjs
// Computes WCAG 2.x contrast for token pairs, including alpha compositing.

const hex = (h) => {
  h = h.replace('#', '');
  if (h.length === 3) h = [...h].map((c) => c + c).join('');
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
};
const lin = (v) => {
  v /= 255;
  return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
};
const lum = ([r, g, b]) => 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
const ratio = (a, b) => {
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};
// Composite a foreground (hex) at alpha over an opaque background (hex)
const over = (fgHex, alpha, bgHex) => {
  const f = hex(fgHex), b = hex(bgHex);
  return f.map((c, i) => Math.round(alpha * c + (1 - alpha) * b[i]));
};

// ---- Worst-case backgrounds sampled from the page gradients ----
const LIGHT_CANVAS = ['#FFF3FA', '#FFE5F2', '#F9C9DC', '#FEC2E6', '#FFCCE5', '#FAB6D4', '#FBBFE1'];
const DARK_CANVAS  = ['#181014', '#211219', '#1B1016', '#3C132E', '#3F122D'];

const LIGHT = {
  textPrimary: '#4A1733', textSecondary: '#7E4665', textOnCanvas: '#66304F',
  accent: '#AC326E', accentText: '#FFFFFF', focus: '#8E2357', link: '#8E2357',
  surfaceSolid: '#FFF7FB', surfaceAlpha: ['#FFF7FB', 0.90],
};
const DARK = {
  textPrimary: '#FFE7E4', textSecondary: '#FFC1B6', accent: '#FF7A7A',
  accentText: '#2A1122', focus: '#FFC1B6',
  surfaceSolid: '#321B29', surfaceAlpha: ['#321B29', 0.94],
};

const rows = [];
const check = (label, fg, bg, min) => {
  const r = ratio(fg, bg);
  rows.push({ label, fg: hex2(fg), bg: hex2(bg), ratio: r.toFixed(2), min, pass: r >= min ? 'PASS' : 'FAIL' });
};
const hex2 = (c) => (Array.isArray(c) ? '#' + c.map((v) => v.toString(16).padStart(2, '0')).join('').toUpperCase() : c);
const rgb = (c) => (Array.isArray(c) ? c : hex(c));

// Light: text on every canvas stop (text sits directly on the gradient)
for (const bg of LIGHT_CANVAS) {
  check(`L textPrimary on canvas ${bg}`, rgb(LIGHT.textPrimary), hex(bg), 4.5);
  check(`L textOnCanvas on canvas ${bg}`, rgb(LIGHT.textOnCanvas), hex(bg), 4.5);
  check(`L focus on canvas ${bg}`, rgb(LIGHT.focus), hex(bg), 3);
}
// Light: text on translucent card composited over each canvas stop
for (const bg of LIGHT_CANVAS) {
  const card = over(...LIGHT.surfaceAlpha, bg);
  check(`L textPrimary on card@${bg}`, hex(LIGHT.textPrimary), card, 4.5);
  check(`L textSecondary on card@${bg}`, hex(LIGHT.textSecondary), card, 4.5);
  check(`L accent text on card@${bg}`, hex(LIGHT.accent), card, 4.5);
}
check('L button white on accent', hex('#FFFFFF'), hex(LIGHT.accent), 4.5);

// Dark
for (const bg of DARK_CANVAS) {
  check(`D textPrimary on canvas ${bg}`, hex(DARK.textPrimary), hex(bg), 4.5);
  check(`D textSecondary on canvas ${bg}`, hex(DARK.textSecondary), hex(bg), 4.5);
  const card = over(...DARK.surfaceAlpha, bg);
  check(`D textPrimary on card@${bg}`, hex(DARK.textPrimary), card, 4.5);
  check(`D textSecondary on card@${bg}`, hex(DARK.textSecondary), card, 4.5);
  check(`D accent (UI/icon) on card@${bg}`, hex(DARK.accent), card, 3);
}
check('D button plum on coral', hex(DARK.accentText), hex(DARK.accent), 4.5);

console.table(rows);
const fails = rows.filter((r) => r.pass === 'FAIL');
if (fails.length) { console.error(`\n${fails.length} failing pair(s)`); process.exit(1); }
console.log('\nAll pairs pass.');
```

**Extending it:** add rows for your `--card-gradient` endpoints (both stops), `--status-*`, `--chip-*`, and `--nav-active-*` pairs. To prove the §2.1 finding yourself, add `check('L textSecondary on canvas', hex('#7E4665'), hex('#FAB6D4'), 4.5)` — it will fail (4.31).

---

## Appendix C — Theme bootstrap (no flash)

Place **inline in `<head>`, before any stylesheet**:

```html
<script>
  (function () {
    try {
      var saved = localStorage.getItem('confessit-theme');          // 'light' | 'dark' | null
      var prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
      var theme = saved || (prefersDark ? 'dark' : 'light');
      document.documentElement.setAttribute('data-theme', theme);
    } catch (e) {
      document.documentElement.setAttribute('data-theme', 'light');
    }
  })();
</script>
```

Toggle handler:

```js
function setTheme(next) {
  const root = document.documentElement;
  root.setAttribute('data-theme', next);
  try { localStorage.setItem('confessit-theme', next); } catch (_) {}
  document.querySelector('meta[name="theme-color"]')
    ?.setAttribute('content', next === 'dark' ? '#181014' : '#FFE5F2');   // mobile browser chrome
}
```

Notes:
- Add `<meta name="theme-color">` so mobile browser UI matches.
- If you use SSR/SSG, the inline script above avoids a hydration flash; keep the attribute out of React state until after mount to prevent hydration mismatch warnings.
- Use the **same storage key** the app already uses if one exists.
- If you want to avoid color transitions firing on first paint, add a `no-transition` class on `<html>` until `DOMContentLoaded`, then remove it.

---

## Appendix D — Playwright visual and a11y tests

```ts
// tests/theme.spec.ts
import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const themes = ['light', 'dark'] as const;
const viewports = [
  { name: 'mobile',  width: 390,  height: 844 },
  { name: 'tablet',  width: 768,  height: 1024 },
  { name: 'desktop', width: 1440, height: 900 },
];

for (const theme of themes) {
  for (const vp of viewports) {
    test(`matchmaking — ${theme} — ${vp.name}`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.addInitScript((t) => localStorage.setItem('confessit-theme', t), theme);
      await page.goto('/match');                                    // adjust route
      await page.emulateMedia({ reducedMotion: 'reduce' });         // freeze hearts for stable screenshots
      await expect(page).toHaveScreenshot(`match-${theme}-${vp.name}.png`, { fullPage: true });
    });
  }

  test(`a11y scan — ${theme}`, async ({ page }) => {
    await page.addInitScript((t) => localStorage.setItem('confessit-theme', t), theme);
    await page.goto('/match');
    const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze();
    // Text over gradients is reported as "incomplete" — covered by manual §8.3 sampling
    expect(results.violations).toEqual([]);
  });
}

test('theme toggle causes no layout shift', async ({ page }) => {
  await page.goto('/match');
  const targets = ['.navbar', '.page-heading', '.match-frame', '.profile-card'];
  const before = await Promise.all(targets.map((s) => page.locator(s).first().boundingBox()));
  await page.getByRole('button', { name: /switch to (dark|light) mode/i }).click();
  const after = await Promise.all(targets.map((s) => page.locator(s).first().boundingBox()));
  expect(after).toEqual(before);
});

test('hearts never intercept pointer events', async ({ page }) => {
  await page.goto('/match');
  const pe = await page.locator('.hearts-layer').evaluate((el) => getComputedStyle(el).pointerEvents);
  expect(pe).toBe('none');
});
```

---

## Appendix E — Stylelint guardrails

```jsonc
// .stylelintrc.json
{
  "rules": {
    "color-no-hex": true,
    "function-disallowed-list": ["rgb", "rgba", "hsl", "hsla", "hwb", "lab", "lch", "oklch", "oklab"],
    "declaration-property-value-disallowed-list": {
      "/^(background|background-image)$/": ["/gradient\\(/"],          // gradients live in tokens + shell only
      "background-attachment": ["fixed"]                               // report: avoid on mobile
    }
  },
  "overrides": [
    {
      "files": ["**/tokens.*.css"],
      "rules": {
        "color-no-hex": null,
        "function-disallowed-list": null,
        "declaration-property-value-disallowed-list": null
      }
    },
    {
      "files": ["**/app-shell.css", "**/base.css"],
      "rules": { "declaration-property-value-disallowed-list": null }
    }
  ]
}
```

> `declaration-property-value-disallowed-list` with a `/gradient\(/` pattern will also flag `background: var(--card-gradient)`? **No** — `var()` contains no literal `gradient(` text, so token references pass. Raw gradients in component CSS are flagged, which is the goal. Adjust the allowlist if a component legitimately needs a local gradient (e.g., a skeleton shimmer).

**Tailwind projects:** add an ESLint rule (e.g., via `eslint-plugin-tailwindcss` `no-custom-classname` or a custom `no-restricted-syntax` regex) banning palette classes such as `bg-pink-*`, `text-rose-*`, `from-*`/`to-*` palette gradients in shared components; allow only token-mapped utilities.

**CSS-in-JS projects:** add `eslint-plugin-no-hardcoded-colors` (or a regex rule matching `#[0-9a-f]{3,8}` / `rgba?\(` in styled-component template literals), allowlisting the tokens file.

---

*End of guide. Suggested first action: ship P0 (§10) — it fixes the report's highest-priority readability issues without waiting on the full tokenization.*
