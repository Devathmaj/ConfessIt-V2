import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * A reconnect must tell its subscriber that the transcript may have moved while
 * the socket was down. The socket carries only what happens next, so the dialog
 * re-reads the authoritative page when this callback fires.
 */

const wsTicket = vi.hoisted(() => ({
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

const open = async (connecting: Promise<boolean>) => {
  wsTicket.release?.();
  await Promise.resolve();
  await Promise.resolve();
  const socket = sockets.at(-1);
  expect(socket).toBeDefined();
  socket?.onopen?.();
  socket?.onmessage?.({ data: JSON.stringify({ type: 'connected' }) });
  await expect(connecting).resolves.toBe(true);
  return socket!;
};

beforeEach(() => {
  sockets.length = 0;
  wsTicket.release = null;
  vi.useFakeTimers();
  vi.stubGlobal('WebSocket', FakeWebSocket);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('chat reconnects', () => {
  it('fires the matchmaking reconnect callback only after a dropped socket reopens', async () => {
    const { messagingClient } = await import('@/lib/messaging');
    const onReconnect = vi.fn();
    messagingClient.onReconnect(onReconnect);

    const socket = await open(messagingClient.connect('conv-1'));
    expect(onReconnect).not.toHaveBeenCalled();

    socket.onclose?.({ code: 1006, reason: 'network' });
    await vi.advanceTimersByTimeAsync(1000);

    wsTicket.release?.();
    await Promise.resolve();
    await Promise.resolve();
    const reopened = sockets.at(-1)!;
    reopened.onopen?.();
    reopened.onmessage?.({ data: JSON.stringify({ type: 'connected' }) });

    expect(onReconnect).toHaveBeenCalledTimes(1);
    messagingClient.disconnect();
  });

  it('fires the Case Closed reconnect callback after recovery', async () => {
    const { caseClosedMessagingClient } = await import('@/lib/caseClosedMessaging');
    const socket = await open(caseClosedMessagingClient.connect());

    // The dialog subscribes after the first connect resolves.
    const onReconnect = vi.fn();
    caseClosedMessagingClient.onReconnect(onReconnect);

    socket.onclose?.({ code: 1006, reason: 'network' });
    await vi.advanceTimersByTimeAsync(1000);

    wsTicket.release?.();
    await Promise.resolve();
    await Promise.resolve();
    const reopened = sockets.at(-1)!;
    reopened.onopen?.();
    reopened.onmessage?.({ data: JSON.stringify({ type: 'connected' }) });

    expect(onReconnect).toHaveBeenCalledTimes(1);
    caseClosedMessagingClient.disconnect();
  });
});
