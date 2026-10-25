import { logger } from './logger';
// src/lib/sessionChannel.ts

/**
 * Telling this browser's other tabs that the session ended.
 *
 * The refresh cookie is shared by every tab of a browser, so a log out in one
 * of them is a log out in all of them: the cookie is gone and the server
 * session is revoked, whether the other tabs know it or not. A tab that is not
 * told keeps looking signed in until one of its requests fails -- which, for a
 * log out, is the difference between "signed out" and "signed out, eventually,
 * after something breaks".
 *
 * The channel carries no secrets: it says that the session ended, never what
 * the session was. The bearer never leaves the tab that holds it.
 *
 * A browser without the API (or a server-side render, where there is no
 * `window` at all) simply has no channel: the feature is a courtesy between
 * tabs, never a requirement for correctness.
 */

/** The channel every tab of this app listens on. */
export const SESSION_CHANNEL_NAME = 'confessit:session';

/** What a tab tells the others. */
export interface SessionEndedMessage {
  type: 'session-ended';
}

type SessionListener = () => void;

const supported = (): boolean =>
  typeof window !== 'undefined' && typeof BroadcastChannel !== 'undefined';

let channel: BroadcastChannel | null = null;
const listeners = new Set<SessionListener>();

const ensureChannel = (): BroadcastChannel | null => {
  if (!supported()) return null;
  if (channel) return channel;
  channel = new BroadcastChannel(SESSION_CHANNEL_NAME);
  channel.onmessage = (event: MessageEvent) => {
    const message = event.data as SessionEndedMessage | null;
    if (!message || message.type !== 'session-ended') return;
    listeners.forEach((listener) => {
      try {
        listener();
      } catch (error) {
        // One tab's bad day must not stop the others from hearing about it.
        logger.error('Session-ended listener failed:', error);
      }
    });
  };
  return channel;
};

/**
 * Tell the other tabs that the session ended.
 *
 * Best effort by nature: a tab that is not listening (or a browser without the
 * API) simply does not hear it, and its own requests will find out soon enough.
 */
export const announceSessionEnded = (): void => {
  const active = ensureChannel();
  if (!active) return;
  try {
    active.postMessage({ type: 'session-ended' } satisfies SessionEndedMessage);
  } catch (error) {
    logger.error('Could not announce the session end:', error);
  }
};

/**
 * Listen for the other tabs' session endings.
 *
 * Returns the unsubscribe function.
 */
export const onSessionEnded = (listener: SessionListener): (() => void) => {
  ensureChannel();
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
};
