import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { ProfileReview } from '@/pages/ProfileReview';

/**
 * Bulk participant onboarding from the profile-review screen.
 *
 * The server applies a file whole or not at all, so the screen has one control, one file and
 * one action. These tests pin the outcomes an operator sees: a successful upload names how
 * many accounts were created, a refusal is shown inside the dialog where the row numbers in
 * the server's message can actually be read -- a toast is the wrong place for a multi-line
 * refusal -- and the locally parsed preview is paginated with a sample file available.
 */

const api = vi.hoisted(() => ({
  getAdminUsers: vi.fn(),
  setAdminUserBlocked: vi.fn(),
  createAdminUser: vi.fn(),
  updateAdminUser: vi.fn(),
  bulkUploadAdminUsers: vi.fn(),
}));

const preview = vi.hoisted(() => ({
  BULK_UPLOAD_PREVIEW_MAX_COLUMNS: 10,
  BULK_UPLOAD_PREVIEW_PAGE_SIZE: 10,
  parseBulkUploadPreview: vi.fn(),
  downloadBulkUploadSample: vi.fn(),
}));

const toast = vi.hoisted(() => ({
  info: vi.fn(),
  error: vi.fn(),
  success: vi.fn(),
  warning: vi.fn(),
}));

vi.mock('@/services/api', () => api);
vi.mock('sonner', () => ({ toast, Toaster: () => null }));
vi.mock('@/lib/bulkUploadPreview', () => preview);

const renderPage = () => {
  api.getAdminUsers.mockResolvedValue({
    users: [],
    total: 0,
    skip: 0,
    limit: 51,
  });
  render(
    <AuthContext.Provider
      value={{
        user: {
          regno: 'viewer',
          account_type: 'admin',
          capabilities: [],
          is_blocked: false,
        },
        isAuthenticated: true,
        loading: false,
        can: () => true,
      } as never}
    >
      <MemoryRouter>
        <ProfileReview />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
};

const openDialog = async () => {
  const participants = (await screen.findByRole('heading', { name: 'Participants' })).closest(
    'section',
  ) as HTMLElement;
  fireEvent.click(
    within(participants).getByRole('button', { name: 'Bulk upload participants' }),
  );
  return screen.findByRole('dialog');
};

const chooseFile = (dialog: HTMLElement, file: File) => {
  fireEvent.change(within(dialog).getByLabelText('CSV or XLSX file'), {
    target: { files: [file] },
  });
};

const previewRow = (index: number) => [
  `preview-${index}`,
  `preview-${index}@example.com`,
  `Preview ${index}`,
  'other',
  '2',
  'CSE',
];

describe('ProfileReview: bulk participant upload', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    preview.parseBulkUploadPreview.mockResolvedValue({
      headers: [
        'registration number',
        'email',
        'full name',
        'gender',
        'year',
        'department',
      ],
      rows: [previewRow(1)],
      truncated: false,
      columnsTruncated: false,
    });
  });

  it('uploads the chosen file and reports how many accounts were created', async () => {
    renderPage();
    const dialog = await openDialog();
    const file = new File(['registration number,email\n'], 'participants.csv', {
      type: 'text/csv',
    });
    chooseFile(dialog, file);
    api.bulkUploadAdminUsers.mockResolvedValue({
      created: 2,
      total_users: 2,
      max_user_count: 500,
      remaining_slots: 498,
    });

    fireEvent.click(within(dialog).getByRole('button', { name: 'Upload participants' }));

    await waitFor(() => expect(api.bulkUploadAdminUsers).toHaveBeenCalledWith(file));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        '2 participants uploaded. 498 account slots remaining.',
      ),
    );
  });

  it('keeps a refusal in the dialog so the row-level reasons stay readable', async () => {
    renderPage();
    const dialog = await openDialog();
    chooseFile(dialog, new File(['x'], 'participants.csv', { type: 'text/csv' }));
    api.bulkUploadAdminUsers.mockRejectedValue({
      response: {
        status: 400,
        data: {
          detail:
            'The file was not uploaded because some rows are invalid. ' +
            'row 3 year: Input should be less than or equal to 12.',
        },
      },
    });

    fireEvent.click(within(dialog).getByRole('button', { name: 'Upload participants' }));

    const message = await within(dialog).findByRole('alert');
    expect(message).toHaveTextContent('row 3 year');
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('offers a sample file and previews the selected file without calling the API', async () => {
    renderPage();
    const dialog = await openDialog();
    const file = new File(['registration number,email\n'], 'participants.csv', {
      type: 'text/csv',
    });

    chooseFile(dialog, file);

    await waitFor(() => expect(preview.parseBulkUploadPreview).toHaveBeenCalledWith(file));
    expect(await within(dialog).findByText('Preview 1')).toBeInTheDocument();
    expect(api.bulkUploadAdminUsers).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Download sample CSV' }));
    expect(preview.downloadBulkUploadSample).toHaveBeenCalledTimes(1);
  });

  it('paginates the preview instead of rendering every row at once', async () => {
    preview.parseBulkUploadPreview.mockResolvedValue({
      headers: [
        'registration number',
        'email',
        'full name',
        'gender',
        'year',
        'department',
      ],
      rows: Array.from({ length: 12 }, (_, index) => previewRow(index + 1)),
      truncated: false,
      columnsTruncated: false,
    });
    renderPage();
    const dialog = await openDialog();

    chooseFile(dialog, new File(['x'], 'participants.csv', { type: 'text/csv' }));

    expect(await within(dialog).findByText('Preview 1')).toBeInTheDocument();
    expect(within(dialog).queryByText('Preview 11')).not.toBeInTheDocument();
    expect(within(dialog).getByText('Page 1 of 2')).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Next' }));

    expect(await within(dialog).findByText('Preview 11')).toBeInTheDocument();
    expect(within(dialog).queryByText('Preview 1')).not.toBeInTheDocument();
    expect(within(dialog).getByText('Page 2 of 2')).toBeInTheDocument();
  });
});
