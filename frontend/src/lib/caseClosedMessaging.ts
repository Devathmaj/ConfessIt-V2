// src/lib/caseClosedMessaging.ts
/**
 * Live client for the Case Closed detective thread.
 *
 * Same transport shape as the matchmaking chat (`lib/messaging.ts`): a
 * short-lived token in the query string, 30s pings, 120s idle reap, five
 * backoff reconnects, no-retry on refused handshakes. The only differences
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
  private token: string | null = null;
  private messageCallbacks: Set<MessageCallback> = new Set();
  private errorCallbacks: Set<ErrorCallback> = new Set();
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 5;
  private reconnectDelay = 1000;
  private pingInterval: number | null = null;

  connect(token: string): Promise<void> {
    return new Promise((resolve, reject) => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        resolve();
        return;
      }
      this.disconnect();
      this.token = token;
      const wsUrl = `${apiWsBase()}/games/case-closed/ws?token=${token}`;

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
              resolve();
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
            setTimeout(() => {
              if (this.token) {
                this.connect(this.token).catch((error) => {
                  console.error('Case Closed WS reconnect failed:', error);
                });
              }
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

  disconnect(): void {
    this.stopPingInterval();
    if (this.ws) {
      try {
        if (this.ws.readyState === WebSocket.OPEN) this.ws.close(1000);
      } catch {
        /* already gone */
      }
      this.ws = null;
    }
    this.token = null;
    this.reconnectAttempts = 0;
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

export const caseClosedLimitNotice = (quota: CaseClosedMessageQuota | null): string | null => {
  if (quota === null || quota.can_send) return null;
  if (quota.limit_reached === 'conversation') {
    return `This detective thread has reached its limit of ${quota.conversation_limit} messages.`;
  }
  return `You have sent all ${quota.participant_limit} messages you can send in this thread.`;
};
