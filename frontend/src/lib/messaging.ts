// src/lib/messaging.ts
/**
 * WebSocket-based messaging client for ConfessIt
 * Provides real-time messaging over the application's WebSocket API.
 */

// These three helpers talk to the API with fetch rather than the shared axios instance, so
// they would otherwise miss the response interceptor that extends an expired session.
// `authorizedFetch` is that same courtesy for the fetch transport: it carries the bearer
// that is current at the moment of the call and refreshes an expired one instead of
// treating the refusal as the end of the session.
import { authorizedFetch } from './authFetch';
import { API_URL, apiWsBase } from './apiBase';
import { CONVERSATION_MESSAGE_MAX } from './limits';
import type { MessageQuota } from './messageQuota';
import { requestWebSocketTicket } from './wsTicket';


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
  /**
   * Whether the live echo went out.
   *
   * The message is stored either way, so this is the only thing that says
   * whether the partner's open socket will show it without the thread being
   * read again: "delivered" when the publish went out, "unavailable" when it
   * did not (a Redis outage, or a publisher that could not get a connection).
   */
  realtime?: 'delivered' | 'unavailable';
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

/**
 * Handshake refusals that retrying cannot fix (SEC-003, mirrors the backend
 * close codes in `app/routers/messages.py`): an invalid ticket, a dialog the
 * caller may not stream, or a foreign origin. Reconnecting on these would
 * re-fail the same gate five times and trip the handshake flood brake for
 * nothing, so they surface to error callbacks instead. Flood (4429) and idle
 * (4400) closes stay retryable -- both are transient by construction.
 */
const WS_CLOSE_NO_RETRY = new Set([4001, 4003, 4403]);

class MessagingClient {
  private ws: WebSocket | null = null;
  private conversationId: string | null = null;
  private messageCallbacks: Set<MessageCallback> = new Set();
  private errorCallbacks: Set<ErrorCallback> = new Set();
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 5;
  private reconnectDelay = 1000;
  private pingInterval: number | null = null;

  /**
   * The connection's generation.
   *
   * A connect waits for a ticket, and that wait can be overtaken: the dialog
   * closes, or the thread changes, and the ticket arrives afterwards for a
   * socket nobody wants. Opening it anyway leaves a live socket behind --
   * still pinging, still holding a Redis connection, and still delivering into
   * a thread that has moved on. The generation is how a connect notices that
   * its answer is no longer wanted.
   */
  private generation = 0;

