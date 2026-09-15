// src/lib/reportReasons.ts
/**
 * The reasons a reporter can pick from, shared by every report dialog in the app.
 *
 * One list rather than one per dialog: a moderator reading "harassment" on a chat report
 * and "Harassment" on a love-note report has to mean the same thing by both, and a list
 * that drifts between screens is how that quietly stops being true. The server stores the
 * value verbatim and does not constrain it, so this list is the whole vocabulary — add a
 * reason here and it becomes reportable everywhere at once.
 */
export const REPORT_REASONS = [
  { value: 'harassment', label: 'Harassment or bullying' },
  { value: 'hate_speech', label: 'Hate speech or discrimination' },
  { value: 'spam', label: 'Spam or scam' },
  { value: 'inappropriate_content', label: 'Inappropriate or offensive content' },
  { value: 'threats', label: 'Threats or violence' },
  { value: 'sexual_content', label: 'Unwanted sexual content' },
  { value: 'impersonation', label: 'Impersonation or fake identity' },
  { value: 'other', label: 'Other violation' },
] as const;
