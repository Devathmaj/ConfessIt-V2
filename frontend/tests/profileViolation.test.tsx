import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AuthContext } from '@/contexts/AuthContext';
import { ProfilePage } from '@/pages/ProfilePage';

const api = vi.hoisted(() => ({
  updateUserProfile: vi.fn(),
  uploadProfilePicture: vi.fn(),
}));

vi.mock('@/services/api', () => api);
vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn() },
  Toaster: () => null,
}));

const violationError = (message: string) => {
  const err: any = new Error(message);
  err.response = { status: 422, data: { detail: message } };
  return err;
};

const renderProfile = () => {
  const fetchAndSetUser = vi.fn().mockResolvedValue(undefined);
  render(
    <AuthContext.Provider
      value={{
        user: {
          id: 'u1',
          name: 'Tester',
          regno: 'CS-1',
          email: 't@example.com',
          bio: '',
          interests: [],
          matchmaking_enabled: true,
          notifications_enabled: true,
          love_notes_receive_enabled: true,
          profile_picture_url: null,
          last_profile_picture_at: null,
        },
        token: 'token-1',
        fetchAndSetUser,
        isAuthenticated: true,
        loading: false,
        can: () => true,
      } as never}
    >
      <MemoryRouter>
        <ProfilePage />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
  return { fetchAndSetUser };
};

const pickFile = (container: HTMLElement) => {
  const input = container.querySelector('input[type="file"]') as HTMLInputElement;
  const file = new File(['png-bytes'], 'face.png', { type: 'image/png' });
  fireEvent.change(input, { target: { files: [file] } });
};

describe('ProfilePage: picture violation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows the violation popup with the server sentence and refreshes the account', async () => {
    api.uploadProfilePicture.mockRejectedValue(
      violationError('Your profile picture was rejected because it violated our content policies.'),
    );
    const { fetchAndSetUser } = renderProfile();

    pickFile(document.body);

    await screen.findByText('Profile picture rejected');
    expect(
      screen.getByText('Your profile picture was rejected because it violated our content policies.'),
    ).toBeInTheDocument();
    // The refused attempt still spent the window, so the page must not keep
    // offering a change the server will refuse.
    await waitFor(() => expect(fetchAndSetUser).toHaveBeenCalledWith('token-1'));
  });

  it('keeps a 400 as a toast and does not open the violation popup', async () => {
    api.uploadProfilePicture.mockRejectedValue({
      response: { status: 400, data: { detail: 'Invalid image file' } },
    });
    const { fetchAndSetUser } = renderProfile();

    pickFile(document.body);

    await waitFor(() => expect(api.uploadProfilePicture).toHaveBeenCalled());
    expect(screen.queryByText('Profile picture rejected')).not.toBeInTheDocument();
    expect(fetchAndSetUser).not.toHaveBeenCalled();
  });

  it('leaves the accepted upload path alone', async () => {
    api.uploadProfilePicture.mockResolvedValue({
      profile_picture_url: 'https://cdn.example/new.png',
      message: 'Profile picture uploaded successfully',
    });
    const { fetchAndSetUser } = renderProfile();

    pickFile(document.body);

    await waitFor(() => expect(fetchAndSetUser).toHaveBeenCalledWith('token-1'));
    expect(screen.queryByText('Profile picture rejected')).not.toBeInTheDocument();
  });

  it('shows the upload control busy while the picture is processed, and one pick is one upload', async () => {
    let resolveUpload!: (value: unknown) => void;
    api.uploadProfilePicture.mockImplementation(
      () => new Promise((resolve) => { resolveUpload = resolve; }),
    );
    renderProfile();

    pickFile(document.body);

    // The control that opens the picker is busy and disabled while the server has the image.
    const busy = await screen.findByRole('button', { name: /uploading profile picture/i });
    expect(busy).toBeDisabled();
    expect(api.uploadProfilePicture).toHaveBeenCalledTimes(1);

    // A second pick while the first is in flight starts nothing: one change window, one upload.
    pickFile(document.body);
    expect(api.uploadProfilePicture).toHaveBeenCalledTimes(1);

    resolveUpload({ profile_picture_url: 'https://cdn.example/new.png', message: 'ok' });
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /change profile picture/i })).toBeEnabled(),
    );
  });
});
