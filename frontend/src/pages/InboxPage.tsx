// src/pages/InboxPage.tsx

import { useState, useEffect } from 'react';
import { useAuth } from '@/contexts/AuthContext';
import { parseTimestamp, resolveProfilePictureUrl, formatDateTimeDDMMYYYY, timestampMs } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { FloatingHearts } from '@/components/ui/floating-hearts';
import { getCurrentConversation, acceptConversation, rejectConversation, getNotifications, getReceivedConversations, getConversationByMatch } from '@/services/api';
import { ConversationDialog } from '@/components/ConversationDialog';
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
    class_name: string;
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

export const InboxPage = () => {
  const { user } = useAuth();
  const [notifications, setNotifications] = useState<NotificationItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedConversation, setSelectedConversation] = useState<ConversationData | null>(null);
  const [showConversationDialog, setShowConversationDialog] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');

  useEffect(() => {
    fetchData();
  }, []);

  // Fetches notifications data. The conversation list lives on the Message Box page now,
  // so this page only needs the notification feed.
  const fetchData = async () => {
    try {
      setLoading(true);
      await fetchNotifications();
    } catch (error) {
      console.error('Failed to fetch data:', error);
    } finally {
      setLoading(false);
    }
  };

  // Fetches notifications from PostgreSQL and derives any conversation alerts.

  const fetchNotifications = async () => {
    try {
      const [notificationsResponse, conversationResponse, receivedConversationsResponse] = await Promise.all([
        getNotifications(),
        getCurrentConversation(),
        getReceivedConversations()
      ]);

      const notifications: NotificationItem[] = [];

      // 1. System notifications (match found, love notes, etc.)
      if (notificationsResponse.notifications) {
        notificationsResponse.notifications.forEach((notification: any) => {
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
            notifications.push({
              id: notification.id,
              type: 'matchmaking',
              title: notification.heading,
              body: notification.body,
              created_at: notification.created_at,
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
            status: 'accepted'
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
            status: 'rejected'
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
            status: 'terminated'
          });
        }
      }

      // 3. For RECEIVER: one notification per request, whatever the current status.
      if (receivedConversationsResponse.status === 'success' && receivedConversationsResponse.conversations) {
        receivedConversationsResponse.conversations.forEach((conv: any) => {
          // Same conversation fields in every state, so the card is the same card and only
          // the part describing the outcome changes.
          const base = {
            user_name: conv.other_user.name,
            user_profile_picture: conv.other_user.profile_picture_url,
            match_id: conv.match.id,
            conversation_id: conv.conversation.id,
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

      setNotifications(notifications);
    } catch (error: any) {
      console.error('Failed to fetch notifications:', error);
      setNotifications([]);
    }
  };

  // Constructs the full URL for a user's profile picture.
  const getProfilePictureUrl = (profilePictureId?: string | null) => resolveProfilePictureUrl(profilePictureId ?? null);

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

  // Filters notifications based on the current search query.

  const filteredNotifications = notifications.filter(notification => {
    if (!searchQuery.trim()) return true; // Show all if no search query
    
    const matchesSearch = (notification.user_name?.toLowerCase().includes(searchQuery.toLowerCase()) ||
                         notification.user_name?.toLowerCase().includes(searchQuery.toLowerCase())) ||
                         notification.title?.toLowerCase().includes(searchQuery.toLowerCase()) ||
                         notification.body?.toLowerCase().includes(searchQuery.toLowerCase());
    return matchesSearch;
  });

  // Handles clicking on a notification, opening the corresponding conversation.
  const handleNotificationClick = async (notification: NotificationItem) => {
    // Only allow clicking on notifications that have conversation data and are not rejected
    if (notification.match_id && notification.conversation_id && notification.status !== 'rejected') {
      console.log('🔔 Notification clicked:', {
        match_id: notification.match_id,
        notification_status: notification.status
      });
      
      // ALWAYS fetch fresh data from API
      try {
        console.log('📡 Fetching conversation from API...');
        const response = await getConversationByMatch(notification.match_id);
        console.log('📡 API Response:', response);
        
        if (response.status === 'success') {
          const conversation = response as ConversationData;
          console.log('✅ Conversation fetched:', {
            conversation_id: conversation.conversation.id,
            status: conversation.conversation.status
          });
          
          setSelectedConversation(conversation);
          setShowConversationDialog(true);
        } else {
          toast.error('Failed to load conversation');
        }
      } catch (error) {
        console.error('❌ Failed to fetch conversation:', error);
        toast.error('Failed to load conversation');
      }
    }
    // System notifications and rejected conversations are not clickable
  };

  // Refreshes all data on the page.
  const handleRefreshData = () => {
    fetchData();
  };

  // Refreshes data after a conversation has been accepted.
  const handleConversationAccepted = () => {
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
            <h1 className="text-2xl sm:text-3xl md:text-4xl lg:text-5xl font-dancing text-pink-600 dark:text-pink-400 mb-2 break-words">
              Notifications 💌
            </h1>
            <p className="text-sm sm:text-base md:text-xl text-muted-foreground break-words">
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
        {/* Search Bar */}
        <div className="mb-6 flex flex-col sm:flex-row gap-4">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 text-muted-foreground w-4 h-4" />
            <input
              type="text"
              placeholder="Search notifications..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full pl-10 pr-4 py-2 border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-pink-500 focus:border-pink-500"
            />
          </div>
        </div>

        {/* Content */}
        {filteredNotifications.length === 0 ? (
          <div className="text-center py-16">
            <Inbox className="w-16 h-16 text-muted-foreground mx-auto mb-4" />
            <h3 className="text-xl font-semibold text-foreground mb-2">No notifications found</h3>
            <p className="text-muted-foreground">
              {searchQuery 
                ? "No notifications match your search."
                : "You don't have any notifications yet."
              }
            </p>
          </div>
        ) : (
          <div className="grid gap-4">
            {filteredNotifications
              .sort((a, b) => timestampMs(b.created_at) - timestampMs(a.created_at))
              .map((notification) => (
              <Card 
                key={notification.id} 
                className={`hover:shadow-lg transition-shadow duration-200 ${
                  notification.match_id && notification.conversation_id && notification.status !== 'rejected' ? 'cursor-pointer' : ''
                }`}
                onClick={() => handleNotificationClick(notification)}
              >
                <CardContent className="p-6">
                  <div className="flex items-start space-x-4">
                    <div className="flex-shrink-0">
                      {getNotificationIcon(notification.type)}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-start justify-between mb-2">
                        <div className="flex items-center space-x-3">
                          {notification.user_profile_picture ? (
                            <img
                              src={getProfilePictureUrl(notification.user_profile_picture)}
                              alt={notification.user_name || 'System'}
                              className="w-12 h-12 rounded-full object-cover border-2 border-pink-500"
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
            ))}
          </div>
        )}
      </div>


      {/* Conversation Dialog */}
      {showConversationDialog && selectedConversation && (
        <ConversationDialog
          conversationData={selectedConversation}
          onClose={() => setShowConversationDialog(false)}
          onRefresh={handleRefreshData}
          onConversationAccepted={handleConversationAccepted}
        />
      )}
    </div>
  );
};

export default InboxPage;
