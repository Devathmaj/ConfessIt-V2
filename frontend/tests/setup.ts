import '@testing-library/jest-dom';
import { vi } from 'vitest';

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
