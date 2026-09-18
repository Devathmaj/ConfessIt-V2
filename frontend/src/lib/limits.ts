/**
 * Maximum lengths for user-supplied text.
 *
 * The API rejects anything longer with a 422, but a user who only discovers that after
 * pressing submit has been told "no" rather than shown why. These mirror the server-side
 * limits in backend/app/limits.py so the browser can stop the input at the same point.
 *
 * The server remains the authority; if the two ever disagree the request is rejected and
 * the UI is simply behind. Change both.
 */

export const CONFESSION_BODY_MAX = 500;

/**
 * Characters the server refuses to store, as a pattern the browser can apply too.
 *
 * The server's rule is that a confession may contain anything a person would actually type
 * -- emoji, Devanagari, Arabic, right-to-left text, newlines -- and must not contain
 * characters that either cannot be persisted at all (NUL, unpaired surrogates) or mean
 * nothing in prose (control characters, DEL, the C1 block, Unicode non-characters). This
 * mirrors the parts that can be expressed in a regex, so the user is told before they press
 * submit.
 *
 * Surrogates are deliberately absent rather than overlooked: JavaScript strings are UTF-16,
 * so a lone surrogate is representable here in a way it is not in a Python one, and a
 * browser cannot send one anyway. Ruling them out at this layer is a formality, not a
 * defence.
 *
 * Deliberately not included: the right-to-left override characters (U+202E, U+202D). They
 * are part of how Arabic and Hebrew are written, and refusing them would refuse the people
 * most likely to be confessing in a language that is not English.
 */
export const UNSTORABLE_CHARACTERS =
  /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F\uFDD0-\uFDEF\uFFFE\uFFFF]/;

/**
 * The astral non-characters: U+1FFFE and U+1FFFF in plane 1, up to U+10FFFE and U+10FFFF in
 * plane 16. A character class cannot express "the last two code points of every plane", because
 * each of these is a surrogate pair, so the pairs are listed explicitly.
 *
 * Both halves of the pair are named, and that is the part worth being careful about. The nine
 * leading surrogates here also begin thousands of assignable characters -- \uD83F is the first
 * half of U+1FC00, a Deseret letter -- so matching the leading surrogate on its own would refuse
 * every Deseret and CJK-extension character in the app. Written as a pair, \uD83F\uDC00 gets
 * through and only \uD83F\uDFFE does not.
 */
const ASTRAL_NON_CHARACTERS =
  /[\uD83F\uD87F\uD8FF\uD97F\uD9FF\uDA7F\uDAFF\uDB7F\uDBFF][\uDFFE\uDFFF]/;

/**
 * True when the text contains something the server will refuse to store.
 *
 * Two patterns rather than one because they answer to different mechanics: the first is a
 * character class over single units, the second is a high-surrogate range. Anything that needs
 * to know the answer in a boolean -- a guard, a form check -- goes through here so the two halves
 * cannot drift apart at a call site.
 */
export const containsUnstorableCharacter = (value: string): boolean =>
  UNSTORABLE_CHARACTERS.test(value) || ASTRAL_NON_CHARACTERS.test(value);

export const COMMENT_BODY_MAX = 500;
export const MESSAGE_BODY_MAX = 100;
export const REPORT_REASON_MAX = 1000;
export const RECIPIENT_NAME_MAX = 160;

/**
 * The identity fields an admin writes, capped to the column they land in.
 *
 * These mirror `USER_NAME_MAX` / `USER_REGNO_MAX` / `USER_DEPARTMENT_MAX` in
 * `backend/app/limits.py`, which is where the server enforces them. They exist on this side
 * only to put `maxLength` on the inputs: without it the operator can type past the cap and
 * discover the rule as a 422 after submitting, rather than as the field refusing the
 * keystroke. `maxLength` counts UTF-16 units and the column counts characters, so a name in an
 * astral script can still be refused by the server -- the cap here narrows that window, it
 * does not close it, which is why the error path has to be readable too.
 */
export const ADMIN_NAME_MAX = 160;
export const ADMIN_REGNO_MAX = 100;
export const ADMIN_DEPARTMENT_MAX = 80;

/**
 * How many messages one conversation holds, and how many of them one person may send.
 *
 * A cap on the conversation is a cap on its transcript, not on anybody's typing: the read
 * endpoint returns the whole thread in one response, so a conversation long enough to
 * overflow that window would hide its own newest messages from the two people in it.
 *
 * The per-person half is what makes that ceiling an exchange rather than a monologue --
 * neither side can spend the whole allowance on its own.
 *
 * The server decides; this exists so the dialog can show what is left and stop offering a
 * composer that would be refused. See `lib/messageQuota.ts` for how the numbers are read.
 */
export const CONVERSATION_MESSAGE_MAX = 200;
export const CONVERSATION_PARTICIPANT_MESSAGE_MAX = 100;
