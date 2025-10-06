import React, { useState, useEffect, useRef } from 'react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Clock, MessageCircle, User, Heart, X, RefreshCw, ChevronDown, Loader2 } from 'lucide-react';
import { getCurrentConversation, acceptConversation, sendMessage, rejectConversation, getConversationMessages } from '@/services/api';
import { websocketService } from '@/services/websocket';
import { toast } from 'sonner';
import { resolveProfilePictureUrl } from '@/lib/utils';

// WebSocket-based real-time messaging system
// This replaces the client-side event system with server-side WebSocket broadcasting

interface ConversationDialogProps {
  onClose: () => void;
  onRefresh: () => void;
  onConversationAccepted?: () => void;
}

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

interface Message {
  id: string;
  text: string;
  sender_id: string;
  receiver_id: string;
  timestamp: string;
  is_sender: boolean;
}

export const ConversationDialog: React.FC<ConversationDialogProps> = ({ onClose, onRefresh, onConversationAccepted }) => {
  const [conversationData, setConversationData] = useState<ConversationData | null>(null);
  const [loading, setLoading] = useState(true);
  const [messageText, setMessageText] = useState('');
  const [messages, setMessages] = useState<Message[]>([]);
  const [timeLeft, setTimeLeft] = useState<string>('');
  const [accepting, setAccepting] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [sending, setSending] = useState(false);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [isConnected, setIsConnected] = useState(false);
  const [showMobileProfile, setShowMobileProfile] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const currentUserRef = useRef<string | null>(null);

  useEffect(() => {
    fetchCurrentConversation();
    const interval = setInterval(updateTimeLeft, 1000);
    return () => clearInterval(interval);
  }, []);

  // Refresh messages when conversation status changes to accepted
  useEffect(() => {
    if (conversationData && conversationData.conversation.status === 'accepted') {
      loadConversationMessages(conversationData.match.id);
    }
  }, [conversationData?.conversation.status]);

  // Scroll to bottom only when messages are first loaded or when user sends a message
  // This prevents auto-scrolling during periodic refresh, allowing users to read older messages
  const [shouldAutoScroll, setShouldAutoScroll] = useState(true);
  
  useEffect(() => {
    if (shouldAutoScroll) {
      scrollToBottom();
    }
  }, [messages, shouldAutoScroll]);

  // WebSocket-based real-time message updates: Messages are refreshed ONLY when:
  // 1. Dialog is opened (fetchCurrentConversation) - initial load + auto-scroll
  // 2. A new message is sent (handleSendMessage) - immediate refresh + auto-scroll
  // 3. Conversation status changes (accept/reject) - full refresh + auto-scroll
  // 4. Manual refresh button (refreshMessagesOnly) - on-demand refresh + preserves scroll position
  // 5. Other user sends message (WebSocket broadcast) - instant refresh for receiver
  // 6. Match expiry warnings and notifications (WebSocket broadcast)
  // 
  // BENEFITS:
  // - True real-time updates via WebSocket - no polling required
  // - Server-side broadcasting ensures cross-session message delivery
  // - Conversation-specific updates - only affects the relevant conversation
  // - Preserves user input and scroll position
  // - Minimal API calls - maximum efficiency
  // - Automatic expiry handling and warnings

  // Connect to WebSocket when conversation dialog is opened (regardless of status)
  useEffect(() => {
    if (conversationData?.match.id) {
      console.log('Connecting to WebSocket for match:', conversationData.match.id);
      // Connect to WebSocket for this match
      websocketService.connect(conversationData.match.id).then(() => {
        console.log('WebSocket connected successfully');
      }).catch(error => {
        console.error('Failed to connect to WebSocket:', error);
      });
      
      // Cleanup on unmount
      return () => {
        console.log('Disconnecting WebSocket');
        websocketService.disconnect();
      };
    }
  }, [conversationData?.match.id]);

  // Subscribe to WebSocket messages when conversation dialog is opened
  useEffect(() => {
    if (conversationData?.match.id) {
      console.log('Setting up WebSocket subscriptions for match:', conversationData.match.id);
      
      // Handle new messages
      const unsubscribeNewMessage = websocketService.onNewMessage((data) => {
        console.log('WebSocket: Received new message:', data);
        
        // Add the new message to the messages array immediately
        if (data.message && data.match_id === conversationData.match.id) {
          setMessages(prevMessages => {
            // Check if message already exists to avoid duplicates
            if (!prevMessages.find(m => m.id === data.message.id)) {
              return [...prevMessages, {
                ...data.message,
                // Set is_sender based on the current user's regno
                is_sender: data.message.sender_id === currentUserRef.current
              }];
            }
            return prevMessages;
          });
          // Auto-scroll for new messages
          setShouldAutoScroll(true);
        }
      });

      // Handle conversation status updates
      const unsubscribeStatusUpdate = websocketService.onConversationStatusUpdate(async (data) => {
        if (data.match_id === conversationData.match.id) {
          // Refresh conversation data to show updated status
          await fetchCurrentConversation();
        }
      });

      // Handle match expiry warnings
      const unsubscribeExpiryWarning = websocketService.onMatchExpiryWarning((data) => {
        if (data.match_id === conversationData.match.id) {
          const minutes = Math.floor(data.time_left_seconds / 60);
          const seconds = data.time_left_seconds % 60;
          toast.warning(`Match expires in ${minutes}m ${seconds}s!`);
        }
      });

      // Handle match expired
      const unsubscribeMatchExpired = websocketService.onMatchExpired((data) => {
        if (data.match_id === conversationData.match.id) {
          toast.error('This match has expired!');
          // Refresh conversation data to show expired status
          fetchCurrentConversation();
        }
      });

      // Cleanup subscriptions
      return () => {
        unsubscribeNewMessage();
        unsubscribeStatusUpdate();
        unsubscribeExpiryWarning();
        unsubscribeMatchExpired();
      };
    }
  }, [conversationData?.conversation.status, conversationData?.match.id]);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  const fetchCurrentConversation = async () => {
    try {
      setLoading(true);
      const response = await getCurrentConversation();
      console.log("ConversationDialog: fetchCurrentConversation response:", response); // Debug log
      if (response.status === 'success') {
        console.log("ConversationDialog: Raw response data:", {
          expires_at: response.match.expires_at,
          created_at: response.match.created_at,
          is_expired: response.match.is_expired,
          current_time: new Date().toISOString()
        }); // Debug log
        setConversationData(response);
        console.log("ConversationDialog: Conversation data set, is_expired:", response.match.is_expired); // Debug log
        updateTimeLeft();
        
        // Load conversation messages if the conversation is accepted
        // This ensures we have the latest messages when opening the dialog
        if (response.conversation.status === 'accepted') {
          await loadConversationMessages(response.match.id);
        }
      } else {
        toast.error(response.message || 'Failed to fetch conversation');
        onClose();
      }
    } catch (error: any) {
      console.error("ConversationDialog: Error fetching conversation:", error); // Debug log
      toast.error(error.response?.data?.detail || 'Failed to fetch conversation');
      onClose();
    } finally {
      setLoading(false);
    }
  };

  const loadConversationMessages = async (matchId: string) => {
    try {
      setLoadingMessages(true);
      const response = await getConversationMessages(matchId);
      if (response.status === 'success') {
        setMessages(response.messages);
        // Update the conversation data with expiry status if it's not already set
        if (response.is_expired !== undefined && conversationData) {
          setConversationData(prev => prev ? {
            ...prev,
            match: {
              ...prev.match,
              is_expired: response.is_expired
            }
          } : null);
        }
        // Enable auto-scroll for initial message load
        setShouldAutoScroll(true);
      } else {
        setMessages([]);
        console.error('Failed to load messages:', response.message);
      }
    } catch (error: any) {
      console.error('Failed to load messages:', error);
      setMessages([]);
    } finally {
      setLoadingMessages(false);
    }
  };

  // Refresh messages without affecting user input
  const refreshMessagesOnly = async (enableAutoScroll = false) => {
    if (!conversationData?.match.id) return;
    
    try {
      console.log('Refreshing messages, enableAutoScroll:', enableAutoScroll);
      // Set auto-scroll based on parameter (enabled when user sends message)
      setShouldAutoScroll(enableAutoScroll);
      
      const response = await getConversationMessages(conversationData.match.id);
      if (response.status === 'success') {
        console.log('Messages refreshed, count:', response.messages.length);
        setMessages(response.messages);
      } else {
        console.error('Failed to refresh messages:', response.message);
      }
    } catch (error: any) {
      console.error('Failed to refresh messages:', error);
    }
  };

  const updateTimeLeft = () => {
    if (!conversationData) return;
    
    console.log("ConversationDialog: updateTimeLeft called, is_expired:", conversationData.match.is_expired); // Debug log
    
    // Check if already expired from backend
    if (conversationData.match.is_expired) {
      setTimeLeft('Expired');
      return;
    }
    
    try {
      // The backend sends UTC timestamps, so we should treat them as UTC
      const now = new Date();
      
      // Parse the UTC timestamp directly without timezone conversion
      // expires_at is already in UTC format from backend
      const expiresUTC = new Date(conversationData.match.expires_at + 'Z').getTime(); // Add 'Z' to ensure UTC interpretation
      const nowUTC = now.getTime();
      
      const difference = expiresUTC - nowUTC;
      
      console.log("ConversationDialog: Time calculation (UTC):", {
        now: now.toISOString(),
        nowUTC: nowUTC,
        expiresAt: conversationData.match.expires_at,
        expiresAtWithZ: conversationData.match.expires_at + 'Z',
        expiresUTC: expiresUTC,
        difference: difference,
        differenceHours: difference / (1000 * 60 * 60)
      }); // Debug log
      
      if (difference <= 0) {
        setTimeLeft('Expired');
        console.log("ConversationDialog: Conversation expired, updating local state"); // Debug log
        // Update the local state to reflect expiry
        setConversationData(prev => prev ? {
          ...prev,
          match: {
            ...prev.match,
            is_expired: true
          }
        } : null);
        // Show expired message
        toast.error("This conversation has expired!");
        // Don't close automatically, let user see the expired state
        return;
      }
      
      const hours = Math.floor(difference / (1000 * 60 * 60));
      const minutes = Math.floor((difference % (1000 * 60 * 60)) / (1000 * 60));
      const seconds = Math.floor((difference % (1000 * 60)) / 1000);
      
      const timeString = `${hours}h ${minutes}m ${seconds}s`;
      console.log("ConversationDialog: Time left calculated:", timeString); // Debug log
      setTimeLeft(timeString);
    } catch (error) {
      console.error('Error calculating time left:', error);
      setTimeLeft('Error');
    }
  };

  const handleAcceptConversation = async () => {
    if (!conversationData) return;
    
    try {
      setAccepting(true);
      await acceptConversation(conversationData.match.id);
      toast.success('Conversation accepted!');
      
      // Refresh conversation data and messages
      await fetchCurrentConversation();
      if (conversationData?.conversation.status === 'accepted') {
        await loadConversationMessages(conversationData.match.id);
      }
      
      onRefresh(); // Refresh parent component
      onConversationAccepted?.(); // Call the new prop callback
    } catch (error: any) {
      toast.error(error.response?.data?.detail || 'Failed to accept conversation');
    } finally {
      setAccepting(false);
    }
  };

  const handleRejectConversation = async () => {
    if (!conversationData) return;
    
    try {
      setRejecting(true);
      await rejectConversation(conversationData.match.id);
      toast.success('Conversation rejected!');
      
      // Refresh conversation data
      await fetchCurrentConversation();
      
      onRefresh(); // Refresh parent component
      onClose(); // Close the dialog
    } catch (error: any) {
      toast.error(error.response?.data?.detail || 'Failed to reject conversation');
    } finally {
      setRejecting(false);
    }
  };

  // Setup WebSocket connection when conversation data is available
  useEffect(() => {
    let connectionCheckInterval: NodeJS.Timeout;

    const setupWebSocket = async () => {
      if (!conversationData?.match.id) return;

      try {
        // Check if token exists
        const token = localStorage.getItem('accessToken');
        if (!token) {
          console.error('No access token found');
          return;
        }

        // Connect to WebSocket
        await websocketService.ensureConnection(conversationData.match.id);
        setIsConnected(true);
        console.log('WebSocket connected successfully');
      } catch (error) {
        console.error('WebSocket connection failed:', error);
        setIsConnected(false);

        // Schedule reconnection
        connectionCheckInterval = setInterval(async () => {
          try {
            await websocketService.ensureConnection(conversationData.match.id);
            setIsConnected(true);
            clearInterval(connectionCheckInterval);
          } catch (error) {
            console.error('Reconnection attempt failed:', error);
          }
        }, 5000);
      }
    };

    setupWebSocket();

    return () => {
      if (connectionCheckInterval) {
        clearInterval(connectionCheckInterval);
      }
      websocketService.disconnect();
    };
  }, [conversationData?.match.id]);

  // Modify handleSendMessage to use ensureConnection
  const handleSendMessage = async () => {
    if (!conversationData || !messageText.trim()) return;
    
    // Check for expiry first
    if (conversationData.match.is_expired) {
      toast.error("Cannot send message: This conversation has expired!");
      return;
    }

    // Ensure WebSocket connection before sending
    try {
      await websocketService.ensureConnection(conversationData.match.id);
      setIsConnected(true);
    } catch (error) {
      console.error('Failed to ensure WebSocket connection:', error);
      toast.error('Connection error. Please try again.');
      return;
    }

    // Rest of sending logic
    try {
        setSending(true);
        const response = await sendMessage(conversationData.match.id, messageText);
        setMessageText('');
        toast.success('Message sent!');
        
        // Refresh only messages immediately after sending to show the new message
        // This is more efficient than refreshing the entire conversation data
        await refreshMessagesOnly(true); // Enable auto-scroll for sent messages
        
        // Notify WebSocket that message was sent (for confirmation)
        if (response.message_id) {
            console.log('Sending WebSocket notification for message:', response.message_id);
            websocketService.sendMessageSentNotification(response.message_id);
        }
    } catch (error: any) {
        console.error("ConversationDialog: Error sending message:", error); // Debug log
        toast.error(error.response?.data?.detail || 'Failed to send message');
    } finally {
        setSending(false);
    }
  };

  const handleClose = () => {
    // If there are unsent messages, ask for confirmation
    if (messageText.trim()) {
      if (window.confirm('You have unsent text. Are you sure you want to close?')) {
        onClose();
      }
    } else {
      onClose();
    }
  };

  const getProfilePictureUrl = (profilePictureId?: string | null) => resolveProfilePictureUrl(profilePictureId ?? null);

  const getStatusBadge = (status: string, isExpired: boolean) => {
    if (isExpired) {
      return <Badge variant="destructive">Expired</Badge>;
    }
    switch (status) {
      case 'pending':
        return <Badge variant="secondary" className="bg-yellow-100 text-yellow-800">Pending</Badge>;
      case 'accepted':
        return <Badge variant="default" className="bg-green-100 text-green-800">Active</Badge>;
      case 'rejected':
        return <Badge variant="destructive">Rejected</Badge>;
      default:
        return <Badge variant="outline">{status}</Badge>;
    }
  };

  if (loading) {
    return (
      <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
        <Card className="w-full max-w-2xl mx-4">
          <CardContent className="p-6">
            <div className="flex items-center justify-center">
              <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
              <span className="ml-2">Loading conversation...</span>
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (!conversationData) {
    return null;
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-2 sm:p-4">
      <Card className="w-full max-w-4xl h-[90vh] sm:h-auto sm:max-h-[90vh] flex flex-col">
        <CardHeader className="pb-3 shrink-0">
          <div className="flex items-center justify-between">
            <div className="flex items-center space-x-3">
              <div className="flex items-center space-x-2">
                <Clock className="h-5 w-5 text-muted-foreground" />
                <span className={`text-sm font-medium ${
                  conversationData.match.is_expired ? 'text-red-600' : 'text-muted-foreground'
                }`}>
                  {conversationData.match.is_expired ? 'Expired' : `Expires in: ${timeLeft}`}
                </span>
              </div>
              {getStatusBadge(conversationData.conversation.status, conversationData.match.is_expired)}
            </div>
            <Button variant="ghost" size="sm" onClick={handleClose}>
              <X className="h-4 w-4" />
            </Button>
          </div>
        </CardHeader>

        <CardContent className="space-y-4 flex-1 overflow-y-auto">
          {/* User Profile - Make it collapsible on mobile */}
          <div className="hidden sm:flex items-center space-x-4 p-4 bg-muted/50 rounded-lg">
            <img
              src={getProfilePictureUrl(conversationData.other_user.profile_picture_id)}
              alt={conversationData.other_user.name}
              className="w-16 h-16 rounded-full object-cover border-2 border-primary"
            />
            <div className="flex-1">
              <h3 className="text-xl font-semibold">{conversationData.other_user.name}</h3>
              <p className="text-sm text-muted-foreground">{conversationData.other_user.which_class}</p>
              {conversationData.other_user.bio && (
                <p className="text-sm mt-1">"{conversationData.other_user.bio}"</p>
              )}
              {conversationData.other_user.interests && (
                <div className="flex flex-wrap gap-1 mt-2">
                  {conversationData.other_user.interests.map((interest, index) => (
                    <Badge key={index} variant="secondary" className="text-xs">
                      {interest}
                    </Badge>
                  ))}
                </div>
              )}
            </div>
          </div>
          
          {/* Mobile Profile Toggle */}
          <div className="sm:hidden">
            <Button
              variant="ghost"
              className="w-full flex items-center justify-between p-2"
              onClick={() => setShowMobileProfile(prev => !prev)}
            >
              <span className="flex items-center">
                <img
                  src={getProfilePictureUrl(conversationData.other_user.profile_picture_id)}
                  alt={conversationData.other_user.name}
                  className="w-8 h-8 rounded-full mr-2"
                />
                <span>{conversationData.other_user.name}</span>
              </span>
              <ChevronDown className={`w-4 h-4 transform transition-transform ${showMobileProfile ? 'rotate-180' : ''}`} />
            </Button>
            
            {showMobileProfile && (
              <div className="p-4 bg-muted/50 rounded-lg mt-2">
                <h3 className="text-lg font-semibold">{conversationData.other_user.name}</h3>
                <p className="text-sm text-muted-foreground">{conversationData.other_user.which_class}</p>
                {conversationData.other_user.bio && (
                  <p className="text-sm mt-1">"{conversationData.other_user.bio}"</p>
                )}
                {conversationData.other_user.interests && (
                  <div className="flex flex-wrap gap-1 mt-2">
                    {conversationData.other_user.interests.map((interest, index) => (
                      <Badge key={index} variant="secondary" className="text-xs">
                        {interest}
                      </Badge>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Messages Section - Adjust height based on viewport */}
          {conversationData.conversation.status === 'accepted' && (
            <div className="space-y-4 flex-1 flex flex-col min-h-0">
              <div className="flex-1 overflow-y-auto">
                {/* Message list with improved spacing */}
                <div className="space-y-3 p-4">
                  {messages.map((message) => (
                    <div
                      key={message.id}
                      className={`flex ${message.is_sender ? 'justify-end' : 'justify-start'} items-end space-x-2`}
                    >
                      {!message.is_sender && (
                        <img
                          src={getProfilePictureUrl(conversationData.other_user.profile_picture_id)}
                          alt=""
                          className="w-6 h-6 rounded-full hidden sm:block"
                        />
                      )}
                      <div
                        className={`max-w-[75%] sm:max-w-[60%] px-3 py-2 rounded-lg ${
                          message.is_sender
                            ? 'bg-pink-500 text-white rounded-br-none'
                            : 'bg-gray-200 dark:bg-gray-700 text-gray-900 dark:text-gray-100 rounded-bl-none'
                        }`}
                      >
                        <p className="text-sm break-words">{message.text}</p>
                        <p className={`text-xs mt-1 ${
                          message.is_sender ? 'text-pink-100' : 'text-gray-500 dark:text-gray-400'
                        }`}>
                          {new Date(message.timestamp).toLocaleTimeString([], {
                            hour: '2-digit',
                            minute: '2-digit'
                          })}
                        </p>
                      </div>
                    </div>
                  ))}
                  <div ref={messagesEndRef} />
                </div>
              </div>

              {/* Input Section - Fixed at bottom */}
              <div className="p-4 border-t bg-background">
                <div className="flex space-x-2">
                  <input
                    type="text"
                    value={messageText}
                    onChange={(e) => setMessageText(e.target.value)}
                    onKeyPress={(e) => e.key === 'Enter' && !e.shiftKey && handleSendMessage()}
                    placeholder={conversationData.match.is_expired ? "Conversation expired" : "Type your message..."}
                    className="flex-1 px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-pink-500
                             disabled:bg-gray-100 disabled:text-gray-500 min-h-[40px] max-h-[120px] resize-none"
                    disabled={conversationData.match.is_expired}
                  />
                  <Button
                    onClick={handleSendMessage}
                    disabled={!messageText.trim() || sending || conversationData.match.is_expired}
                    className={`shrink-0 ${conversationData.match.is_expired ? "bg-gray-400" : ""}`}
                  >
                    {sending ? (
                      <div className="flex items-center">
                        <Loader2 className="w-4 h-4 animate-spin mr-2" />
                        <span>Sending</span>
                      </div>
                    ) : (
                      'Send'
                    )}
                  </Button>
                </div>
              </div>
            </div>
          )}
          
          {/* Pending and Rejected States - Simplified and unified layout */}
          {conversationData.conversation.status !== 'accepted' && (
            <div className="text-center p-6 bg-yellow-50 border border-yellow-200 rounded-lg">
              {conversationData.conversation.status === 'pending' && (
                <>
                  <MessageCircle className="h-12 w-12 text-yellow-500 mx-auto mb-3" />
                  <h3 className="text-lg font-semibold text-yellow-800 mb-2">
                    {conversationData.is_initiator ? 'Waiting for Response' : 'Conversation Request'}
                  </h3>
                  {conversationData.is_initiator ? (
                    <p className="text-yellow-700">
                      You've sent a conversation request to {conversationData.other_user.name}. 
                      They'll need to accept it before you can start chatting.
                    </p>
                  ) : (
                    <div className="space-y-3">
                      <p className="text-yellow-700">
                        {conversationData.other_user.name} wants to start a conversation with you!
                      </p>
                      <div className="flex space-x-3 justify-center">
                        <Button onClick={handleAcceptConversation} className="bg-green-600 hover:bg-green-700" disabled={accepting}>
                          {accepting ? 'Accepting...' : 'Accept Conversation'}
                        </Button>
                        <Button onClick={handleRejectConversation} variant="outline" className="border-red-300 text-red-600 hover:bg-red-50" disabled={rejecting}>
                          {rejecting ? 'Rejecting...' : 'Reject Conversation'}
                        </Button>
                      </div>
                    </div>
                  )}
                </>
              )}

              {conversationData.conversation.status === 'rejected' && (
                <>
                  <X className="h-12 w-12 text-red-500 mx-auto mb-3" />
                  <h3 className="text-lg font-semibold text-red-800 mb-2">Conversation Rejected</h3>
                  <p className="text-red-700">
                    {conversationData.is_initiator 
                      ? `Your conversation request to ${conversationData.other_user.name} was rejected.`
                      : `You rejected the conversation request from ${conversationData.other_user.name}.`
                    }
                  </p>
                </>
              )}
            </div>
          )}

          {/* Match Info - Always visible at the bottom */}
          <div className="text-xs text-muted-foreground text-center border-t pt-4">
            <p>Match created: {new Date(conversationData.match.created_at + 'Z').toLocaleString()}</p>
            {conversationData.conversation.accepted_at && (
              <p>Conversation accepted: {new Date(conversationData.conversation.accepted_at + 'Z').toLocaleString()}</p>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
};

export default ConversationDialog;