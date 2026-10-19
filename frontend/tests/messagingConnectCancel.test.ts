import { beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * A connect that is overtaken must not leave a socket behind.
 *
 * `connect` waits for a WebSocket ticket, and that wait can be overtaken: the
 * dialog closes, or the case ends, while the ticket is still being fetched. The
 * connect then used to carry on and open its socket anyway -- a live socket
 * that nobody closes, still pinging (so the server's idle reaper never fires),
 * still holding one of the Redis pool's connections, and still delivering into a
 * thread that has moved on.
 *
 * These tests hold the ticket open, disconnect while it is outstanding, and
 * check that no socket is ever opened.
 */

const wsTicket = vi.hoisted(() => ({
  /** Releases the held ticket when the test wants it to land. */
  release: null as null | (() => void),
}));

vi.mock('@/lib/wsTicket', () => ({
  requestWebSocketTicket: vi.fn(
    () =>
      new Promise<string>((resolve) => {
        wsTicket.release = () => resolve('one-use-ticket');
      }),
  ),
}));

/** A socket that records being built, so a test can see one that should not exist. */
const sockets: FakeWebSocket[] = [];

class FakeWebSocket {
  static OPEN = 1;
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  onclose: ((event: { code: number; reason: string }) => void) | null = null;
  send() {}
  close() {
    this.readyState = 3;
  }
  constructor(public url: string) {
    sockets.push(this);
  }
}

beforeEach(() => {
  sockets.length = 0;
  wsTicket.release = null;
  vi.stubGlobal('WebSocket', FakeWebSocket);
});

describe('a connect that is overtaken', () => {
  it('opens no socket when the matchmaking client is disconnected mid-flight', async () => {
    const { messagingClient } = await import('@/lib/messaging');

    const connecting = messagingClient.connect('conv-1');
    // The ticket is still in flight.
    await Promise.resolve();

    // The dialog closes.
    messagingClient.disconnect();

    // The ticket lands: the connect must notice it was overtaken and open
    // nothing.
    wsTicket.release?.();
    await expect(connecting).resolves.toBe(false);
    expect(sockets).toHaveLength(0);
  });

  it('opens no socket when the case closed client is disconnected mid-flight', async () => {
    const { caseClosedMessagingClient } = await import('@/lib/caseClosedMessaging');

    const connecting = caseClosedMessagingClient.connect();
    await Promise.resolve();

    // The thread closes.
    caseClosedMessagingClient.disconnect();

    wsTicket.release?.();
    await expect(connecting).resolves.toBe(false);
    expect(sockets).toHaveLength(0);
  });

  it('still opens a socket when nothing overtook it', async () => {
    const { messagingClient } = await import('@/lib/messaging');

    const connecting = messagingClient.connect('conv-1');
    await Promise.resolve();
    wsTicket.release?.();
    // The socket opens, and the connect resolves once the server says hello.
    await Promise.resolve();
    await Promise.resolve();
    const opened = sockets[0];
    expect(opened).toBeDefined();
    opened?.onopen?.();
    opened?.onmessage?.({ data: JSON.stringify({ type: 'connected' }) });

    await expect(connecting).resolves.toBe(true);
    messagingClient.disconnect();
  });
});
