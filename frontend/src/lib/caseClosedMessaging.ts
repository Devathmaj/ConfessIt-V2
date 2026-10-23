// src/lib/caseClosedMessaging.ts
/**
 * Live client for the Case Closed detective thread.
 *
 * Same transport shape as the matchmaking chat (`lib/messaging.ts`): a
 * short-lived, single-use ticket in the query string, 30s pings, 120s idle
 * reap, five backoff reconnects, no-retry on refused handshakes. The only differences
 * are the endpoint and the scope: `/games/case-closed/ws` resolves the
 * caller's couple session server-side, so there is no conversation id in
 * the path and the channel is `case-closed:{session_id}` (distinct
 * namespace, same Redis infrastructure).
 */

import { apiWsBase } from './apiBase';
import {
  CASE_CLOSED_CONVERSATION_MESSAGE_MAX,
  CASE_CLOSED_CONVERSATION_PARTICIPANT_MESSAGE_MAX,
} from './limits';
import { quotaAfterIncomingMessage } from './messageQuota';
import { requestWebSocketTicket } from './wsTicket';
import type { CaseClosedMessageQuota } from '@/services/api';

export interface CaseClosedLiveMessage {
  id: string;
  game_id?: string | null;
  session_id: string;
  sender_user_id: string;
  sender_name?: string;
  body: string;
  created_at: string;
  is_read: boolean;
}

interface WsFrame {
  type: 'connected' | 'message' | 'error';
  game_id?: string;
  session_id?: string;
  data?: CaseClosedLiveMessage;
  error?: string;
}

type MessageCallback = (message: CaseClosedLiveMessage) => void;
type ErrorCallback = (error: Error) => void;

const WS_CLOSE_NO_RETRY = new Set([4001, 4003, 4403]);

class CaseClosedMessagingClient {
  private ws: WebSocket | null = null;
  private messageCallbacks: Set<MessageCallback> = new Set();
  private errorCallbacks: Set<ErrorCallback> = new Set();
  private reconnectCallbacks: Set<() => void> = new Set();
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 5;
  private reconnectDelay = 1000;
  private pingInterval: number | null = null;
  /** True while this connect is the retry of a dropped socket, not the first open. */
  private recovering = false;

  /**
   * The connection's generation.
   *
   * A connect waits for a ticket, and that wait can be overtaken: the thread
   * closes, or the case ends, and the ticket arrives afterwards for a socket
   * nobody wants. Opening it anyway leaves a live socket behind -- still
   * pinging, still holding a Redis connection, and still delivering into a
   * thread that has moved on.
   */
  private generation = 0;

  /**
   * Open a live stream for the caller's thread.
   *
   * Answers whether the stream was opened: a connect that is overtaken opens
   * nothing and answers false, so the caller knows not to subscribe to a
   * socket that will never deliver anything.
   */
  async connect(reconnect = false): Promise<boolean> {
    if (this.ws?.readyState === WebSocket.OPEN) {
      return true;
    }
    if (reconnect) {
      // A reconnect must keep the subscribers and the recovering flag that the
      // dropped socket set. Full disconnect() is for the dialog closing.
      this.closeSocket();
    } else {
      this.disconnect();
    }
    // Read after the disconnect, which moves the generation on: this connect
    // belongs to the generation it starts in.
    const generation = this.generation;

    // The URL carries a one-use ticket, never the access token: Uvicorn logs the
    // accepted handshake path with its query string. A reconnect mints a fresh
    // ticket, so a spent or expired value is never reused. The bearer behind the
    // ticket is read from the store when the exchange happens -- a token captured
    // when the dialog opened would be an expired one by the evening.
    const ticket = await requestWebSocketTicket();
    if (generation !== this.generation) {
      // Overtaken while the ticket was being fetched: the thread closed and
      // the socket this would open is not wanted.
      return false;
    }
    const wsUrl = `${apiWsBase()}/games/case-closed/ws?ticket=${encodeURIComponent(ticket)}`;

    return new Promise((resolve, reject) => {
      try {
        this.ws = new WebSocket(wsUrl);

        this.ws.onopen = () => {
          this.reconnectAttempts = 0;
          this.startPingInterval();
        };

        this.ws.onmessage = (event) => {
          try {
            const data: WsFrame = JSON.parse(event.data);
            if (data.type === 'connected') {
              const recovered = this.recovering;
              this.recovering = false;
              resolve(true);
              if (recovered) {
                this.reconnectCallbacks.forEach((callback) => {
                  try {
                    callback();
                  } catch (error) {
                    console.error('Error in Case Closed reconnect callback:', error);
                  }
                });
              }
            } else if (data.type === 'message' && data.data) {
              this.messageCallbacks.forEach((callback) => {
                try {
                  callback(data.data!);
                } catch (error) {
                  console.error('Error in Case Closed message callback:', error);
                }
              });
            } else if (data.type === 'error') {
              this.errorCallbacks.forEach((callback) => {
                callback(new Error(data.error || 'Unknown WebSocket error'));
              });
            }
          } catch (error) {
            console.error('Error parsing Case Closed WS frame:', error);
          }
        };

        this.ws.onerror = () => {
          this.errorCallbacks.forEach((callback) => {
            callback(new Error('WebSocket connection error'));
          });
          reject(new Error('WebSocket connection failed'));
        };

        this.ws.onclose = (event) => {
          this.stopPingInterval();
          if (WS_CLOSE_NO_RETRY.has(event.code)) {
            const refusal = new Error(`WebSocket refused the handshake (code ${event.code})`);
            this.errorCallbacks.forEach((callback) => {
              try {
                callback(refusal);
              } catch (error) {
                console.error('Error in error callback:', error);
              }
            });
            return;
          }
          if (event.code !== 1000 && this.reconnectAttempts < this.maxReconnectAttempts) {
            this.reconnectAttempts++;
            this.recovering = true;
            setTimeout(() => {
              this.connect(true).catch((error) => {
                console.error('Case Closed WS reconnect failed:', error);
              });
            }, this.reconnectDelay * this.reconnectAttempts);
          }
        };
      } catch (error) {
        reject(error);
      }
    });
  }

