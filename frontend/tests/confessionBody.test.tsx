import { describe, it, expect } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { ConfessionBody } from '@/components/ConfessionBody';

/**
 * The layout rules here are CSS, so they cannot be asserted in jsdom -- jsdom has no layout and
 * reports every element as zero-width. What can be asserted is that the right class names are
 * present, which is the part that actually breaks: Tailwind emits rules by scanning source, so
 * a class written as a template literal is never generated and the utility silently does
 * nothing. That failure looks exactly like the bug it was meant to fix.
 */
const render = (props: { children: string; clamp?: number; className?: string }) =>
  renderToStaticMarkup(<ConfessionBody {...props} />);

/** The classes an overflow bug needs fixed, and the reason each one is load-bearing. */
const REQUIRED = {
  'whitespace-pre-wrap': 'preserves the newlines the server accepts and the compose box allows',
  'break-words': 'breaks a long word at the card edge',
  '[overflow-wrap:anywhere]':
    'breaks a run with no break opportunity at all, which break-words alone will not do',
  'min-w-0': 'lets the flex parent shrink this item instead of holding its widest word',
};

describe('ConfessionBody', () => {
  it.each(Object.entries(REQUIRED))('applies %s', (className) => {
    // Every one of these is a no-op without the others, so the whole set is asserted together
    // rather than one per test.
    expect(render({ children: 'text' })).toContain(className);
  });

  it('renders the text itself', () => {
    expect(render({ children: 'I still think about you' })).toContain(
      'I still think about you'
    );
  });

  it('renders the author’s line breaks rather than collapsing them', () => {
    const html = render({ children: 'first line\nsecond line' });
    // Two things have to hold: the literal newline survives into the markup, or pre-wrap has
    // nothing to preserve; and the class that tells the browser to honour it is applied. The
    // compiled `white-space:pre-wrap` declaration itself is not assertable here -- it lives in
    // the stylesheet, which this render path never loads.
    expect(html).toContain('first line\nsecond line');
    expect(html).toContain('whitespace-pre-wrap');
  });

  it('escapes the text instead of rendering it as markup', () => {
    // A confession is user input that staff read in this row. Rendering it raw would let anyone
    // posting a confession inject markup into the moderation screen.
    const html = render({ children: '<script>alert(1)</script>' });
    expect(html).not.toContain('<script>');
    expect(html).toContain('&lt;script&gt;');
  });

  it('escapes a confession that tries to close the tag early', () => {
    const html = render({ children: '</p><img src=x onerror=alert(1)>' });
    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;img');
  });

  describe('clamp', () => {
    it('is absent when not asked for, so the full text shows', () => {
      expect(render({ children: 'text' })).not.toContain('line-clamp-');
    });

    it.each([2, 3, 4, 5, 6])('uses a real Tailwind class for a clamp of %i', (clamp) => {
      // The point of the lookup table. `line-clamp-${clamp}` compiles to a class name that
      // appears in no source file, so Tailwind never emits the rule and the clamp does nothing
      // while looking applied.
      const html = render({ children: 'text', clamp });

      expect(html).toContain(`line-clamp-${clamp}`);
    });

    it('refuses a clamp it has no class for, rather than silently not applying it', () => {
      // A missing class is the failure mode this component exists to prevent, so an unknown
      // clamp has to be loud.
      expect(() => render({ children: 'text', clamp: 47 })).toThrow(/line-clamp-47/);
    });
  });

  it('keeps a caller class alongside its own', () => {
    const html = render({ children: 'text', className: 'text-foreground leading-relaxed' });
    expect(html).toContain('text-foreground');
    expect(html).toContain('leading-relaxed');
    expect(html).toContain('min-w-0');
  });
});
