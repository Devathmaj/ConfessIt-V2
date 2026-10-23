/**
 * Bounded in-memory message windows.
 *
 * Chat transcripts are the one kind of data this app deliberately keeps out of
 * persistent storage. The live socket owns the current state, and a reconnect
 * re-reads the authoritative window from the backend. Even in memory the list is
 * bounded: a long-lived tab must not grow without limit, and a bound equal to the
 * server's conversation ceiling means normal paging never loses a visible row.
 *
 * The newest rows are kept. If a caller ever hands in more than the ceiling --
 * a legacy server returning the whole thread, or an older page plus a live
 * arrival -- the oldest are dropped and can be re-read with the pager.
 */
export const boundMessages = <T>(messages: T[], max: number): T[] => {
  if (max <= 0) return [];
  return messages.length <= max ? messages : messages.slice(messages.length - max);
};

/** Append one message while keeping the newest `max` rows. */
export const appendBounded = <T>(messages: T[], message: T, max: number): T[] =>
  boundMessages([...messages, message], max);
