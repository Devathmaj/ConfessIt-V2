// src/pages/StaffNotificationsPage.tsx

import { logger } from '@/lib/logger';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { useAuth } from '@/contexts/AuthContext';
import { AdminNavigation } from '@/components/AdminNavigation';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { formatDateTimeDDMMYYYY, parseTimestamp } from '@/lib/utils';
import { serverNow } from '@/lib/serverClock';
import {
  getStaffNotifications,
  markStaffNotificationRead,
  setStaffNotificationsEnabled,
  updateStaffNotificationPreferences,
  type StaffNotification,
  type StaffNotificationPreferences,
} from '@/services/api';

/**
 * The role-based notification inbox for staff.
 *
 * One component serves both roles: an admin sees `report`, `love_note` and
 * `game` notices with three category switches, while a moderator sees only
 * `report` and `love_note` with two. The switches and the master on/off
 * toggle are stored per account but deliberately do not filter this feed --
 * every notice is shown regardless, and the switches record intent for a
 * future delivery layer.
 *
 * Admin-facing, so plain cards and no decorative artwork.
 */

const PAGE_SIZE = 20;

const CATEGORY_LABELS: Record<string, string> = {
  report: 'Report',
  love_note: 'Love note',
  game: 'Game',
};

const getTimeAgo = (createdAt: string): string => {
  const parsed = parseTimestamp(createdAt);
  if (!parsed) return '—';
  const diffMinutes = Math.floor((serverNow() - parsed.getTime()) / (1000 * 60));
  if (diffMinutes < 1) return 'Just now';
  if (diffMinutes < 60) return `${diffMinutes}m ago`;
  if (diffMinutes < 1440) return `${Math.floor(diffMinutes / 60)}h ago`;
  return `${Math.floor(diffMinutes / 1440)}d ago`;
};

