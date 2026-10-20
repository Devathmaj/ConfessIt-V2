import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { CopyLoginLink } from '@/pages/CopyLoginLink';

const writeText = vi.fn<(text: string) => Promise<void>>();

const renderPage = (loginUrl: string) =>
  render(
    <MemoryRouter
      initialEntries={[`/copy-login-link?url=${encodeURIComponent(loginUrl)}`]}
    >
      <CopyLoginLink />
    </MemoryRouter>,
  );

describe('CopyLoginLink', () => {
  beforeEach(() => {
    writeText.mockReset();
    writeText.mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
  });

  it('copies the exact sign-in URL on load', async () => {
    const loginUrl = `${window.location.origin}/verify-login?token=secret-token`;

    renderPage(loginUrl);

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(loginUrl));
    expect(
      await screen.findByText(/the link is on your clipboard/i),
    ).toBeInTheDocument();
    expect(screen.getByText(loginUrl)).toBeInTheDocument();
  });

  it('offers a manual copy when the browser blocks automatic access', async () => {
    const loginUrl = `${window.location.origin}/verify-login?token=secret-token`;
    writeText
      .mockRejectedValueOnce(new Error('clipboard blocked'))
      .mockResolvedValueOnce(undefined);
    Object.defineProperty(document, 'execCommand', {
      configurable: true,
      value: vi.fn().mockReturnValue(false),
    });

    renderPage(loginUrl);

    expect(
      await screen.findByText(/browser blocked automatic copying/i),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /^copy link$/i }));

    expect(
      await screen.findByText(/the link is on your clipboard/i),
    ).toBeInTheDocument();
    expect(writeText).toHaveBeenNthCalledWith(2, loginUrl);
  });

  it('refuses to copy a URL outside this frontend', async () => {
    renderPage('https://evil.example/verify-login?token=secret-token');

    expect(
      await screen.findByText(/copy link is invalid or incomplete/i),
    ).toBeInTheDocument();
    expect(writeText).not.toHaveBeenCalled();
  });
});
