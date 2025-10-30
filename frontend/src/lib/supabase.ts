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
 * Cleanup and disconnect Supabase client
 */
export const disconnectSupabase = () => {
  if (supabaseClient) {
    supabaseClient.removeAllChannels();
    supabaseClient = null;
    currentConversationId = null;
  }
};