export const StaffNotificationsPage = () => {
  const { user } = useAuth();
  const navigate = useNavigate();
  const isAdmin = user?.account_type === 'admin';

  const [notifications, setNotifications] = useState<StaffNotification[]>([]);
  const [total, setTotal] = useState(0);
  const [fetched, setFetched] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [search, setSearch] = useState('');
  const [appliedSearch, setAppliedSearch] = useState('');
  const [category, setCategory] = useState<string>('all');
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [preferences, setPreferences] = useState<StaffNotificationPreferences | null>(null);
  const [notificationsEnabled, setNotificationsEnabled] = useState(true);
  const [categories, setCategories] = useState<string[]>([]);
  const [savingSwitch, setSavingSwitch] = useState<string | null>(null);
  /** Bumped by the Refresh button: the only fetch the filters do not already trigger. */
  const [refreshKey, setRefreshKey] = useState(0);

  /** Submit the draft search box: the effect re-reads from the first page. */
  const submitSearch = () => {
    setAppliedSearch(search.trim());
  };

  const readPage = async (skip: number) => {
    const page = await getStaffNotifications({
      skip,
      limit: PAGE_SIZE,
      ...(appliedSearch.trim() ? { q: appliedSearch.trim() } : {}),
      ...(category !== 'all' ? { category } : {}),
      ...(unreadOnly ? { unread_only: true } : {}),
    });
    setNotifications((prev) => (skip === 0 ? page.notifications : [...prev, ...page.notifications]));
    setTotal(page.total);
    setFetched(skip + page.notifications.length);
    setPreferences(page.preferences);
    setNotificationsEnabled(page.notifications_enabled);
    setCategories(page.categories);
  };

  // Every read re-runs when the question changes, because the filter, the
  // search and the unread switch are the server's to apply: a client-side
  // filter over the first page would show "no reports" while reports exist
  // on page two.
  useEffect(() => {
    let active = true;
    const load = async () => {
      setLoading(true);
      try {
        await readPage(0);
      } catch (error) {
        if (!active) return;
        logger.error('Failed to load staff notifications', error);
        toast.error('Unable to load notifications right now.');
      } finally {
        if (active) setLoading(false);
      }
    };

    load();
    return () => {
      active = false;
    };
    // `readPage` reads the three filter states, so they -- and the manual
    // refresh counter -- are the re-read triggers. Declared outside and read
    // here rather than passed as a callback, so the effect owns the fetch the
    // way the report queues do.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appliedSearch, category, unreadOnly, refreshKey]);

  const handleToggleEnabled = async () => {
    const next = !notificationsEnabled;
    setSavingSwitch('enabled');
    try {
      const result = await setStaffNotificationsEnabled(next);
      setNotificationsEnabled(result.notifications_enabled);
      toast.success(
        result.notifications_enabled ? 'Notifications enabled.' : 'Notifications disabled.',
      );
    } catch (error) {
      logger.error('Failed to flip the notification switch', error);
      toast.error('Could not update the notification setting.');
    } finally {
      setSavingSwitch(null);
    }
  };

  const handleTogglePreference = async (key: keyof StaffNotificationPreferences) => {
    if (!preferences) return;
    // Moderators have no game notices, so there is no game switch to flip.
    if (!isAdmin && key === 'games_enabled') return;
    setSavingSwitch(key);
    try {
      const result = await updateStaffNotificationPreferences({ [key]: !preferences[key] });
      setPreferences(result.preferences);
      toast.success('Preference saved.');
    } catch (error) {
      logger.error('Failed to save notification preference', error);
      toast.error('Could not save that preference.');
    } finally {
      setSavingSwitch(null);
    }
  };

  const handleMarkRead = async (notice: StaffNotification) => {
    if (notice.is_read) return;
    try {
      await markStaffNotificationRead(notice.id);
      setNotifications((prev) => prev.map((n) => (n.id === notice.id ? { ...n, is_read: true } : n)));
    } catch (error) {
      logger.error('Failed to mark notification as read', error);
      toast.error('Could not mark that notification as read.');
    }
  };

  const openTarget = (notice: StaffNotification) => {
    // Stored rows carry no target; the queue behind each category is the
    // destination. Game notices open the games console.
    if (notice.category === 'game') {
      navigate('/admin/games');
    } else if (notice.category === 'love_note') {
      navigate('/admin/love-notes');
    } else {
      navigate('/admin/reports');
    }
  };

  const preferenceRows: Array<{ key: keyof StaffNotificationPreferences; label: string; hint: string }> = [
    { key: 'reports_enabled', label: 'Report notifications', hint: 'A report is filed against a post, note or message.' },
    { key: 'love_notes_enabled', label: 'Love-note notifications', hint: 'A love note arrives awaiting approval.' },
    // Admin-only: moderators never receive game notices.
    ...(isAdmin
      ? [{ key: 'games_enabled' as const, label: 'Game notifications', hint: 'A game needs feeding, or no game is featured today.' }]
      : []),
  ];

  const unreadCount = notifications.filter((n) => !n.is_read).length;

  return (
    <div className="relative min-h-screen bg-background">
      <AdminNavigation />

      <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 px-4 pb-16 pt-28">
        <header className="flex flex-col gap-2">
          <p className="text-xs uppercase tracking-wider text-muted-foreground">
            {isAdmin ? 'Admin' : 'Moderation'} · Notifications
          </p>
          <h1 className="text-3xl font-semibold">Notifications</h1>
          <p className="text-sm text-muted-foreground">
            {isAdmin
              ? 'Reports, love notes awaiting approval, and games that need your hand.'
              : 'Reports filed by participants, and love notes awaiting approval.'}
            {unreadCount > 0 && ` ${unreadCount} unread on this page.`}
          </p>
        </header>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">Notification settings</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center justify-between gap-4">
              <div>
                <p className="text-sm font-medium">Enable notifications</p>
                <p className="text-xs text-muted-foreground">
                  Your master switch. Turning it off records your choice; this page still lists everything.
                </p>
              </div>
              <Button
                type="button"
                variant={notificationsEnabled ? 'default' : 'outline'}
                size="sm"
                disabled={savingSwitch === 'enabled'}
                onClick={() => void handleToggleEnabled()}
                aria-pressed={notificationsEnabled}
              >
                {savingSwitch === 'enabled' ? 'Saving…' : notificationsEnabled ? 'Enabled' : 'Disabled'}
              </Button>
            </div>

            <div className="space-y-3 border-t pt-4">
              <p className="text-sm font-medium">Which notices do you want delivered?</p>
              <p className="text-xs text-muted-foreground">
                Stored for a future delivery layer. The list below shows every notice regardless of these switches.
              </p>
              {preferenceRows.map((row) => (
                <div key={row.key} className="flex items-center justify-between gap-4">
                  <div>
                    <p className="text-sm">{row.label}</p>
                    <p className="text-xs text-muted-foreground">{row.hint}</p>
                  </div>
                  <Button
                    type="button"
                    variant={preferences?.[row.key] ? 'default' : 'outline'}
                    size="sm"
                    disabled={savingSwitch === row.key || preferences === null}
                    onClick={() => void handleTogglePreference(row.key)}
                    aria-pressed={preferences?.[row.key] ?? true}
                  >
                    {savingSwitch === row.key ? 'Saving…' : preferences?.[row.key] ? 'On' : 'Off'}
                  </Button>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>

        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-2 sm:flex-row">
            <input
              type="text"
              placeholder="Search notifications..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  submitSearch();
                }
              }}
              className="w-full flex-1 rounded-lg border border-border px-4 py-2 focus:outline-none focus:ring-2 focus:ring-pink-500"
            />
            <div className="flex gap-2">
              <Button variant="secondary" size="sm" onClick={submitSearch}>
                Search
              </Button>
              {appliedSearch && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setSearch('');
                    setAppliedSearch('');
                  }}
                >
                  Clear
                </Button>
              )}
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant={category === 'all' ? 'default' : 'outline'}
              size="sm"
              onClick={() => setCategory('all')}
            >
              All
            </Button>
            {(categories.length > 0 ? categories : isAdmin ? ['report', 'love_note', 'game'] : ['report', 'love_note']).map(
              (cat) => (
                <Button
                  key={cat}
                  type="button"
                  variant={category === cat ? 'default' : 'outline'}
                  size="sm"
                  onClick={() => setCategory(cat)}
                >
                  {CATEGORY_LABELS[cat] ?? cat}
                </Button>
              ),
            )}
            <Button
              type="button"
              variant={unreadOnly ? 'default' : 'outline'}
              size="sm"
              onClick={() => setUnreadOnly((prev) => !prev)}
              aria-pressed={unreadOnly}
            >
              Unread only
            </Button>
            <Button type="button" variant="ghost" size="sm" onClick={() => setRefreshKey((key) => key + 1)}>
              Refresh
            </Button>
          </div>
        </div>

        {loading ? (
          <div className="flex items-center justify-center py-16">
            <div className="h-10 w-10 animate-spin rounded-full border-b-2 border-primary" />
            <span className="ml-3">Loading notifications...</span>
          </div>
        ) : notifications.length === 0 ? (
          <Card>
            <CardContent className="py-16 text-center">
              <p className="text-lg font-semibold">No notifications</p>
              <p className="mt-1 text-sm text-muted-foreground">
                {appliedSearch || category !== 'all' || unreadOnly
                  ? 'Nothing matches these filters.'
                  : 'There is nothing waiting on you right now.'}
              </p>
            </CardContent>
          </Card>
        ) : (
          <div className="grid gap-3">
            {notifications.map((notice) => (
              <Card key={notice.id} className={notice.is_read ? 'opacity-80' : ''}>
                <CardContent className="p-5">
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge variant="secondary">{CATEGORY_LABELS[notice.category] ?? notice.category}</Badge>
                      {!notice.is_read && <Badge>Unread</Badge>}
                    </div>
                    <span
                      className="shrink-0 text-xs text-muted-foreground"
                      title={formatDateTimeDDMMYYYY(notice.created_at)}
                    >
                      {getTimeAgo(notice.created_at)}
                    </span>
                  </div>
                  <h2 className="mt-2 text-base font-semibold">{notice.heading}</h2>
                  <p className="mt-1 text-sm text-muted-foreground">{notice.body}</p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Button type="button" variant="outline" size="sm" onClick={() => openTarget(notice)}>
                      {notice.category === 'game'
                        ? 'Open Games console'
                        : notice.category === 'love_note'
                          ? 'Open Love Notes review'
                          : 'Open Reports review'}
                    </Button>
                    {!notice.is_read && (
                      <Button type="button" variant="ghost" size="sm" onClick={() => void handleMarkRead(notice)}>
                        Mark as read
                      </Button>
                    )}
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        )}

        {!loading && fetched < total && (
          <div className="flex justify-center">
            <Button
              variant="outline"
              size="sm"
              disabled={loadingMore}
              onClick={() => {
                setLoadingMore(true);
                readPage(fetched)
                  .catch((error) => {
                    logger.error('Failed to load more notifications', error);
                    toast.error('Could not load more notifications.');
                  })
                  .finally(() => setLoadingMore(false));
              }}
            >
              {loadingMore ? 'Loading…' : `Show more (${total - fetched} remaining)`}
            </Button>
          </div>
        )}
      </div>
    </div>
  );
};

export default StaffNotificationsPage;
