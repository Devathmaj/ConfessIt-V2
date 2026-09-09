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

export const CONFESSION_BODY_MAX = 2000;
export const COMMENT_BODY_MAX = 500;
export const MESSAGE_BODY_MAX = 2000;
export const REPORT_REASON_MAX = 1000;
export const RECIPIENT_NAME_MAX = 160;
