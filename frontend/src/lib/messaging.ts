// src/lib/messaging.ts
/**
 * WebSocket-based messaging client for ConfessIt
 * Provides real-time messaging over the application's WebSocket API.
 */

// These three helpers talk to the API with fetch rather than the shared axios instance, so
// they would otherwise miss the response interceptor that ends an expired session.
import { endSessionIfTokenWasRejected } from './authEvents';
import { API_URL, apiWsBase } from './apiBase';
import { CONVERSATION_MESSAGE_MAX } from './limits';
import type { MessageQuota } from './messageQuota';


interface Message {
  id: string;
  conversation_id: string;
  sender_regno: string;
  receiver_regno: string;
  body: string;
  created_at: string;
  is_read: boolean;
  is_sender?: boolean;
}

/**
 * A sent message, carrying the conversation's allowance as the send left it.
 *
 * The quota rides along so a client that has just used up its allowance is told in the
 * response to the send that spent it, rather than finding out by being refused next time.
 */
export interface SentMessage extends Message {
  quota: MessageQuota;
}

/** The transcript of a conversation, plus how much room is left in it. */
export interface ConversationTranscript {
  messages: Message[];
  quota: MessageQuota | null;
  /** How many visible messages the thread holds, loaded or not. Absent on legacy answers. */
  total?: number;
}

interface WebSocketMessage {
  type: 'connected' | 'message' | 'error';
  conversation_id?: string;
  user_regno?: string;
  data?: Message;
  error?: string;
}

type MessageCallback = (message: Message) => void;
type ErrorCallback = (error: Error) => void;

class MessagingClient {
  private ws: WebSocket | null = null;
  private conversationId: string | null = null;
  private token: string | null = null;
  private messageCallbacks: Set<MessageCallback> = new Set();
  private errorCallbacks: Set<ErrorCallback> = new Set();
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 5;
  private reconnectDelay = 1000;
  private pingInterval: number | null = null;

  /**
   * Connect to WebSocket for a conversation
   */
  connect(conversationId: string, token: string): Promise<void> {
    return new Promise((resolve, reject) => {
      if (this.ws?.readyState === WebSocket.OPEN && this.conversationId === conversationId) {
        resolve();
        return;
      }

      // Close existing connection if different conversation
      if (this.ws && this.conversationId !== conversationId) {
        this.disconnect();
      }

      this.conversationId = conversationId;
      this.token = token;

      // The socket hangs off the same API origin as every REST call, so one variable
      // fixes both transports. `https` becomes `wss` and `http` becomes `ws`.
      const wsUrl = `${apiWsBase()}/messages/ws/${conversationId}?token=${token}`;

      console.log('🔌 Connecting to WebSocket:', wsUrl.replace(token, 'TOKEN'));

      try {
        this.ws = new WebSocket(wsUrl);

        this.ws.onopen = () => {
          console.log('✅ WebSocket connected');
          this.reconnectAttempts = 0;
          this.startPingInterval();
        };

        this.ws.onmessage = (event) => {
          try {
            const data: WebSocketMessage = JSON.parse(event.data);
            
            if (data.type === 'connected') {
              console.log('✅ WebSocket authenticated:', data.user_regno);
              resolve();
            } else if (data.type === 'message' && data.data) {
              // Broadcast message to all callbacks
              this.messageCallbacks.forEach(callback => {
                try {
                  callback(data.data!);
                } catch (error) {
                  console.error('Error in message callback:', error);
                }
              });
            } else if (data.type === 'error') {
              console.error('WebSocket error:', data.error);
              this.errorCallbacks.forEach(callback => {
                callback(new Error(data.error || 'Unknown WebSocket error'));
              });
            }
          } catch (error) {
            console.error('Error parsing WebSocket message:', error);
          }
        };

        this.ws.onerror = (error) => {
          console.error('❌ WebSocket error:', error);
          this.errorCallbacks.forEach(callback => {
            callback(new Error('WebSocket connection error'));
          });
          reject(new Error('WebSocket connection failed'));
        };

        this.ws.onclose = (event) => {
          console.log('🔌 WebSocket disconnected:', event.code, event.reason);
          this.stopPingInterval();

          // Attempt to reconnect if not a normal closure
          if (event.code !== 1000 && this.reconnectAttempts < this.maxReconnectAttempts) {
            this.reconnectAttempts++;
            console.log(`Reconnecting... (attempt ${this.reconnectAttempts})`);
            
            setTimeout(() => {
              if (this.conversationId && this.token) {
                this.connect(this.conversationId, this.token).catch(console.error);
              }
            }, this.reconnectDelay * this.reconnectAttempts);
          }
        };
      } catch (error) {
        console.error('Failed to create WebSocket:', error);
        reject(error);
      }
    });
  }

