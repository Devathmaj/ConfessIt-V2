/**
 * The closed vocabularies for account fields, mirroring the server.
 *
 * These are not suggestions. `USER_GENDER_VALUES` and `INTEREST_VALUES` are the same lists the
 * server holds in `backend/app/limits.py`, from which it builds both the CHECK constraints and
 * the OpenAPI enums. A value outside them is refused with a 422.
 *
 * They are written out here rather than fetched at runtime because a form cannot offer an
 * option that will be rejected on submit, and fetching the list would mean rendering a dialog
 * with a gap in it until the request came back. The server stays the authority; if the two
 * ever disagree the request is rejected and the UI is simply behind. Change both.
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