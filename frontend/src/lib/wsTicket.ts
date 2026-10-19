// src/lib/wsTicket.ts

/**
 * Exchange an access token for a one-use WebSocket ticket.
 *
 * The access token is deliberately never placed in a WebSocket URL: Uvicorn logs
 * the accepted handshake path and its query string, so a 30-minute bearer there
 * would be written to stdout and any log store it reaches. The ticket returned
 * here is bound to the caller's session, expires in under a minute, and can be
 * redeemed once -- by the time a log line is read, it is normally already spent.
 *
 * The bearer is read from the store at the moment of the call, never handed in
 * by a caller: the token the auth context held used to be a copy that went
 * stale half an hour into a session, and a ticket exchange with a stale bearer
 * was answered 401 -- which read as "the session is over" and signed the person
 * out while their refresh cookie was still good for another day. `authorizedFetch`
 * refreshes an expired token and replays the request instead, so opening a chat
 * is no longer how a session ends.
 */

import { authorizedFetch } from './authFetch';
import { API_URL } from './apiBase';

export async function requestWebSocketTicket(): Promise<string> {
  const response = await authorizedFetch(`${API_URL}/auth/ws-ticket`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    signal: AbortSignal.timeout(15000),
  });

  if (!response.ok) {
    let detail = 'Could not open a live connection';
    let body: { detail?: unknown } | undefined;
    try {
      body = await response.json();
      if (typeof body?.detail === 'string') detail = body.detail;
    } catch {
      /* non-JSON refusal: keep the generic message */
    }
    throw new Error(detail);
  }

  const body = await response.json();
  if (typeof body?.ticket !== 'string' || body.ticket.length === 0) {
    throw new Error('The server did not return a WebSocket ticket');
  }
  return body.ticket;
}
