import { describe, it, expect } from 'vitest';
import {
  CONVERSATION_MESSAGE_MAX,
  CONVERSATION_PARTICIPANT_MESSAGE_MAX,
} from '@/lib/limits';
import {
  canSendAnotherMessage,
  emptyMessageQuota,
  messageLimitNotice,
  messageQuotaLabel,
  quotaAfterIncomingMessage,
} from '@/lib/messageQuota';
import type { MessageQuota } from '@/lib/messageQuota';

/**
 * What the chat dialog believes about how much of a conversation's allowance is left.
 *
 * The server is the authority on the cap itself -- these tests assume it says `can_send: false`
 * and are about what the dialog then *shows*. That is worth testing on its own because the
 * failure is invisible when it happens: a dialog that keeps offering a composer which is always
 * refused looks exactly like one that is working, right up until someone hits the limit.
 *
 * The specific risk worth guarding is the wording. "You have sent all 100 messages" and "This
 * conversation has reached its limit of 200 messages" describe genuinely different situations,
 * and a user who has spent their own share is owed the first of those sentences: the other
 * conversation still has room, it is theirs that ran out, and telling them otherwise implies
 * something the other person did that they did not do.
 *
 * Pure-function tests deliberately: these rules are separated into `lib/messageQuota.ts` so they
 * can be checked without rendering the dialog, and a module that exports both a component and a
 * plain function cannot Fast Refresh.
 */

const quotaOf = (overrides: Partial<MessageQuota> = {}): MessageQuota => ({
  ...emptyMessageQuota(),
  ...overrides,
});

describe('emptyMessageQuota', () => {
  it('starts a conversation at the full allowance', () => {
    const quota = emptyMessageQuota();

    expect(quota.conversation_limit).toBe(CONVERSATION_MESSAGE_MAX);
    expect(quota.participant_limit).toBe(CONVERSATION_PARTICIPANT_MESSAGE_MAX);
    expect(quota.conversation_remaining).toBe(CONVERSATION_MESSAGE_MAX);
    expect(quota.participant_remaining).toBe(CONVERSATION_PARTICIPANT_MESSAGE_MAX);
    expect(quota.can_send).toBe(true);
    expect(quota.limit_reached).toBeNull();
  });
});

describe('canSendAnotherMessage', () => {
  it('allows a send while the server says there is room', () => {
    expect(canSendAnotherMessage(quotaOf({ participant_count: 99 }))).toBe(true);
  });

  it('refuses a send once the allowance is spent', () => {
    expect(canSendAnotherMessage(quotaOf({ can_send: false }))).toBe(false);
  });

  it('allows a send when the allowance has not arrived yet', () => {
    // The first paint of a conversation has no allowance to show. Treating "unknown" as "full"
    // costs at most one refused send, which the server then explains; treating it as "spent"
    // would close a conversation that has room in it, with no way to reopen it.
    expect(canSendAnotherMessage(null)).toBe(true);
  });
});

describe('messageQuotaLabel', () => {
  it('reports the caller own spending against their own limit', () => {
    // The caller's share, not the conversation's: the number a reader watches going up is the
    // one they control.
    expect(messageQuotaLabel(quotaOf({ participant_count: 12, conversation_count: 90 }))).toBe(
      '12/100 sent'
    );
  });

  it('says nothing when the server reported no allowance', () => {
    expect(messageQuotaLabel(null)).toBeNull();
  });
});

describe('messageLimitNotice', () => {
  it('is silent while there is room to send', () => {
    expect(messageLimitNotice(quotaOf({ participant_count: 12 }))).toBeNull();
  });

  it('blames the caller own allowance when that is what ran out', () => {
    // The conversation may still have room. Saying otherwise would read as an accusation about
    // the other person.
    const notice = messageLimitNotice(
      quotaOf({
        participant_count: 100,
        conversation_count: 130,
        participant_remaining: 0,
        conversation_remaining: 70,
        can_send: false,
        limit_reached: 'participant',
      })
    );

    expect(notice).toBe('You have sent all 100 messages you can send in this conversation.');
    expect(notice).not.toContain('200');
  });

  it('blames the conversation when the thread itself is full', () => {
    const notice = messageLimitNotice(
      quotaOf({
        conversation_count: 200,
        participant_count: 100,
        conversation_remaining: 0,
        participant_remaining: 0,
        can_send: false,
        limit_reached: 'conversation',
      })
    );

    expect(notice).toBe('This conversation has reached its limit of 200 messages.');
  });
});

describe('quotaAfterIncomingMessage', () => {
  it('spends the caller own allowance on their own message', () => {
    const quota = quotaAfterIncomingMessage(quotaOf({ participant_count: 99 }), true)!;

    expect(quota.participant_count).toBe(100);
    expect(quota.participant_remaining).toBe(0);
    expect(quota.can_send).toBe(false);
    expect(quota.limit_reached).toBe('participant');
  });

  it('spends only the conversation room on a partner message', () => {
    // The partner's message is not something this person wrote, and taking it off their own
    // allowance would close a conversation they still have room to speak in.
    const quota = quotaAfterIncomingMessage(quotaOf({ participant_count: 10 }), false)!;

    expect(quota.conversation_count).toBe(1);
    expect(quota.participant_count).toBe(10);
    expect(quota.participant_remaining).toBe(90);
    expect(quota.can_send).toBe(true);
  });

  it('closes the conversation when the last room goes to a partner message', () => {
    const quota = quotaAfterIncomingMessage(
      quotaOf({
        conversation_count: CONVERSATION_MESSAGE_MAX - 1,
        participant_count: 5,
        conversation_remaining: 1,
        participant_remaining: 95,
      }),
      false
    )!;

    expect(quota.conversation_remaining).toBe(0);
    expect(quota.can_send).toBe(false);
    expect(quota.limit_reached).toBe('conversation');
  });

  it('clamps at zero rather than showing a negative', () => {
    // A client briefly behind the server -- a websocket frame the reload had not caught up with --
    // should show a spent allowance, not one already overrun.
    const quota = quotaAfterIncomingMessage(
      quotaOf({
        conversation_count: CONVERSATION_MESSAGE_MAX,
        participant_count: CONVERSATION_PARTICIPANT_MESSAGE_MAX,
        conversation_remaining: 0,
        participant_remaining: 0,
      }),
      false
    )!;

    expect(quota.conversation_remaining).toBe(0);
    expect(quota.participant_remaining).toBe(0);
    expect(quota.can_send).toBe(false);
  });

  it('stays unknown when the server has not reported an allowance', () => {
    expect(quotaAfterIncomingMessage(null, false)).toBeNull();
  });
});