  /**
   * Open a live stream for a conversation.
   *
   * Answers whether the stream was opened. A connect that is overtaken -- the
   * dialog closed while the ticket was being fetched -- opens nothing and
   * answers false, so the caller knows not to subscribe to a socket that will
   * never deliver anything.
   */
  async connect(conversationId: string): Promise<boolean> {
    if (this.ws?.readyState === WebSocket.OPEN && this.conversationId === conversationId) {
      return true;
    }

    // Any socket that is already open is closed before a new one is taken,
    // not only one that belongs to another conversation. Re-initialising the
    // same conversation would otherwise overwrite the reference and leave the
    // old socket running: still pinging, still holding a Redis connection, and
    // still delivering into a thread that has moved on.
    //
    // The subscribers stay: the thread is the one they signed up for, and
    // dropping them would leave the new socket delivering into nobody.
    this.closeSocket('Replaced by a new connection');

    // The generation is read before the ticket is fetched, so a disconnect
    // while the fetch is in flight is noticed when it answers.
    const generation = this.generation;
    this.conversationId = conversationId;

    // The socket hangs off the same API origin as every REST call, so one variable
    // fixes both transports. `https` becomes `wss` and `http` becomes `ws`. The URL
    // carries a one-use ticket, not the access token: Uvicorn logs the handshake
    // path with its query string, and an access token there would be a live bearer.
    // The ticket exchange reads the bearer that is current right now -- a token
    // captured when the dialog opened would be an hour-old bearer by the evening.
    const ticket = await requestWebSocketTicket();
    if (generation !== this.generation) {
      // Overtaken while the ticket was being fetched: the dialog closed (or the
      // thread changed) and the socket this would open is not wanted. Opening
      // it would leave a live socket behind that nobody closes -- still
      // pinging, still holding a Redis connection, and still delivering into a
      // thread that has moved on.
      console.log('🔌 Connect overtaken; not opening a socket');
      return false;
    }
    const wsUrl = `${apiWsBase()}/messages/ws/${conversationId}?ticket=${encodeURIComponent(ticket)}`;

    console.log('🔌 Connecting to WebSocket:', wsUrl.replace(ticket, 'TICKET'));

    return new Promise((resolve, reject) => {
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
              resolve(true);
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

          // A refused handshake (bad ticket, no entitlement, foreign origin)
          // fails the same way every time: report it instead of reconnecting
          // into the same refusal (and into the handshake flood brake).
          if (WS_CLOSE_NO_RETRY.has(event.code)) {
            const refusal = new Error(
              `WebSocket refused the handshake (code ${event.code})`
            );
            this.errorCallbacks.forEach((callback) => {
              try {
                callback(refusal);
              } catch (error) {
                console.error('Error in error callback:', error);
              }
            });
            return;
          }

          // Attempt to reconnect if not a normal closure
          if (event.code !== 1000 && this.reconnectAttempts < this.maxReconnectAttempts) {
            this.reconnectAttempts++;
            console.log(`Reconnecting... (attempt ${this.reconnectAttempts})`);
            setTimeout(() => {
              if (this.conversationId) {
                // Explicit call form, not `.catch(console.error)`: the production
                // build strips console.* calls via esbuild `drop`, which cannot
                // remove a bare function reference.
                this.connect(this.conversationId).catch((error) => {
                  console.error('WebSocket reconnect failed:', error);
                });
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
   * Close the socket, keeping the subscriptions.
   *
   * `disconnect` ends the client's whole life -- socket and subscribers -- and
   * is what a closed dialog wants. Replacing the socket is a different thing:
   * the thread is still the one the subscribers signed up for, so they stay
   * and the new socket takes over delivering to them.
   */
  private closeSocket(reason: string): void {
    if (!this.ws) return;
    try {
      this.ws.close(1000, reason);
    } catch {
      /* already gone */
    }
    this.ws = null;
  }

  /**
   * Disconnect from WebSocket
   */
  disconnect() {
    this.stopPingInterval();

    // The generation moves on, so a connect that is still waiting for its
    // ticket learns that its answer is no longer wanted and opens nothing.
    this.generation += 1;

    this.closeSocket('Client disconnecting');

    this.conversationId = null;
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
 * A failed send, carrying the HTTP status so a caller can tell a content-policy
 * removal (422) from an ordinary refusal. The detail the server sent is kept as
 * the message: it is the one sentence that tells the user what happened.
 */
export class SendMessageError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'SendMessageError';
    this.status = status;
  }
}

/**
 * Send a message via REST API
 *
 * The bearer is not a parameter: it is read from the store when the request is
 * made, so a send an hour into a session carries the token that is current
 * rather than the one the dialog happened to open with.
 */
export async function sendMessage(
  conversationId: string,
  body: string
): Promise<SentMessage> {
  const response = await authorizedFetch(`${API_URL}/messages/send`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      conversation_id: conversationId,
      body
    }),
    signal: AbortSignal.timeout(15000),
  });

  if (!response.ok) {
    const error = await response.json();
    // The refusal is the message: a conversation or a participant that has run out of room
    // answers with the reason, and replacing it with a generic failure would throw away the
    // one sentence that tells the user what to do next. The status rides along so a caller
    // can single out a content-policy removal (422) for its own dialog.
    throw new SendMessageError(error.detail || 'Failed to send message', response.status);
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
  limit: number = CONVERSATION_MESSAGE_MAX,
  before?: string,
): Promise<ConversationTranscript> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (before) params.set('before', before);
  const url = `${API_URL}/messages/${conversationId}?${params.toString()}`;
  console.log('🌐 Fetching messages from:', url);

  try {
    const response = await authorizedFetch(url, {
      signal: AbortSignal.timeout(15000),
    });

    console.log('📡 Fetch response status:', response.status, response.statusText);

    if (!response.ok) {
      // Fixed string, never the server's `detail`: this endpoint answers 404
      // for an unknown conversation and 403 for someone else's, and rendering
      // the distinction would turn the toast into an existence oracle for
      // conversation ids. Callers already toast this same sentence.
      throw new Error('Failed to fetch messages');
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
  reason: string
): Promise<void> {
  const response = await authorizedFetch(`${API_URL}/messages/report`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      message_id: messageId,
      reason
    }),
    signal: AbortSignal.timeout(15000),
  });

  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.detail || 'Failed to report message');
  }
}
