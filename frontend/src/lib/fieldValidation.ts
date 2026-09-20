/**
 * Checking the identity fields an admin writes, before the request is made.
 *
 * **Why this is here rather than in the dialog.** The server is the authority on all of it --
 * `AdminUserCreate` and `AdminUserUpdate` cap every field, require the gender and cohort of a
 * participant, and refuse extras. But a rule that only exists on the server is discovered by
 * submitting:
 * the operator fills the dialog, presses Save, waits for a round-trip, and is then told what is
 * wrong in a toast that has already moved on. These are the same rules stated earlier and in
 * place, so the answer arrives while the operator is still looking at the field.
 *
 * **Why the rules are stated rather than inferred.** Each message below names the field and the
 * bound that was broken, so a person can act on it. "Invalid input" is what a browser's own
 * bubble says, and it is why `type="email"` was not sufficient on its own: it refuses to submit
 * a native form, reports in the browser's own words, and says nothing at all about a value that
 * is well-formed but not deliverable.
 *
 * Nothing here is a substitute for the server. Every rule is duplicated from
 * `backend/app/limits.py` and the closed vocabularies in `accountFields`, and the server stays
 * the one that decides -- these exist to avoid the round-trip, not to replace it.
 */

import { isUserGender, isUserYear } from './accountFields';
import { ADMIN_DEPARTMENT_MAX, ADMIN_NAME_MAX, ADMIN_REGNO_MAX } from './limits';

/**
 * The fields both dialogs write.
 *
 * One list for both because the rules are one list. The fields themselves are shared; what
 * differs is which of them a given row is required to have, which is what `isParticipant`
 * decides -- gender and the cohort pair apply to a participant and mean nothing on a staff row.
 */
export const IDENTITY_FIELDS = [
  'name',
  'regno',
  'email',
  'gender',
  'year',
  'department',
] as const;

export type IdentityField = (typeof IDENTITY_FIELDS)[number];

/** A message per offending field. A field that is absent from the record is valid. */
export type FieldIssues = Partial<Record<IdentityField, string>>;

/**
 * The form state these checks read.
 *
 * `year` is `number | '' | null` because the two dialogs disagree about what "not chosen" is:
 * create seeds `''` (a `<select>` has no null state) and edit seeds `null` (the field is a
 * number). Accepting all three means the checks do not have to know which dialog called them,
 * and `isUserYear` already rejects every one of them.
 */
export interface IdentityValues {
  name?: string | null;
  regno?: string | null;
  email?: string | null;
  gender?: string | null;
  year?: number | '' | null;
  department?: string | null;
}

/**
 * A plain-text explanation of what is wrong, or `null` when the address is acceptable.
 *
 * Deliberately a readable approximation of the server's `EmailStr` rather than a full RFC 5322
 * parser. The bar that matters here is "would a person recognise this as their address": a
 * missing or doubled `@`, a missing domain, a domain with no dot, and a top-level domain that
 * is not letters are the five ways a typed address is actually wrong. Quoting local parts and
 * IP-literal domains are legal and are accepted rather than refused, because refusing a valid
 * address is a worse failure than accepting an invalid one.
 */
export const validateEmail = (value: string | null | undefined): string | null => {
  const trimmed = (value ?? '').trim();

  if (!trimmed) return 'Enter an email address, for example name@example.com.';
  if (/\s/.test(trimmed)) return 'An email address cannot contain a space.';

  const at = trimmed.indexOf('@');
  if (at < 0) return 'An email address needs an @, for example name@example.com.';
  if (at !== trimmed.lastIndexOf('@')) return 'An email address can only contain one @.';

  const local = trimmed.slice(0, at);
  const domain = trimmed.slice(at + 1);

  if (!local) return 'Add the part before the @, for example name@example.com.';
  if (!domain) return 'Add the domain after the @, for example example.com.';
  if (domain.startsWith('.')) return 'The domain cannot start with a dot.';
  if (domain.includes('..')) return 'The domain cannot contain two dots in a row.';
  if (!domain.includes('.')) return 'The domain needs a dot, for example example.com.';

  const tld = domain.slice(domain.lastIndexOf('.') + 1);
  if (!/^[A-Za-z]{2,}$/.test(tld)) {
    return 'That does not look like a domain, for example example.com.';
  }

  return null;
};

/**
 * Every problem with the form at once, rather than the first one found.
 *
 * Reported together on purpose: an admin filling six fields can get several wrong, and stopping
 * at the first turns one visit into several.
 */
export const validateIdentityFields = (
  values: IdentityValues,
  { isParticipant }: { isParticipant: boolean },
): FieldIssues => {
  const issues: FieldIssues = {};

  const name = (values.name ?? '').trim();
  if (!name) {
    issues.name = 'Enter the full name.';
  } else if (name.length > ADMIN_NAME_MAX) {
    issues.name = `The full name cannot be longer than ${ADMIN_NAME_MAX} characters.`;
  }

  const regno = (values.regno ?? '').trim();
  if (!regno) {
    issues.regno = 'Enter the register number.';
  } else if (regno.length > ADMIN_REGNO_MAX) {
    issues.regno = `The register number cannot be longer than ${ADMIN_REGNO_MAX} characters.`;
  }

  const email = validateEmail(values.email);
  if (email) issues.email = email;

  // Gender and the cohort pair are the parts that are not on every table, and each is required
  // exactly when the row being written is a participant's: the staff tables have no gender
  // column either, so a staff form that demanded one would be asking for an answer the row
  // cannot hold.
  if (isParticipant) {
    if (!isUserGender(values.gender ?? '')) {
      issues.gender = 'Choose a gender.';
    }

    if (!isUserYear(values.year ?? null)) {
      issues.year = 'Choose a year of study.';
    }

    const department = (values.department ?? '').trim();
    if (!department) {
      issues.department = 'Enter the department or division.';
    } else if (department.length > ADMIN_DEPARTMENT_MAX) {
      issues.department = `The department cannot be longer than ${ADMIN_DEPARTMENT_MAX} characters.`;
    }
  }

  return issues;
};

/** Whether anything is wrong, for the one-line guard before a request is sent. */
export const hasFieldIssues = (issues: FieldIssues): boolean =>
  IDENTITY_FIELDS.some((field) => issues[field] !== undefined);

/**
 * The offending field to point at first.
 *
 * Ordered by the dialog's own layout rather than by the alphabet, so focus and scroll land on
 * the first thing the operator would have read.
 */
export const FIRST_FIELD_ORDER: readonly IdentityField[] = [
  'name',
  'regno',
  'email',
  'gender',
  'year',
  'department',
];

export const firstInvalidField = (issues: FieldIssues): IdentityField | null =>
  FIRST_FIELD_ORDER.find((field) => issues[field] !== undefined) ?? null;