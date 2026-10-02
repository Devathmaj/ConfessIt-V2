// Updated ConversationDialog.tsx with WebSocket messaging
import React, { useState, useEffect, useRef } from 'react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Clock, MessageCircle, User, Heart, X, RefreshCw, ChevronDown, Loader2, Copy, Flag, ShieldOff, ShieldCheck, EyeOff, Eye } from 'lucide-react';
import { getCurrentConversation, acceptConversation, rejectConversation, blockConversation as blockConversationAPI, unblockConversation as unblockConversationAPI, getConversationByMatch } from '@/services/api';
import { messagingClient, sendMessage, fetchMessages, reportMessage } from '@/lib/messaging';
import { MESSAGE_BODY_MAX } from '@/lib/limits';
import {
  canSendAnotherMessage,
  emptyMessageQuota,
  messageLimitNotice,
  messageQuotaLabel,
  quotaAfterIncomingMessage,
} from '@/lib/messageQuota';
import type { MessageQuota } from '@/lib/messageQuota';
import { toast } from 'sonner';
import { parseTimestamp, timestampMs } from '@/lib/utils';
import { cohortLabel } from '@/lib/accountFields';
import { ReportMessageDialog } from './ReportMessageDialog';
import { ProfileAvatar } from '@/components/ProfileAvatar';
import { BlockUserDialog } from './BlockUserDialog';
import { useAuth } from '@/contexts/AuthContext';
import { serverNow } from '@/lib/serverClock';
import { describeApiError } from '@/lib/apiError';
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

/**
 * Every status the API can report.
 *
 * `terminated` is a moderator closing the thread. It is not a stage the conversation can
 * move on from, so it is treated as finished everywhere below: the history is still loaded
 * and shown, and nothing about it is live.
 */
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
  is_blocked?: boolean;
  blocked_by?: string;
}

interface Message {
  id: string;
  body: string;
  sender_regno: string;
  receiver_regno: string;
  created_at: string;
  is_sender?: boolean;
}

/**
 * How many messages one thread read returns. A conversation caps at 200, so thirty
 * opens on the newest window with room to grow upwards in a handful of clicks, and no
 * read ever needs to consider more than the ceiling allows.
 */
