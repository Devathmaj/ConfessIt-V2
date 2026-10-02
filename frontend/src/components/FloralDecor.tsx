/*! @license Floral artwork (public/floral_svgs): FREE LICENSE (WITH ATTRIBUTION) — designed by Freepik - Magnific.com. Web attribution: <a href="https://www.magnific.com">designed by Freepik - Magnific.com</a> (invisible credit; the same notice is embedded in each SVG file and in index.html). */
import { cn } from '@/lib/utils';

/**
 * Invisible artwork credit (FREE LICENSE WITH ATTRIBUTION — designed by
 * Freepik - Magnific.com, https://www.magnific.com). A plain comment would be
 * stripped by the production minifier, so the notice is assigned to globalThis:
 * an observable side effect the bundler must keep, shipping the attribution in
 * every bundle that includes this module without rendering anything to users.
 */
(globalThis as Record<string, unknown>).__CONFESSIT_FLORAL_CREDIT__ =
  'Floral artwork: designed by Freepik - Magnific.com (https://www.magnific.com), FREE LICENSE (WITH ATTRIBUTION).';

/**
 * Theme-aware tree-branch decoration, sourced from `public/floral_svgs`.
 *
 * One file serves both modes: every `*_darkened` twin was verified to be a
 * uniform ×0.9 per-channel remap of its base file, so dark mode applies
 * `brightness(0.9)` via CSS (see `.floral-decor img` in index.css) instead of
 * downloading a duplicate multi-megabyte file. `loading="lazy"` keeps these
 * Illustrator exports off the critical path. Decorative only: hidden from
 * assistive tech and never clickable. Background rects were stripped from the
 * files so the branches overlay the app gradient (4536919 was already
 * transparent).
 */
export interface FloralPair {
  src: string;
  label: string;
}

export const FLORAL_PAIRS: FloralPair[] = [
  // `?v=3` marks the optimized generation: SVGO cut file weight ~43% after
  // immutable year-long caching began, so the query busts stale entries into
  // fetching the new bytes, which then cache hard.
  { src: '/floral_svgs/1706715_no_text_no_attribution.svg?v=3', label: 'Blossom branch' },
  { src: '/floral_svgs/1748704_cherry_blossom_no_attribution.svg?v=3', label: 'Cherry blossom' },
  { src: '/floral_svgs/4016406_cherry_blossom_no_text_no_attribution.svg?v=3', label: 'Cherry branch' },
  { src: '/floral_svgs/4126474_cherry_blossom_no_attribution.svg?v=3', label: 'Blossom sprig' },
  { src: '/floral_svgs/4536919_no_text_no_attribution.svg?v=3', label: 'Garden branch' },
  { src: '/floral_svgs/cherry_blossom_cropped.svg?v=3', label: 'Cropped blossom' },
  { src: '/floral_svgs/1706715_top_no_text_no_attribution.svg?v=3', label: 'Blossom branch top half' },
  { src: '/floral_svgs/1706715_bottom_no_text_no_attribution.svg?v=3', label: 'Blossom branch bottom half' },
];

interface FloralDecorProps {
  /** Index into FLORAL_PAIRS (0-7). */
  pair?: number;
  /** Position/size, e.g. "right-0 bottom-0 w-72 sm:w-96". Branches are wide
      landscape art and can go large. */
  className?: string;
  /** Applied to the img, e.g. opacity or rotation tweaks. */
  imgClassName?: string;
}

export const FloralDecor = ({ pair = 0, className, imgClassName }: FloralDecorProps) => {
  const floral = FLORAL_PAIRS[pair] ?? FLORAL_PAIRS[0];
  return (
    <span aria-hidden="true" data-floral={floral.label} className={cn('floral-decor', className)}>
      <img
        src={floral.src}
        alt=""
        draggable={false}
        loading="lazy"
        decoding="async"
        className={imgClassName}
      />
    </span>
  );
};

/**
 * Placement rules for the floral set (`public/floral_svgs`).
 *
 * - Pair 7 (`1706715_bottom`) is always bottom-left: page bottom-left or a
 *   container's bottom-left edge. Mirrors move it predictably: `-scale-x-100`
 *   (vertical axis) sends it to bottom-right, `-scale-y-100` (horizontal axis)
 *   to top-left, both to top-right.
 * - Pair 0 (`1706715` full) splits horizontally into two halves that behave as
 *   two SVGs (see `FloralSplit`): the upper half goes top-right, the lower
 *   half bottom-left. Each half mirrors like its pre-split twin (pair 6/7).
 * - Pair 6 (`1706715_top`) is always top-right. Mirror on the vertical axis
 *   (`-scale-x-100`) for top-left, on the horizontal axis (`-scale-y-100`) for
 *   bottom-right, on both for bottom-left.
 * - Pair 1 (`1748704`) is always bottom-left. `-scale-x-100` for bottom-right,
 *   `-scale-y-100` for top-left, both for top-right.
 * - Pair 2 (`4016406`) spans the entire bottom edge and is reserved for the
 *   matchmaking page only — never reuse it elsewhere.
 * - Pair 3 (`4126474`) is always top-left. `-scale-x-100` for top-right,
 *   `-scale-y-100` for bottom-left, both for bottom-right.
 * - Pair 4 (`4536919`) envelopes a container: one large centered piece behind
 *   it (item in the middle, art on all 4 edges/corners), never a corner piece.
 * - Pair 5 (`cherry_blossom_cropped`) is always top-left, mirroring like
 *   pair 3 (`-scale-x-100` top-right, `-scale-y-100` bottom-left, both
 *   bottom-right).
 */
export const FloralSplit = ({
  half,
  className,
  imgClassName,
}: {
  /** 'top' renders the upper half (placed top-right); 'bottom' the lower half (bottom-left). */
  half: 'top' | 'bottom';
  className?: string;
  imgClassName?: string;
}) => (
  <span
    aria-hidden="true"
    data-floral={half === 'top' ? 'Blossom branch (upper half)' : 'Blossom branch (lower half)'}
    className={cn('floral-decor', className)}
  >
    <img
      src={FLORAL_PAIRS[0].src}
      alt=""
      draggable={false}
      loading="lazy"
      decoding="async"
      style={half === 'top' ? { clipPath: 'inset(0 0 50% 0)' } : { clipPath: 'inset(50% 0 0 0)' }}
      className={imgClassName}
    />
  </span>
);

export default FloralDecor;
