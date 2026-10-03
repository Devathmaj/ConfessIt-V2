# Romantic edge restyle: implementation report

Scope: `frontend/src`. Goal: change the edges, shapes and icon containers of existing components to a romantic look, with a one-line switch between the style directions shown in the mockups.

## 1. Guardrails

What changes (structure only):

- Border radius, border width and border style (solid, dashed, dotted)
- Inner-frame lines, corner brackets, and the shape of icon containers (circle, oval, leaf, square)
- Optional ornament elements (seal, tape, ribbon, sticker, postmark) that only use existing tokens

What stays untouched:

- Every color value, font, font size and weight
- All gradients, including `page-bg` and any text or font-color gradient
- `--shadow-card`, `--shadow-romantic`, `--shadow-love`, `--shadow-glow` and their hover use
- `backdrop-blur`, hover `translateY`, transitions, animations, focus rings, overlays
- Global tokens (`index.css:235,328-329,358-359,372-374,520`), `FloralDecor` and `ScatteredIcons`

Rule for reviewers: a changed line may contain `radius`, `border-style`, `border-width`, `clip-path`, `outline` or ornament markup. It must not add or edit a hex value, `rgba()`, `hsl()`, `gradient`, `font-*`, `text-[...]` or a shadow token.

## 2. Approach: one edge layer

Add a new file `src/edge-style.css`, imported after `index.css` in `main.tsx`. It defines new `--edge-*` variables only. No existing token value is edited.

```css
:root {
  --edge-card: var(--radius);          /* 16px, same as today */
  --edge-card-sm: 12px;                /* nav shell */
  --edge-control: 14px;                /* inputs, buttons, tabs */
  --edge-pill: 999px;
  --edge-chip: 999px;
  --edge-icon: 12px;                   /* game icon tiles */
  --edge-avatar: 50%;
  --edge-line: solid;                  /* solid | dashed | dotted */
}
```

The defaults equal today's values, so merging phase 0 changes nothing visually. A preset is chosen by `data-edge="leaf"` on `<html>` and overrides these variables:

```css
[data-edge="leaf"] {
  --edge-card: 30px 6px 30px 6px;
  --edge-control: 18px 4px 18px 4px;
  --edge-chip: 12px 3px 12px 3px;
  --edge-icon: 18px 4px 18px 4px;
}
```

React components use the variable instead of a fixed Tailwind radius. Use the arbitrary-property form, which also works for multi-value (asymmetric) radii and on any Tailwind version: `[border-radius:var(--edge-card)]`.

## 3. Preset values

Each direction is one `[data-edge]` block. Values are for card / control / chip, plus the line style and extras.

- `petal`: 28px / 999px / 999px, solid, circular icon badges
- `leaf`: 30px 6px 30px 6px / 18px 4px 18px 4px / 12px 3px 12px 3px, solid
- `tag` (heart tag): 16px / 12px / 8px, dashed card edge, heart ornament on card
- `parchment`: 6px / 4px / 4px, solid, inner frame, underline-only fields, seal ornament
- `envelope`: 3px / 999px buttons, 2px fields / 2px chips, dashed fields and chips, flap ornament
- `stamp`: 2px everywhere, dotted edges (card 6px, fields and buttons 2-3px), postmark ornament
- `note` (handwritten): 3px / 255px 14px 225px 14px / 14px 225px 14px 255px, hand-drawn radius on controls, tape ornament
- `cameo`: 50% 50% 8px 8px / 34px 34px 8px 8px on cards, 14px 14px 4px 4px on buttons, oval avatars, dotted lace bottom edge
- `candy`: 22px / 16px / 999px, 2px solid outline, heart-shaped avatar, sticker ornament
- `chat`: 20px 20px 20px 4px (received) and 20px 20px 4px 20px (sent), pill fields and buttons, bubble tail
- `polaroid`: 2px / 8px / 999px, dashed fields, tape ornament, slight rotation on cards
- `brackets`: 0 everywhere, corner brackets, diamond avatars
- `ribbon`: 10px / 10px / swallowtail tag shape, ribbon header ornament

## 4. Component change map

All of these swap a radius or border style for an `--edge-*` variable. Nothing else on the line is edited.

**Base primitives (`components/ui`)**

- `card.tsx:11`: `rounded-lg` becomes `--edge-card`. Keep `border`, `bg-card`, `shadow-sm`.
- `button.tsx:8-27`: `rounded-md` becomes `--edge-control`. `default` and `outline` variants keep their fill and border colors.
- `input.tsx:11`, `textarea.tsx:13`, `select.tsx:20`: `rounded-md` becomes `--edge-control`. Border width and color stay. Preset-only underline fields (`parchment`) are done in the preset block by setting border width to `0 0 1.5px`, not in the component.
- `select.tsx:76`, Popover, HoverCard, Tooltip: `rounded-md` becomes `--edge-control`.
- `dialog.tsx:22,39`: `sm:rounded-lg` becomes `sm:[border-radius:var(--edge-card)]`. Keep the `sm:` prefix so mobile behavior is unchanged. `sheet.tsx:32`: no radius today, leave as is.
- `alert.tsx:7`: `rounded-lg` becomes `--edge-card-sm`.
- `badge.tsx:7`: `rounded-full` becomes `--edge-chip`.
- `avatar.tsx:13`: `rounded-full` becomes `--edge-avatar`. Keep `overflow-hidden`.
- `tabs`: `TabsList rounded-md` and `Trigger rounded-sm` become `--edge-control`.
- `separator`: no change. Optional `HeartDivider` is a new component (section 6).

