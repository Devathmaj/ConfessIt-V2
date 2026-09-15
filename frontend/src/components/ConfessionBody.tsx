/**
 * The one place a confession's text is rendered.
 *
 * Three problems, all of them invisible until a confession is long enough or shaped a certain
 * way, which is why they were spread across five call sites before this existed:
 *
 * 1. **Unbroken runs overflow the card.** A confession is free text with no guarantee that it
 *    contains a space. 500 characters of a URL, a Devanagari conjunct chain, or an unbroken
 *    run of emoji is one unbreakable token as far as the layout is concerned, and it pushes
 *    past the card's edge. `overflow-wrap: anywhere` breaks inside the word when there is no
 *    other option, which `break-words` alone will not do.
 *
 * 2. **Newlines were being thrown away.** The server accepts `\n` and the compose box lets a
 *    person press Enter, but inside a plain `<p>` the browser collapses consecutive whitespace
 *    into a single space. A confession written as two deliberate lines was being shown as one
 *    run-on paragraph. `whitespace-pre-wrap` keeps the author's line breaks and still wraps.
 *
 * 3. **The flex parent would not let it shrink.** A flex item defaults to `min-width: auto`,
 *    so it refuses to get narrower than its widest word no matter what the text inside it says.
 *    Inside the admin row that made the badges get pushed off the right edge rather than the
 *    text wrapping. `min-w-0` on the item is what lets the wrap actually happen.
 *
 * `clamp` is for the feed and the collapsed admin row, where the body is a preview and the
 * full text is one click away. The expanded views pass nothing and get all of it.
 *
 * The clamp values are spelled out rather than interpolated because Tailwind builds its CSS by
 * scanning source for complete class names. `` `line-clamp-${clamp}` `` produces a class string
 * that appears in no file, so the rule is never emitted and the clamp silently does nothing --
 * the layout keeps overflowing and the only clue is that a utility was "applied". A missing
 * class is the failure mode; adding a clamp value here is one edit, and the compiler fails
 * loudly if the name is wrong.
 */
const CLAMP_CLASSES: Record<number, string> = {
  2: 'line-clamp-2',
  3: 'line-clamp-3',
  4: 'line-clamp-4',
  5: 'line-clamp-5',
  6: 'line-clamp-6',
};

interface ConfessionBodyProps {
  children: string;
  /** Lines to show before clamping. Omit for the whole text. */
  clamp?: number;
  className?: string;
  /**
   * Render a `<span className="block">` instead of a `<p>`. Required when the body is the
   * label of a `<button>`: a `<p>` is flow content, a button may only contain phrasing
   * content, and the browser's parser is entitled to relocate an invalid element out of the
   * button it was written inside.
   *
   * `block` is only added when there is no clamp, because `line-clamp-*` sets its own
   * `display: -webkit-box` and the two would fight over which one applies.
   */
  element?: 'p' | 'span';
}

export const ConfessionBody = ({
  children,
  clamp,
  className = '',
  element = 'p',
}: ConfessionBodyProps) => {
  if (clamp !== undefined && CLAMP_CLASSES[clamp] === undefined) {
    throw new Error(
      `ConfessionBody: no Tailwind class for line-clamp-${clamp}. Add it to CLAMP_CLASSES or the ` +
        'clamp will not be applied, because Tailwind only emits classes it can see in source.'
    );
  }

  const classes = [
    'whitespace-pre-wrap break-words [overflow-wrap:anywhere] min-w-0',
    clamp === undefined ? '' : CLAMP_CLASSES[clamp],
    element === 'span' && clamp === undefined ? 'block' : '',
    className,
  ]
    .filter(Boolean)
    .join(' ');

  // A paragraph rather than a div, so it still inherits type scale from whatever slot it is
  // dropped into and screen readers announce it as prose.
  if (element === 'span') {
    return <span className={classes}>{children}</span>;
  }
  return <p className={classes}>{children}</p>;
};
