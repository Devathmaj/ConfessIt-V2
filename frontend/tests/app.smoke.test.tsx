import { render, screen } from '@testing-library/react';
import App from '@/App';
import { describe, it, expect } from 'vitest';

describe('App Component', () => {
  it('renders without crashing', () => {
    render(<App />);
    // The App initially renders a loading state or login page
    // We just verify it doesn't crash here.
    expect(document.body).toBeInTheDocument();
  });
});
