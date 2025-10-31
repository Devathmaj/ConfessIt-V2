// Updated ConversationDialog.tsx with WebSocket messaging
import React, { useState, useEffect, useRef } from 'react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Clock, MessageCircle, User, Heart, X, RefreshCw, ChevronDown, Loader2, Copy, Flag, ShieldOff, ShieldCheck, EyeOff, Eye } from 'lucide-react';
import { getCurrentConversation, acceptConversation, rejectConversation, blockConversation as blockConversationAPI, unblockConversation as unblockConversationAPI, getConversationByMatch } from '@/services/api';
import { messagingClient, sendMessage, fetchMessages, reportMessage } from '@/lib/messaging';
import { toast } from 'sonner';
import { resolveProfilePictureUrl } from '@/lib/utils';
import { ReportMessageDialog } from './ReportMessageDialog';
import { BlockUserDialog } from './BlockUserDialog';
import { useAuth } from '@/contexts/AuthContext';
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuTrigger,
} from '@/components/ui/context-menu';

interface ConversationDialogProps {
  onClose: () => void;
  onRefresh: () => void;
  onConversationAccepted?: () => void;
  conversationData?: ConversationData; // Optional initial data
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
  is_blocked?: boolean;
  blocked_by?: string;
}

interface Message {
  id: string;
  text: string;
  sender_id: string;
  receiver_id: string;
  timestamp: string;
  is_sender?: boolean;
}

