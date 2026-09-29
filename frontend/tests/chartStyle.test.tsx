import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { ChartStyle } from '@/components/ui/chart';

/**
 * The `<style dangerouslySetInnerHTML>` sink in ChartStyle.
 *
 * Config keys become custom-property names and colours become declaration values, so
 * an unvalidated pair breaks out of the rule into arbitrary CSS. Keys must be
 * identifier-safe, colours hex literals, and the element id reduced to identifier
 * characters; anything else is dropped and degrades to unstyled rather than executing.
 */
describe('ChartStyle', () => {
  it('renders validated keys and colours, dropping hostile entries', () => {
    const { container } = render(
      <ChartStyle
        id="sales"
        config={{
          good: { color: '#ff0000' },
          'evil]; attack: yes': { color: 'red' },
          sneaky: { color: 'red; } .pwned { color: blue' },
          themed: { theme: { light: '#00ff00', dark: 'hsl(0, 100%, 50%)' } },
        }}
      />,
    );

    const css = container.querySelector('style')?.textContent ?? '';
    expect(css).toContain('--color-good: #ff0000');
    // The light theme value is hex and survives; the hsl() one does not.
    expect(css).toContain('--color-themed: #00ff00');
    expect(css).not.toContain('evil');
    expect(css).not.toContain('pwned');
    expect(css).not.toContain('hsl(');
    expect(css).not.toContain('attack');
  });

  it('sanitises the element id used in the selector', () => {
    const { container } = render(
      <ChartStyle id={'a"]} [data-x="'} config={{ good: { color: '#ff0000' } }} />,
    );

    const css = container.querySelector('style')?.textContent ?? '';
    expect(css).toContain('[data-chart=adata-x]');
    expect(css).not.toContain('"');
  });

  it('renders nothing when no entry survives validation', () => {
    const { container } = render(
      <ChartStyle
        id="sales"
        config={{ 'not valid!!': { color: 'not-a-colour' } }}
      />,
    );

    expect(container.querySelector('style')).toBeNull();
  });
});