**Classes in `index.css` (component rules only, not the global token blocks)**

- `.confession-card` (645-656): `border-radius:var(--radius)` becomes `var(--edge-card)`, add `border-style:var(--edge-line)`. Keep background, shadow, hover transform. The `border-2` override in `Login.tsx:429` must keep working, so test it.
- `.btn-romantic`, `.btn-love` (711-784): radius becomes `--edge-control`. Keep the 2px `--border-strong` width and color on `.btn-love`, and all hover effects.
- `.chip` (898), `.status-badge` (788): radius becomes `--edge-chip`. `.chip` border style uses `--edge-line`.
- `.avatar` (889): `50%` becomes `--edge-avatar`. The `--avatar-ring` box-shadow stays.
- `.match-frame` (841 area): `20px` becomes `--edge-card`. Keep the existing dashed empty state.

**Feature components**

- `GameCard`, `StatCard` (`rounded-2xl`): becomes `--edge-card`. Icon tile `rounded-xl` becomes `--edge-icon`. Keep `border-border/60`, `bg-card/70`, `backdrop-blur` and the hover classes.
- Same swap in `GamesDashboardPage.tsx:152,273`, `GameCategorySection.tsx:209,225` and the `Admin*Config.tsx` files. The empty state keeps `border-dashed` and only changes radius.
- `Navigation.tsx:43,52`: shell `rounded-xl` becomes `--edge-card-sm`, links `rounded-full` become `--edge-pill`. Keep the blur and nav shadow.
- `GamesLayout.tsx`: only the `border-b` and `border-t` lines, so no change.
- Pages (`ConfessionsPage`, `MatchmakingPage`, `LoveNotesPage`, `About`, `Login`) get the change through `.confession-card`. Search each for local `rounded-*` overrides on the same element.

## 5. Ornaments (optional, per preset)

Create `components/edge/EdgeOrnament.tsx` plus CSS in `edge-style.css`. Hidden by default (`display:none`), shown only by the matching `data-edge` preset.

- Types: seal, flap, tape, ribbon, postmark, sticker, corner brackets, bubble tail.
- Color sources are limited to existing tokens: `--action`, `--surface`, `--border`, `--border-strong`, `--chip-bg`. No new hex values.
- Icons use the lucide set already in the app (Heart, Mail, Flower, Sparkles) at `strokeWidth` 1.5 to match `ScatteredIcons`.
- Always `aria-hidden`, `pointer-events:none`, absolutely positioned inside a `position:relative` parent. They sit above the Decor layer (`z-index:-1`) so Decor is not affected.
- Inner frames use an `::after` pseudo-element with `inset`, not `outline`, so the focus outline is never taken.

## 6. Rollout

1. Phase 0: add `edge-style.css` with defaults only. Screenshot every page in light and dark as the baseline. Expect zero diff.
2. Phase 1: swap `components/ui/*` to variables. Still zero diff with the default preset.
3. Phase 2: swap the `index.css` component classes and the feature components in section 4.
4. Phase 3: turn on the chosen preset with `data-edge` in `main.tsx`. Review all pages.
5. Phase 4: add ornaments for the chosen preset only.

Finding every spot: `rg "rounded-(sm|md|lg|xl|2xl|full)" frontend/src`, `rg "border-(2|dashed|dotted)" frontend/src`, `rg "border-radius" frontend/src/index.css`.

## 7. Risks and checks

- Asymmetric radii break `rounded-t-*` or `rounded-l-*` usages (tables, sheets, joined button groups). Review those by hand.
- `overflow-hidden` children, such as images in cards, clip to the new corners. Check card media.
- `clip-path` shapes (ribbon tags, heart avatar) also clip borders, shadows and focus rings. Use them only on non-interactive badges and avatars. Drop the avatar ring on the `candy` preset.
- Dotted or dashed 6px borders add visual width. Compare card paddings on narrow screens.
- Hover lift on `.confession-card` and `GameCard` must still look right with ornaments attached.
- Automated guard: `git diff -U0 | rg "^\+.*(#[0-9a-fA-F]{3,8}|rgba?\(|hsl|gradient|font-|text-\[)"` must return nothing.
- Manual: light and dark mode, 375px and 1280px widths, keyboard focus on every input and button, the Login `border-2` card.

## 8. Decision needed

Pick one preset to ship first (or two to compare via `data-edge`). Phases 0 to 2 are identical for all of them, so they can start immediately.
