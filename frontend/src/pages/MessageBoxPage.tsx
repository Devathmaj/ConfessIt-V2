// src/pages/MessageBoxPage.tsx

import { useState, useEffect } from 'react';
import { useAuth } from '@/contexts/AuthContext';
import { resolveProfilePictureUrl } from '@/lib/utils';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { FloatingHearts } from '@/components/ui/floating-hearts';
import { getCurrentConversation, getReceivedConversations, getConversationByMatch } from '@/services/api';
import { ConversationDialog } from '@/components/ConversationDialog';
import { MessageCircle, Clock, Heart, ArrowRight, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { Navigation } from '@/components/Navigation';

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

export const MessageBoxPage = () => {
  const { user } = useAuth();
  const [conversations, setConversations] = useState<ConversationData[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedConversation, setSelectedConversation] = useState<ConversationData | null>(null);
  const [showConversationDialog, setShowConversationDialog] = useState(false);

  useEffect(() => {
    fetchConversations();
  }, []);

  const fetchConversations = async () => {
    try {
      setLoading(true);
      const [initiatedResponse, receivedResponse] = await Promise.all([
        getCurrentConversation(),
        getReceivedConversations()
      ]);

      const allConversations: ConversationData[] = [];

      // Add initiated conversation ONLY if status is 'accepted'
      if (initiatedResponse.status === 'success' && 
          initiatedResponse.conversation.status === 'accepted') {
        allConversations.push(initiatedResponse);
      }

      // Add received conversations ONLY if status is 'accepted'
      if (receivedResponse.status === 'success' && receivedResponse.conversations) {
        receivedResponse.conversations.forEach((conv: any) => {
          if (conv.conversation.status === 'accepted') {
            allConversations.push(conv);
          }
        });
      }

      // Sort by accepted_at (most recent first)
      allConversations.sort((a, b) => {
        const dateA = a.conversation.accepted_at ? new Date(a.conversation.accepted_at).getTime() : 0;
        const dateB = b.conversation.accepted_at ? new Date(b.conversation.accepted_at).getTime() : 0;
        return dateB - dateA;
      });

      setConversations(allConversations);
    } catch (error) {
      console.error('Failed to fetch conversations:', error);
      toast.error('Failed to load messages');
      setConversations([]);
    } finally {
      setLoading(false);
    }
  };

  const handleOpenConversation = async (conversation: ConversationData) => {
    try {
      console.log('📡 Fetching conversation from API for match:', conversation.match.id);
      const response = await getConversationByMatch(conversation.match.id);
      
      if (response.status === 'success') {
        const freshConversation = response as ConversationData;
        setSelectedConversation(freshConversation);
        setShowConversationDialog(true);
      } else {
        toast.error('Failed to load conversation');
      }
    } catch (error) {
      console.error('❌ Failed to fetch conversation:', error);
      toast.error('Failed to load conversation');
    }
  };

  const isExpired = (expiresAt: string) => {
    // Ensure timestamp is treated as UTC by appending 'Z' if not present
    const now = new Date();
    const expiresUTC = new Date(expiresAt.endsWith('Z') ? expiresAt : expiresAt + 'Z').getTime();
    const nowUTC = now.getTime();
    return nowUTC > expiresUTC;
  };

  const getTimeLeft = (expiresAt: string) => {
    const now = new Date();
    const expiresUTC = new Date(expiresAt.endsWith('Z') ? expiresAt : expiresAt + 'Z').getTime();
    const nowUTC = now.getTime();
    const diff = expiresUTC - nowUTC;

    if (diff <= 0) return 'Expired';

    const hours = Math.floor(diff / (1000 * 60 * 60));
    const minutes = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));

    if (hours > 24) {
      const days = Math.floor(hours / 24);
      return `in ${days}d ${hours % 24}h`;
    }
    return `in ${hours}h ${minutes}m`;
  };

  const getTimeAgo = (timestamp: string) => {
    // Ensure timestamp is treated as UTC by appending 'Z' if not present
    const now = new Date();
    const timeUTC = new Date(timestamp.endsWith('Z') ? timestamp : timestamp + 'Z').getTime();
    const nowUTC = now.getTime();
    const diff = nowUTC - timeUTC;

    const minutes = Math.floor(diff / (1000 * 60));
    const hours = Math.floor(diff / (1000 * 60 * 60));
    const days = Math.floor(diff / (1000 * 60 * 60 * 24));

    if (days > 0) return `${days}d ago`;
    if (hours > 0) return `${hours}h ago`;
    if (minutes > 0) return `${minutes}m ago`;
    return 'just now';
  };

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
      
      <div className="max-w-7xl mx-auto px-4">
        <div className="mb-8">
          <h1 className="text-2xl sm:text-3xl md:text-4xl font-bold mb-2 flex items-center gap-2 sm:gap-3 break-words">
            <MessageCircle className="w-6 h-6 sm:w-8 sm:h-8 flex-shrink-0" />
            <span>Message Box</span>
          </h1>
          <p className="text-sm sm:text-base text-muted-foreground break-words">
            Your active conversations
          </p>
        </div>

        {conversations.length === 0 ? (
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
            {conversations.map((conversation) => (
              <Card 
                key={conversation.conversation.id}
                className="hover:shadow-lg transition-all cursor-pointer border-2 hover:border-primary/50"
                onClick={() => handleOpenConversation(conversation)}
              >
                <CardHeader className="pb-3">
                  <CardTitle className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <img
                        src={resolveProfilePictureUrl(conversation.other_user.profile_picture_id)}
                        alt={conversation.other_user.name}
                        className="w-12 h-12 rounded-full object-cover"
                      />
                      <div>
                        <h3 className="text-lg font-semibold">{conversation.other_user.name}</h3>
                        <p className="text-sm text-muted-foreground">@{conversation.other_user.username}</p>
                      </div>
                    </div>
                    <span className="px-3 py-1 bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300 rounded-full text-xs font-medium">
                      Active
                    </span>
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="space-y-3">
                    <p className="text-sm text-muted-foreground line-clamp-2">
                      {conversation.other_user.bio || 'No bio available'}
                    </p>
                    
                    {conversation.other_user.interests && conversation.other_user.interests.length > 0 && (
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
                          Started {getTimeAgo(conversation.conversation.accepted_at || conversation.conversation.created_at)}
                        </span>
                        <span className={`flex items-center ${
                          isExpired(conversation.match.expires_at) ? 'text-red-600' : ''
                        }`}>
                          <Heart className="w-3 h-3 mr-1" />
                          {isExpired(conversation.match.expires_at) ? 'Expired' : `Expires ${getTimeLeft(conversation.match.expires_at)}`}
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
