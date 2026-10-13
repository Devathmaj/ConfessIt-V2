// src/lib/wsTicket.ts

/**
 * Exchange an access token for a one-use WebSocket ticket.
 *
 * The access token is deliberately never placed in a WebSocket URL: Uvicorn logs
 * the accepted handshake path and its query string, so a 30-minute bearer there
 * would be written to stdout and any log store it reaches. The ticket returned
 * here is bound to the caller's session, expires in under a minute, and can be
 * redeemed once -- by the time a log line is read, it is normally already spent.
 */

import { endSessionIfTokenWasRejected } from './authEvents';
import { API_URL } from './apiBase';

export async function requestWebSocketTicket(token: string): Promise<string> {
  const response = await fetch(`${API_URL}/auth/ws-ticket`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${token}`,
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
    // This fetch does not pass through the axios refresh interceptor. A 401 means
    // the access token itself was rejected, so end the session rather than leaving
    // the UI looking signed in. The response envelope carries the headers that let
    // the shared helper distinguish an ordinary expiry from a superseded session.
    if (response.status === 401) {
      endSessionIfTokenWasRejected({
        response: {
          status: response.status,
          headers: Object.fromEntries(response.headers.entries()),
          data: body,
        },
      });
    }
    throw new Error(detail);
  }

  const body = await response.json();
  if (typeof body?.ticket !== 'string' || body.ticket.length === 0) {
    throw new Error('The server did not return a WebSocket ticket');
  }
  return body.ticket;
}
