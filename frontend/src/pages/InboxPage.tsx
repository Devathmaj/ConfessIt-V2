// src/pages/InboxPage.tsx

import { useState, useEffect } from 'react';
import { useAuth } from '@/contexts/AuthContext';
import { resolveProfilePictureUrl } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { FloatingHearts } from '@/components/ui/floating-hearts';
import { getCurrentConversation, acceptConversation, rejectConversation } from '@/services/api';
import { ConversationDialog } from '@/components/ConversationDialog';
import {
  Inbox,
  MessageCircle,
  Clock,
  User,
  Heart,
  CheckCircle,
  XCircle,
  ArrowRight,
  Mail,
  Search,
  Filter,
  RefreshCw,
  ArrowLeft,
  Bell,
  Send,
  UserPlus,
  UserCheck,
  UserX,
} from 'lucide-react';
import { toast } from 'sonner';
import { Navigation } from '@/components/Navigation';

// Defines the structure for conversation data.
interface ConversationData {
  match: {
    id: string;
    expires_at: string;
    created_at: string;
    is_expired: boolean;
  };
  conversation: {
    id: string;
    status: 'pending' | 'accepted' | 'rejected';
    initiator_id: string;
    receiver_id: string;
    created_at: string;
    accepted_at?: string;
  };
  other_user: {
    regno: string;
    name: string;
    username: string;
    profile_picture_id: string;
    which_class: string;
    bio: string;
    interests: string[];
  };
  is_initiator: boolean;
}

// Defines the structure for notification items.
interface NotificationItem {
  id: string;
  type: 'message_request_sent' | 'message_request_received' | 'message_accepted' | 'message_rejected';
  title: string;
  message: string;
  timestamp: string;
  user_name: string;
  user_username: string;
  user_profile_picture: string;
  match_id?: string;
  conversation_id?: string;
  status?: 'pending' | 'accepted' | 'rejected';
}

