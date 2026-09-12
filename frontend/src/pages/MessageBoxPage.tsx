// src/pages/MessageBoxPage.tsx

import { useState, useEffect } from 'react';
import { parseTimestamp, resolveProfilePictureUrl, timestampMs } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { FloatingHearts } from '@/components/ui/floating-hearts';
import { getCurrentConversation, getReceivedConversations, getConversationByMatch } from '@/services/api';
import { ConversationDialog } from '@/components/ConversationDialog';
import {
  MessageCircle,
  Clock,
  Heart,
  CheckCircle,
  XCircle,
  Search,
  ArrowRight,
  ArrowLeft,
  Loader2,
} from 'lucide-react';
import { toast } from 'sonner';
import { Navigation } from '@/components/Navigation';
import { serverNow } from '@/lib/serverClock';

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
    status: 'pending' | 'requested' | 'accepted' | 'rejected';
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

/** The tabs on the full conversation list. */
export type ConversationFilter = 'all' | 'pending' | 'active' | 'expired' | 'rejected';

/** Checks if a given expiration timestamp has passed. */
export const isExpired = (expiresAt: string) => {
  const parsed = parseTimestamp(expiresAt);
  return parsed ? serverNow() > parsed.getTime() : false;
};

/**
 * Whether a conversation is past its match expiry.
 *
 * Read off the clock rather than the server's is_expired flag so a page left open past the
 * four hours reclassifies itself without waiting for a refetch.
 */
export const conversationIsExpired = (conv: ConversationData) => isExpired(conv.match.expires_at);

/**
 * Whether a conversation belongs under the given tab.
 *
 * An expired conversation is held by Expired rather than Active: it can no longer be chatted
 * in. It is moved rather than dropped, so it stays reachable and still appears on the
 * landing view, which lists every accepted conversation regardless of expiry. Expiry is
 * checked ahead of the stored status because that is also the order the status badge uses.
 */
export const matchesConversationFilter = (
  conv: ConversationData,
  filter: ConversationFilter
) => {
  switch (filter) {
    case 'all':
      return true;
    case 'expired':
      return conversationIsExpired(conv);
    case 'active':
      return conv.conversation.status === 'accepted' && !conversationIsExpired(conv);
    default:
      return conv.conversation.status === filter;
  }
};

