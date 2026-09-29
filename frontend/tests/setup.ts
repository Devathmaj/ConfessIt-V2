import '@testing-library/jest-dom/vitest';
import { vi } from 'vitest';
import { configure } from '@testing-library/react';

// Async queries (`findBy*`, `waitFor`) poll on a 1000ms budget by default, which a
// fully parallel suite run regularly exceeds on a loaded machine -- not because the
// app is slow, but because 64 test files share the CPUs. The assertions themselves
// are unchanged; only the wait budget grows, so a genuinely missing element still
// fails, just after a tolerance appropriate for contention.
configure({ asyncUtilTimeout: 2500 });

// jsdom ships no matchMedia, and the layout primitives in the UI read it on mount. Every
// test file used to carry its own copy of this stub; it lives here now so a component
// cannot be exercised against a different environment depending on which test pulled it in.
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation(query => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(), // deprecated
    removeListener: vi.fn(), // deprecated
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});

// jsdom implements neither PointerEvent nor the capture APIs, and Radix's Select opens on
// pointer down and tracks selection with `hasPointerCapture`. Without these it throws on the
// first interaction rather than failing an assertion, so any test that touches a <Select> dies
// before it can say what it meant to say. `scrollIntoView` is the third of the set: Radix
// scrolls the highlighted item into view when the list opens.
//
// Scoped to the elements rather than the window, and `addEventListener` is left alone -- Radix
// passes these functions as callbacks and expects the browser's own event object back.
Object.defineProperty(Element.prototype, 'hasPointerCapture', {
  writable: true,
  value: vi.fn().mockReturnValue(false),
});
Object.defineProperty(Element.prototype, 'setPointerCapture', {
  writable: true,
  value: vi.fn(),
});
Object.defineProperty(Element.prototype, 'releasePointerCapture', {
  writable: true,
  value: vi.fn(),
});
Object.defineProperty(Element.prototype, 'scrollIntoView', {
  writable: true,
  value: vi.fn(),
});
