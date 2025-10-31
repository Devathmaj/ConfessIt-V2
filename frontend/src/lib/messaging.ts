// src/lib/messaging.ts
/**
 * WebSocket-based messaging client for ConfessIt
 * Replaces Supabase real-time messaging
 */

interface Message {
  id: string;
  conversation_id: string;
  sender_id: string;
  receiver_id: string;
  text: string;
  timestamp: string;
  read: boolean;
  is_sender?: boolean;
}

interface WebSocketMessage {
  type: 'connected' | 'message' | 'error';
  conversation_id?: string;
  user_id?: string;
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

      // Get WebSocket URL (use wss:// in production)
      const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const wsHost = import.meta.env.VITE_API_URL 
        ? import.meta.env.VITE_API_URL.replace(/^https?:\/\//, '')
        : window.location.host.replace(':5173', ':8001');
      
      const wsUrl = `${wsProtocol}//${wsHost}/messages/ws/${conversationId}?token=${token}`;

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
              console.log('✅ WebSocket authenticated:', data.user_id);
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
 */

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8001';

/**
 * Send a message via REST API
 */
export async function sendMessage(
  conversationId: string,
  text: string,
  token: string
): Promise<Message> {
  const response = await fetch(`${API_URL}/messages/send`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      conversation_id: conversationId,
      text
    })
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to send message');
  }

  return response.json();
}

/**
 * Fetch message history
 */
export async function fetchMessages(
  conversationId: string,
  token: string,
  limit: number = 200
): Promise<Message[]> {
  const url = `${API_URL}/messages/${conversationId}?limit=${limit}`;
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
      const error = await response.json();
      console.error('❌ Fetch error response:', error);
      throw new Error(error.detail || 'Failed to fetch messages');
    }

    const data = await response.json();
    console.log('✅ Fetch response data:', data);
    return data.messages;
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
    const error = await response.json();
    throw new Error(error.detail || 'Failed to report message');
  }
}
