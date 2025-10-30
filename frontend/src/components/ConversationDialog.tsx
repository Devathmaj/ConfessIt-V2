// Updated ConversationDialog.tsx with Supabase integration
import React, { useState, useEffect, useRef } from 'react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Clock, MessageCircle, User, Heart, X, RefreshCw, ChevronDown, Loader2, Copy, Flag, ShieldOff, ShieldCheck, EyeOff, Eye } from 'lucide-react';
import { getCurrentConversation, acceptConversation, rejectConversation, blockConversation as blockConversationAPI, unblockConversation as unblockConversationAPI } from '@/services/api';
import { initSupabaseClient, subscribeToMessages, fetchMessages, sendMessage as sendSupabaseMessage, disconnectSupabase } from '@/lib/supabase';
import { RealtimeChannel } from '@supabase/supabase-js';
import { toast } from 'sonner';
import { resolveProfilePictureUrl } from '@/lib/utils';
import { ReportMessageDialog } from './ReportMessageDialog';
import { BlockUserDialog } from './BlockUserDialog';
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
  supabase_token?: string;
  supabase_anon_key?: string;
  conversation_id_supabase?: string;
  supabase_url?: string;
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
  const [conversationData, setConversationData] = useState<ConversationData | null>(initialConversationData || null);
  const [loading, setLoading] = useState(!initialConversationData);
  const [messageText, setMessageText] = useState('');
  const [messages, setMessages] = useState<Message[]>([]);
  const [timeLeft, setTimeLeft] = useState<string>('');
  const [accepting, setAccepting] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [sending, setSending] = useState(false);
  const [loadingMessages, setLoadingMessages] = useState(false);
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
  const supabaseChannelRef = useRef<RealtimeChannel | null>(null);

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
    if (conversationData?.conversation_id_supabase) {
      checkBlockStatus();
    }
  }, [conversationData?.conversation_id_supabase]);

  // Initialize Supabase when conversation is accepted
  useEffect(() => {
    if (
      conversationData &&
      conversationData.conversation.status === 'accepted' &&
      !conversationData.match.is_expired &&
      conversationData.supabase_token &&
      conversationData.conversation_id_supabase &&
      conversationData.supabase_url &&
      !isBlocked  // Don't initialize if blocked
    ) {
      initializeSupabase();
    }

    return () => {
      cleanup();
    };
  }, [conversationData?.conversation.status, conversationData?.supabase_token, isBlocked]);

  const cleanup = () => {
    if (supabaseChannelRef.current) {
      supabaseChannelRef.current.unsubscribe();
      supabaseChannelRef.current = null;
    }
    disconnectSupabase();
  };

  const initializeSupabase = async () => {
    if (!conversationData) return;

    const { supabase_token, supabase_anon_key, conversation_id_supabase, supabase_url } = conversationData;

    console.log('🔍 Supabase Config:', {
      has_token: !!supabase_token,
      has_anon_key: !!supabase_anon_key,
      token_preview: supabase_token?.substring(0, 50) + '...',
      conversation_id: conversation_id_supabase,
      supabase_url
    });

    if (!supabase_token || !supabase_anon_key || !conversation_id_supabase || !supabase_url) {
      console.error('❌ Missing Supabase configuration:', {
        has_token: !!supabase_token,
        has_anon_key: !!supabase_anon_key,
        has_conversation_id: !!conversation_id_supabase,
        has_url: !!supabase_url
      });
      return;
    }

    try {
      // Initialize Supabase client
      await initSupabaseClient({
        supabaseUrl: supabase_url,
        supabaseAnonKey: supabase_anon_key,
        supabaseToken: supabase_token,
        conversationId: conversation_id_supabase
      });

      // Load existing messages
      await loadMessagesFromSupabase();

      // Subscribe to new messages
      const channel = subscribeToMessages(
        conversation_id_supabase,
        (newMessage) => {
          // Add is_sender flag based on current user
          const messageWithFlag = {
            ...newMessage,
            is_sender: newMessage.sender_id === currentUserRef.current
          };
          setMessages((prev) => [...prev, messageWithFlag]);
          scrollToBottom();
        },
        (error) => {
          console.error('Supabase subscription error:', error);
          toast.error('Real-time connection error');
        }
      );

      supabaseChannelRef.current = channel;
      setIsConnected(true);
      toast.success('Connected to real-time messaging');
    } catch (error) {
      console.error('Failed to initialize Supabase:', error);
      toast.error('Failed to connect to messaging service');
      setIsConnected(false);
    }
  };

  const loadMessagesFromSupabase = async () => {
    if (!conversationData?.conversation_id_supabase) return;

    setLoadingMessages(true);
    try {
      const supabaseMessages = await fetchMessages(conversationData.conversation_id_supabase);
      
      // Add is_sender flag to each message
      const messagesWithFlags = supabaseMessages.map((msg) => ({
        ...msg,
        is_sender: msg.sender_id === currentUserRef.current
      }));

      setMessages(messagesWithFlags);
      scrollToBottom();
    } catch (error) {
      console.error('Failed to load messages:', error);
      toast.error('Failed to load messages');
    } finally {
      setLoadingMessages(false);
    }
  };

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  const setupConversationData = (data: ConversationData) => {
    // Store current user ID for determining is_sender
    if (data.is_initiator) {
      currentUserRef.current = data.conversation.initiator_id;
    } else {
      currentUserRef.current = data.conversation.receiver_id;
    }
    
    // Set block status from the data
    if (data.is_blocked !== undefined) {
      setIsBlocked(data.is_blocked);
      setBlockedBy(data.blocked_by || null);
      
      // If blocked and user is not the blocker, check localStorage for hide preference
      if (data.is_blocked && data.blocked_by !== currentUserRef.current) {
        const hideKey = `hide_messages_${data.match.id}`;
        const shouldHide = localStorage.getItem(hideKey) === 'true';
        setMessagesHidden(shouldHide);
      }
    }
    
    // Debug: Log the conversation data to see what we have
    console.log('📦 Setup Conversation Data:', {
      status: data.conversation.status,
      is_initiator: data.is_initiator,
      is_blocked: data.is_blocked,
      blocked_by: data.blocked_by,
      has_supabase_token: !!data.supabase_token,
      has_anon_key: !!data.supabase_anon_key,
      has_conversation_id: !!data.conversation_id_supabase,
      has_supabase_url: !!data.supabase_url,
      supabase_token_preview: data.supabase_token?.substring(0, 50),
      conversation_id: data.conversation_id_supabase
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
          has_supabase_token: !!data.supabase_token,
          has_anon_key: !!data.supabase_anon_key,
          has_conversation_id: !!data.conversation_id_supabase,
          has_supabase_url: !!data.supabase_url,
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
        
        // Re-fetch conversation to get Supabase token
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
    if (!conversationData.conversation_id_supabase) {
      toast.error('Messaging not initialized');
      return;
    }

    const textToSend = messageText.trim();
    setMessageText('');
    setSending(true);

    try {
      const otherUserId = conversationData.is_initiator
        ? conversationData.conversation.receiver_id
        : conversationData.conversation.initiator_id;

      await sendSupabaseMessage(
        conversationData.conversation_id_supabase,
        currentUserRef.current!,
        otherUserId,
        textToSend
      );

      // Message will appear via real-time subscription
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
      // Fetch conversation data from backend which includes block status
      const response = await getCurrentConversation();
      if (response.status === 'success' && response.is_blocked !== undefined) {
        setIsBlocked(response.is_blocked || false);
        setBlockedBy(response.blocked_by || null);
        
        // If blocked and user is not the blocker, check if messages should be hidden from localStorage
        if (response.is_blocked && response.blocked_by !== currentUserRef.current) {
          const hideKey = `hide_messages_${conversationData.match.id}`;
          const shouldHide = localStorage.getItem(hideKey) === 'true';
          setMessagesHidden(shouldHide);
        }
      }
    } catch (error) {
      console.error('Error checking block status:', error);
    }
  };

  const handleBlockUser = () => {
    setBlockDialogOpen(true);
  };

  const handleBlockSuccess = async () => {
    await checkBlockStatus();
    setMessages([]); // Clear messages
    cleanup(); // Disconnect Supabase
    toast.info('User has been notified that you blocked them');
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
      
      // Reinitialize Supabase connection
      if (conversationData.conversation.status === 'accepted' && !conversationData.match.is_expired) {
        await initializeSupabase();
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
      <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
        <Card className="w-full max-w-4xl">
          <CardContent className="flex items-center justify-center p-8">
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
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <Card className="w-full max-w-6xl h-[90vh] flex flex-col">
        <CardHeader className="border-b flex-shrink-0">
          <div className="flex justify-between items-start">
            <div className="flex-1">
              <div className="flex items-center gap-3 mb-2">
                <img
                  src={getProfilePictureUrl(conversationData.other_user.profile_picture_id)}
                  alt={conversationData.other_user.name}
                  className="w-12 h-12 rounded-full object-cover"
                />
                <div>
                  <CardTitle className="text-xl">{conversationData.other_user.name}</CardTitle>
                  <CardDescription>@{conversationData.other_user.username} • {conversationData.other_user.which_class}</CardDescription>
                </div>
                {getStatusBadge(conversationData.conversation.status, isExpired)}
              </div>

              <div className="flex items-center gap-4 text-sm text-muted-foreground">
                <div className="flex items-center gap-1">
                  <Clock className="h-4 w-4" />
                  <span>{timeLeft}</span>
                </div>
                {isAccepted && (
                  <div className="flex items-center gap-1">
                    <MessageCircle className="h-4 w-4" />
                    <span>{messages.length} messages</span>
                  </div>
                )}
                {isConnected && (
                  <Badge variant="outline" className="bg-green-50 text-green-700 border-green-300">
                    ● Connected
                  </Badge>
                )}
                {isBlocked && (
                  <Badge variant="destructive" className="flex items-center gap-1">
                    <ShieldOff className="h-3 w-3" />
                    Blocked
                  </Badge>
                )}
              </div>

              {/* Block/Unblock Actions */}
              {isAccepted && !isExpired && (
                <div className="flex gap-2 mt-3">
                  {!isBlocked ? (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={handleBlockUser}
                      className="text-red-600 hover:text-red-700 hover:bg-red-50"
                    >
                      <ShieldOff className="h-4 w-4 mr-2" />
                      Block User
                    </Button>
                  ) : (
                    <>
                      {blockedBy === currentUserRef.current && (
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={handleUnblockUser}
                          className="text-green-600 hover:text-green-700 hover:bg-green-50"
                        >
                          <ShieldCheck className="h-4 w-4 mr-2" />
                          Unblock
                        </Button>
                      )}
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={toggleMessagesVisibility}
                      >
                        {messagesHidden ? (
                          <>
                            <Eye className="h-4 w-4 mr-2" />
                            Show Messages
                          </>
                        ) : (
                          <>
                            <EyeOff className="h-4 w-4 mr-2" />
                            Hide Messages
                          </>
                        )}
                      </Button>
                    </>
                  )}
                </div>
              )}
            </div>

            <Button variant="ghost" size="icon" onClick={handleClose}>
              <X className="h-5 w-5" />
            </Button>
          </div>
        </CardHeader>

        <CardContent className="flex-1 overflow-hidden p-0 flex">
          {/* Main Chat Area */}
          <div className="flex-1 flex flex-col">
            {/* Blocked Warning */}
            {isBlocked && (
              <div className="bg-red-50 border-b border-red-200 p-4">
                <div className="flex items-center gap-2 text-red-900">
                  <ShieldOff className="h-5 w-5" />
                  <div>
                    <p className="font-semibold">This conversation is blocked</p>
                    <p className="text-sm">
                      {blockedBy === currentUserRef.current 
                        ? 'You blocked this user. They have been notified.' 
                        : `${conversationData.other_user.name} blocked this conversation.`}
                    </p>
                  </div>
                </div>
              </div>
            )}

            {/* Messages Area */}
            <div className="flex-1 overflow-y-auto p-4 space-y-4">
              {isRequested && isReceiver && (
                <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 mb-4">
                  <p className="text-sm text-blue-900 mb-3">
                    <strong>{conversationData.other_user.name}</strong> wants to start a conversation with you!
                  </p>
                  <div className="flex gap-2">
                    <Button
                      onClick={handleAcceptConversation}
                      disabled={accepting}
                      className="flex-1"
                    >
                      {accepting ? <Loader2 className="h-4 w-4 animate-spin mr-2" /> : <Heart className="h-4 w-4 mr-2" />}
                      Accept
                    </Button>
                    <Button
                      onClick={handleRejectConversation}
                      variant="outline"
                      disabled={rejecting}
                      className="flex-1"
                    >
                      {rejecting ? <Loader2 className="h-4 w-4 animate-spin mr-2" /> : <X className="h-4 w-4 mr-2" />}
                      Reject
                    </Button>
                  </div>
                </div>
              )}

              {isRequested && conversationData.is_initiator && (
                <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-4 mb-4 text-center">
                  <p className="text-sm text-yellow-900">
                    Waiting for <strong>{conversationData.other_user.name}</strong> to accept your conversation request...
                  </p>
                </div>
              )}

              {isPending && (
                <div className="bg-gray-50 border border-gray-200 rounded-lg p-4 mb-4 text-center">
                  <p className="text-sm text-gray-900">
                    Conversation not yet requested. Click "Send Message Request" to initiate.
                  </p>
                </div>
              )}

              {isRejected && (
                <div className="bg-red-50 border border-red-200 rounded-lg p-4 mb-4 text-center">
                  <p className="text-sm text-red-900">
                    This conversation request was rejected.
                  </p>
                </div>
              )}

              {isExpired && (
                <div className="bg-gray-50 border border-gray-200 rounded-lg p-4 mb-4 text-center">
                  <p className="text-sm text-gray-900">
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
                        className={`max-w-[70%] rounded-lg p-3 cursor-pointer ${
                          message.is_sender
                            ? 'bg-primary text-primary-foreground'
                            : 'bg-muted'
                        }`}
                      >
                        <p className="text-sm">{message.text}</p>
                        <p className={`text-xs mt-1 ${message.is_sender ? 'text-primary-foreground/70' : 'text-muted-foreground'}`}>
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
                    <EyeOff className="h-12 w-12 mx-auto mb-2 opacity-50" />
                    <p>Messages are hidden</p>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={toggleMessagesVisibility}
                      className="mt-2"
                    >
                      <Eye className="h-4 w-4 mr-2" />
                      Show Messages
                    </Button>
                  </div>
                </div>
              )}

              <div ref={messagesEndRef} />
            </div>

            {/* Message Input */}
            {canChat && !isBlocked && (
              <div className="border-t p-4 flex gap-2">
                <input
                  type="text"
                  value={messageText}
                  onChange={(e) => setMessageText(e.target.value)}
                  onKeyPress={(e) => e.key === 'Enter' && handleSendMessage()}
                  placeholder="Type a message..."
                  className="flex-1 px-4 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary"
                  disabled={sending || !isConnected}
                />
                <Button
                  onClick={handleSendMessage}
                  disabled={!messageText.trim() || sending || !isConnected}
                >
                  {sending ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Send'}
                </Button>
              </div>
            )}

            {/* Blocked State Message */}
            {isBlocked && canChat && (
              <div className="border-t p-4 bg-red-50">
                <div className="flex items-center justify-center gap-2 text-red-900">
                  <ShieldOff className="h-5 w-5" />
                  <p className="font-medium">This conversation is blocked</p>
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
          conversationId={conversationData.conversation_id_supabase!}
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