  /**
   * Subscribe to new messages
   */
  onMessage(callback: MessageCallback): () => void {
    this.messageCallbacks.add(callback);
    
    // Return unsubscribe function
    return () => {
      this.messageCallbacks.delete(callback);
    };
  }

  /**
   * Subscribe to errors
   */
  onError(callback: ErrorCallback): () => void {
    this.errorCallbacks.add(callback);
    
    return () => {
      this.errorCallbacks.delete(callback);
    };
  }

  /**
   * Send ping to keep connection alive
   */
  private startPingInterval() {
    this.stopPingInterval();
    
    this.pingInterval = window.setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send('ping');
      }
    }, 30000); // Ping every 30 seconds
  }

  private stopPingInterval() {
    if (this.pingInterval !== null) {
      clearInterval(this.pingInterval);
      this.pingInterval = null;
    }
  }

  /**
   * Disconnect from WebSocket
   */
  disconnect() {
    this.stopPingInterval();
    
    if (this.ws) {
      this.ws.close(1000, 'Client disconnecting');
      this.ws = null;
    }
    
    this.conversationId = null;
    this.token = null;
    this.messageCallbacks.clear();
    this.errorCallbacks.clear();
    this.reconnectAttempts = 0;
  }

  /**
   * Check if connected
   */
  isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }
}

// Singleton instance
export const messagingClient = new MessagingClient();

/**
 * API functions for message operations
 *
 * The REST origin is the shared one -- see `./apiBase`.
 */

/**
 * Send a message via REST API
 */
export async function sendMessage(
  conversationId: string,
  body: string,
  token: string
): Promise<SentMessage> {
  const response = await fetch(`${API_URL}/messages/send`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      conversation_id: conversationId,
      body
    })
  });

  if (!response.ok) {
    endSessionIfTokenWasRejected(response.status);
    const error = await response.json();
    // The refusal is the message: a conversation or a participant that has run out of room
    // answers with the reason, and replacing it with a generic failure would throw away the
    // one sentence that tells the user what to do next.
    throw new Error(error.detail || 'Failed to send message');
  }

  return response.json();
}

/**
 * Fetch message history, and the conversation's remaining allowance alongside it.
 *
 * One window, oldest first with the newest last: `limit` caps the window (defaulting to
 * the conversation ceiling, which is the whole thread -- a conversation cannot hold more
 * messages than that), and `before` names the oldest message on screen to read the next
 * older page above it. `total` counts every visible message in the thread so the dialog
 * knows how many older ones remain.
 */
export async function fetchMessages(
  conversationId: string,
  token: string,
  limit: number = CONVERSATION_MESSAGE_MAX,
  before?: string,
): Promise<ConversationTranscript> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (before) params.set('before', before);
  const url = `${API_URL}/messages/${conversationId}?${params.toString()}`;
  console.log('🌐 Fetching messages from:', url);
  console.log('🔑 Using token:', token ? 'TOKEN_EXISTS' : 'NO_TOKEN');
  
  try {
    const response = await fetch(url, {
      headers: {
        'Authorization': `Bearer ${token}`
      }
    });

    console.log('📡 Fetch response status:', response.status, response.statusText);

    if (!response.ok) {
      endSessionIfTokenWasRejected(response.status);
      const error = await response.json();
      console.error('❌ Fetch error response:', error);
      throw new Error(error.detail || 'Failed to fetch messages');
    }

    const data = await response.json();
    console.log('✅ Fetch response data:', data);
    // Null rather than a default: a response without the field means the server did not say,
    // and `messageQuota` decides what an absent allowance means rather than this file
    // inventing one. `total` is absent on legacy answers, which the dialog reads as
    // "everything came back".
    return {
      messages: Array.isArray(data) ? data : (data.messages ?? []),
      quota: data.quota ?? null,
      total: Array.isArray(data) ? data.length : (data.total ?? data.messages?.length ?? 0),
    };
  } catch (error) {
    console.error('❌ Fetch exception:', error);
    throw error;
  }
}

/**
 * Report a message
 */
export async function reportMessage(
  messageId: string,
  reason: string,
  token: string
): Promise<void> {
  const response = await fetch(`${API_URL}/messages/report`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      message_id: messageId,
      reason
    })
  });

  if (!response.ok) {
    endSessionIfTokenWasRejected(response.status);
    const error = await response.json();
    throw new Error(error.detail || 'Failed to report message');
  }
}
