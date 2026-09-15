import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  matchesConversationFilter,
  conversationIsExpired,
  conversationIsTerminated,
  type ConversationFilter,
} from '@/pages/MessageBoxPage';

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;

const STATUSES = ['pending', 'requested', 'accepted', 'rejected', 'terminated'] as const;

/**
 * A conversation `offsetMs` from now, so "expired" is a function of the fake clock rather
 * than a hardcoded literal that would rot.
 */
const conversation = (status: (typeof STATUSES)[number], offsetMs: number) =>
  ({
    match: {
      id: 'm1',
      expires_at: new Date(Date.now() + offsetMs).toISOString(),
      created_at: new Date().toISOString(),
      is_expired: offsetMs < 0,
    },
    conversation: {
      id: 'c1',
      status,
      initiator_regno: 'A',
      receiver_regno: 'B',
      created_at: new Date().toISOString(),
    },
    other_user: { regno: 'B', name: 'Bob', class_name: 'CSE' },
    is_initiator: true,
  }) as any;

const LIVE = 2 * HOUR;
const GONE = -1 * HOUR;

describe('matchesConversationFilter', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-03-01T12:00:00Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('puts an expired accepted conversation under Expired, not Active', () => {
    // The change: expiry used to leave these in Active, where they read as chatable.
    const expired = conversation('accepted', GONE);

    expect(matchesConversationFilter(expired, 'expired')).toBe(true);
    expect(matchesConversationFilter(expired, 'active')).toBe(false);
  });

  it('leaves a live accepted conversation under Active', () => {
    const live = conversation('accepted', LIVE);

    expect(matchesConversationFilter(live, 'active')).toBe(true);
    expect(matchesConversationFilter(live, 'expired')).toBe(false);
  });

  it('keeps an expired conversation reachable rather than dropping it', () => {
    const expired = conversation('accepted', GONE);

    expect(matchesConversationFilter(expired, 'all')).toBe(true);
    expect(matchesConversationFilter(expired, 'expired')).toBe(true);
  });

  it('does not let expiry pull a conversation out of All', () => {
    const every: ConversationFilter[] = [
      'all', 'pending', 'active', 'expired', 'rejected', 'terminated',
    ];

    STATUSES.forEach(status => {
      expect(matchesConversationFilter(conversation(status, GONE), 'all')).toBe(true);
    });
    expect(every).toHaveLength(6);
  });

  it('reports an expired conversation regardless of its stored status', () => {
    // Expiry is checked ahead of status, which is the order the status badge already used.
    STATUSES.filter(status => status !== 'terminated').forEach(status => {
      expect(matchesConversationFilter(conversation(status, GONE), 'expired')).toBe(true);
    });
  });

  it('leaves the other status tabs on their stored status', () => {
    expect(matchesConversationFilter(conversation('pending', LIVE), 'pending')).toBe(true);
    expect(matchesConversationFilter(conversation('rejected', LIVE), 'rejected')).toBe(true);
    expect(matchesConversationFilter(conversation('accepted', LIVE), 'pending')).toBe(false);
  });

  it('switches to expired on the same side of the boundary as the server', () => {
    // The server asks `current_time > match_expires_at`, so a conversation is still live at
    // the exact instant of expiry and has lapsed a millisecond later. Kept strict to match.
    expect(matchesConversationFilter(conversation('accepted', 1), 'active')).toBe(true);
    expect(matchesConversationFilter(conversation('accepted', 0), 'expired')).toBe(false);
    expect(matchesConversationFilter(conversation('accepted', -1), 'expired')).toBe(true);
  });


  it('treats an unparseable expiry as not expired rather than throwing', () => {
    const broken = conversation('accepted', LIVE);
    broken.match.expires_at = 'nonsense';

    expect(conversationIsExpired(broken)).toBe(false);
    expect(matchesConversationFilter(broken, 'active')).toBe(true);
  });

  it('lists a terminated thread under Terminated even before the match expires', () => {
    // The reported bug: closing the thread moved it out of the receiver's message box. It has
    // to stay listed, and a fresh termination is not expired -- which is exactly why the two
    // questions cannot be answered from one flag.
    const closed = conversation('terminated', LIVE);

    expect(conversationIsTerminated(closed)).toBe(true);
    expect(conversationIsExpired(closed)).toBe(false);
    expect(matchesConversationFilter(closed, 'terminated')).toBe(true);
    expect(matchesConversationFilter(closed, 'all')).toBe(true);
  });

  it('keeps a terminated thread out of the tabs it can no longer be used from', () => {
    const closed = conversation('terminated', LIVE);

    // Chat and accept/reject are gone, so listing it beside live threads would offer actions
    // that the server refuses.
    expect(matchesConversationFilter(closed, 'active')).toBe(false);
    expect(matchesConversationFilter(closed, 'pending')).toBe(false);
    expect(matchesConversationFilter(closed, 'rejected')).toBe(false);
  });

  it('does not let a terminated thread also claim to be merely expired', () => {
    // Termination outranks expiry even when the match has since lapsed, so the tabs stay a
    // partition: one row, one tab.
    const closed = conversation('terminated', GONE);

    expect(matchesConversationFilter(closed, 'terminated')).toBe(true);
    expect(matchesConversationFilter(closed, 'expired')).toBe(false);
  });

  it('leaves the other tabs on their stored status when a thread is terminated', () => {
    STATUSES.filter(status => status !== 'terminated').forEach(status => {
      expect(conversationIsTerminated(conversation(status, LIVE))).toBe(false);
      expect(matchesConversationFilter(conversation(status, LIVE), 'terminated')).toBe(false);
    });
  });
});
