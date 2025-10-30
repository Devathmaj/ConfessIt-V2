// src/lib/supabase.ts
import { createClient, SupabaseClient, RealtimeChannel } from '@supabase/supabase-js';

interface Message {
  id: string;
  conversation_id: string;
  sender_id: string;
  receiver_id: string;
  text: string;
  timestamp: string;
}

interface SupabaseClientConfig {
  supabaseUrl: string;
  supabaseAnonKey: string;  // Public anon key
  supabaseToken: string;     // Ephemeral JWT token
  conversationId: string;
}

let supabaseClient: SupabaseClient | null = null;
let currentConversationId: string | null = null;

/**
 * Initialize or reinitialize the Supabase client with a new token
 */
export const initSupabaseClient = async (config: SupabaseClientConfig): Promise<SupabaseClient> => {
  const { supabaseUrl, supabaseAnonKey, supabaseToken, conversationId } = config;

  // If we already have a client for the same conversation, return it
  if (supabaseClient && currentConversationId === conversationId) {
    return supabaseClient;
  }

  // Clean up existing client if conversation changed
  if (supabaseClient && currentConversationId !== conversationId) {
    supabaseClient.removeAllChannels();
  }

  // Create new client with the anon key
  // Security is enforced by backend validation before issuing credentials
  supabaseClient = createClient(supabaseUrl, supabaseAnonKey, {
    auth: {
      autoRefreshToken: false,
      persistSession: false,
      detectSessionInUrl: false
    },
    realtime: {
      params: {
        eventsPerSecond: 10
      }
    }
  });

  // Authenticate with the ephemeral token
  await supabaseClient.auth.setSession({
    access_token: supabaseToken,
    refresh_token: supabaseToken  // Use same token as refresh for ephemeral
  });

  currentConversationId = conversationId;

  return supabaseClient;
};

/**
 * Get the current Supabase client instance
 */
export const getSupabaseClient = (): SupabaseClient | null => {
  return supabaseClient;
};

/**
 * Subscribe to real-time messages for a conversation
 */
export const subscribeToMessages = (
  conversationId: string,
  onMessage: (message: Message) => void,
  onError?: (error: Error) => void
): RealtimeChannel | null => {
  if (!supabaseClient) {
    console.error('Supabase client not initialized');
    return null;
  }

  const channel = supabaseClient
    .channel(`messages:${conversationId}`)
    .on(
      'postgres_changes',
      {
        event: 'INSERT',
        schema: 'public',
        table: 'messages',
        filter: `conversation_id=eq.${conversationId}`
      },
      (payload) => {
        console.log('New message received:', payload);
        if (payload.new) {
          onMessage(payload.new as Message);
        }
      }
    )
    .subscribe((status) => {
      if (status === 'SUBSCRIBED') {
        console.log('Subscribed to messages channel');
      } else if (status === 'CLOSED') {
        console.log('Messages channel closed');
      } else if (status === 'CHANNEL_ERROR') {
        console.error('Channel error');
        if (onError) {
          onError(new Error('Realtime subscription error'));
        }
      }
    });

  return channel;
};

/**
 * Fetch existing messages for a conversation
 */
export const fetchMessages = async (conversationId: string): Promise<Message[]> => {
  if (!supabaseClient) {
    throw new Error('Supabase client not initialized');
  }

  const { data, error } = await supabaseClient
    .from('messages')
    .select('*')
    .eq('conversation_id', conversationId)
    .order('timestamp', { ascending: true });

  if (error) {
    console.error('Error fetching messages:', error);
    throw error;
  }

  return data as Message[];
};

/**
 * Send a new message
 * Note: sender_id and receiver_id are stored as text (Regno) directly
 */
export const sendMessage = async (
  conversationId: string,
  senderId: string,
  receiverId: string,
  text: string
): Promise<Message> => {
  if (!supabaseClient) {
    throw new Error('Supabase client not initialized');
  }

  // No conversion needed - store Regno as-is since Supabase uses text fields
  const { data, error } = await supabaseClient
    .from('messages')
    .insert({
      conversation_id: conversationId,
      sender_id: senderId,  // Regno as text
      receiver_id: receiverId,  // Regno as text
      text,
      timestamp: new Date().toISOString()
    })
    .select()
    .single();

  if (error) {
    console.error('Error sending message:', error);
    throw error;
  }

  return data as Message;
};

/**
 * Report a message
 */
export const reportMessage = async (
  messageId: string,
  reporterId: string,
  reportedUserId: string,
  reason: string,
  conversationId: string
): Promise<void> => {
  if (!supabaseClient) {
    throw new Error('Supabase client not initialized');
  }

  const { error } = await supabaseClient
    .from('message_reports')
    .insert({
      message_id: messageId,
      reporter_id: reporterId,
      reported_user_id: reportedUserId,
      reason,
      conversation_id: conversationId,
      reported_at: new Date().toISOString()
    });

  if (error) {
    console.error('Error reporting message:', error);
    throw error;
  }
};

/**
 * Block a conversation
 */
export const blockConversation = async (
  conversationId: string,
  blockedBy: string
): Promise<void> => {
  if (!supabaseClient) {
    throw new Error('Supabase client not initialized');
  }

  const { error } = await supabaseClient
    .from('conversations')
    .update({
      is_blocked: true,
      blocked_by: blockedBy,
      blocked_at: new Date().toISOString()
    })
    .eq('id', conversationId);

  if (error) {
    console.error('Error blocking conversation:', error);
    throw error;
  }
};

/**
 * Unblock a conversation
 */
export const unblockConversation = async (
  conversationId: string,
  userId: string
): Promise<void> => {
  if (!supabaseClient) {
    throw new Error('Supabase client not initialized');
  }

  // First check if the user is the one who blocked
  const { data: conversation, error: fetchError } = await supabaseClient
    .from('conversations')
    .select('blocked_by')
    .eq('id', conversationId)
    .single();

  if (fetchError) {
    console.error('Error fetching conversation:', fetchError);
    throw fetchError;
  }

  if (conversation.blocked_by !== userId) {
    throw new Error('Only the user who blocked can unblock');
  }

  const { error } = await supabaseClient
    .from('conversations')
    .update({
      is_blocked: false,
      blocked_by: null,
      blocked_at: null
    })
    .eq('id', conversationId);

  if (error) {
    console.error('Error unblocking conversation:', error);
    throw error;
  }
};

/**
 * Check if conversation is blocked
 */
export const getConversationBlockStatus = async (
  conversationId: string
): Promise<{ is_blocked: boolean; blocked_by: string | null; blocked_at: string | null }> => {
  if (!supabaseClient) {
    throw new Error('Supabase client not initialized');
  }

  const { data, error } = await supabaseClient
    .from('conversations')
    .select('is_blocked, blocked_by, blocked_at')
    .eq('id', conversationId)
    .single();

  if (error) {
    console.error('Error getting block status:', error);
    throw error;
  }

  return data;
};

/**
 * Cleanup and disconnect Supabase client
 */
export const disconnectSupabase = () => {
  if (supabaseClient) {
    supabaseClient.removeAllChannels();
    supabaseClient = null;
    currentConversationId = null;
  }
};