  onMessage(callback: MessageCallback): () => void {
    this.messageCallbacks.add(callback);
    return () => {
      this.messageCallbacks.delete(callback);
    };
  }

  onError(callback: ErrorCallback): () => void {
    this.errorCallbacks.add(callback);
    return () => {
      this.errorCallbacks.delete(callback);
    };
  }

  /** Fired after a dropped socket reopens; re-read the transcript here. */
  onReconnect(callback: () => void): () => void {
    this.reconnectCallbacks.add(callback);
    return () => {
      this.reconnectCallbacks.delete(callback);
    };
  }

  private closeSocket(): void {
    if (!this.ws) return;
    try {
      if (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING) {
        this.ws.close(1000);
      }
    } catch {
      /* already gone */
    }
    this.ws = null;
  }

  disconnect(): void {
    this.stopPingInterval();
    // The generation moves on, so a connect that is still waiting for its
    // ticket learns that its answer is no longer wanted and opens nothing.
    this.generation += 1;
    this.closeSocket();
    this.reconnectAttempts = 0;
    this.recovering = false;
    this.reconnectCallbacks.clear();
  }

  private startPingInterval() {
    this.stopPingInterval();
    this.pingInterval = window.setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) this.ws.send('ping');
    }, 30000);
  }

  private stopPingInterval() {
    if (this.pingInterval !== null) {
      clearInterval(this.pingInterval);
      this.pingInterval = null;
    }
  }
}

export const caseClosedMessagingClient = new CaseClosedMessagingClient();

/** Quota fallback before the first transcript arrives (400/200, not 200/100). */
export const emptyCaseClosedQuota = (): CaseClosedMessageQuota => ({
  conversation_limit: CASE_CLOSED_CONVERSATION_MESSAGE_MAX,
  participant_limit: CASE_CLOSED_CONVERSATION_PARTICIPANT_MESSAGE_MAX,
  conversation_count: 0,
  participant_count: 0,
  conversation_remaining: CASE_CLOSED_CONVERSATION_MESSAGE_MAX,
  participant_remaining: CASE_CLOSED_CONVERSATION_PARTICIPANT_MESSAGE_MAX,
  can_send: true,
  limit_reached: null,
});

export const caseClosedQuotaLabel = (quota: CaseClosedMessageQuota | null): string | null =>
  quota === null ? null : `${quota.participant_count}/${quota.participant_limit} sent`;

/**
 * The allowance after the caller's own Case Closed turn was removed by content
 * safety.
 *
 * Mirrors the matchmaking case: the refused send (HTTP 422) carries no `quota`
 * but the attempt was still spent, so the counter has to move now rather than
 * wait for the next transcript read, or the header keeps showing the
 * pre-removal figure. Shares the arithmetic with `messageQuota` so the two
 * chats can never drift apart.
 */
export const caseClosedQuotaAfterRemovedMessage = (
  quota: CaseClosedMessageQuota | null
): CaseClosedMessageQuota | null => quotaAfterIncomingMessage(quota, true);

export const caseClosedLimitNotice = (quota: CaseClosedMessageQuota | null): string | null => {
  if (quota === null || quota.can_send) return null;
  if (quota.limit_reached === 'conversation') {
    return `This detective thread has reached its limit of ${quota.conversation_limit} messages.`;
  }
  return `You have sent all ${quota.participant_limit} messages you can send in this thread.`;
};
