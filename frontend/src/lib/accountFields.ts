/**
 * The closed vocabularies for account fields, mirroring the server.
 *
 * These are not suggestions. `USER_GENDER_VALUES`, the `USER_YEAR_MIN`/`USER_YEAR_MAX` bounds and
 * `INTEREST_VALUES` are the same lists the server holds in `backend/app/limits.py`, from which it
 * builds both the CHECK constraints and the OpenAPI enums. A value outside them is refused with a
 * 422.
 *
 * They are written out here rather than fetched at runtime because a form cannot offer an
 * option that will be rejected on submit, and fetching the list would mean rendering a dialog
 * with a gap in it until the request came back. The server stays the authority; if the two
 * ever disagree the request is rejected and the UI is simply behind. Change both.
 *
 * `department` is deliberately absent from this list, because it has no closed vocabulary to
 * mirror. Departments belong to the institution rather than to the codebase, so it is free text
 * on both sides and only a length limit is shared.
 */

/** The only gender values the server accepts, lower-case and exactly these three. */
export const USER_GENDER_VALUES = ["male", "female", "other"] as const;

export type UserGender = (typeof USER_GENDER_VALUES)[number];

/**
 * The same three, shaped for a `<select>`.
 *
 * `UserGender` rather than `string`: the label and the stored value differ in case ("Male"
 * versus "male"), and typing the option as `string` would let that difference be lost in a
 * refactor without the compiler noticing. The value is what goes on the wire.
 */
export const USER_GENDER_OPTIONS: ReadonlyArray<{ value: UserGender; label: string }> = [
  { value: "male", label: "Male" },
  { value: "female", label: "Female" },
  { value: "other", label: "Other" },
];

/**
 * The bounds of a participant's year of study, and the same two numbers the server holds in
 * `limits.USER_YEAR_MIN` / `limits.USER_YEAR_MAX`.
 *
 * A range rather than a list of names, because the same schema serves a school as well as an
 * undergraduate programme: one runs Class 1 to Class 12, the other 1st to 4th Year, and the two
 * spell the same field differently. Had the column stored the *name*, this list would have had to
 * be extended -- and re-extended server-side too -- for a deployment that differs only in
 * vocabulary. Twelve is the longer of the two.
 *
 * Still closed, and for the same reason gender is: matchmaking groups participants by year, so "2"
 * and "02" would be two cohorts that never match each other and no screen could tell them apart.
 * The server stores an integer, which makes that particular pair unwritable rather than merely
 * discouraged -- strictly stronger than the closed list of strings this replaced.
 */
export const USER_YEAR_MIN = 1;
export const USER_YEAR_MAX = 12;

/**
 * A year as the server stores it. Plain `number` rather than a union of literals, because twelve
 * of them would be a type the compiler cannot narrow usefully and a list somebody has to keep in
 * step with the bounds above.
 */
export type UserYear = number;

/**
 * The bounds, shaped for a `<select>`.
 *
 * The label is the bare number: the field is already labelled "Year", so "Year 2" beneath a
 * heading that says Year reads as a stutter. Value and label are the same number, which is what
 * lets a selected option round-trip through a `<select>`'s string-valued API unchanged.
 */
export const USER_YEAR_OPTIONS: ReadonlyArray<{ value: UserYear; label: string }> = Array.from(
  { length: USER_YEAR_MAX - USER_YEAR_MIN + 1 },
  (_, index) => {
    const value = USER_YEAR_MIN + index;
    return { value, label: String(value) };
  }
);

/**
 * True when a stored year is one the form can offer.
 *
 * Takes `unknown` rather than `string`, because the value usually arrives from a response typed at
 * the boundary rather than from this module, and the check that matters is the one that refuses it
 * before it reaches a `<select>` that has no matching option.
 */
export const isUserYear = (value: unknown): value is UserYear =>
  typeof value === 'number' &&
  Number.isInteger(value) &&
  value >= USER_YEAR_MIN &&
  value <= USER_YEAR_MAX;

/**
 * How a `(year, department)` pair reads in one line of UI text.
 *
 * Department first, because it is the part that identifies somebody: "CSE · 2" reads as a
 * department with a cohort, where "2 · CSE" reads as a year with a stray word. Returns the
 * half that exists rather than a placeholder, because a participant whose row predates the split
 * can genuinely be missing one -- and a screen that renders "Unspecified · " has invented data.
 *
 * The year is tested for being a finite number rather than for being truthy or non-empty, because
 * it is one now: `Boolean(0)` would drop a year that is out of range but whose presence the caller
 * is trying to report, and `String(NaN)` would put the word "NaN" on a matchmaking card.
 */
export const cohortLabel = (year?: number | null, department?: string | null): string => {
  const trimmed = department?.trim();
  return [
    trimmed ? trimmed : null,
    typeof year === 'number' && Number.isFinite(year) ? String(year) : null,
  ]
    .filter((part): part is string => part !== null)
    .join(' · ');
};

/**
 * Whether two cohorts are the same one.
 *
 * Compares the pair rather than a joined string, so "CSE" in year 2 and "CSE" in year 3 are
 * correctly different -- which was the whole reason for splitting the column, and the thing that
 * would quietly come back if a comparison were written against a display label.
 */
export const isSameCohort = (
  a: { year?: number | null; department?: string | null },
  b: { year?: number | null; department?: string | null }
): boolean => a.year === b.year && (a.department ?? '') === (b.department ?? '');

/**
 * The sentinel a cohort filter carries for "no filter", chosen because it cannot collide with a
 * real cohort key: a `cohortKey` always contains the U+0000 separator below, so it can never
 * equal this.
 */