const THREAD_PAGE_SIZE = 30;

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
  /**
   * The thread on screen: always the contiguous newest-anchored window of what has been
   * read, oldest first with the newest last. Older pages prepend above; live arrivals
   * and sends append below.
   */
  const [messages, setMessages] = useState<Message[]>([]);
  /** How many visible messages the thread holds, loaded or not. The pager reads it. */
  const [totalMessages, setTotalMessages] = useState(0);
  /** False while answers arrive without a total (older servers, unit-test mocks). */
  const [pagedThread, setPagedThread] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  /** The scroll container, for holding the reader's place when older pages prepend above. */
  const scrollContainerRef = useRef<HTMLDivElement | null>(null);
  /**
   * A pending scroll correction, set just before older rows prepend and consumed by the
   * effect below once they have rendered. Carries the length it expects so a live
   * arrival winning the race cannot spend it on the wrong render.
   */
  const pendingPrependRef = useRef<{
    prevLength: number;
    added: number;
    top: number;
    height: number;
  } | null>(null);
  const [timeLeft, setTimeLeft] = useState<string>('');
  const [accepting, setAccepting] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [sending, setSending] = useState(false);
  const [loadingMessages, setLoadingMessages] = useState(false);
  // How much of the conversation's message allowance is left, kept against the conversation it
  // was read for. The pair matters because this component is re-used when the parent swaps
  // `conversationData`: the allowance is per conversation, so one thread's remaining room shown
  // over another's transcript would be worse than showing none at all. A reading for a
  // conversation that is not on screen reads as `null` -- "not known yet" -- which
  // canSendAnotherMessage treats as open.
  const [quotaState, setQuotaState] = useState<{ conversationId: string; quota: MessageQuota } | null>(null);

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
  const [is_blocked, setIsBlocked] = useState(false);
  const [blocked_by, setBlockedBy] = useState<string | null>(null);
  /**
   * Whether the authoritative block answer for the conversation on screen has
   * landed. The setup payload may not carry the flag at all, and the confirmation
   * read below travels on its own effect; until it lands the flag is still its
   * default, which must not decide anything.
   */
  const [blockStatusKnown, setBlockStatusKnown] = useState(false);
  /**
   * A fresh mirror of the block flag for control flow after awaits. State read in
   * an async continuation freezes at call time, so a block (or unblock) landing
   * mid-load would decide wrong; every writer below keeps this beside the state,
   * synchronously, so post-await reads see the answer as of now.
   */
  const blockedRef = useRef(false);
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

  // Spend a pending scroll correction after older rows prepend: the new rows grow the
  // scrollable height above the reader, so the top offset moves down by exactly the
  // growth and the message that was at the top stays where it was. Skipped unless the
  // render holds precisely the rows the prepend promised -- a live arrival in between
  // consumes nothing and the reader keeps whatever place the live update gave them.
  useEffect(() => {
    const anchor = pendingPrependRef.current;
    pendingPrependRef.current = null;
    const container = scrollContainerRef.current;
    if (!anchor || !container) return;
    if (messages.length !== anchor.prevLength + anchor.added) return;
    container.scrollTop = anchor.top + (container.scrollHeight - anchor.height);
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
      // A new conversation restarts the answer: the flag still describes the
      // previous one until this read lands, so the init below waits afresh.
      setBlockStatusKnown(false);
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

    // The block answer travels on its own read (see the effect above), so while it
    // is in flight the flag is still its default. Initializing messaging on the
    // default opens a socket into a conversation the other side already blocked --
    // refused, retried, erroring -- and then loads the history twice, once live
    // and once read-only. Wait for the answer instead: both branches below then
    // run exactly once, down the right one.
    if (!blockStatusKnown) return;

    const isAcceptedConversation = conversationData.conversation.status === 'accepted';
    const isTerminatedConversation = conversationData.conversation.status === 'terminated';
    // A terminated conversation is finished like an expired one, whatever the clock says:
    // no live socket is opened into it, but its history is still loaded and readable. The
    // status rather than the expiry is what decides, because a moderator can close a thread
    // while its match is still well inside the four hours.
    const shouldInitialize = (
      isAcceptedConversation &&
      !isTerminatedConversation &&
      !conversationData.match.is_expired &&
      !is_blocked
    );

    if (shouldInitialize) {
      console.log('🚀 Initializing messaging (not blocked)');
      initializeMessaging();
    } else if (isAcceptedConversation || isTerminatedConversation) {
      // Expired, blocked or terminated: the conversation is read-only, but its history is
      // still readable, so load it without a live connection. This branch used to require
      // `is_blocked`, so an expired conversation fell through both branches and the
      // history was never fetched at all.
      console.log('📖 Read-only conversation - loading history without a live connection', {
        is_expired: conversationData.match.is_expired,
        is_blocked: is_blocked,
        current_messages: messages.length,
        has_token: !!token
      });

      if (token && messages.length === 0) {
        loadMessagesFromBackend(token).catch(error => {
          console.error('Failed to load messages for read-only conversation:', error);
        });
      }
    }

    // Note: Cleanup is handled separately, not here
    // We don't want to clean up messages when is_blocked changes
  }, [conversationData?.conversation?.id, conversationData?.conversation?.status, is_blocked, blockStatusKnown]);

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
      is_blocked: is_blocked
    });

    try {
      // Always load existing messages first
      console.log('⏳ Loading messages...');
      await loadMessagesFromBackend(token);
      console.log('✅ Messages loaded');

      // Only connect WebSocket if not blocked (for real-time updates). The ref, not
      // the state: this resumes after awaits, and a block (or unblock) landing
      // mid-load must decide, not the flag as of the call -- without this, blocking
      // during the history read orphans a live socket into a blocked conversation,
      // and unblocking leaves the thread silently socketless.
      if (!blockedRef.current) {
        console.log('⏳ Connecting to WebSocket...');
        await messagingClient.connect(conversationId, token);
        console.log('✅ WebSocket connection promise resolved');

        // Subscribe to new messages
        const unsubscribeMessage = messagingClient.onMessage((newMessage) => {
          console.log('📨 New message received:', newMessage);
          const isFromCaller = newMessage.sender_regno === currentUserRef.current;
          const messageWithFlag = {
            ...newMessage,
            is_sender: isFromCaller
          };
          // Dedupe against the backup: a message redelivered while a page read is in
          // flight must not land twice, and the total below must not count it twice
          // either -- a phantom extra row would offer one empty older page too many,
          // which at least heals on the next read.
          if (!messagesBackupRef.current.some((message) => message.id === newMessage.id)) {
            setMessages((prev) => [...prev, messageWithFlag]);
            setTotalMessages((prev) => prev + 1);
          }
          // The live socket is how a partner's message reaches this thread, so the allowance
          // has to move here too or the composer keeps offering a room that is no longer
          // there. Derived from the server's counts rather than recounted from the list, and
          // only for the conversation this subscription was opened for.
          setQuotaState((previous) =>
            previous && previous.conversationId === newMessage.conversation_id
              ? {
                  conversationId: previous.conversationId,
                  quota: quotaAfterIncomingMessage(previous.quota, isFromCaller) ?? previous.quota,
                }
              : previous
          );
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
      if (!blockedRef.current) {
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
      // The opening window is the newest one: latest message last, as the thread reads.
      const {
        messages,
        quota: fetchedQuota,
        total: fetchedTotal,
      } = await fetchMessages(conversationData.conversation.id, token, THREAD_PAGE_SIZE);
      console.log('✅ Messages fetched:', messages.length);

      // Add is_sender flag to each message
      const messagesWithFlags = messages.map((msg) => ({
        ...msg,
        is_sender: msg.sender_regno === currentUserRef.current
      }));

      setMessages(messagesWithFlags);
      // A numeric total means the server pages: the window above is the newest page and
      // older ones wait behind the pager. Without one the whole thread came back, as it
      // always did before paging existed.
      setTotalMessages(typeof fetchedTotal === 'number' ? fetchedTotal : messagesWithFlags.length);
      setPagedThread(typeof fetchedTotal === 'number');
      // An allowance the server did not report falls back to the full one rather than to a
      // closed composer: guessing "full" can cost one refused send, guessing "empty" would
      // lock a conversation that has room in it.
      recordQuota(fetchedQuota);
      scrollToBottom();
    } catch (error) {
      console.error('❌ Failed to load messages:', error);
      toast.error('Failed to load messages');
      throw error; // Re-throw so caller knows it failed
    } finally {
      setLoadingMessages(false);
    }
  };

  /**
   * Grow the thread upwards: the next older page prepends above the messages already on
   * screen, and the reader stays on the message they were reading -- the newest page is
   * the anchor at the bottom, not the place a pager should return anyone to. Guarded so
   * overlapping clicks cannot interleave two prepends.
   */
  const loadOlderMessages = async () => {
    if (!conversationData || !token || loadingOlder) return;
    const oldest = messages[0];
    if (!oldest) return;
    setLoadingOlder(true);
    try {
      const { messages: older, total: fetchedTotal } = await fetchMessages(
        conversationData.conversation.id,
        token,
        THREAD_PAGE_SIZE,
        oldest.id,
      );
      const container = scrollContainerRef.current;
      if (container) {
        pendingPrependRef.current = {
          prevLength: messages.length,
          added: older.length,
          top: container.scrollTop,
          height: container.scrollHeight,
        };
      }
      const flagged = older.map((msg) => ({
        ...msg,
        is_sender: msg.sender_regno === currentUserRef.current,
      }));
      setMessages((prev) => {
        const known = new Set(prev.map((message) => message.id));
        return [...flagged.filter((message) => !known.has(message.id)), ...prev];
      });
      if (typeof fetchedTotal === 'number') setTotalMessages(fetchedTotal);
    } catch (error) {
      pendingPrependRef.current = null;
      console.error('❌ Failed to load older messages:', error);
      toast.error('Failed to load older messages');
    } finally {
      setLoadingOlder(false);
    }
  };

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  const setupConversationData = (data: ConversationData) => {
    // Store current user ID from the logged-in user (most reliable)
    // Fall back to conversation data if user not available
    if (user?.regno) {
      currentUserRef.current = user.regno;
    } else if (data.is_initiator) {
      currentUserRef.current = data.conversation.initiator_regno;
    } else {
      currentUserRef.current = data.conversation.receiver_regno;
    }
    
    console.log('👤 Current user set to:', currentUserRef.current, {
      from_auth: user?.regno,
      is_initiator: data.is_initiator,
      initiator_regno: data.conversation.initiator_regno,
      receiver_regno: data.conversation.receiver_regno
    });
    
    // Set block status from the data
    if (data.is_blocked !== undefined) {
      blockedRef.current = data.is_blocked;
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

    const now = serverNow();
    const expiresAt = timestampMs(conversationData.match.expires_at);
    const diff = expiresAt - now;

    if (diff <= 0) {
      setTimeLeft('Expired');
      // The countdown alone doesn't move the rest of the component: is_expired is what
      // canChat and the read-only notice read, so flip it or the composer stays enabled
      // while the header says "Expired". Guarded so this only commits once.
      setConversationData(prev => (
        prev && !prev.match.is_expired
          ? { ...prev, match: { ...prev.match, is_expired: true } }
          : prev
      ));
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

    // Stopped before the request rather than after it. The server refuses this too, but the
    // composer is disabled in this state, and a send that cannot succeed should not be the
    // thing that says so.
    if (!canSendAnotherMessage(quota)) {
      toast.error(messageLimitNotice(quota) ?? 'This conversation has no messages left to send.');
      return;
    }

    const textToSend = messageText.trim();
    setMessageText('');
    setSending(true);

    try {
      const sent = await sendMessage(
        conversationData.conversation.id,
        textToSend,
        token
      );

      // The send reports the allowance as it left it, so the counter moves to the server's
      // answer rather than to a guess made here.
      recordQuota(sent.quota);

      // Message will appear via real-time WebSocket subscription
      scrollToBottom();
    } catch (error) {
      console.error('Error sending message:', error);
      // The server's own refusal is the useful text: a run-out-of-room answer arrives as a
      // 403 whose detail says which allowance ran out, and a generic failure would throw it
      // away. Anything else still falls back to the generic sentence.
      toast.error(error instanceof Error ? error.message : 'Failed to send message');
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
    if (!conversationData?.match?.id) {
      // Nothing to ask about: fall back to whatever the setup data carried, and
      // release the init below to decide on that rather than waiting forever.
      setBlockStatusKnown(true);
      return;
    }

    try {
      // IMPORTANT: Use getConversationByMatch instead of getCurrentConversation
      // This works for both initiators and receivers!
      const response = await getConversationByMatch(conversationData.match.id);
      if (response.status === 'success' && response.is_blocked !== undefined) {
        console.log('✅ Block status checked:', {
          is_blocked: response.is_blocked,
          blocked_by: response.blocked_by
        });
        // The ref first: a blocked (or unblocked) answer must decide control flow
        // immediately, without waiting for the re-render the state schedules.
        blockedRef.current = response.is_blocked || false;
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
    } finally {
      // Answered or failed: either way the init below must stop waiting. On failure
      // it decides on the setup value, exactly as it did before this gate existed.
      setBlockStatusKnown(true);
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
        blockedRef.current = newConversationData.is_blocked || false;
        setIsBlocked(newConversationData.is_blocked || false);
        setBlockedBy(newConversationData.blocked_by || null);
        setMessagesHidden(false);
        
        // Update conversation data
        setConversationData(newConversationData);
        
        // Force immediate UI update with React 18's automatic batching workaround
        setTimeout(() => {
          console.log('🔄 Force updating block state after timeout');
          blockedRef.current = newConversationData.is_blocked || false;
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
              .then(({ messages: fetchedMessages, quota: fetchedQuota, total: fetchedTotal }) => {
                const messagesWithFlags = fetchedMessages.map((msg) => ({
                  ...msg,
                  is_sender: msg.sender_regno === currentUserRef.current
                }));
                setMessages(messagesWithFlags);
                setTotalMessages(
                  typeof fetchedTotal === 'number' ? fetchedTotal : messagesWithFlags.length,
                );
                setPagedThread(typeof fetchedTotal === 'number');
                messagesBackupRef.current = messagesWithFlags;
                // The allowance comes back with the history rather than being carried over:
                // this reload is the freshest reading of it, and it is also the answer that
                // may have moved while the thread was blocked.
                recordQuota(fetchedQuota);
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
      // `detail` is a plain string for an HTTPException and an array of Pydantic error objects for a
      // 422. `Array.prototype.includes` exists too, so the old `.includes(...)` on a 422
      // answered false rather than throwing, and the operator was told "Failed to unblock user"
      // for a reason the server had actually spelled out. Reading it through the shared
      // describer turns the array into text first, so the match is against what is displayed.
      const detail = describeApiError(error, 'Failed to unblock user');
      if (detail.includes('only unblock if you were')) {
        toast.error('Only the person who blocked can unblock');
      } else {
        toast.error(detail);
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

  const getStatusBadge = (status: string, isExpired: boolean) => {
    // Termination outranks expiry: a thread a moderator closed is reported as terminated
    // even when its match is also past due, so the reason it is closed stays visible.
    if (status === 'terminated') {
      return <Badge variant="secondary">Terminated</Badge>;
    }

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
      <div className="fixed inset-0 bg-[var(--scrim)] flex items-center justify-center z-50 p-2 sm:p-4">
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
  const isTerminated = conversationData.conversation.status === 'terminated';
  const isReceiver = !conversationData.is_initiator;
  // A terminated thread is read-only like an expired one. `isAccepted` already excludes it,
  // and it is spelled out because the whole point of the terminated state is that it must
  // not read as a live conversation.
  const canChat = isAccepted && !isExpired && !isTerminated;

  const currentConversationId = conversationData?.conversation.id ?? null;
  const quota = quotaState?.conversationId === currentConversationId ? quotaState.quota : null;

  // Record an allowance against the conversation it was read for, so the derived `quota`
  // above can tell a current reading from a previous thread's. Absent a server figure the
  // full allowance is recorded rather than an absence: guessing "full" can cost one refused
  // send, guessing "empty" would close a conversation that has room in it.
  const recordQuota = (value: MessageQuota | null) => {
    if (currentConversationId === null) return;
    setQuotaState({ conversationId: currentConversationId, quota: value ?? emptyMessageQuota() });
  };

  // Two readings of the same allowance: whether there is room for another message, and the
  // sentence that explains it when there is not. Both come from the server's counts.
  const composerOpen = canSendAnotherMessage(quota);
  const quotaNotice = messageLimitNotice(quota);

  return (
    <div className="fixed inset-0 bg-[var(--scrim)] flex items-center justify-center z-50 p-2 sm:p-4 touch-none">
      <Card className="w-full max-w-6xl h-[100vh] sm:h-[90vh] flex flex-col overflow-hidden">
        <CardHeader className="border-b flex-shrink-0 p-3 sm:p-6">
          <div className="flex justify-between items-start gap-2">
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 sm:gap-3 mb-2">
                <ProfileAvatar
                  src={conversationData.other_user.profile_picture_url}
                  name={conversationData.other_user.name}
                  className="w-10 h-10 sm:w-12 sm:h-12"
                />
                <div className="min-w-0 flex-1">
                  <CardTitle className="text-base sm:text-xl truncate">{conversationData.other_user.name}</CardTitle>
                  <CardDescription className="text-xs sm:text-sm truncate">{conversationData.other_user.name} • {cohortLabel(conversationData.other_user.year, conversationData.other_user.department)}</CardDescription>
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
                {(isAccepted || isTerminated) && (
                  <div className="flex items-center gap-1 flex-shrink-0">
                    <MessageCircle className="h-3 w-3 sm:h-4 sm:w-4" />
                    <span className="whitespace-nowrap">{totalMessages} msgs</span>
                  </div>
                )}
                {isConnected && (
                  <Badge variant="outline" className="bg-green-50 text-green-700 border-green-300 text-xs px-2 py-0 flex-shrink-0">
                    ● Connected
                  </Badge>
                )}
                {is_blocked && (
                  <Badge variant="destructive" className="flex items-center gap-1 text-xs px-2 py-0 flex-shrink-0">
                    <ShieldOff className="h-3 w-3" />
                    Blocked
                  </Badge>
                )}
              </div>

              {/* Block/Unblock Actions. Not offered on a terminated thread: there is nothing left to
                protect anyone from, and the server refuses the send regardless. */}
              {isAccepted && !isExpired && !isTerminated && (
                <div className="flex flex-wrap gap-2 mt-3">
                  {!is_blocked ? (
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
                      {blocked_by === currentUserRef.current && (
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
            {is_blocked && (
              <div className="bg-red-50 border-b border-red-200 p-3 sm:p-4">
                <div className="flex items-center gap-2 text-red-900">
                  <ShieldOff className="h-4 w-4 sm:h-5 sm:w-5 flex-shrink-0" />
                  <div className="min-w-0">
                    <p className="font-semibold text-sm sm:text-base">This conversation is blocked</p>
                    <p className="text-xs sm:text-sm">
                      {blocked_by === currentUserRef.current 
                        ? 'You blocked this user. They have been notified.' 
                        : `${conversationData.other_user.name} blocked this conversation.`}
                    </p>
                  </div>
                </div>
              </div>
            )}

            {/* Messages Area */}
            <div ref={scrollContainerRef} className="flex-1 overflow-y-auto p-3 sm:p-4 space-y-3 sm:space-y-4">
              {/* Older history grows upwards, latest last: the pager sits above the
                  oldest loaded message rather than below the newest one, and a click
                  prepends without moving the reader off the message they were on. */}
              {pagedThread && messages.length < totalMessages && (
                <div className="flex justify-center pb-1">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => void loadOlderMessages()}
                    disabled={loadingOlder}
                    aria-label="Show older messages"
                  >
                    {loadingOlder
                      ? 'Loading…'
                      : `Show older messages (${totalMessages - messages.length} remaining)`}
                  </Button>
                </div>
              )}
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

              {isTerminated && (
                <div className="bg-gray-50 border border-gray-200 rounded-lg p-3 sm:p-4 mb-4 text-center">
                  <p className="text-xs sm:text-sm text-gray-900">
                    This conversation was terminated by a moderator. Messages are read-only.
                  </p>
                </div>
              )}

              {loadingMessages && (
                <div className="flex justify-center">
                  <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
                </div>
              )}

              {(isAccepted || isTerminated) && !messagesHidden && messages.map((message) => (
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
                        <p className="text-xs sm:text-sm break-words">{message.body}</p>
                        <p className={`text-xs mt-1 ${message.is_sender ? 'text-primary-foreground/70' : 'text-muted-foreground'}`}>
                          {parseTimestamp(message.created_at)?.toLocaleTimeString() ?? ''}

                        </p>
                      </div>
                    </div>
                  </ContextMenuTrigger>
                  <ContextMenuContent>
                    <ContextMenuItem onClick={() => handleCopyMessage(message.body)}>
                      <Copy className="mr-2 h-4 w-4" />
                      Copy
                    </ContextMenuItem>
                    {!message.is_sender && !is_blocked && (
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
            {canChat && !is_blocked && (
              <div className="border-t px-2 pt-2 sm:px-4 sm:pt-2 text-center text-xs text-muted-foreground">
                You can send up to 100 messages in this conversation.
              </div>
            )}

            {canChat && !is_blocked && composerOpen && (
              <div className="border-t p-2 sm:p-4">
                <div className="flex items-center gap-2">
                  <input
                    type="text"
                    value={messageText}
                    maxLength={MESSAGE_BODY_MAX}
                    onChange={(e) => setMessageText(e.target.value)}
                    onKeyPress={(e) => e.key === 'Enter' && handleSendMessage()}
                    placeholder="Type a message..."
                    className="flex-1 px-3 sm:px-4 py-2 text-sm sm:text-base border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary min-w-0"
                    disabled={sending || !isConnected}
                  />
                  <span className="whitespace-nowrap text-xs text-muted-foreground">
                    {messageText.length}/{MESSAGE_BODY_MAX}
                  </span>
                  <Button
                    onClick={handleSendMessage}
                    disabled={!messageText.trim() || sending || !isConnected}
                    className="flex-shrink-0 px-3 sm:px-4 h-9 sm:h-10 text-sm"
                  >
                    {sending ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Send'}
                  </Button>
                </div>
                {/* How much of this person's own allowance is spent. Shown from the first
                    message rather than near the end of the allowance: someone who has not
                    been told the limit cannot plan around reaching it. */}
                {messageQuotaLabel(quota) && (
                  <p className="mt-1 text-right text-xs text-muted-foreground">
                    {messageQuotaLabel(quota)}
                  </p>
                )}
              </div>
            )}

            {/* Message Allowance Spent. The thread is still readable, so this replaces the
                composer with the reason rather than with a silence -- and the two reasons are
                worded differently because they are different situations: an allowance of your
                own is spent, or the conversation's. */}
            {canChat && !is_blocked && !composerOpen && (
              <div className="border-t p-3 sm:p-4 bg-gray-50">
                <p className="text-center text-xs sm:text-base text-gray-900">
                  {quotaNotice}
                </p>
              </div>
            )}

            {/* Blocked State Message */}
            {is_blocked && canChat && (
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
          messageText={selectedMessageForReport.body}
          reporterId={currentUserRef.current!}
          reportedUserId={conversationData.is_initiator 
            ? conversationData.conversation.receiver_regno 
            : conversationData.conversation.initiator_regno}
          conversationId={conversationData.conversation.id}
        />
      )}

      {/* Block User Dialog */}
      {conversationData && (
        <BlockUserDialog
          isOpen={blockDialogOpen}
          onClose={() => setBlockDialogOpen(false)}
          match_id={conversationData.match.id}
          otherUserName={conversationData.other_user.name}
          onBlockSuccess={handleBlockSuccess}
        />
      )}
    </div>
  );
};

export default ConversationDialog;
