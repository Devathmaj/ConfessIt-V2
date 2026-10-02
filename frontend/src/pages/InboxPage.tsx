// src/pages/InboxPage.tsx

import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '@/contexts/AuthContext';
import { parseTimestamp, formatDateTimeDDMMYYYY, timestampMs } from '@/lib/utils';
import { notificationIsClickable, notificationTargetPath } from '@/lib/notificationTargets';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { FloatingHearts } from '@/components/ui/floating-hearts';
import { getCurrentConversation, acceptConversation, rejectConversation, getNotifications, getReceivedConversations } from '@/services/api';
import {
  Inbox,
  Search,
  RefreshCw,
  Bell,
  Send,
  UserPlus,
  UserCheck,
  UserX,
  Ban,
} from 'lucide-react';

import { toast } from 'sonner';
import { Navigation } from '@/components/Navigation';
import { ProfileAvatar } from '@/components/ProfileAvatar';
import { serverNow } from '@/lib/serverClock';

// Defines the structure for conversation data.
type ConversationStatus = 'pending' | 'requested' | 'accepted' | 'rejected' | 'terminated';

interface ConversationData {
  match: {
    id: string;
    expires_at: string;
    created_at: string;
    is_expired: boolean;
  };
  conversation: {
    id: string;
    status: ConversationStatus;
    initiator_regno: string;
    receiver_regno: string;
    created_at: string;
    accepted_at?: string;
  };
  other_user: {
    regno: string;
    name: string;
    profile_picture_url: string;
    year: number;
    department: string;
    bio: string;
    interests: string[];
  };
  is_initiator: boolean;
}

// Defines the structure for notification items.
interface NotificationItem {
  id: string;
  type: 'matchmaking' | 'message_request_sent' | 'message_request_received' | 'message_accepted' | 'message_rejected' | 'message_terminated';
  title: string;
  body: string;
  created_at: string;
  user_name?: string;
  user_profile_picture?: string;
  match_id?: string;
  conversation_id?: string;
  status?: ConversationStatus;
  // What a click on this card opens. Supplied by the server for the rows it writes, and
  // derived from the match for the conversation cards below, which are not rows at all.
  // Absent on both for a purely informational notice, which is then not clickable.
  target_kind?: string | null;
  target_id?: string | null;
}

/**
 * Headings whose notification rows describe a conversation rather than standing on their own.
 *
 * The row written when a request is sent, accepted or rejected is a frozen copy of a fact the
 * conversation itself already carries, which is why the feed below is derived from
 * conversation state. Termination joins that family: it has to, or the row the server writes
 * for both participants appears here as a second card alongside the derived one.
 */
const CONVERSATION_NOTIFICATION_HEADINGS = [
  'Message request from',
  'accepted your message request',
  'rejected your message request',
  'Conversation terminated by a moderator',
];

/** The card wording, per role, for a conversation a moderator has closed. */
const TERMINATION_TITLE = 'Conversation terminated by a moderator';
const TERMINATION_BODY =
  'This conversation was closed by a moderator. You can still read it, but it can no longer be used.';

/**
 * How many notices one inbox read returns. Rows are small, so twenty keeps one read
 * cheap while a "show more" click still grows the list by a visible amount. The
 * backend clamps anything larger regardless.
 */
const INBOX_PAGE_SIZE = 20;