export const ALL_COHORTS = 'all';

/**
 * A `(year, department)` pair, as the server sends and accepts them.
 *
 * Two fields rather than one display string, which is the point of the split: with a single
 * `class_name` the pair could only be compared as a joined value, so "CSE 2" and "2 CSE" were
 * different cohorts to a filter. `cohortLabel` below joins them for display, and the result is
 * never sent to the server.
 *
 * Declared here rather than in `@/services/api` so the vocabulary and the helpers that depend on
 * it live together; the API module imports this instead of redefining it.
 */
export interface Cohort {
  year: UserYear;
  department: string;
}

/**
 * What goes between the two halves of a cohort key. U+0000, which is why it is written as an
 * escape rather than as the character: a literal NUL in source is invisible, so nobody reviewing
 * this could see what the separator actually was.
 */
const COHORT_KEY_SEPARATOR = '\u0000';

/**
 * A single string naming one cohort, for a `<select>` whose value type is a string.
 *
 * Radix Select carries a string, and a cohort is two fields, so something has to be flattened.
 * The display label is *not* it: a department containing the separator would make two different
 * cohorts share a label, and the filter would then show whichever row came second. U+0000 is used
 * instead because it is a control character that cannot be typed into a text box, so the two halves
 * always come back apart.
 *
 * The year half is stringified on the way in and parsed back on the way out. `String(2)` is
 * round-trip safe here because the other half cannot begin with a digit-run that would be read as
 * part of the year -- the separator is a NUL, so the split point is unambiguous no matter what the
 * department contains.
 *
 * Never display this. `cohortLabel` is for that.
 */
export const cohortKey = (cohort: { year?: number | null; department?: string | null }): string =>
  [cohort.year ?? '', cohort.department ?? ''].join(COHORT_KEY_SEPARATOR);

/**
 * The cohort a `cohortKey` names, or null when the key is not one (including `ALL_COHORTS`).
 *
 * The year is parsed back to a number and range-checked rather than handed on as the string that
 * came out of the key. `Number('')` is 0 and `Number('CSE')` is NaN, so a key whose year half is
 * missing or was never a number would otherwise become year 0 -- outside the range, matching no
 * cohort at all, and rendering as a real option on a filter that can then never select anything.
 * Returning null sends the caller back to "no filter", which is what an unrecognised key means.
 */
export const cohortFromKey = (key: string): Cohort | null => {
  const separator = key.indexOf(COHORT_KEY_SEPARATOR);
  if (separator < 0) return null;
  const year = Number(key.slice(0, separator));
  if (!isUserYear(year)) return null;
  return {
    year,
    department: key.slice(separator + COHORT_KEY_SEPARATOR.length),
  };
};

/**
 * The payload a cohort carries while it is being dragged, and the parser for it.
 *
 * JSON, deliberately *not* `cohortKey`. `cohortKey` is fine as a `<select>` value, which never
 * leaves this page's own memory, but the drag payload is handed to the browser through
 * `DataTransfer.setData` and read back out through `getData`, and that is a boundary where a
 * control character is not safe to rely on. `cohortKey`'s U+0000 is exactly that: if it does not
 * survive the round trip, `cohortFromKey` sees no separator, returns null, and the drop is
 * silently discarded -- the chip just snaps back with nothing to show for it.
 *
 * JSON has no such character, so the two halves arrive intact whatever the browser does with the
 * string. The parser validates rather than trusting the payload, because a drag can originate
 * outside this page and `JSON.parse` will happily hand back anything.
 */
export const cohortDragPayload = (cohort: Cohort): string =>
  JSON.stringify({ year: cohort.year, department: cohort.department });

export const cohortFromDragPayload = (payload: string): Cohort | null => {
  let parsed: unknown;
  try {
    parsed = JSON.parse(payload);
  } catch {
    // Not JSON at all: a drag from another page, a file, or selected text.
    return null;
  }
  if (typeof parsed !== 'object' || parsed === null) return null;

  const { year, department } = parsed as { year?: unknown; department?: unknown };
  if (!isUserYear(year)) return null;
  if (typeof department !== 'string') return null;

  return { year, department };
};

/** Everything a participant may list as an interest. Twelve, and no others. */
export const INTEREST_VALUES = [
  "Music",
  "Movies",
  "Reading",
  "Gaming",
  "Traveling",
  "Cooking",
  "Sports",
  "Art",
  "Photography",
  "Dancing",
  "Writing",
  "Coding",
] as const;

export type Interest = (typeof INTEREST_VALUES)[number];

/** True when the value is one the server will store. */
export const isInterest = (value: string): value is Interest =>
  (INTEREST_VALUES as readonly string[]).includes(value);

/**
 * Narrow an arbitrary list to what the server accepts.
 *
 * Stored interests can predate the closed vocabulary -- the CHECK constraint is added NOT
 * VALID, so rows written before it existed keep whatever they held. Rendering a row like that
 * must not throw, so the display layer filters rather than asserts. Anything dropped here is
 * a legacy value the server will refuse the next time somebody edits that participant.
 */
export const toInterests = (values: readonly string[] | null | undefined): Interest[] =>
  (values ?? []).filter(isInterest);

/**
 * True when the stored value is a gender the form can offer.
 *
 * Same reasoning as `toInterests`: a row written before the constraint existed may hold
 * something the select has no option for, and a select with no matching option silently
 * displays the wrong thing as though it were selected.
 */
export const isUserGender = (value: string): value is UserGender =>
  (USER_GENDER_VALUES as readonly string[]).includes(value);
