import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthContext } from '@/contexts/AuthContext';
import { StaffNotificationsPage } from '@/pages/StaffNotificationsPage';
import { CAPABILITIES, type Capability, can } from '@/lib/capabilities';

/**
 * The staff notification inbox.
 *
 * One component serves both roles, and the difference is the whole test: a
 * moderator gets two category switches (reports, love notes) while an admin
 * gets three (plus games), and the game alerts never reach a moderator's
 * list. The switches and the master on/off toggle write through to the
 * server but never filter this feed -- every notice stays on screen.
 */

const api = vi.hoisted(() => ({
  getStaffNotifications: vi.fn(),
  markStaffNotificationRead: vi.fn(),
  setStaffNotificationsEnabled: vi.fn(),
  updateStaffNotificationPreferences: vi.fn(),
}));

vi.mock('@/services/api', () => api);
vi.mock('sonner', () => ({
  toast: { info: vi.fn(), error: vi.fn(), success: vi.fn() },
  Toaster: () => null,
}));

const MODERATION_CAPABILITIES = [
  CAPABILITIES.CONFESSIONS_REVIEW,
  CAPABILITIES.LOVE_NOTES_REVIEW,
  CAPABILITIES.REPORTS_REVIEW,
  CAPABILITIES.REPORTS_MODERATE,
];

const ADMIN_CAPABILITIES = [...MODERATION_CAPABILITIES, CAPABILITIES.STATS_READ];

const ALL_ON = { reports_enabled: true, love_notes_enabled: true, games_enabled: true };

const reportNotice = {
  id: 'notice-1',
  audience: 'all',
  category: 'report',
  heading: 'New confession report',
  body: 'A confession was reported. Open Reports review to rule on it.',
  created_at: new Date().toISOString(),
  is_read: false,
  is_live: false,
  target_kind: null,
  target_id: null,
};

const loveNoteNotice = {
  id: 'notice-2',
  audience: 'all',
  category: 'love_note',
  heading: 'Love note awaiting approval',
  body: 'A love note was submitted and is waiting for approval.',
  created_at: new Date().toISOString(),
  is_read: false,
  is_live: false,
  target_kind: null,
  target_id: null,
};

const gameAlert = {
  id: 'game:featured-missing:2026-10-12',
  audience: 'admin',
  category: 'game',
  heading: 'No featured game set for today',
  body: 'No game is featured. Open the Games console to feature one.',
  created_at: new Date().toISOString(),
  is_read: false,
  is_live: true,
  target_kind: null,
  target_id: null,
};

const pageOf = (notifications: unknown[], categories: string[]) => ({
  notifications,
  total: notifications.length,
  skip: 0,
  limit: 20,
  preferences: { ...ALL_ON },
  notifications_enabled: true,
  categories,
});

const renderPage = (accountType: 'admin' | 'moderator', capabilities: string[]) =>
  render(
    <AuthContext.Provider
      value={
        {
          user: {
            id: 'staff-1',
            regno: 'STAFF-1',
            name: 'A Staff Member',
            email: 'staff@example.com',
            account_type: accountType,
            capabilities,
            is_blocked: false,
          },
          isAuthenticated: true,
          isLoading: false,
          logout: vi.fn(),
          can: (capability: Capability) => can(capabilities, capability),
          signInPath: '/login',
        } as never
      }
    >
      <MemoryRouter>
        <StaffNotificationsPage />
      </MemoryRouter>
    </AuthContext.Provider>,
  );

beforeEach(() => {
  vi.clearAllMocks();
  api.getStaffNotifications.mockResolvedValue(pageOf([reportNotice, loveNoteNotice], ['love_note', 'report']));
  api.markStaffNotificationRead.mockResolvedValue({ message: 'Notification marked as read' });
  api.setStaffNotificationsEnabled.mockResolvedValue({ notifications_enabled: false });
  api.updateStaffNotificationPreferences.mockResolvedValue({ preferences: { ...ALL_ON, reports_enabled: false } });
});

describe('StaffNotificationsPage', () => {
  it('shows a moderator two switches and no game switch', async () => {
    renderPage('moderator', MODERATION_CAPABILITIES);

    expect(await screen.findByText('Report notifications')).toBeTruthy();
    expect(screen.getByText('Love-note notifications')).toBeTruthy();
    expect(screen.queryByText('Game notifications')).toBeNull();
    expect(screen.getByText('New confession report')).toBeTruthy();
    expect(screen.getByText('Love note awaiting approval')).toBeTruthy();
  });

  it('shows an admin three switches including games', async () => {
    api.getStaffNotifications.mockResolvedValue(
      pageOf([reportNotice, loveNoteNotice, gameAlert], ['game', 'love_note', 'report']),
    );
    renderPage('admin', ADMIN_CAPABILITIES);

    expect(await screen.findByText('Game notifications')).toBeTruthy();
    expect(screen.getByText('No featured game set for today')).toBeTruthy();
    expect(screen.getByText('Open Games console')).toBeTruthy();
  });

  it('flips the master switch through the enabled endpoint', async () => {
    renderPage('moderator', MODERATION_CAPABILITIES);

    fireEvent.click(await screen.findByText('Enabled'));
    await waitFor(() => expect(api.setStaffNotificationsEnabled).toHaveBeenCalledWith(false));
    expect(await screen.findByText('Disabled')).toBeTruthy();
  });

  it('saves a category switch without hiding the feed', async () => {
    renderPage('moderator', MODERATION_CAPABILITIES);

    // Two "On" buttons: the report and love-note switches. Flip the first.
    const onButtons = await screen.findAllByText('On');
    fireEvent.click(onButtons[0]);
    await waitFor(() =>
      expect(api.updateStaffNotificationPreferences).toHaveBeenCalledWith({ reports_enabled: false }),
    );
    // The notice the switch is about stays on screen regardless.
    expect(screen.getByText('New confession report')).toBeTruthy();
  });

  it('marks a stored notice as read, but never a live game alert', async () => {
    api.getStaffNotifications.mockResolvedValue(
      pageOf([reportNotice, gameAlert], ['game', 'love_note', 'report']),
    );
    renderPage('admin', ADMIN_CAPABILITIES);

    fireEvent.click(await screen.findByText('Mark as read'));
    await waitFor(() => expect(api.markStaffNotificationRead).toHaveBeenCalledWith('notice-1'));
    // The stored notice is now read, so its button is gone; the live game
    // alert never had one.
    await waitFor(() => expect(screen.queryByText('Mark as read')).toBeNull());
  });

  it('filters by category through the server', async () => {
    renderPage('moderator', MODERATION_CAPABILITIES);
    await screen.findByText('New confession report');

    fireEvent.click(screen.getByRole('button', { name: 'Love note' }));
    await waitFor(() =>
      expect(api.getStaffNotifications).toHaveBeenCalledWith(
        expect.objectContaining({ category: 'love_note', skip: 0 }),
      ),
    );
  });
});