export const InboxPage = () => {
  const { user } = useAuth();
  const [conversations, setConversations] = useState<ConversationData[]>([]);
  const [notifications, setNotifications] = useState<NotificationItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedConversation, setSelectedConversation] = useState<ConversationData | null>(null);
  const [showConversationDialog, setShowConversationDialog] = useState(false);
  const [currentView, setCurrentView] = useState<'notifications' | 'messages'>('notifications');
  const [filter, setFilter] = useState<'all' | 'pending' | 'active' | 'rejected'>('all');
  const [searchQuery, setSearchQuery] = useState('');

  useEffect(() => {
    fetchData();
  }, []);

  // Fetches both conversations and notifications data.
  const fetchData = async () => {
    try {
      setLoading(true);
      await Promise.all([
        fetchConversations(),
        fetchNotifications()
      ]);
    } catch (error) {
      console.error('Failed to fetch data:', error);
    } finally {
      setLoading(false);
    }
  };

  // Fetches the current user's conversations.
  const fetchConversations = async () => {
    try {
      const response = await getCurrentConversation();
      if (response.status === 'success') {
        setConversations([response]);
      } else {
        setConversations([]);
      }
    } catch (error: any) {
      console.error('Failed to fetch conversations:', error);
      setConversations([]);
    }
  };

  // Fetches notifications, generating them from conversation data for now.
  const fetchNotifications = async () => {
    try {
      const response = await getCurrentConversation();
      if (response.status === 'success') {
        const notification: NotificationItem = {
          id: response.conversation.id,
          type: response.is_initiator ? 'message_request_sent' : 'message_request_received',
          title: response.is_initiator 
            ? `Message request sent to ${response.other_user.name}`
            : `Message request from ${response.other_user.name}`,
          message: response.is_initiator
            ? `You sent a message request to ${response.other_user.name}. Waiting for their response.`
            : `${response.other_user.name} wants to start a conversation with you!`,
          timestamp: response.conversation.created_at,
          user_name: response.other_user.name,
          user_username: response.other_user.username,
          user_profile_picture: response.other_user.profile_picture_id,
          match_id: response.match.id,
          conversation_id: response.conversation.id,
          status: response.conversation.status
        };

        const notifications: NotificationItem[] = [notification];
        
        if (response.conversation.status === 'accepted' && response.conversation.accepted_at) {
          notifications.push({
            id: `${response.conversation.id}_accepted`,
            type: response.is_initiator ? 'message_accepted' : 'message_accepted',
            title: response.is_initiator 
              ? `Message request accepted by ${response.other_user.name}`
              : `You accepted ${response.other_user.name}'s message request`,
            message: response.is_initiator
              ? `${response.other_user.name} accepted your message request! You can now start chatting.`
              : `You accepted the message request from ${response.other_user.name}. You can now start chatting.`,
            timestamp: response.conversation.accepted_at,
            user_name: response.other_user.name,
            user_username: response.other_user.username,
            user_profile_picture: response.other_user.profile_picture_id,
            match_id: response.match.id,
            conversation_id: response.conversation.id,
            status: 'accepted'
          });
        } else if (response.conversation.status === 'rejected') {
          notifications.push({
            id: `${response.conversation.id}_rejected`,
            type: response.is_initiator ? 'message_rejected' : 'message_rejected',
            title: response.is_initiator 
              ? `Message request rejected by ${response.other_user.name}`
              : `You rejected ${response.other_user.name}'s message request`,
            message: response.is_initiator
              ? `${response.other_user.name} rejected your message request.`
              : `You rejected the message request from ${response.other_user.name}.`,
            timestamp: response.conversation.created_at, // Use created_at as fallback
            user_name: response.other_user.name,
            user_username: response.other_user.username,
            user_profile_picture: response.other_user.profile_picture_id,
            match_id: response.match.id,
            conversation_id: response.conversation.id,
            status: 'rejected'
          });
        }

        setNotifications(notifications);
      } else {
        setNotifications([]);
      }
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
      default:
        return <Bell className="w-5 h-5 text-gray-500" />;
    }
  };

  // Returns a styled status badge based on conversation status and expiration.
  const getStatusBadge = (status: string, expiresAt: string) => {
    const expired = isExpired(expiresAt);
    
    if (expired) {
      return (
        <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-800">
          <Clock className="w-3 h-3 mr-1" />
          Expired
        </span>
      );
    }
    
    switch (status) {
      case 'pending':
        return (
          <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-yellow-100 text-yellow-800">
            <Clock className="w-3 h-3 mr-1" />
            Pending
          </span>
        );
      case 'accepted':
        return (
          <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-800">
            <CheckCircle className="w-3 h-3 mr-1" />
            Active
          </span>
        );
      case 'rejected':
        return (
          <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-800">
            <XCircle className="w-3 h-3 mr-1" />
            Rejected
          </span>
        );
      default:
        return (
          <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-800">
            {status}
          </span>
        );
    }
  };

  // Converts a timestamp to a human-readable "time ago" format.
  const getTimeAgo = (timestamp: string) => {
    const now = new Date();
    const timeUTC = new Date(timestamp + 'Z').getTime();
    const nowUTC = now.getTime();
    
    const diffInMinutes = Math.floor((nowUTC - timeUTC) / (1000 * 60));
    
    if (diffInMinutes < 1) return 'Just now';
    if (diffInMinutes < 60) return `${diffInMinutes}m ago`;
    if (diffInMinutes < 1440) return `${Math.floor(diffInMinutes / 60)}h ago`;
    return `${Math.floor(diffInMinutes / 1440)}d ago`;
  };

  // Checks if a given expiration timestamp has passed.
  const isExpired = (expiresAt: string) => {
    const now = new Date();
    const expiresUTC = new Date(expiresAt + 'Z').getTime();
    const nowUTC = now.getTime();
    
    return nowUTC > expiresUTC;
  };

  // Calculates the time remaining until a given expiration timestamp.
  const getTimeLeft = (expiresAt: string) => {
    const now = new Date();
    const expiresUTC = new Date(expiresAt + 'Z').getTime();
    const nowUTC = now.getTime();
    
    const diffInMinutes = Math.floor((expiresUTC - nowUTC) / (1000 * 60));
    
    if (diffInMinutes <= 0) return 'Expired';
    if (diffInMinutes < 60) return `${diffInMinutes}m left`;
    
    const hours = Math.floor(diffInMinutes / 60);
    const minutes = diffInMinutes % 60;
    if (diffInMinutes < 1440) {
      return `${hours}h ${minutes}m left`;
    }
    
    const days = Math.floor(diffInMinutes / 1440);
    const remainingHours = Math.floor((diffInMinutes % 1440) / 60);
    return `${days}d ${remainingHours}h left`;
  };

  // Filters conversations based on the current filter and search query.
  const filteredConversations = conversations.filter(conv => {
    let statusMatches = false;
    if (filter === 'all') {
      statusMatches = true;
    } else if (filter === 'active') {
      statusMatches = conv.conversation.status === 'accepted';
    } else {
      statusMatches = conv.conversation.status === filter;
    }
    
    const matchesSearch = conv.other_user.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
                         conv.other_user.username.toLowerCase().includes(searchQuery.toLowerCase());
    
    return statusMatches && matchesSearch;
  });

  // Filters notifications based on the current search query.
  const filteredNotifications = notifications.filter(notification => {
    const matchesSearch = notification.user_name.toLowerCase().includes(searchQuery.toLowerCase()) ||
                         notification.user_username.toLowerCase().includes(searchQuery.toLowerCase());
    return matchesSearch;
  });

  // Handles clicking on a notification, opening the corresponding conversation.
  const handleNotificationClick = (notification: NotificationItem) => {
    if (notification.match_id && notification.conversation_id) {
      const conversation = conversations.find(conv => conv.match.id === notification.match_id);
      if (conversation) {
        setSelectedConversation(conversation);
        setShowConversationDialog(true);
      }
    }
  };

  // Switches the view to the messages list.
  const handleViewMessages = () => {
    setCurrentView('messages');
  };

  // Opens the conversation dialog for a selected conversation.
  const handleOpenConversation = (conversation: ConversationData) => {
    setSelectedConversation(conversation);
    setShowConversationDialog(true);
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
            <div className="flex items-center justify-center lg:justify-start gap-3 mb-2">
              {currentView === 'messages' && (
                <Button 
                  variant="ghost" 
                  size="sm" 
                  onClick={() => setCurrentView('notifications')}
                  className="p-2"
                >
                  <ArrowLeft className="w-4 h-4" />
                </Button>
              )}
              <h1 className="text-5xl font-dancing text-pink-600 dark:text-pink-400">
                {currentView === 'notifications' ? 'Inbox 💌' : 'Messages 💬'}
              </h1>
            </div>
            <p className="text-xl text-muted-foreground">
              {currentView === 'notifications' 
                ? 'Manage your notifications and message requests' 
                : 'Your conversations and messages'
              }
            </p>
          </div>
                     <div className="flex items-center space-x-2">
             {currentView === 'notifications' && conversations.length > 0 && (
               <Button variant="outline" onClick={handleViewMessages}>
                 <MessageCircle className="w-4 h-4 mr-2" />
                 View Messages
               </Button>
             )}
             <Button variant="outline" onClick={handleRefreshData}>
               <RefreshCw className="w-4 h-4 mr-2" />
               Refresh
             </Button>
           </div>
        </div>
      </div>

      <div className="max-w-7xl mx-auto">
        {/* Search and Filter Bar */}
        <div className="mb-6 flex flex-col sm:flex-row gap-4">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 text-muted-foreground w-4 h-4" />
            <input
              type="text"
              placeholder={currentView === 'notifications' ? "Search notifications..." : "Search by name or username..."}
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full pl-10 pr-4 py-2 border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-pink-500 focus:border-pink-500"
            />
          </div>
          {currentView === 'messages' && (
            <div className="flex gap-2">
              <Button
                variant={filter === 'all' ? 'default' : 'outline'}
                onClick={() => setFilter('all')}
                size="sm"
              >
                All
              </Button>
              <Button
                variant={filter === 'pending' ? 'default' : 'outline'}
                onClick={() => setFilter('pending')}
                size="sm"
              >
                Pending
              </Button>
              <Button
                variant={filter === 'active' ? 'default' : 'outline'}
                onClick={() => setFilter('active')}
                size="sm"
              >
                Active
              </Button>
              <Button
                variant={filter === 'rejected' ? 'default' : 'outline'}
                onClick={() => setFilter('rejected')}
                size="sm"
              >
                Rejected
              </Button>
            </div>
          )}
        </div>

        {/* Content */}
        {currentView === 'notifications' ? (
          // Notifications View
          filteredNotifications.length === 0 ? (
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
                .sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime())
                .map((notification) => (
                <Card 
                  key={notification.id} 
                  className="hover:shadow-lg transition-shadow duration-200 cursor-pointer"
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
                            <img
                              src={getProfilePictureUrl(notification.user_profile_picture)}
                              alt={notification.user_name}
                              className="w-12 h-12 rounded-full object-cover border-2 border-pink-500"
                            />
                            <div>
                              <h3 className="text-lg font-semibold text-foreground">
                                {notification.title}
                              </h3>
                              <p className="text-sm text-muted-foreground">
                                @{notification.user_username}
                              </p>
                            </div>
                          </div>
                          <div className="text-sm text-muted-foreground">
                            {getTimeAgo(notification.timestamp)}
                          </div>
                        </div>
                        
                        <p className="text-sm text-muted-foreground mb-3">
                          {notification.message}
                        </p>
                        
                                                 <div className="flex items-center justify-between">
                           <div className="flex items-center space-x-2">
                             {notification.status && (
                               <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                                 notification.status === 'pending' ? 'bg-yellow-100 text-yellow-800' :
                                 notification.status === 'accepted' ? 'bg-green-100 text-green-800' :
                                 'bg-red-100 text-red-800'
                               }`}>
                                 {notification.status === 'pending' ? 'Pending' :
                                  notification.status === 'accepted' ? 'Accepted' : 'Rejected'}
                               </span>
                             )}
                           </div>
                           <div className="flex items-center space-x-2">
                             <Button 
                               variant="outline" 
                               size="sm" 
                               onClick={(e) => {
                                 e.stopPropagation();
                                 handleViewMessages();
                               }}
                               className="text-xs"
                             >
                               View Messages
                             </Button>
                             <div className="flex items-center text-pink-600 dark:text-pink-400">
                               <span>View details</span>
                               <ArrowRight className="w-4 h-4 ml-1" />
                             </div>
                           </div>
                         </div>
                      </div>
                    </div>
                  </CardContent>
                </Card>
              ))}
            </div>
          )
        ) : (
          // Messages View
          filteredConversations.length === 0 ? (
            <div className="text-center py-16">
              <MessageCircle className="w-16 h-16 text-muted-foreground mx-auto mb-4" />
              <h3 className="text-xl font-semibold text-foreground mb-2">No conversations found</h3>
              <p className="text-muted-foreground">
                {filter === 'all' 
                  ? "You don't have any conversations yet. Start matching to begin conversations!"
                  : `No ${filter} conversations found.`
                }
              </p>
              {filter !== 'all' && (
                <Button 
                  variant="outline" 
                  onClick={() => setFilter('all')}
                  className="mt-4"
                >
                  View all conversations
                </Button>
              )}
            </div>
          ) : (
            <div className="grid gap-4">
              {filteredConversations
                .sort((a, b) => new Date(b.conversation.created_at).getTime() - new Date(a.conversation.created_at).getTime())
                .map((conversation) => (
                <Card 
                  key={conversation.conversation.id} 
                  className="hover:shadow-lg transition-shadow duration-200 cursor-pointer"
                  onClick={() => handleOpenConversation(conversation)}
                >
                  <CardContent className="p-6">
                    <div className="flex items-start space-x-4">
                      <img
                        src={getProfilePictureUrl(conversation.other_user.profile_picture_id)}
                        alt={conversation.other_user.name}
                        className="w-16 h-16 rounded-full object-cover border-2 border-pink-500 flex-shrink-0"
                      />
                      <div className="flex-1 min-w-0">
                        <div className="flex items-start justify-between mb-2">
                          <div>
                            <h3 className="text-lg font-semibold text-foreground">
                              {conversation.other_user.name}
                            </h3>
                            <p className="text-sm text-muted-foreground">
                              @{conversation.other_user.username} • {conversation.other_user.which_class}
                            </p>
                          </div>
                          <div className="flex items-center space-x-2">
                            {getStatusBadge(conversation.conversation.status, conversation.match.expires_at)}
                          </div>
                        </div>
                        
                        {isExpired(conversation.match.expires_at) && (
                          <div className="mb-3 p-2 bg-red-50 border border-red-200 rounded text-xs text-red-700">
                            <Clock className="w-3 h-3 inline mr-1" />
                            This conversation has expired. You can view messages but cannot send new ones.
                          </div>
                        )}
                        
                        {conversation.other_user.bio && (
                          <p className="text-sm text-muted-foreground mb-3 overflow-hidden text-ellipsis" style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' }}>
                            "{conversation.other_user.bio}"
                          </p>
                        )}
                        
                        {conversation.other_user.interests && conversation.other_user.interests.length > 0 && (
                          <div className="flex flex-wrap gap-1 mb-3">
                            {conversation.other_user.interests.slice(0, 3).map((interest, index) => (
                              <span 
                                key={index} 
                                className="px-2 py-1 bg-secondary text-secondary-foreground rounded-full text-xs"
                              >
                                {interest}
                              </span>
                            ))}
                            {conversation.other_user.interests.length > 3 && (
                              <span className="px-2 py-1 bg-secondary text-secondary-foreground rounded-full text-xs">
                                +{conversation.other_user.interests.length - 3} more
                              </span>
                            )}
                          </div>
                        )}
                        
                        <div className="flex items-center justify-between text-sm text-muted-foreground">
                          <div className="flex items-center space-x-4">
                            <span className="flex items-center">
                              <Clock className="w-3 h-3 mr-1" />
                              Created {getTimeAgo(conversation.conversation.created_at)}
                            </span>
                            <span className={`flex items-center ${
                              isExpired(conversation.match.expires_at) ? 'text-red-600' : ''
                            }`}>
                              <Heart className="w-3 h-3 mr-1" />
                              {isExpired(conversation.match.expires_at) ? 'Expired' : `Expires ${getTimeLeft(conversation.match.expires_at)}`}
                            </span>
                          </div>
                          <div className={`flex items-center ${
                            isExpired(conversation.match.expires_at) ? 'text-gray-500' : 'text-pink-600 dark:text-pink-400'
                          }`}>
                            <span>{isExpired(conversation.match.expires_at) ? 'View conversation (expired)' : 'View conversation'}</span>
                            <ArrowRight className="w-4 h-4 ml-1" />
                          </div>
                        </div>
                      </div>
                    </div>
                  </CardContent>
                </Card>
              ))}
            </div>
          )
        )}
      </div>

      {/* Conversation Dialog */}
      {showConversationDialog && selectedConversation && (
        <ConversationDialog
          onClose={() => setShowConversationDialog(false)}
          onRefresh={handleRefreshData}
          onConversationAccepted={handleConversationAccepted}
        />
      )}
    </div>
  );
};

export default InboxPage;
