// services/websocket.ts

export interface WebSocketMessage {
  type: string;
  [key: string]: any;
}

export interface NewMessageData {
  id: string;
  text: string;
  sender_id: string;
  receiver_id: string;
  timestamp: string;
  is_sender: boolean;
}

export interface ConversationStatusData {
  status: 'accepted' | 'rejected' | 'expired';
  accepted_by?: string;
  accepted_at?: string;
  rejected_by?: string;
  rejected_at?: string;
  reason?: string;
}

export interface TypingData {
  user_id: string;
  match_id: string;
  is_typing: boolean;
  timestamp: string;
}

export interface MatchExpiryData {
  match_id: string;
  time_left_seconds: number;
  timestamp: string;
}

class WebSocketService {
  private ws: WebSocket | null = null;
  private matchId: string | null = null;
  private token: string | null = null;
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 5;
  private reconnectDelay = 1000; // Start with 1 second
  private messageHandlers: Map<string, ((data: any) => void)[]> = new Map();
  private isConnecting = false;
  private pingInterval: NodeJS.Timeout | null = null;
  private pongTimeout: NodeJS.Timeout | null = null;

  constructor() {
    // Get token from localStorage
    this.token = localStorage.getItem('token');
  }

  connect(matchId: string): Promise<void> {
    return new Promise((resolve, reject) => {
      if (this.isConnecting) {
        reject(new Error('Connection already in progress'));
        return;
      }

      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        if (this.matchId === matchId) {
          resolve(); // Already connected to the same match
          return;
        } else {
          this.disconnect(); // Disconnect from previous match
        }
      }

      this.isConnecting = true;
      this.matchId = matchId;

      if (!this.token) {
        reject(new Error('No authentication token available'));
        this.isConnecting = false;
        return;
      }

      const wsUrl = `ws://localhost:8001/ws/${matchId}?token=${this.token}`;
      
      try {
        this.ws = new WebSocket(wsUrl);
        
        this.ws.onopen = () => {
          console.log(`WebSocket connected to match ${matchId}`);
          this.isConnecting = false;
          this.reconnectAttempts = 0;
          this.reconnectDelay = 1000;
          this.startPingInterval();
          resolve();
        };

        this.ws.onmessage = (event) => {
          try {
            const message: WebSocketMessage = JSON.parse(event.data);
            this.handleMessage(message);
          } catch (error) {
            console.error('Error parsing WebSocket message:', error);
          }
        };

        this.ws.onclose = (event) => {
          console.log(`WebSocket disconnected from match ${matchId}:`, event.code, event.reason);
          this.isConnecting = false;
          this.stopPingInterval();
          
          // Attempt to reconnect if not a normal closure
          if (event.code !== 1000 && this.reconnectAttempts < this.maxReconnectAttempts) {
            this.scheduleReconnect();
          }
        };

        this.ws.onerror = (error) => {
          console.error('WebSocket error:', error);
          this.isConnecting = false;
          reject(error);
        };

      } catch (error) {
        this.isConnecting = false;
        reject(error);
      }
    });
  }

  disconnect(): void {
    this.stopPingInterval();
    
    if (this.ws) {
      this.ws.close(1000, 'Normal closure');
      this.ws = null;
    }
    
    this.matchId = null;
    this.reconnectAttempts = 0;
  }

  private scheduleReconnect(): void {
    this.reconnectAttempts++;
    const delay = Math.min(this.reconnectDelay * Math.pow(2, this.reconnectAttempts - 1), 30000);
    
    console.log(`Scheduling WebSocket reconnect attempt ${this.reconnectAttempts} in ${delay}ms`);
    
    setTimeout(() => {
      if (this.matchId) {
        this.connect(this.matchId).catch(error => {
          console.error('Reconnect failed:', error);
        });
      }
    }, delay);
  }

  private startPingInterval(): void {
    this.pingInterval = setInterval(() => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.sendMessage({
          type: 'ping',
          timestamp: new Date().toISOString()
        });
        
        // Set pong timeout
        this.pongTimeout = setTimeout(() => {
          console.warn('Pong timeout - reconnecting WebSocket');
          this.disconnect();
          if (this.matchId) {
            this.connect(this.matchId);
          }
        }, 5000); // 5 second timeout
      }
    }, 30000); // Send ping every 30 seconds
  }

  private stopPingInterval(): void {
    if (this.pingInterval) {
      clearInterval(this.pingInterval);
      this.pingInterval = null;
    }
    
    if (this.pongTimeout) {
      clearTimeout(this.pongTimeout);
      this.pongTimeout = null;
    }
  }

  sendMessage(message: WebSocketMessage): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(message));
    } else {
      console.warn('WebSocket is not connected, cannot send message:', message);
    }
  }

  sendTypingIndicator(isTyping: boolean): void {
    this.sendMessage({
      type: 'typing',
      is_typing: isTyping,
      timestamp: new Date().toISOString()
    });
  }

  sendMessageSentNotification(messageId: string): void {
    this.sendMessage({
      type: 'message_sent',
      message_id: messageId,
      timestamp: new Date().toISOString()
    });
  }

  private handleMessage(message: WebSocketMessage): void {
    console.log('Received WebSocket message:', message);
    
    // Handle pong messages
    if (message.type === 'pong') {
      if (this.pongTimeout) {
        clearTimeout(this.pongTimeout);
        this.pongTimeout = null;
      }
      return;
    }
    
    // Handle connection established
    if (message.type === 'connection_established') {
      console.log('WebSocket connection established for match:', message.match_id);
      return;
    }
    
    // Handle message confirmation
    if (message.type === 'message_confirmed') {
      console.log('Message confirmed:', message.message_id);
      return;
    }
    
    // Notify handlers
    const handlers = this.messageHandlers.get(message.type);
    if (handlers) {
      console.log(`Notifying ${handlers.length} handlers for message type: ${message.type}`);
      handlers.forEach(handler => {
        try {
          handler(message);
        } catch (error) {
          console.error(`Error in message handler for type ${message.type}:`, error);
        }
      });
    } else {
      console.log(`No handlers found for message type: ${message.type}`);
    }
  }

  onMessage(type: string, handler: (data: any) => void): () => void {
    if (!this.messageHandlers.has(type)) {
      this.messageHandlers.set(type, []);
    }
    
    this.messageHandlers.get(type)!.push(handler);
    
    // Return unsubscribe function
    return () => {
      const handlers = this.messageHandlers.get(type);
      if (handlers) {
        const index = handlers.indexOf(handler);
        if (index > -1) {
          handlers.splice(index, 1);
        }
        if (handlers.length === 0) {
          this.messageHandlers.delete(type);
        }
      }
    };
  }

  onNewMessage(handler: (data: { message: NewMessageData; match_id: string; timestamp: string }) => void): () => void {
    console.log('Subscribing to new_message WebSocket events');
    return this.onMessage('new_message', handler);
  }

  onConversationStatusUpdate(handler: (data: { status: ConversationStatusData; match_id: string; timestamp: string }) => void): () => void {
    return this.onMessage('conversation_status_update', handler);
  }

  onTyping(handler: (data: TypingData) => void): () => void {
    return this.onMessage('typing', handler);
  }

  onMatchExpiryWarning(handler: (data: MatchExpiryData) => void): () => void {
    return this.onMessage('match_expiry_warning', handler);
  }

  onMatchExpired(handler: (data: { match_id: string; timestamp: string }) => void): () => void {
    return this.onMessage('match_expired', handler);
  }

  isConnected(): boolean {
    return this.ws !== null && this.ws.readyState === WebSocket.OPEN;
  }

  getMatchId(): string | null {
    return this.matchId;
  }
}

// Export singleton instance
export const websocketService = new WebSocketService();