export const ConversationDialog: React.FC<ConversationDialogProps> = ({ 
  onClose, 
  onRefresh, 
  onConversationAccepted,
  conversationData: initialConversationData 
}) => {
  const { token, user } = useAuth(); // Get token and user from AuthContext
  const [conversationData, setConversationData] = useState<ConversationData | null>(initialConversationData || null);
  const [loading, setLoading] = useState(!initialConversationData);
  const [messageText, setMessageText] = useState('');
  const [messages, setMessages] = useState<Message[]>([]);
  const [timeLeft, setTimeLeft] = useState<string>('');
  const [accepting, setAccepting] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [sending, setSending] = useState(false);
  const [loadingMessages, setLoadingMessages] = useState(false);

  // Prevent body scroll when modal is open
  useEffect(() => {
    // Add overflow-hidden to body
    document.body.style.overflow = 'hidden';
    
    // Cleanup on unmount
    return () => {
      document.body.style.overflow = 'unset';
    };
  }, []);
  const [isConnected, setIsConnected] = useState(false);
  const [showMobileProfile, setShowMobileProfile] = useState(false);
  const [reportDialogOpen, setReportDialogOpen] = useState(false);
  const [selectedMessageForReport, setSelectedMessageForReport] = useState<Message | null>(null);
  const [blockDialogOpen, setBlockDialogOpen] = useState(false);
  const [isBlocked, setIsBlocked] = useState(false);
  const [blockedBy, setBlockedBy] = useState<string | null>(null);
  const [messagesHidden, setMessagesHidden] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const currentUserRef = useRef<string | null>(null);
  const messagesBackupRef = useRef<Message[]>([]); // Backup for messages during state changes
  const unsubscribeMessageRef = useRef<(() => void) | null>(null);
  const unsubscribeErrorRef = useRef<(() => void) | null>(null);

  // Keep backup of messages updated
  useEffect(() => {
    if (messages.length > 0) {
      messagesBackupRef.current = messages;
    }
  }, [messages]);

  useEffect(() => {
    // If initial data was provided, use it; otherwise fetch
    if (initialConversationData) {
      setupConversationData(initialConversationData);
      setLoading(false);
    } else {
      fetchCurrentConversation();
    }
    const interval = setInterval(updateTimeLeft, 1000);
    return () => {
      clearInterval(interval);
      cleanup();
    };
  }, []);

  // Check block status when conversation data changes
  useEffect(() => {
    if (conversationData?.conversation?.id) {
      checkBlockStatus();
    }
  }, [conversationData?.conversation?.id]);

  // Cleanup only on unmount
  useEffect(() => {
    return () => {
      console.log('🧹 Component unmounting - cleaning up');
      cleanup();
    };
  }, []);

  // Initialize WebSocket when conversation is accepted (but not when blocked)
  useEffect(() => {
    // Skip if we don't have conversation data yet
    if (!conversationData) return;

    const shouldInitialize = (
      conversationData.conversation.status === 'accepted' &&
      !conversationData.match.is_expired &&
      !isBlocked
    );

    if (shouldInitialize) {
      console.log('🚀 Initializing messaging (not blocked)');
      initializeMessaging();
    } else if (isBlocked && conversationData.conversation.status === 'accepted') {
      // If blocked, load messages without WebSocket (only if we don't have messages already)
      console.log('⚠️ Blocked state - checking if messages need loading', {
        current_messages: messages.length,
        has_token: !!token
      });
      
      if (token && messages.length === 0) {
        console.log('📥 Loading messages for blocked conversation');
        loadMessagesFromBackend(token).catch(error => {
          console.error('Failed to load messages when blocked:', error);
        });
      } else if (messages.length > 0) {
        console.log('✅ Messages already loaded, keeping them visible');
      }
    }

    // Note: Cleanup is handled separately, not here
    // We don't want to clean up messages when isBlocked changes
  }, [conversationData?.conversation?.id, conversationData?.conversation?.status, isBlocked]);

  const cleanup = () => {
    if (unsubscribeMessageRef.current) {
      unsubscribeMessageRef.current();
      unsubscribeMessageRef.current = null;
    }
    if (unsubscribeErrorRef.current) {
      unsubscribeErrorRef.current();
      unsubscribeErrorRef.current = null;
    }
    messagingClient.disconnect();
  };

  const initializeMessaging = async () => {
    if (!conversationData) return;

    if (!token) {
      console.error('❌ No authentication token found');
      toast.error('Authentication token missing');
      return;
    }

    const conversationId = conversationData.conversation.id;

    console.log('🔍 Initializing messaging:', {
      conversation_id: conversationId,
      has_token: !!token,
      is_blocked: isBlocked
    });

    try {
      // Always load existing messages first
      console.log('⏳ Loading messages...');
      await loadMessagesFromBackend(token);
      console.log('✅ Messages loaded');

      // Only connect WebSocket if not blocked (for real-time updates)
      if (!isBlocked) {
        console.log('⏳ Connecting to WebSocket...');
        await messagingClient.connect(conversationId, token);
        console.log('✅ WebSocket connection promise resolved');

        // Subscribe to new messages
        const unsubscribeMessage = messagingClient.onMessage((newMessage) => {
          console.log('📨 New message received:', newMessage);
          const messageWithFlag = {
            ...newMessage,
            is_sender: newMessage.sender_id === currentUserRef.current
          };
          setMessages((prev) => [...prev, messageWithFlag]);
          scrollToBottom();
        });
        unsubscribeMessageRef.current = unsubscribeMessage;

        // Subscribe to errors
        const unsubscribeError = messagingClient.onError((error) => {
          console.error('WebSocket error:', error);
          toast.error('Real-time connection error');
        });
        unsubscribeErrorRef.current = unsubscribeError;

        console.log('✅ Setting isConnected to true');
        setIsConnected(true);
      } else {
        console.log('⚠️ Conversation blocked - skipping WebSocket connection');
        setIsConnected(false);
      }
    } catch (error) {
      console.error('Failed to initialize messaging:', error);
      // Don't show error toast if just loading messages failed when blocked
      if (!isBlocked) {
        toast.error('Failed to load messages');
      }
      setIsConnected(false);
    }
  };

  const loadMessagesFromBackend = async (token: string) => {
    console.log('📥 loadMessagesFromBackend called:', {
      has_conversationData: !!conversationData,
      conversation_id: conversationData?.conversation?.id,
      has_token: !!token
    });
    
    if (!conversationData?.conversation?.id) {
      console.log('⚠️ No conversation ID, returning early');
      return;
    }

    setLoadingMessages(true);
    try {
      console.log('🔄 Fetching messages for:', conversationData.conversation.id);
      const messages = await fetchMessages(conversationData.conversation.id, token);
      console.log('✅ Messages fetched:', messages.length);
      
      // Add is_sender flag to each message
      const messagesWithFlags = messages.map((msg) => ({
        ...msg,
        is_sender: msg.sender_id === currentUserRef.current
      }));

      setMessages(messagesWithFlags);
      scrollToBottom();
    } catch (error) {
      console.error('❌ Failed to load messages:', error);
      toast.error('Failed to load messages');
      throw error; // Re-throw so caller knows it failed
    } finally {
      setLoadingMessages(false);
    }
  };

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  const setupConversationData = (data: ConversationData) => {
    // Store current user ID from the logged-in user (most reliable)
    // Fall back to conversation data if user not available
    if (user?.Regno) {
      currentUserRef.current = user.Regno;
    } else if (data.is_initiator) {
      currentUserRef.current = data.conversation.initiator_id;
    } else {
      currentUserRef.current = data.conversation.receiver_id;
    }
    
    console.log('👤 Current user set to:', currentUserRef.current, {
      from_auth: user?.Regno,
      is_initiator: data.is_initiator,
      initiator_id: data.conversation.initiator_id,
      receiver_id: data.conversation.receiver_id
    });
    
    // Set block status from the data
    if (data.is_blocked !== undefined) {
      setIsBlocked(data.is_blocked);
      setBlockedBy(data.blocked_by || null);
      
      // Only hide messages if user explicitly chose to hide them (check localStorage)
      // Don't hide by default just because conversation is blocked
      const hideKey = `hide_messages_${data.match.id}`;
      const shouldHide = localStorage.getItem(hideKey) === 'true';
      setMessagesHidden(shouldHide);
    }
    
    // Debug: Log the conversation data to see what we have
    console.log('📦 Setup Conversation Data:', {
      status: data.conversation.status,
      is_initiator: data.is_initiator,
      is_blocked: data.is_blocked,
      blocked_by: data.blocked_by,
      conversation_id: data.conversation.id
    });
    
    setConversationData(data);
  };

  const fetchCurrentConversation = async () => {
    try {
      const response = await getCurrentConversation();
      
      console.log('📡 API Response:', response);
      
      if (response.status === 'success' && response.match) {
        const data = response as ConversationData;
        
        console.log('✅ Conversation Data:', {
          conversation_id: data.conversation.id,
          conversation_status: data.conversation.status
        });
        
        setupConversationData(data);
      } else {
        toast.error('No active conversation found');
        onClose();
      }
    } catch (error) {
      console.error('Failed to fetch conversation:', error);
      toast.error('Failed to load conversation');
      onClose();
    } finally {
      setLoading(false);
    }
  };

  const updateTimeLeft = () => {
    if (!conversationData) return;

    // Use the backend-provided is_expired flag as the source of truth
    if (conversationData.match.is_expired) {
      setTimeLeft('Expired');
      return;
    }

    const now = new Date();
    const expiresAt = new Date(conversationData.match.expires_at);
    const diff = expiresAt.getTime() - now.getTime();

    if (diff <= 0) {
      setTimeLeft('Expired');
      return;
    }

    const hours = Math.floor(diff / (1000 * 60 * 60));
    const minutes = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));
    const seconds = Math.floor((diff % (1000 * 60)) / 1000);

    setTimeLeft(`${hours}h ${minutes}m ${seconds}s`);
  };

  const handleAcceptConversation = async () => {
    if (!conversationData) return;

    setAccepting(true);
    try {
      const response = await acceptConversation(conversationData.match.id);
      
      if (response.status === 'success') {
        toast.success('Conversation accepted!');
        
        // Re-fetch conversation to get updated status
        await fetchCurrentConversation();
        
        if (onConversationAccepted) {
          onConversationAccepted();
        }
        onRefresh();
      } else {
        toast.error('Failed to accept conversation');
      }
    } catch (error) {
      console.error('Error accepting conversation:', error);
      toast.error('Failed to accept conversation');
    } finally {
      setAccepting(false);
    }
  };

  const handleRejectConversation = async () => {
    if (!conversationData) return;

    setRejecting(true);
    try {
      await rejectConversation(conversationData.match.id);
      toast.success('Conversation rejected');
      onRefresh();
      onClose();
    } catch (error) {
      console.error('Error rejecting conversation:', error);
      toast.error('Failed to reject conversation');
    } finally {
      setRejecting(false);
    }
  };

  const handleSendMessage = async () => {
    if (!messageText.trim() || !conversationData) return;
    if (!conversationData.conversation.id) {
      toast.error('Messaging not initialized');
      return;
    }

    if (!token) {
      toast.error('Authentication token missing');
      return;
    }

    const textToSend = messageText.trim();
    setMessageText('');
    setSending(true);

    try {
      await sendMessage(
        conversationData.conversation.id,
        textToSend,
        token
      );

      // Message will appear via real-time WebSocket subscription
      scrollToBottom();
    } catch (error) {
      console.error('Error sending message:', error);
      toast.error('Failed to send message');
      setMessageText(textToSend); // Restore message on error
    } finally {
      setSending(false);
    }
  };

  const handleClose = () => {
    cleanup();
    onClose();
  };

  const handleCopyMessage = (messageText: string) => {
    navigator.clipboard.writeText(messageText)
      .then(() => {
        toast.success('Message copied to clipboard');
      })
      .catch((error) => {
        console.error('Failed to copy message:', error);
        toast.error('Failed to copy message');
      });
  };

  const handleReportMessage = (message: Message) => {
    // Only receivers can report messages (can't report own messages)
    if (message.is_sender) {
      toast.error('You cannot report your own messages');
      return;
    }

    setSelectedMessageForReport(message);
    setReportDialogOpen(true);
  };

  const checkBlockStatus = async () => {
    if (!conversationData?.match?.id) return;

    try {
      // IMPORTANT: Use getConversationByMatch instead of getCurrentConversation
      // This works for both initiators and receivers!
      const response = await getConversationByMatch(conversationData.match.id);
      if (response.status === 'success' && response.is_blocked !== undefined) {
        console.log('✅ Block status checked:', {
          is_blocked: response.is_blocked,
          blocked_by: response.blocked_by
        });
        setIsBlocked(response.is_blocked || false);
        setBlockedBy(response.blocked_by || null);
        
        // Only hide messages if user explicitly chose to hide them (check localStorage)
        // Don't hide by default just because conversation is blocked
        const hideKey = `hide_messages_${conversationData.match.id}`;
        const shouldHide = localStorage.getItem(hideKey) === 'true';
        setMessagesHidden(shouldHide);
      }
    } catch (error) {
      console.error('Error checking block status:', error);
    }
  };

  const handleBlockUser = () => {
    setBlockDialogOpen(true);
  };

  const handleBlockSuccess = async () => {
    if (!conversationData || !currentUserRef.current || !token) return;

    console.log('🔒 Block successful, updating UI...', {
      current_user: currentUserRef.current,
      current_messages_count: messages.length,
      backup_messages_count: messagesBackupRef.current.length,
      conversation_id: conversationData.conversation.id,
      match_id: conversationData.match.id,
      is_initiator: conversationData.is_initiator
    });

    try {
      // Save current messages from backup (more reliable than state)
      const savedMessages = messagesBackupRef.current.length > 0 
        ? [...messagesBackupRef.current] 
        : [...messages];
      console.log('💾 Saved messages for restore:', savedMessages.length);
      
      // Disconnect WebSocket first
      cleanup();
      setIsConnected(false);
      
      // IMPORTANT: Use getConversationByMatch instead of getCurrentConversation
      // This works for both initiators and receivers!
      const response = await getConversationByMatch(conversationData.match.id);
      console.log('📡 Reloaded conversation after block:', response);
      
      if (response.status === 'success') {
        const newConversationData = response as ConversationData;
        
        // Update block state immediately from response
        console.log('🔄 Setting block state:', {
          is_blocked: newConversationData.is_blocked,
          blocked_by: newConversationData.blocked_by
        });
        setIsBlocked(newConversationData.is_blocked || false);
        setBlockedBy(newConversationData.blocked_by || null);
        setMessagesHidden(false);
        
        // Update conversation data
        setConversationData(newConversationData);
        
        // Force immediate UI update with React 18's automatic batching workaround
        setTimeout(() => {
          console.log('🔄 Force updating block state after timeout');
          setIsBlocked(newConversationData.is_blocked || false);
          setBlockedBy(newConversationData.blocked_by || null);
          
          // Restore messages
          if (savedMessages.length > 0) {
            console.log('🔄 Restoring messages after state update:', savedMessages.length);
            setMessages(savedMessages);
            messagesBackupRef.current = savedMessages;
          } else {
            // If there were no messages, try to load them
            console.log('📥 No saved messages, loading from backend...');
            fetchMessages(newConversationData.conversation.id, token)
              .then(fetchedMessages => {
                const messagesWithFlags = fetchedMessages.map((msg) => ({
                  ...msg,
                  is_sender: msg.sender_id === currentUserRef.current
                }));
                setMessages(messagesWithFlags);
                messagesBackupRef.current = messagesWithFlags;
                console.log('✅ Messages loaded:', fetchedMessages.length);
              })
              .catch(error => {
                console.error('Failed to load messages:', error);
              });
          }
        }, 50); // Reduced timeout for faster UI update
        
        toast.success('User blocked successfully');
      }
    } catch (error) {
      console.error('❌ Error reloading after block:', error);
      toast.error('Block successful but failed to refresh data');
    }
    
    onRefresh(); // Refresh parent
  };

  const handleUnblockUser = async () => {
    if (!conversationData?.match?.id || !currentUserRef.current) return;

    try {
      await unblockConversationAPI(conversationData.match.id);
      toast.success('User has been unblocked');
      await checkBlockStatus();
      setMessagesHidden(false);
      
      // Clear hide preference from localStorage
      const hideKey = `hide_messages_${conversationData.match.id}`;
      localStorage.removeItem(hideKey);
      
      // Reinitialize WebSocket connection
      if (conversationData.conversation.status === 'accepted' && !conversationData.match.is_expired) {
        await initializeMessaging();
      }
      
      onRefresh(); // Refresh parent
    } catch (error: any) {
      console.error('Error unblocking user:', error);
      if (error.response?.data?.detail?.includes('only unblock if you were')) {
        toast.error('Only the person who blocked can unblock');
      } else {
        toast.error('Failed to unblock user');
      }
    }
  };

  const toggleMessagesVisibility = () => {
    if (!conversationData?.match?.id) return;
    
    const newHiddenState = !messagesHidden;
    setMessagesHidden(newHiddenState);
    
    // Store preference in localStorage (per-user, per-conversation)
    const hideKey = `hide_messages_${conversationData.match.id}`;
    if (newHiddenState) {
      localStorage.setItem(hideKey, 'true');
    } else {
      localStorage.removeItem(hideKey);
    }
  };

  const getProfilePictureUrl = (profilePictureId?: string | null) => resolveProfilePictureUrl(profilePictureId ?? null);

  const getStatusBadge = (status: string, isExpired: boolean) => {
    if (isExpired) {
      return <Badge variant="destructive">Expired</Badge>;
    }

    switch (status) {
      case 'pending':
        return <Badge variant="secondary">Pending</Badge>;
      case 'accepted':
        return <Badge variant="default" className="bg-green-500">Active</Badge>;
      case 'rejected':
        return <Badge variant="destructive">Rejected</Badge>;
      default:
        return <Badge variant="outline">Unknown</Badge>;
    }
  };

  if (loading) {
    return (
      <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-2 sm:p-4">
        <Card className="w-full max-w-4xl">
          <CardContent className="flex items-center justify-center p-6 sm:p-8">
            <Loader2 className="h-8 w-8 animate-spin" />
          </CardContent>
        </Card>
      </div>
    );
  }

  if (!conversationData) {
    return null;
  }

  const isExpired = conversationData.match.is_expired;
  const isPending = conversationData.conversation.status === 'pending';
  const isRequested = conversationData.conversation.status === 'requested';
  const isAccepted = conversationData.conversation.status === 'accepted';
  const isRejected = conversationData.conversation.status === 'rejected';
  const isReceiver = !conversationData.is_initiator;
  const canChat = isAccepted && !isExpired;

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-2 sm:p-4 touch-none">
      <Card className="w-full max-w-6xl h-[100vh] sm:h-[90vh] flex flex-col overflow-hidden">
        <CardHeader className="border-b flex-shrink-0 p-3 sm:p-6">
          <div className="flex justify-between items-start gap-2">
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 sm:gap-3 mb-2">
                <img
                  src={getProfilePictureUrl(conversationData.other_user.profile_picture_id)}
                  alt={conversationData.other_user.name}
                  className="w-10 h-10 sm:w-12 sm:h-12 rounded-full object-cover flex-shrink-0"
                />
                <div className="min-w-0 flex-1">
                  <CardTitle className="text-base sm:text-xl truncate">{conversationData.other_user.name}</CardTitle>
                  <CardDescription className="text-xs sm:text-sm truncate">@{conversationData.other_user.username} • {conversationData.other_user.which_class}</CardDescription>
                </div>
                <div className="hidden sm:block flex-shrink-0">
                  {getStatusBadge(conversationData.conversation.status, isExpired)}
                </div>
              </div>

              {/* Mobile status badge */}
              <div className="sm:hidden mb-2">
                {getStatusBadge(conversationData.conversation.status, isExpired)}
              </div>

              <div className="flex flex-wrap items-center gap-2 sm:gap-4 text-xs sm:text-sm text-muted-foreground">
                <div className="flex items-center gap-1 flex-shrink-0">
                  <Clock className="h-3 w-3 sm:h-4 sm:w-4" />
                  <span className="whitespace-nowrap">{timeLeft}</span>
                </div>
                {isAccepted && (
                  <div className="flex items-center gap-1 flex-shrink-0">
                    <MessageCircle className="h-3 w-3 sm:h-4 sm:w-4" />
                    <span className="whitespace-nowrap">{messages.length} msgs</span>
                  </div>
                )}
                {isConnected && (
                  <Badge variant="outline" className="bg-green-50 text-green-700 border-green-300 text-xs px-2 py-0 flex-shrink-0">
                    ● Connected
                  </Badge>
                )}
                {isBlocked && (
                  <Badge variant="destructive" className="flex items-center gap-1 text-xs px-2 py-0 flex-shrink-0">
                    <ShieldOff className="h-3 w-3" />
                    Blocked
                  </Badge>
                )}
              </div>

              {/* Block/Unblock Actions */}
              {isAccepted && !isExpired && (
                <div className="flex flex-wrap gap-2 mt-3">
                  {!isBlocked ? (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={handleBlockUser}
                      className="text-red-600 hover:text-red-700 hover:bg-red-50 text-xs sm:text-sm h-8"
                    >
                      <ShieldOff className="h-3 w-3 sm:h-4 sm:w-4 mr-1 sm:mr-2" />
                      Block User
                    </Button>
                  ) : (
                    <>
                      {blockedBy === currentUserRef.current && (
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={handleUnblockUser}
                          className="text-green-600 hover:text-green-700 hover:bg-green-50 text-xs sm:text-sm h-8"
                        >
                          <ShieldCheck className="h-3 w-3 sm:h-4 sm:w-4 mr-1 sm:mr-2" />
                          Unblock
                        </Button>
                      )}
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={toggleMessagesVisibility}
                        className="text-xs sm:text-sm h-8"
                      >
                        {messagesHidden ? (
                          <>
                            <Eye className="h-3 w-3 sm:h-4 sm:w-4 mr-1 sm:mr-2" />
                            <span className="hidden sm:inline">Show Messages</span>
                            <span className="sm:hidden">Show</span>
                          </>
                        ) : (
                          <>
                            <EyeOff className="h-3 w-3 sm:h-4 sm:w-4 mr-1 sm:mr-2" />
                            <span className="hidden sm:inline">Hide Messages</span>
                            <span className="sm:hidden">Hide</span>
                          </>
                        )}
                      </Button>
                    </>
                  )}
                </div>
              )}
            </div>

            <Button variant="ghost" size="icon" onClick={handleClose} className="flex-shrink-0 h-8 w-8 sm:h-10 sm:w-10">
              <X className="h-4 w-4 sm:h-5 sm:w-5" />
            </Button>
          </div>
        </CardHeader>

        <CardContent className="flex-1 overflow-hidden p-0 flex">
          {/* Main Chat Area */}
          <div className="flex-1 flex flex-col">
            {/* Blocked Warning */}
            {isBlocked && (
              <div className="bg-red-50 border-b border-red-200 p-3 sm:p-4">
                <div className="flex items-center gap-2 text-red-900">
                  <ShieldOff className="h-4 w-4 sm:h-5 sm:w-5 flex-shrink-0" />
                  <div className="min-w-0">
                    <p className="font-semibold text-sm sm:text-base">This conversation is blocked</p>
                    <p className="text-xs sm:text-sm">
                      {blockedBy === currentUserRef.current 
                        ? 'You blocked this user. They have been notified.' 
                        : `${conversationData.other_user.name} blocked this conversation.`}
                    </p>
                  </div>
                </div>
              </div>
            )}

            {/* Messages Area */}
            <div className="flex-1 overflow-y-auto p-3 sm:p-4 space-y-3 sm:space-y-4">
              {isRequested && isReceiver && (
                <div className="bg-blue-50 border border-blue-200 rounded-lg p-3 sm:p-4 mb-4">
                  <p className="text-xs sm:text-sm text-blue-900 mb-3">
                    <strong>{conversationData.other_user.name}</strong> wants to start a conversation with you!
                  </p>
                  <div className="flex gap-2">
                    <Button
                      onClick={handleAcceptConversation}
                      disabled={accepting}
                      className="flex-1 text-sm h-9"
                    >
                      {accepting ? <Loader2 className="h-4 w-4 animate-spin mr-2" /> : <Heart className="h-4 w-4 mr-2" />}
                      Accept
                    </Button>
                    <Button
                      onClick={handleRejectConversation}
                      variant="outline"
                      disabled={rejecting}
                      className="flex-1 text-sm h-9"
                    >
                      {rejecting ? <Loader2 className="h-4 w-4 animate-spin mr-2" /> : <X className="h-4 w-4 mr-2" />}
                      Reject
                    </Button>
                  </div>
                </div>
              )}

              {isRequested && conversationData.is_initiator && (
                <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-3 sm:p-4 mb-4 text-center">
                  <p className="text-xs sm:text-sm text-yellow-900">
                    Waiting for <strong>{conversationData.other_user.name}</strong> to accept your conversation request...
                  </p>
                </div>
              )}

              {isPending && (
                <div className="bg-gray-50 border border-gray-200 rounded-lg p-3 sm:p-4 mb-4 text-center">
                  <p className="text-xs sm:text-sm text-gray-900">
                    Conversation not yet requested. Click "Send Message Request" to initiate.
                  </p>
                </div>
              )}

              {isRejected && (
                <div className="bg-red-50 border border-red-200 rounded-lg p-3 sm:p-4 mb-4 text-center">
                  <p className="text-xs sm:text-sm text-red-900">
                    This conversation request was rejected.
                  </p>
                </div>
              )}

              {isExpired && (
                <div className="bg-gray-50 border border-gray-200 rounded-lg p-3 sm:p-4 mb-4 text-center">
                  <p className="text-xs sm:text-sm text-gray-900">
                    This conversation has expired. Messages are read-only.
                  </p>
                </div>
              )}

              {loadingMessages && (
                <div className="flex justify-center">
                  <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
                </div>
              )}

              {isAccepted && !messagesHidden && messages.map((message) => (
                <ContextMenu key={message.id}>
                  <ContextMenuTrigger>
                    <div
                      className={`flex ${message.is_sender ? 'justify-end' : 'justify-start'}`}
                    >
                      <div
                        className={`max-w-[85%] sm:max-w-[70%] rounded-lg p-2 sm:p-3 cursor-pointer ${
                          message.is_sender
                            ? 'bg-primary text-primary-foreground'
                            : 'bg-muted'
                        }`}
                      >
                        <p className="text-xs sm:text-sm break-words">{message.text}</p>
                        <p className={`text-[10px] sm:text-xs mt-1 ${message.is_sender ? 'text-primary-foreground/70' : 'text-muted-foreground'}`}>
                          {new Date(message.timestamp).toLocaleTimeString()}
                        </p>
                      </div>
                    </div>
                  </ContextMenuTrigger>
                  <ContextMenuContent>
                    <ContextMenuItem onClick={() => handleCopyMessage(message.text)}>
                      <Copy className="mr-2 h-4 w-4" />
                      Copy
                    </ContextMenuItem>
                    {!message.is_sender && !isBlocked && (
                      <ContextMenuItem 
                        onClick={() => handleReportMessage(message)}
                        className="text-red-600 focus:text-red-600"
                      >
                        <Flag className="mr-2 h-4 w-4" />
                        Report
                      </ContextMenuItem>
                    )}
                  </ContextMenuContent>
                </ContextMenu>
              ))}

              {messagesHidden && (
                <div className="flex items-center justify-center h-full text-muted-foreground">
                  <div className="text-center">
                    <EyeOff className="h-10 w-10 sm:h-12 sm:w-12 mx-auto mb-2 opacity-50" />
                    <p className="text-sm sm:text-base">Messages are hidden</p>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={toggleMessagesVisibility}
                      className="mt-2 text-xs sm:text-sm h-8"
                    >
                      <Eye className="h-3 w-3 sm:h-4 sm:w-4 mr-2" />
                      Show Messages
                    </Button>
                  </div>
                </div>
              )}

              <div ref={messagesEndRef} />
            </div>

            {/* Message Input */}
            {canChat && !isBlocked && (
              <div className="border-t p-2 sm:p-4 flex gap-2">
                {/* Debug: Log states */}
                {console.log('🔍 Message Input States:', { canChat, isBlocked, sending, isConnected })}
                <input
                  type="text"
                  value={messageText}
                  onChange={(e) => setMessageText(e.target.value)}
                  onKeyPress={(e) => e.key === 'Enter' && handleSendMessage()}
                  placeholder="Type a message..."
                  className="flex-1 px-3 sm:px-4 py-2 text-sm sm:text-base border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary min-w-0"
                  disabled={sending || !isConnected}
                />
                <Button
                  onClick={handleSendMessage}
                  disabled={!messageText.trim() || sending || !isConnected}
                  className="flex-shrink-0 px-3 sm:px-4 h-9 sm:h-10 text-sm"
                >
                  {sending ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Send'}
                </Button>
              </div>
            )}

            {/* Blocked State Message */}
            {isBlocked && canChat && (
              <div className="border-t p-3 sm:p-4 bg-red-50">
                <div className="flex items-center justify-center gap-2 text-red-900">
                  <ShieldOff className="h-4 w-4 sm:h-5 sm:w-5" />
                  <p className="font-medium text-xs sm:text-base">This conversation is blocked</p>
                </div>
              </div>
            )}
          </div>

          {/* Profile Sidebar (Desktop) */}
          <div className="hidden lg:block w-80 border-l p-4 overflow-y-auto">
            <h3 className="font-semibold mb-3">Profile</h3>
            <div className="space-y-4">
              <div>
                <p className="text-sm text-muted-foreground mb-1">Bio</p>
                <p className="text-sm">{conversationData.other_user.bio || 'No bio available'}</p>
              </div>

              {conversationData.other_user.interests && conversationData.other_user.interests.length > 0 && (
                <div>
                  <p className="text-sm text-muted-foreground mb-2">Interests</p>
                  <div className="flex flex-wrap gap-2">
                    {conversationData.other_user.interests.map((interest, index) => (
                      <Badge key={index} variant="secondary">{interest}</Badge>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Report Message Dialog */}
      {selectedMessageForReport && conversationData && (
        <ReportMessageDialog
          isOpen={reportDialogOpen}
          onClose={() => {
            setReportDialogOpen(false);
            setSelectedMessageForReport(null);
          }}
          messageId={selectedMessageForReport.id}
          messageText={selectedMessageForReport.text}
          reporterId={currentUserRef.current!}
          reportedUserId={conversationData.is_initiator 
            ? conversationData.conversation.receiver_id 
            : conversationData.conversation.initiator_id}
          conversationId={conversationData.conversation.id}
        />
      )}

      {/* Block User Dialog */}
      {conversationData && (
        <BlockUserDialog
          isOpen={blockDialogOpen}
          onClose={() => setBlockDialogOpen(false)}
          matchId={conversationData.match.id}
          otherUserName={conversationData.other_user.name}
          onBlockSuccess={handleBlockSuccess}
        />
      )}
    </div>
  );
};

export default ConversationDialog;
