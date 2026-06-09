import { render, screen } from '@testing-library/react';
import App from './App';
import { describe, it, expect, vi } from 'vitest';

// Mock matchMedia for window since jsdom doesn't support it
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

describe('App Component', () => {
  it('renders without crashing', () => {
    render(<App />);
    // The App initially renders a loading state or login page
    // We just verify it doesn't crash here.
    expect(document.body).toBeInTheDocument();
  });
});