export const InboxPage = () => {
  const { user } = useAuth();
  const navigate = useNavigate();
  /**
   * The system notices on screen: always the contiguous window [0, length) of the
   * newest-first server order. Derived conversation cards live beside them, complete
   * and unpaged -- a handful of matches, not a table -- so only this window pages.
   */
  const [systemNotices, setSystemNotices] = useState<NotificationItem[]>([]);
  /** How many system notices the server holds, loaded or not. */
  const [systemTotal, setSystemTotal] = useState(0);
  /**
   * How many server rows the window has consumed under the current query, filtered
   * rows included. The conversation family is filtered out client-side after each
   * page lands, so the visible length undershoots what the server has handed over;
   * paging and the remaining count off the visible length would re-read rows the
   * screen already holds -- and stall forever on a tail of only filtered rows.
   */
  const [systemFetched, setSystemFetched] = useState(0);
  /** False while answers arrive as legacy bare arrays (older servers, unit-test mocks). */
  const [pagedInbox, setPagedInbox] = useState(false);
  /** Conversation cards derived from match state: always complete, never paged. */
  const [derivedCards, setDerivedCards] = useState<NotificationItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  /** What the search box currently says. Draft only: nothing is fetched until it is submitted. */
  const [searchTerm, setSearchTerm] = useState('');
  /**
   * The search text the server has actually been asked about.
   *
   * Set only by the Search button (or Enter), never by typing: each keystroke firing
   * the inbox reads would answer a query the reader had already retyped. The derived
   * conversation cards -- complete in memory -- are narrowed by the same submitted
   * text on the client.
   */
  const [appliedSearch, setAppliedSearch] = useState('');

  useEffect(() => {
    fetchData();
    // Runs once, on mount: later questions go through the handlers below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Fetches notifications data. The conversation list lives on the Message Box page now,
  // so this page only needs the notification feed.
  const fetchData = async () => {
    try {
      setLoading(true);
      await fetchNotifications(0, INBOX_PAGE_SIZE, appliedSearch);
    } catch (error) {
      console.error('Failed to fetch data:', error);
    } finally {
      setLoading(false);
    }
  };

  /**
   * Submit the draft search box as the query the inbox is read with, back on the first
   * page: the previous offset may not exist under the new question.
   */
  const submitSearch = () => {
    setAppliedSearch(searchTerm.trim());
    void fetchNotifications(0, INBOX_PAGE_SIZE, searchTerm.trim());
  };

  /** Clear the search box and its submitted question, back on the full first page. */
  const clearSearch = () => {
    setSearchTerm('');
    setAppliedSearch('');
    void fetchNotifications(0, INBOX_PAGE_SIZE, '');
  };

  /**
   * Grow the system list downwards: the next page appends beneath the notices already
   * on screen, so the reader's place never moves. The offset is the count of server
   * rows already consumed, not the visible length: filtered-out rows still occupy
   * their page, and re-reading from the visible length would duplicate them.
   */
  const loadMore = async () => {
    if (loadingMore) return;
    setLoadingMore(true);
    try {
      await fetchNotifications(systemFetched, INBOX_PAGE_SIZE, appliedSearch);
    } catch (error) {
      console.error('Failed to load more notifications:', error);
    } finally {
      setLoadingMore(false);
    }
  };

  /**
   * The rows inside a notifications answer, whether it is the page envelope, an
   * envelope without a total, or a legacy bare array. A bare array means the params
   * went nowhere, so the screen falls back to the single-list behaviour it had before
   * paging existed.
   */
  const splitSystemPage = (answer: unknown) => {
    if (Array.isArray(answer)) {
      const rows = answer as unknown[];
      return { rows, total: rows.length, enveloped: false };
    }
    const rows = (answer as { notifications?: unknown[] } | null | undefined)?.notifications;
    const list = Array.isArray(rows) ? rows : [];
    const total = (answer as { total?: unknown } | null | undefined)?.total;
    return {
      rows: list,
      total: typeof total === 'number' && Number.isFinite(total) ? total : list.length,
      enveloped: true,
    };
  };

  // Fetches notifications from PostgreSQL and derives any conversation alerts.

  const fetchNotifications = async (skip: number, limit: number, query: string) => {
    try {
      const [systemAnswer, conversationResponse, receivedConversationsResponse] = await Promise.all([
        getNotifications({
          skip,
          limit,
          ...(query.trim() ? { q: query.trim() } : {}),
        }),
        getCurrentConversation(),
        getReceivedConversations()
      ]);

      const systemPage = splitSystemPage(systemAnswer);
      const systemRows: NotificationItem[] = [];
      const notifications: NotificationItem[] = [];

      // 1. System notifications (match found, love notes, etc.)
      if (systemPage.rows) {
        systemPage.rows.forEach((notification: any) => {
          // A conversation is represented by the conversation itself, which carries a single
          // status that advances requested -> accepted/rejected and can then be terminated by
          // a moderator. The row written to the notifications table when that happens is a
          // second, frozen copy of the same fact: it can never show the current outcome, and
          // rendering it alongside the derived one is what put two cards for one conversation
          // in the inbox. So the whole conversation family is filtered out here and the alerts
          // below are derived from conversation state instead -- which is also how a
          // terminated conversation keeps a card at all, since both of these reads used to
          // drop it.
          const isConversationNotification = CONVERSATION_NOTIFICATION_HEADINGS.some(
            heading => notification.heading?.includes(heading)
          );

          if (!isConversationNotification) {
            systemRows.push({
              id: notification.id,
              type: 'matchmaking',
              title: notification.heading,
              body: notification.body,
              created_at: notification.created_at,
              // The nulls here are the server's answer, not a placeholder: they mean this
              // notice has no item to open. They are passed through rather than defaulted,
              // because a default would invent clickability the server did not offer.
              target_kind: notification.target_kind ?? null,
              target_id: notification.target_id ?? null,
            });
          }
        });
      }

      // 2. For INITIATOR: Only show accepted/rejected/terminated notification
      if (conversationResponse.status === 'success' && conversationResponse.is_initiator) {
        if (conversationResponse.conversation.status === 'accepted' && conversationResponse.conversation.accepted_at) {
          notifications.push({
            id: `${conversationResponse.conversation.id}_accepted`,
            type: 'message_accepted',
            title: `${conversationResponse.other_user.name} accepted your message request`,
            body: `${conversationResponse.other_user.name} accepted your message request. You can now start chatting!`,
            created_at: conversationResponse.conversation.accepted_at,
            user_name: conversationResponse.other_user.name,
            user_profile_picture: conversationResponse.other_user.profile_picture_url,
            match_id: conversationResponse.match.id,
            conversation_id: conversationResponse.conversation.id,
            status: 'accepted',
            target_kind: 'conversation',
            target_id: conversationResponse.match.id,
          });
        } else if (conversationResponse.conversation.status === 'rejected') {
          notifications.push({
            id: `${conversationResponse.conversation.id}_rejected`,
            type: 'message_rejected',
            title: `${conversationResponse.other_user.name} rejected your message request`,
            body: `${conversationResponse.other_user.name} rejected your message request.`,
            created_at: conversationResponse.conversation.created_at,
            user_name: conversationResponse.other_user.name,
            user_profile_picture: conversationResponse.other_user.profile_picture_url,
            match_id: conversationResponse.match.id,
            conversation_id: conversationResponse.conversation.id,
            status: 'rejected',
            target_kind: 'conversation',
            target_id: conversationResponse.match.id,
          });
        } else if (conversationResponse.conversation.status === 'terminated') {
          // A moderator closing the thread is news for the initiator too, and the thread is
          // still theirs to read, so it gets the same card as any other outcome -- pointing
          // at a read-only conversation rather than at nothing.
          notifications.push({
            id: `${conversationResponse.conversation.id}_terminated`,
            type: 'message_terminated',
            title: TERMINATION_TITLE,
            body: TERMINATION_BODY,
            created_at: conversationResponse.conversation.created_at,
            user_name: conversationResponse.other_user.name,
            user_profile_picture: conversationResponse.other_user.profile_picture_url,
            match_id: conversationResponse.match.id,
            conversation_id: conversationResponse.conversation.id,
            status: 'terminated',
            target_kind: 'conversation',
            target_id: conversationResponse.match.id,
          });
        }
      }

      // 3. For RECEIVER: one notification per request, whatever the current status.
      if (receivedConversationsResponse.status === 'success' && receivedConversationsResponse.conversations) {
        receivedConversationsResponse.conversations.forEach((conv: any) => {
          // Same conversation fields in every state, so the card is the same card and only
          // the part describing the outcome changes.
          // Every one of these cards points at the same match, so the target is set once here
          // rather than repeated in each branch -- including the rejected one. A rejected
          // request is still the thread the reader asked about, and the Message Box shows it
          // read-only, so going there answers the card better than a dead card would.
          const base = {
            user_name: conv.other_user.name,
            user_profile_picture: conv.other_user.profile_picture_url,
            match_id: conv.match.id,
            conversation_id: conv.conversation.id,
            target_kind: 'conversation',
            target_id: conv.match.id,
          };

          if (conv.conversation.status === 'requested') {
            // Still open, so this is the one that carries the accept/reject actions.
            notifications.push({
              ...base,
              id: conv.conversation.id,
              type: 'message_request_received',
              title: `Message request from ${conv.other_user.name}`,
              body: `${conv.other_user.name} wants to start a conversation with you!`,
              created_at: conv.conversation.created_at,
              status: 'requested',
            });
          } else if (conv.conversation.status === 'accepted') {
            // The receiver acted, so the outcome is the receiver's own action, not the
            // sender's. Timestamped when they accepted, which is when this became news.
            notifications.push({
              ...base,
              id: `${conv.conversation.id}_accepted`,
              type: 'message_accepted',
              title: `You accepted ${conv.other_user.name}'s message request`,
              body: `You can now start chatting with ${conv.other_user.name}.`,
              created_at: conv.conversation.accepted_at || conv.conversation.created_at,
              status: 'accepted',
            });
          } else if (conv.conversation.status === 'rejected') {
            notifications.push({
              ...base,
              id: `${conv.conversation.id}_rejected`,
              type: 'message_rejected',
              title: `You rejected ${conv.other_user.name}'s message request`,
              body: `You rejected the message request from ${conv.other_user.name}.`,
              created_at: conv.conversation.created_at,
              status: 'rejected',
            });
          } else if (conv.conversation.status === 'terminated') {
            // Same for a moderator-closed request. Without this the receiver's card
            // disappeared from the inbox at the moment it was terminated.
            notifications.push({
              ...base,
              id: `${conv.conversation.id}_terminated`,
              type: 'message_terminated',
              title: TERMINATION_TITLE,
              body: TERMINATION_BODY,
              created_at: conv.conversation.created_at,
              status: 'terminated',
            });
          }
          // 'pending' is not a request yet, so it produces no notification.
        });
      }

      // A first page replaces the window; an appended page grows it. Either way the
      // derived cards are rebuilt whole -- a handful of matches, always complete.
      // The consumed count is the raw page size, not the survivors: the next read
      // starts past every row this one touched, filtered or not.
      if (skip === 0) {
        setSystemNotices(systemRows);
      } else {
        setSystemNotices((prev) => [...prev, ...systemRows]);
      }
      setSystemFetched(skip + systemPage.rows.length);
      setSystemTotal(systemPage.total);
      setPagedInbox(systemPage.enveloped);
      setDerivedCards(notifications);
    } catch (error: any) {
      console.error('Failed to fetch notifications:', error);
      if (skip === 0) {
        setSystemNotices([]);
        setDerivedCards([]);
      }
    }
  };

  // Returns an icon component based on the notification type.
  const getNotificationIcon = (type: string) => {
    switch (type) {
      case 'message_request_sent':
        return <Send className="w-5 h-5 text-blue-500" />;
      case 'message_request_received':
        return <UserPlus className="w-5 h-5 text-green-500" />;
      case 'message_accepted':
        return <UserCheck className="w-5 h-5 text-green-500" />;
case 'message_rejected':
      return <UserX className="w-5 h-5 text-red-500" />;
    case 'message_terminated':
      return <Ban className="w-5 h-5 text-gray-500" />;
      default:
        return <Bell className="w-5 h-5 text-gray-500" />;
    }
  };

  // Converts a timestamp to a human-readable "time ago" format.
  const getTimeAgo = (created_at: string) => {
    const parsed = parseTimestamp(created_at);
    if (!parsed) return '—';
    const timeUTC = parsed.getTime();
    const nowUTC = serverNow();
    
    const diffInMinutes = Math.floor((nowUTC - timeUTC) / (1000 * 60));
    
    if (diffInMinutes < 1) return 'Just now';
    if (diffInMinutes < 60) return `${diffInMinutes}m ago`;
    if (diffInMinutes < 1440) return `${Math.floor(diffInMinutes / 60)}h ago`;
    return `${Math.floor(diffInMinutes / 1440)}d ago`;
  };

// Filters notifications based on the submitted search query.
//
// Against the server the system window already arrives narrowed, so only the derived
// cards -- complete in memory -- are narrowed here. Against legacy bare-array answers
// the params went nowhere, so the loaded system window is narrowed here too, which is
// also what keeps narrowing immediate while typing there.
const matchesQuery = (notification: NotificationItem, query: string) => {
  if (!query) return true; // Show all if no search query

  // The sender's name is searched once. It used to be tested twice, in both halves of an
  // `a || a`, which meant a note whose sender is anonymous fell through to matching on
  // title and body alone -- fine, except that the duplicate also made it look like the name
  // was being matched with different logic on the two sides.
  return (
    (notification.user_name?.toLowerCase().includes(query) ?? false) ||
    notification.title.toLowerCase().includes(query) ||
    notification.body.toLowerCase().includes(query)
  );
};

/** The search text currently narrowing the list: submitted against the server, live
 * against legacy answers whose params go nowhere. */
const activeSearch = (pagedInbox ? appliedSearch : searchTerm).trim().toLowerCase();

const filteredNotifications = [
  ...derivedCards.filter((notification) => matchesQuery(notification, activeSearch)),
  ...(pagedInbox
    ? systemNotices
    : systemNotices.filter((notification) => matchesQuery(notification, activeSearch))),
].sort((a, b) => timestampMs(b.created_at) - timestampMs(a.created_at));

/** Whether the submitted query is narrowing the list, which is what the clear button is for. */
const filtersNarrowed = activeSearch !== '';

  // Handles clicking on a notification by navigating to the item it is about.
  const handleNotificationClick = (notification: NotificationItem) => {
    const path = notificationTargetPath(notification.target_kind, notification.target_id);
    // No target means there is nothing to open -- a match that was drawn, a report that was
    // reviewed, content that was taken down. The card is styled and announced as inert for
    // the same reason, so this branch should be unreachable from a real click; the guard is
    // here so a card can never navigate somewhere meaningless.
    if (!path) return;
    navigate(path);
  };

  // Refreshes all data on the page.
  const handleRefreshData = () => {
    fetchData();
  };

  if (loading) {
    return (
      <div className="min-h-screen p-4 pt-24 relative overflow-hidden">
        <Navigation />
        <FloatingHearts />
        <div className="max-w-7xl mx-auto">
          <div className="flex items-center justify-center h-64">
            <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-primary"></div>
            <span className="ml-3 text-lg">Loading inbox...</span>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen p-4 pt-24 relative overflow-hidden">
      <Navigation />
      <FloatingHearts />
      
      <div className="max-w-7xl mx-auto mb-8">
        <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-6">
          <div className="text-center lg:text-left">
            <h1 className="text-2xl sm:text-3xl md:text-4xl lg:text-5xl font-dancing page-heading mb-2 break-words">
              Notifications 💌
            </h1>
            <p className="text-sm sm:text-base md:text-xl status-hint break-words">
              Manage your notifications and message requests
            </p>
          </div>
          <div className="flex items-center space-x-2">
            <Button variant="outline" onClick={handleRefreshData}>
              <RefreshCw className="w-4 h-4 mr-2" />
              Refresh
            </Button>
          </div>
        </div>
      </div>

      <div className="max-w-7xl mx-auto">
        {/* Search Bar: the box is a draft and Search submits it, so typing alone never
            refetches the inbox for a query the reader had already retyped. */}
        <div className="mb-6 flex flex-col sm:flex-row gap-4">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 text-muted-foreground w-4 h-4" />
            <input
              type="text"
              placeholder="Search notifications..."
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  submitSearch();
                }
              }}
              className="w-full pl-10 pr-4 py-2 border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-pink-500 focus:border-pink-500"
            />
          </div>
          <div className="flex gap-2">
            <Button variant="secondary" onClick={submitSearch} aria-label="Search notifications">
              <Search className="w-4 h-4 mr-2" />
              Search
            </Button>
            {filtersNarrowed && (
              <Button variant="ghost" size="sm" onClick={clearSearch}>
                Clear
              </Button>
            )}
          </div>
        </div>

        {/* Content */}
        {filteredNotifications.length === 0 ? (
          <div className="text-center py-16">
            <Inbox className="w-16 h-16 text-muted-foreground mx-auto mb-4" />
            <h3 className="text-xl font-semibold text-foreground mb-2">No notifications found</h3>
            <p className="text-muted-foreground">
              {filtersNarrowed
                ? "No notifications match your search."
                : "You don't have any notifications yet."
              }
            </p>
            {filtersNarrowed && (
              <Button variant="outline" size="sm" className="mt-4" onClick={clearSearch}>
                Show all notifications
              </Button>
            )}
          </div>
        ) : (
          <div className="grid gap-4">
            {filteredNotifications
              .map((notification) => {
                const isClickable = notificationIsClickable(
                  notification.target_kind,
                  notification.target_id
                );
                const targetPath = notificationTargetPath(
                  notification.target_kind,
                  notification.target_id
                );

                return (
              <Card 
                key={notification.id}
                className={`transition-shadow duration-200 ${
                  isClickable ? 'cursor-pointer hover:shadow-lg hover:border-pink-400 focus-visible:ring-2 focus-visible:ring-pink-500' : ''
                }`}
                // Keyboard-reachable for the targeted ones. A non-target card stays out of
                // the tab order entirely, because there is no action to perform -- an inert
                // card that still takes focus is worse than one that is visibly not a control.
                role={isClickable ? 'button' : undefined}
                tabIndex={isClickable ? 0 : undefined}
                aria-label={isClickable && targetPath ? `Open ${notification.title}` : undefined}
                onClick={() => handleNotificationClick(notification)}
                onKeyDown={(e) => {
                  if (!isClickable) return;
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    handleNotificationClick(notification);
                  }
                }}
              >
                <CardContent className="p-6">
                  <div className="flex items-start space-x-4">
                    <div className="flex-shrink-0">
                      {getNotificationIcon(notification.type)}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-start justify-between mb-2">
                        <div className="flex items-center space-x-3">
                          {notification.user_profile_picture || notification.user_name ? (
                            <ProfileAvatar
                              src={notification.user_profile_picture}
                              name={notification.user_name}
                              className="w-12 h-12 border-2 border-pink-500"
                            />
                          ) : (
                            <div className="w-12 h-12 rounded-full bg-pink-500 flex items-center justify-center border-2 border-pink-300">
                              <Bell className="w-6 h-6 text-white" />
                            </div>
                          )}
                          <div>
                            <h3 className="text-lg font-semibold text-foreground">
                              {notification.title}
                            </h3>
                            <p className="text-sm text-muted-foreground">{notification.user_name || 'System Notification'}</p>
                          </div>
                        </div>
                        <div className="text-sm text-muted-foreground">
                          {/* The exact moment is rendered here rather than baked into the
                              notification body by the server, which has no way to know the
                              reader's timezone. */}
                          <span title={formatDateTimeDDMMYYYY(notification.created_at)}>
                            {getTimeAgo(notification.created_at)}
                          </span>
                        </div>
                      </div>
                      
                      <p className="text-sm text-muted-foreground mb-3">
                        {notification.body}
                      </p>
                      
                      <div className="flex items-center justify-between">
                        <div className="flex items-center space-x-2">
                          {notification.status && (
                            <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                              notification.status === 'pending' ? 'bg-yellow-100 text-yellow-800' :
                              notification.status === 'requested' ? 'bg-yellow-100 text-yellow-800' :
                              notification.status === 'accepted' ? 'bg-green-100 text-green-800' :
                              notification.status === 'terminated' ? 'bg-gray-200 text-gray-800' :
                              'bg-red-100 text-red-800'
                            }`}>
                              {notification.status === 'pending' ? 'Pending' :
                               notification.status === 'requested' ? 'Pending' :
                               notification.status === 'accepted' ? 'Accepted' :
                               notification.status === 'terminated' ? 'Terminated' : 'Rejected'}
                            </span>
                          )}
                        </div>
                        <div className="flex items-center space-x-2">
                          {/* Show Accept/Reject buttons for message_request_received with requested status */}
                          {notification.type === 'message_request_received' && notification.status === 'requested' && notification.match_id ? (
                            <>
                              <Button 
                                variant="default" 
                                size="sm" 
                                onClick={async (e) => {
                                  e.stopPropagation();
                                  try {
                                    await acceptConversation(notification.match_id!);
                                    toast.success('Conversation accepted!');
                                    fetchData();
                                  } catch (error: any) {
                                    toast.error('Failed to accept conversation');
                                  }
                                }}
                                className="text-xs bg-green-600 hover:bg-green-700"
                              >
                                <UserCheck className="w-3 h-3 mr-1" />
                                Accept
                              </Button>
                              <Button 
                                variant="outline" 
                                size="sm" 
                                onClick={async (e) => {
                                  e.stopPropagation();
                                  try {
                                    await rejectConversation(notification.match_id!);
                                    toast.success('Conversation rejected');
                                    fetchData();
                                  } catch (error: any) {
                                    toast.error('Failed to reject conversation');
                                  }
                                }}
                                className="text-xs"
                              >
                                <UserX className="w-3 h-3 mr-1" />
                                Reject
                              </Button>
                            </>
                          ) : notification.status === 'rejected' ? (
                            <div className="text-xs text-red-600">
                              Conversation rejected
                            </div>
                          ) : notification.status === 'terminated' ? (
                            <div className="text-xs text-gray-600">
                              Conversation terminated
                            </div>
                          ) : notification.status === 'accepted' ? (
                            // The status badge above already reads "Accepted", and for
                            // a derived conversation card the title states who accepted,
                            // so a label here would only misdescribe it.
                            null
                          ) : (
                            <div className="text-xs text-muted-foreground">
                              System notification
                            </div>
                          )}
                        </div>
                      </div>
                    </div>
                  </div>
</CardContent>
              </Card>
                );
              })}
          </div>
        )}
        {/* The list only grows downwards: the next page appends beneath the notices
            already on screen, so the reader's place never moves. Shown only while the
            server holds rows the list has not consumed yet; the count is consumed rows,
            not visible cards, because a tail of filtered-out conversation rows would
            otherwise hold the button up forever with nothing new to show. The derived
            conversation cards are always complete, and legacy answers never page. */}
        {pagedInbox && systemFetched < systemTotal && (
          <div className="flex justify-center pt-6">
            <Button
              variant="outline"
              size="sm"
              onClick={() => void loadMore()}
              disabled={loadingMore}
              aria-label="Show more notifications"
            >
              {loadingMore
                ? 'Loading…'
                : `Show more (${systemTotal - systemFetched} remaining)`}
            </Button>
          </div>
        )}
      </div>
    </div>
  );
};

export default InboxPage;
