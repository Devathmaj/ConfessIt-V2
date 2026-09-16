/**
 * Reading the message allowance of a conversation.
 *
 * The server decides whether a message may be sent and says how much room is left; this
 * module only decides what the chat dialog *shows* about that answer. It exists apart from
 * the dialog for the reason `lib/reportAuthors.ts` does: the arithmetic is worth testing,
 * and a module that exports both a component and a plain function cannot Fast Refresh.
 *
 * Everything here reads the server's numbers. Nothing counts messages out of the transcript
 * the client happens to be holding, because that list is capped by what was fetched, by
 * what has arrived live since, and by what a moderator has taken down -- three different
 * answers to "how many messages have been sent here", none of them the server's.
 */

import {
  CONVERSATION_MESSAGE_MAX,
  CONVERSATION_PARTICIPANT_MESSAGE_MAX,
} from './limits';

/** Which allowance ran out, as the server names them. */
export type MessageLimitKind = 'participant' | 'conversation' | null;

export interface MessageQuota {
  /** How many messages the whole conversation may hold. */
  conversation_limit: number;
  /** How many of those one participant may send. */
  participant_limit: number;
  /** How many the conversation holds now, including any a moderator has removed. */
  conversation_count: number;
  /** How many of them the caller wrote. */
  participant_count: number;
  conversation_remaining: number;
  participant_remaining: number;
  can_send: boolean;
  limit_reached: MessageLimitKind;
}

/**
 * Whether the composer should accept a message.
 *
 * A conversation the client has not been told about yet is not a closed one: the allowance
 * arrives with the history, and until it does the honest answer is that sending is allowed
 * and the server has the last word. Treating "unknown" as "full" would lock the dialog for
 * a conversation that has room in it.
 */
export const canSendAnotherMessage = (quota: MessageQuota | null): boolean =>
  quota === null || quota.can_send;

/**
 * Why the composer is closed, in a sentence.
 *
 * `null` when there is nothing to explain. The two sentences are deliberately different:
 * a participant who has spent their own allowance has a thread they can still read and
 * nothing more to put in it, which is a different situation from a thread that has run out
 * of room for both of them.
 */
export const messageLimitNotice = (quota: MessageQuota | null): string | null => {
  if (quota === null || quota.can_send) return null;
  if (quota.limit_reached === 'conversation') {
    return `This conversation has reached its limit of ${quota.conversation_limit} messages.`;
  }
  return `You have sent all ${quota.participant_limit} messages you can send in this conversation.`;
};

/**
 * The caller's own spending, for the thread header: `12/100 sent`.
 *
 * Counts what this person has written rather than what the conversation holds, so the
 * number a reader watches going up is the one they control.
 */
export const messageQuotaLabel = (quota: MessageQuota | null): string | null =>
  quota === null ? null : `${quota.participant_count}/${quota.participant_limit} sent`;

/**
 * The same allowance after a message arrives over the live socket.
 *
 * A message the caller did not write spends the conversation's room, not theirs, so only
 * that half moves. Derived from the server's numbers rather than counted, and clamped at
 * zero so a client that is briefly behind the server cannot show a negative.
 */
export const quotaAfterIncomingMessage = (
  quota: MessageQuota | null,
  fromCaller: boolean
): MessageQuota | null => {
  if (quota === null) return null;

  const conversationCount = quota.conversation_count + 1;
  const participantCount = quota.participant_count + (fromCaller ? 1 : 0);
  const conversationRemaining = Math.max(0, quota.conversation_limit - conversationCount);
  const participantRemaining = Math.max(0, quota.participant_limit - participantCount);

  // Same order the server uses: the caller's own allowance first, because it is the limit
  // that binds in normal use and the more useful thing to be told.
  const limitReached: MessageLimitKind =
    participantRemaining === 0
      ? 'participant'
      : conversationRemaining === 0
        ? 'conversation'
        : null;

  return {
    ...quota,
    conversation_count: conversationCount,
    participant_count: participantCount,
    conversation_remaining: conversationRemaining,
    participant_remaining: participantRemaining,
    can_send: limitReached === null,
    limit_reached: limitReached,
  };
};

/**
 * A quota for a conversation the server has not reported one for.
 *
 * Only used when a read fails to carry the field -- an older backend, or a response shape
 * this client does not expect -- so that the dialog still has something to render. It starts
 * at the full allowance, which is what "nothing has been spent yet" means, and it is why the
 * server has to be the authority: a client that guesses here can only ever be too generous,
 * and being too generous costs one refused send.
 */
export const emptyMessageQuota = (): MessageQuota => ({
  conversation_limit: CONVERSATION_MESSAGE_MAX,
  participant_limit: CONVERSATION_PARTICIPANT_MESSAGE_MAX,
  conversation_count: 0,
  participant_count: 0,
  conversation_remaining: CONVERSATION_MESSAGE_MAX,
  participant_remaining: CONVERSATION_PARTICIPANT_MESSAGE_MAX,
  can_send: true,
  limit_reached: null,
});