import { describe, it, expect } from 'vitest';
import {
  notificationTargetPath,
  notificationIsClickable,
} from '@/lib/notificationTargets';

/**
 * Where a notification takes you.
 *
 * The server decides *whether* a notification is clickable by sending a target pair or by
 * sending neither, and this module decides *where* the pair goes. Both halves are exercised
 * here because the interesting failures are on either side of the boundary and both look the
 * same in the UI if they get through: a card that looks live and does nothing, or one that
 * navigates to a page which quietly does not contain what the notification announced.
 *
 * These are pure-function tests, deliberately: the routing rules sit in this module precisely
 * so they can be checked without rendering a page, and a card test cannot tell "wrong path"
 * apart from "no path" any better than this can.
 */

describe('notificationTargetPath', () => {
  it('sends a conversation notification to that match in the Message Box', () => {
    // The target is a *match* id, not a conversation id -- every route that reaches a
    // conversation is keyed by match, and the param is named for what the client must supply.
    expect(notificationTargetPath('conversation', 'm-123')).toBe('/inbox?match=m-123');
  });

  it('sends a love note notification to the note in the inbox tab', () => {
    expect(notificationTargetPath('love_note', 'n-456')).toBe(
      '/love-notes?tab=inbox&note=n-456'
    );
  });

  it('opens nothing for a notification with no target', () => {
    // The common case, and the one that used to be a dead card with a live-looking cursor.
    expect(notificationTargetPath(null, null)).toBeNull();
    expect(notificationTargetPath(undefined, undefined)).toBeNull();
  });

  it('opens nothing when only half a target arrived', () => {
    // The server refuses to store half a pair, so this state is unreachable from it. Treating
    // it as clickable anyway would mean one half of the pair reached the client -- through an
    // older server, a cache, or a hand-written row -- and produced a link to nothing.
    expect(notificationTargetPath('conversation', null)).toBeNull();
    expect(notificationTargetPath(null, 'm-123')).toBeNull();
  });

  it('opens nothing for a target kind it has never heard of', () => {
    // The asymmetry is the point: the server may know a kind this build does not, and the
    // answer then has to be "not clickable" rather than a guessed URL. A guess would put the
    // reader on a 404 with a card that had promised them something.
    expect(notificationTargetPath('report', 'r-1')).toBeNull();
    expect(notificationTargetPath('', 'm-123')).toBeNull();
  });

  it('escapes an id rather than splicing it into the URL', () => {
    // Ids are server-generated, but the query string is assembled by string concatenation
    // precisely because the two routes carry different params. Encoding is what stops a value
    // containing '&' or '=' from silently adding or overwriting one.
    expect(notificationTargetPath('conversation', 'm 1&tab=create')).toBe(
      '/inbox?match=m%201%26tab%3Dcreate'
    );
  });
});

describe('notificationIsClickable', () => {
  it('agrees with the path it would navigate to', () => {
    // One predicate rather than an `if (path)` repeated for the cursor, the focus ring and the
    // handler -- those three drifting apart is how a card ends up keyboard-reachable and inert.
    const cases: [string | null, string | null][] = [
      ['conversation', 'm-123'],
      ['love_note', 'n-456'],
      [null, null],
      ['conversation', null],
      [null, 'm-123'],
      ['report', 'r-1'],
    ];

    cases.forEach(([kind, id]) => {
      expect(notificationIsClickable(kind, id)).toBe(
        notificationTargetPath(kind, id) !== null
      );
    });
  });

  it('is true for both kinds the server can send', () => {
    expect(notificationIsClickable('conversation', 'm-123')).toBe(true);
    expect(notificationIsClickable('love_note', 'n-456')).toBe(true);
  });

  it('is false for a purely informational notice', () => {
    // "You appeared in matchmaking" is the example that has to stay inert: a match id is a
    // fact about two people, not a page, and there is no list of your matches to open.
    expect(notificationIsClickable(null, null)).toBe(false);
  });
});