export const MessageBoxPage = () => {
  const [conversations, setConversations] = useState<ConversationData[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedConversation, setSelectedConversation] = useState<ConversationData | null>(null);
  const [showConversationDialog, setShowConversationDialog] = useState(false);

  // The page lands on accepted conversations. "View Conversations" swaps in the
  // full list, including pending requests, expired ones and rejected ones.
  const [showAllConversations, setShowAllConversations] = useState(false);
  const [filter, setFilter] = useState<ConversationFilter>('all');
  const [searchQuery, setSearchQuery] = useState('');

  useEffect(() => {
    fetchConversations();
  }, []);

  // Fetches every conversation the user is part of, in any state. The two views
  // above narrow this down: the landing view keeps only accepted ones, and the
  // status tabs narrow it by the selected status.
  const fetchConversations = async () => {
    try {
      setLoading(true);
      const [initiatedResponse, receivedResponse] = await Promise.all([
        getCurrentConversation(),
        getReceivedConversations(),
      ]);

      const allConversations: ConversationData[] = [];

      if (initiatedResponse.status === 'success' && initiatedResponse.conversation?.id) {
        allConversations.push(initiatedResponse);
      }

      if (receivedResponse.status === 'success' && receivedResponse.conversations) {
        receivedResponse.conversations.forEach((conv: ConversationData) => {
          if (conv.conversation?.id) {
            allConversations.push(conv);
          }
        });
      }

      setConversations(allConversations);
    } catch (error) {
      console.error('Failed to fetch conversations:', error);
      toast.error('Failed to load messages');
      setConversations([]);
    } finally {
      setLoading(false);
    }
  };

  // Opens the conversation dialog for a selected conversation.
  const handleOpenConversation = async (conversation: ConversationData) => {
    try {
      const response = await getConversationByMatch(conversation.match.id);

      if (response.status === 'success') {
        setSelectedConversation(response as ConversationData);
        setShowConversationDialog(true);
      } else {
        toast.error('Failed to load conversation');
      }
    } catch (error) {
      console.error('Failed to fetch conversation:', error);
      toast.error('Failed to load conversation');
    }
  };

  // Constructs the full URL for a user's profile picture.
  const getProfilePictureUrl = (profilePictureId?: string | null) =>
    resolveProfilePictureUrl(profilePictureId ?? null);

  // Returns a styled status badge based on conversation status and expiration.
  const getStatusBadge = (status: string, expiresAt: string) => {
    if (isExpired(expiresAt)) {
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
      case 'requested':
        return (
          <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-yellow-100 text-yellow-800">
            <Clock className="w-3 h-3 mr-1" />
            Requested
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
  const getTimeAgo = (created_at: string) => {
    const parsed = parseTimestamp(created_at);
    if (!parsed) return '—';
    const timeUTC = parsed.getTime();
    const nowUTC = serverNow();
    const diff = nowUTC - timeUTC;

    const minutes = Math.floor(diff / (1000 * 60));
    const hours = Math.floor(diff / (1000 * 60 * 60));
    const days = Math.floor(diff / (1000 * 60 * 60 * 24));

    if (days > 0) return `${days}d ago`;
    if (hours > 0) return `${hours}h ago`;
    if (minutes > 0) return `${minutes}m ago`;
    return 'just now';
  };

  // Leaving the full list drops the filters, which mean nothing on the landing view: it
  // shows accepted conversations only, so a leftover search would silently narrow it.
  const handleBackToActive = () => {
    setShowAllConversations(false);
    setFilter('all');
    setSearchQuery('');
  };

  // Calculates the time remaining until a given expiration timestamp.
  const getTimeLeft = (expiresAt: string) => {
    const parsed = parseTimestamp(expiresAt);
    if (!parsed) return '—';

    const diff = parsed.getTime() - serverNow();
    if (diff <= 0) return 'Expired';

    const hours = Math.floor(diff / (1000 * 60 * 60));
    const minutes = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));

    if (hours > 24) {
      const days = Math.floor(hours / 24);
      return `in ${days}d ${hours % 24}h`;
    }
    return `in ${hours}h ${minutes}m`;
  };

  // Accepted conversations only, most recently accepted first. Expired ones stay here:
  // they are still the user's conversations, and the chat behind them is readable.
  const acceptedConversations = conversations
    .filter((conv) => conv.conversation.status === 'accepted')
    .sort((a, b) => {
      const dateA = timestampMs(a.conversation.accepted_at);
      const dateB = timestampMs(b.conversation.accepted_at);
      return dateB - dateA;
    });

  // Narrows the full list by the selected status and the name search.
  const filteredConversations = conversations
    .filter((conv) => {
      const statusMatches = matchesConversationFilter(conv, filter);

      const matchesSearch = (conv.other_user?.name || '')
        .toLowerCase()
        .includes(searchQuery.toLowerCase());

      return statusMatches && matchesSearch;
    })
    .sort(
      (a, b) => timestampMs(b.conversation.created_at) - timestampMs(a.conversation.created_at)
    );

  if (loading) {
    return (
      <div className="min-h-screen p-4 pt-24 relative overflow-hidden">
        <Navigation />
        <FloatingHearts />
        <div className="max-w-7xl mx-auto">
          <div className="flex items-center justify-center h-64">
            <Loader2 className="h-12 w-12 animate-spin text-primary" />
            <span className="ml-3 text-lg">Loading messages...</span>
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
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
          <div>
            <h1 className="text-2xl sm:text-3xl md:text-4xl font-bold mb-2 flex items-center gap-2 sm:gap-3 break-words">
              <MessageCircle className="w-6 h-6 sm:w-8 sm:h-8 flex-shrink-0" />
              <span>Message Box</span>
            </h1>
            <p className="text-sm sm:text-base text-muted-foreground break-words">
              {showAllConversations
                ? 'Your conversations and message requests'
                : 'Your active conversations'}
            </p>
          </div>
          {!showAllConversations && (
            <Button
              variant="outline"
              onClick={() => setShowAllConversations(true)}
              className="self-start sm:self-auto"
            >
              <MessageCircle className="w-4 h-4 mr-2" />
              View Conversations
            </Button>
          )}
        </div>
      </div>

      <div className="max-w-7xl mx-auto px-4">
        {showAllConversations ? (
          <>
            {/* Search and Filter Bar */}
            <div className="mb-6 flex flex-col gap-4">
              <div>
                <Button variant="outline" size="sm" onClick={handleBackToActive}>
                  <ArrowLeft className="w-4 h-4 mr-2" />
                  Back
                </Button>
              </div>
              <div className="flex flex-col sm:flex-row gap-4">
                <div className="relative flex-1">
                  <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 text-muted-foreground w-4 h-4" />
                  <input
                    type="text"
                    placeholder="Search by name..."
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    className="w-full pl-10 pr-4 py-2 border border-border rounded-lg focus:outline-none focus:ring-2 focus:ring-pink-500 focus:border-pink-500"
                  />
                </div>
                <div className="flex flex-wrap gap-2">
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
                    variant={filter === 'expired' ? 'default' : 'outline'}
                    onClick={() => setFilter('expired')}
                    size="sm"
                  >
                    Expired
                  </Button>
                  <Button
                    variant={filter === 'rejected' ? 'default' : 'outline'}
                    onClick={() => setFilter('rejected')}
                    size="sm"
                  >
                    Rejected
                  </Button>
                </div>
              </div>
            </div>

            {filteredConversations.length === 0 ? (
              <div className="text-center py-16">
                <MessageCircle className="w-16 h-16 text-muted-foreground mx-auto mb-4" />
                <h3 className="text-xl font-semibold text-foreground mb-2">No conversations found</h3>
                <p className="text-muted-foreground">
                  {searchQuery
                    ? 'No conversations match your search.'
                    : filter === 'all'
                      ? "You don't have any conversations yet. Start matching to begin conversations!"
                      : `No ${filter} conversations found.`}
                </p>
                {(filter !== 'all' || searchQuery) && (
                  <Button
                    variant="outline"
                    onClick={() => {
                      setFilter('all');
                      setSearchQuery('');
                    }}
                    className="mt-4"
                  >
                    View all conversations
                  </Button>
                )}
              </div>
            ) : (
              <div className="grid gap-4">
                {filteredConversations.map((conversation) => (
                  <Card
                    key={conversation.conversation.id}
                    className="hover:shadow-lg transition-shadow duration-200 cursor-pointer"
                    onClick={() => handleOpenConversation(conversation)}
                  >
                    <CardContent className="p-6">
                      <div className="flex items-start space-x-4">
                        <img
                          src={getProfilePictureUrl(conversation.other_user.profile_picture_url)}
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
                                {conversation.other_user.name} • {conversation.other_user.class_name}
                              </p>
                            </div>
                            <div className="flex items-center space-x-2">
                              {getStatusBadge(
                                conversation.conversation.status,
                                conversation.match.expires_at
                              )}
                            </div>
                          </div>

                          {isExpired(conversation.match.expires_at) && (
                            <div className="mb-3 p-2 bg-red-50 border border-red-200 rounded text-xs text-red-700">
                              <Clock className="w-3 h-3 inline mr-1" />
                              This conversation has expired. You can view messages but cannot send
                              new ones.
                            </div>
                          )}

                          {conversation.other_user.bio && (
                            <p
                              className="text-sm text-muted-foreground mb-3 overflow-hidden text-ellipsis"
                              style={{
                                display: '-webkit-box',
                                WebkitLineClamp: 2,
                                WebkitBoxOrient: 'vertical',
                              }}
                            >
                              "{conversation.other_user.bio}"
                            </p>
                          )}

                          {conversation.other_user.interests &&
                            conversation.other_user.interests.length > 0 && (
                              <div className="flex flex-wrap gap-1 mb-3">
                                {conversation.other_user.interests
                                  .slice(0, 3)
                                  .map((interest, index) => (
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
                              <span
                                className={`flex items-center ${
                                  isExpired(conversation.match.expires_at) ? 'text-red-600' : ''
                                }`}
                              >
                                <Heart className="w-3 h-3 mr-1" />
                                {isExpired(conversation.match.expires_at)
                                  ? 'Expired'
                                  : `Expires ${getTimeLeft(conversation.match.expires_at)}`}
                              </span>
                            </div>
                            <div
                              className={`flex items-center ${
                                isExpired(conversation.match.expires_at)
                                  ? 'text-gray-500'
                                  : 'text-pink-600 dark:text-pink-400'
                              }`}
                            >
                              <span>
                                {isExpired(conversation.match.expires_at)
                                  ? 'View conversation (expired)'
                                  : 'View conversation'}
                              </span>
                              <ArrowRight className="w-4 h-4 ml-1" />
                            </div>
                          </div>
                        </div>
                      </div>
                    </CardContent>
                  </Card>
                ))}
              </div>
            )}
          </>
        ) : acceptedConversations.length === 0 ? (
          <Card>
            <CardContent className="flex flex-col items-center justify-center py-12">
              <MessageCircle className="w-16 h-16 text-muted-foreground mb-4" />
              <h3 className="text-xl font-semibold mb-2">No Active Conversations</h3>
              <p className="text-muted-foreground text-center">
                Accept message requests from your notifications to start chatting!
              </p>
            </CardContent>
          </Card>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {acceptedConversations.map((conversation) => (
              <Card
                key={conversation.conversation.id}
                className="hover:shadow-lg transition-all cursor-pointer border-2 hover:border-primary/50"
                onClick={() => handleOpenConversation(conversation)}
              >
                <CardHeader className="pb-3">
                  <CardTitle className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <img
                        src={resolveProfilePictureUrl(conversation.other_user.profile_picture_url)}
                        alt={conversation.other_user.name}
                        className="w-12 h-12 rounded-full object-cover"
                      />
                      <div>
                        <h3 className="text-lg font-semibold">{conversation.other_user.name}</h3>
                        <p className="text-sm text-muted-foreground">
                          {conversation.other_user.name}
                        </p>
                      </div>
                    </div>
                    {conversationIsExpired(conversation) ? (
                      <span className="px-3 py-1 bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-300 rounded-full text-xs font-medium inline-flex items-center">
                        <Clock className="w-3 h-3 mr-1" />
                        Expired
                      </span>
                    ) : (
                      <span className="px-3 py-1 bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300 rounded-full text-xs font-medium">
                        Active
                      </span>
                    )}
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="space-y-3">
                    <p className="text-sm text-muted-foreground line-clamp-2">
                      {conversation.other_user.bio || 'No bio available'}
                    </p>

                    {conversation.other_user.interests &&
                      conversation.other_user.interests.length > 0 && (
                        <div className="flex flex-wrap gap-1">
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

                    <div className="flex items-center justify-between text-sm text-muted-foreground pt-2 border-t">
                      <div className="flex items-center space-x-4">
                        <span className="flex items-center">
                          <Clock className="w-3 h-3 mr-1" />
                          Started{' '}
                          {getTimeAgo(
                            conversation.conversation.accepted_at ||
                              conversation.conversation.created_at
                          )}
                        </span>
                        <span
                          className={`flex items-center ${
                            isExpired(conversation.match.expires_at) ? 'text-red-600' : ''
                          }`}
                        >
                          <Heart className="w-3 h-3 mr-1" />
                          {isExpired(conversation.match.expires_at)
                            ? 'Expired'
                            : `Expires ${getTimeLeft(conversation.match.expires_at)}`}
                        </span>
                      </div>
                      <div className="flex items-center text-primary">
                        <span className="text-sm font-medium">Open chat</span>
                        <ArrowRight className="w-4 h-4 ml-1" />
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
          onClose={() => {
            setShowConversationDialog(false);
            setSelectedConversation(null);
          }}
          onRefresh={fetchConversations}
          onConversationAccepted={fetchConversations}
        />
      )}
    </div>
  );
};

export default MessageBoxPage;
