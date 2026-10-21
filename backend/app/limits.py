"""Caps the application puts on a single conversation: text lengths and message counts.

Text lengths are enforced twice -- Pydantic (a 422 naming the field) and the database (writes
outside the API cannot exceed what the application accepts; a narrower varchar is a 500 waiting
to happen); counts only in ``message_service``. Mirrored in ``frontend/src/lib/limits.ts``.
"""

# A confession is a short personal note, not an essay: 500 keeps the feed scannable (lowered
# from 2000, where one card pushed everything below it offscreen). Love notes bound separately.
CONFESSION_BODY_MAX = 500

# A love note is *drawn* (rasterised onto the template PNG, no markup or scroll), so 1000 is a
# legibility ceiling against the tightest template: template 3's 895x328 box at 32px fits ~450
# characters at intended size, and the renderer scales type down beyond that.
LOVE_NOTE_BODY_MAX = 1000

# A comment replies to a confession, so it is deliberately much shorter.
COMMENT_BODY_MAX = 500

# Chat turns. Sized to the same order as a confession; a real message is a sentence.
MESSAGE_BODY_MAX = 100

# Messages one conversation may ever hold, across both participants. ``GET /messages/{id}``
# returns the oldest N with no cursor, so this ceiling must stay at or below that read window
# or a conversation would hide its newest messages.
CONVERSATION_MESSAGE_MAX = 200

# Messages one participant may send into a conversation (otherwise 200 is a monologue). Twice
# this is the conversation ceiling, so this binds in normal use.
CONVERSATION_PARTICIPANT_MESSAGE_MAX = 100

# Case Closed detective thread: double the matchmaking allowance (400 per thread, 200 per
# detective) so a full five-action investigation plus accusation discussion fits.
CASE_CLOSED_CONVERSATION_MESSAGE_MAX = 400
CASE_CLOSED_CONVERSATION_PARTICIPANT_MESSAGE_MAX = 200

# Reviewed by an admin: readable but not a data dump. Matches reports.reason.
REPORT_REASON_MAX = 1000

# The name a confession is addressed to. Matches users.name and confessions.recipient_name.
RECIPIENT_NAME_MAX = 160

# A fixed set of keys held in JSONB, so the service rejects anything else; this bounds what
# a client can make it parse and log first.
REACTION_MAX = 24

# Admin-supplied profile fields, sized to their columns. (USER_ROLE_MAX removed with the
# ``role`` column -- the table a row lives in is its account type.)
USER_NAME_MAX = 160
USER_BIO_MAX = 500
USER_GENDER_MAX = 24
USER_REGNO_MAX = 100

# Free text and deliberately not a closed set: departments belong to the institution this
# deployment serves, not this codebase. A display field and matchmaking grouping.
USER_DEPARTMENT_MAX = 80

# How far a client may page: offset becomes a PostgreSQL OFFSET (bigint), and a value past
# that range is a 500 ("bigint out of range"). Enforced here because 31 routes were unbounded.
PAGE_OFFSET_MAX = 1_000_000

# Bulk participant onboarding (``POST /admin/users/bulk-upload``). Fixed application limits,
# not a function of MAX_USER_COUNT: they bound one request's parse/transaction. A file over
# the row ceiling is refused whole, never truncated (a partial import hides which half
# landed); the ASGI body limit still applies first.
BULK_UPLOAD_MAX_ROWS = 1000
BULK_UPLOAD_MAX_BYTES = 10 * 1024 * 1024

# Distinct (case-insensitive) ``users.department`` values; departments are free text, so this
# caps distinct values, not rows. Enforced in app/routers/admin.py behind the account-cap
# advisory lock so two concurrent creates cannot both open the 51st slot. Lowering below the
# stored count deletes nothing, it only refuses new values until the count falls back.
MAX_DEPARTMENTS = 50

# The closed gender set, enforced by Pydantic, the frontend Select and a CHECK constraint.
# Closed because gender is a matchmaking *filter*: free text meant "Male"/"male "/"M" never
# matched; ``other`` is a real value, not NULL.
USER_GENDER_VALUES: tuple[str, ...] = ("male", "female", "other")

# Years of study, closed because matchmaking groups by year ("2"/"02"/"second year" would
# never match; an int makes the first two unwritable). A number rather than names, since
# institutions number cohorts differently. Enforced like USER_GENDER_VALUES + accountFields.ts.
USER_YEAR_MIN = 1
USER_YEAR_MAX = 12

# The closed interest set, capitalised and matched exactly (stored/compared values, not
# labels): interests feed matchmaking scoring, and free text meant "music"/"Music"/"musics"
# never matched. frontend/src/lib/interests.ts restates the set -- change both.
INTEREST_VALUES: tuple[str, ...] = (
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
)

# The size of the whole vocabulary: a backstop against a client sending the list twice, not
# a limit on real selections. No per-interest length cap -- the vocabulary is closed.
INTEREST_MAX_SELECTED = len(INTEREST_VALUES)


# Wordle. The answer is five letters: grid, keyboard, validation and word list all assume
# it. One number so column, validator and word list cannot drift apart.
WORDLE_ANSWER_LENGTH = 5

# Six guesses, and therefore the size of the board. Appears in the CHECK constraint on
# ``wordle_user_games.attempt_count`` (named after the number), the participation answer's
# ``max_attempts``, and the frontend grid. Product decision: the Wordle norm.
WORDLE_MAX_ATTEMPTS = 6

# Shortest word the *random* draw may return; an admin-typed answer is accepted down to one
# letter, since "cat" and "encyclopedia" both play. Two numbers on purpose. The draw filters
# ``backend/assets/anagram_words.txt`` by this, leaving the file as committed.
ANAGRAM_RANDOM_MIN_LENGTH = 5

# The longest typed answer; a column bound (word list max is 23), not a game rule. Enforced
# twice: a 400 from the service and a CHECK constraint for writes outside the API.
ANAGRAM_ANSWER_MAX = 100

# Five guesses, and therefore the size of the game. Restated in the CHECK constraint on
# ``anagram_user_games.attempt_count``, the participation answer's ``max_attempts`` and the
# attempts-remaining line. One fewer than Wordle because a rearrangement costs more.
ANAGRAM_MAX_ATTEMPTS = 5

# Couple Prediction: static content -- 50 templates sharing one shape (a question naming
# ``X`` and exactly four options). One number each so seed, CHECKs and UI cannot drift.
COUPLE_QUESTIONS_COUNT = 50
COUPLE_OPTIONS_PER_QUESTION = 4

# Column bounds far past the longest seeded entry; enforced by the service (readable error)
# and a CHECK, like every other length here.
COUPLE_QUESTION_TEXT_MAX = 300
COUPLE_OPTION_TEXT_MAX = 200

# A mutual session draws this many bank questions: 5 shared + 10 exclusive each = 25 unique,
# 15 answered per participant. One number each so the draw, assignment CHECKs and player
# surface cannot drift apart.
COUPLE_SESSION_QUESTIONS = 25
COUPLE_SESSION_SHARED_QUESTIONS = 5
COUPLE_SESSION_EXCLUSIVE_PER_PLAYER = 10
# Derived, not restated: shared plus their own exclusives, so the relationship stays visible.
COUPLE_ANSWERS_PER_PARTICIPANT = COUPLE_SESSION_SHARED_QUESTIONS + COUPLE_SESSION_EXCLUSIVE_PER_PLAYER

# --- Game sessions, featured games and availability ------------------------------
# Games share one session lifecycle but each owns its cadence (no global reset setting): a
# ``reset_cadence`` counts calendar days, and the featured game is a spotlight that never
# drives session creation. All values below are mirrored by CHECK constraints on ``game_configs``.
GAME_IDS: tuple[str, ...] = (
    "wordle",
    "anagram",
    "couple-questionnaire",
    "reaction-time",
    "connections",
    "trivia-drop",
    "two-key-escape",
    "plot-twist",
    "case-closed",
)

# The only reset cadences; each game has its own ``game_configs`` row, so Wordle can reset
# daily while Anagram runs every five days and the Couple Questionnaire runs weekly.
RESET_CADENCE_VALUES: tuple[str, ...] = (
    "daily",
    "every_2_days",
    "every_3_days",
    "every_5_days",
    "weekly",
)

# Days per cadence, excluding weekly: weekly is always Monday-Sunday UTC regardless of when
# its session started (see ``game_periods.period_end``).
RESET_CADENCE_DAYS: dict[str, int] = {
    "daily": 1,
    "every_2_days": 2,
    "every_3_days": 3,
    "every_5_days": 5,
}

# ``random`` draws from the committed word list at reset with no approval step; ``manual``
# waits for an administrator and otherwise leaves the session pending.
GAME_CONTENT_MODES: tuple[str, ...] = ("random", "manual")

# ``active`` is playable, ``pending`` awaits a manual word, ``ended`` is history that keeps
# user progress. At most one active-or-pending row exists per game.
SESSION_STATUS_VALUES: tuple[str, ...] = ("active", "pending", "ended")

# Farthest ahead a manual word may be scheduled. Bounded so the schedule cannot become an
# unbounded promise; enforced server-side and named in the admin UI.
MAX_FUTURE_SCHEDULE_DAYS = 30

# Reaction Time: one featured game per date, five submissions per user per date, budget fixed
# here rather than in a settings table. A valid reaction is a click after green, timed
# client-side; the bounds keep a fabricated payload honest (1ms is not a reaction, 10s an
# abandoned screen). False-start elapsed time is informational only, never a score.
REACTION_TIME_MAX_SUBMISSIONS = 5
REACTION_TIME_MIN_VALID_MS = 1
REACTION_TIME_MAX_VALID_MS = 10_000
REACTION_TIME_MAX_ELAPSED_MS = 60_000

# ``pending`` consumed a submission but has no result yet; ``valid`` is a click after green;
# ``too_early`` a click before green carrying only informational elapsed time.
REACTION_TIME_STATUS_VALUES: tuple[str, ...] = ("pending", "valid", "too_early")

# Connections: sixteen words in four hidden groups of four, configured manually (no random
# mode). One number each so puzzle validation, submission validation, scoring and the player
# surface cannot drift apart.
CONNECTIONS_WORDS_TOTAL = 16
CONNECTIONS_GROUPS_COUNT = 4
CONNECTIONS_WORDS_PER_GROUP = 4

# Column bound far past any reasonable word or short phrase; a 400 from the service and a
# CHECK for writes outside the API.
CONNECTIONS_WORD_MAX = 50

# Trivia Drop: exactly four options per question, one or more correct; single- vs
# multiple-answer is derived from the correct count, never configured. One number for the
# option count so validation, admin and player surfaces cannot drift. Text ceilings are
# enforced by the service and a CHECK, like every other length here.
TRIVIA_OPTIONS_COUNT = 4
TRIVIA_QUESTION_TEXT_MAX = 500
TRIVIA_OPTION_TEXT_MAX = 200

# Two-Key Escape: a square-grid maze for one couple -- the Navigator moves one cell at a time,
# the Key Operator uses keys that each unlock one cell and block one. Keys are reusable
# (single-use keys plus relocking can strand the couple); grid size is the only gameplay knob,
# lock count the mapping below, cooldown server-side.
TWO_KEY_ESCAPE_GRID_SIZES: tuple[int, ...] = (12, 13, 14, 15)
TWO_KEY_ESCAPE_DEFAULT_GRID_SIZE = 12
TWO_KEY_ESCAPE_KEY_CHARGES: dict[int, int] = {12: 12, 13: 13, 14: 14, 15: 15}
TWO_KEY_ESCAPE_KEY_COOLDOWN_SECONDS = 10
TWO_KEY_ESCAPE_ROLES: tuple[str, ...] = ("navigator", "key_operator")


def two_key_escape_key_charges(grid_size: int) -> int:
    """How many key charges a maze of ``grid_size`` grants, one charge per key."""
    return TWO_KEY_ESCAPE_KEY_CHARGES[grid_size]


# Plot Twist: asynchronous cooperative storytelling for couples. Stories are AI-generated into
# versioned packages before play; the engine executes them deterministically and never calls
# the model during gameplay.
PLOT_TWIST_SCENE_TYPES: tuple[str, ...] = (
    "opening",
    "encounter",
    "connector",
    "discovery",
    "ending",
)
PLOT_TWIST_ACTORS: tuple[str, ...] = (
    "participant_one",
    "participant_two",
    "either",
    "joint",
)
PLOT_TWIST_STATE_OPS: tuple[str, ...] = (
    "set",
    "increment",
    "decrement",
    "add_flag",
    "remove_flag",
)
PLOT_TWIST_CONDITION_OPS: tuple[str, ...] = (
    "eq",
    "gte",
    "lte",
    "has_flag",
    "missing_flag",
)
PLOT_TWIST_DIFFICULTIES: tuple[str, ...] = ("easy", "medium", "hard")
PLOT_TWIST_ENDING_TYPES: tuple[str, ...] = (
    "escape",
    "failure",
    "treasure",
    "capture",
    "mystery_solved",
    "twist",
)
PLOT_TWIST_PACKAGE_STATUSES: tuple[str, ...] = (
    "draft",
    "generating",
    "validation_failed",
    "ready_for_review",
    "published",
    "generation_failed",
)
PLOT_TWIST_BLUEPRINT_STATUSES: tuple[str, ...] = (
    "draft",
    "generating",
    "ready_for_review",
    "approved",
    "generation_failed",
)
PLOT_TWIST_SESSION_STATUSES: tuple[str, ...] = (
    "awaiting_user_a",
    "awaiting_user_b",
    "completed",
)
PLOT_TWIST_MAX_SCENES = 40
PLOT_TWIST_MAX_DECISIONS = 24
# How often one couple may revisit a scene. A loop of compatible scenes would otherwise play
# forever; past this the way back is no longer offered (the least-visited forward path always
# remains, so the story cannot strand). Linear stories never notice.
PLOT_TWIST_MAX_SCENE_VISITS = 2
# Decision budget when a package names none: reaching it on a non-terminal scene concludes at
# the best qualifying ending rather than letting an unnoticed loop run past it.
PLOT_TWIST_DEFAULT_MAX_DECISIONS = 24
# Past this fraction of shared (either/joint) scenes a package reads as a solo story with an
# audience rather than a two-sided one.
PLOT_TWIST_SHARED_SCENE_FRACTION = 0.5
PLOT_TWIST_MAX_INTERCHANGEABLE_PER_SLOT = 6
# Repair rounds: scene patches are cheap, whole-package regenerations expensive. Ten leaves
# room for both (a live run chained five patches; another converged on its eighth round); a
# regeneration that would add errors is discarded rather than erasing progress.
PLOT_TWIST_MAX_REPAIR_ATTEMPTS = 10
PLOT_TWIST_MAX_BATCH_SCENES = 8
# Scenes one patch request may carry: a patch touches only the scenes the errors name (plus
# their direct successors), so it cannot fix one scene and regress another.
PLOT_TWIST_MAX_PATCH_SCENES = 6
# Consecutive whole-package regenerations that came back worse than the draft in hand. Past
# this, more rolls are not finding a better structure and the loop ends on the best draft.
PLOT_TWIST_MAX_STUCK_REGENERATIONS = 3
# Ending -> league base points; the engine caps the final base at 100 however tiers combine.
PLOT_TWIST_REWARD_TIERS: dict[str, int] = {
    "participation": 40,
    "bronze": 60,
    "silver": 80,
    "gold": 100,
}
PLOT_TWIST_TREASURE_BONUS_EACH = 5
PLOT_TWIST_TREASURE_BONUS_MAX = 15
# Per-request timeout and attempt caps so a stalled provider fails visibly: 150s covers the
# largest legitimate package (~20k tokens of JSON plus hidden reasoning), and a cut-off answer
# is retried inside the attempt cap either way.
PLOT_TWIST_LLM_TIMEOUT_SECONDS = 150
PLOT_TWIST_SESSION_LOCK_KEY = 7_250_003

# Case Closed: cooperative mystery -- one immutable published case per event, five
# investigation actions per partner, three initial leads, ~15-24 DAG lead nodes, joint
# accusation with hidden correctness until finalization. Deterministic; LLM only
# pre-publication.
CASE_CLOSED_DIFFICULTIES: tuple[str, ...] = ("easy", "medium", "hard")
CASE_CLOSED_EVIDENCE_KINDS: tuple[str, ...] = (
    "establishes_fact",
    "contradicts_statement",
    "supports_theory",
    "misleading",
)
CASE_CLOSED_PACKAGE_STATUSES: tuple[str, ...] = (
    "draft",
    "generating",
    "validation_failed",
    "ready_for_review",
    "published",
    "generation_failed",
)
CASE_CLOSED_BLUEPRINT_STATUSES: tuple[str, ...] = (
    "draft",
    "generating",
    "ready_for_review",
    "approved",
    "generation_failed",
)
CASE_CLOSED_SESSION_STATUSES: tuple[str, ...] = (
    "investigating",
    "accusation_locked",
    "completed",
    "expired",
)
CASE_CLOSED_SCHEMA_VERSION = 1
CASE_CLOSED_BUDGET_PER_PARTNER = 5
CASE_CLOSED_INITIAL_LEADS_COUNT = 3
CASE_CLOSED_MIN_LEADS = 15
CASE_CLOSED_MAX_LEADS = 24
CASE_CLOSED_MAX_FOLLOW_UPS = 3
CASE_CLOSED_MIN_DEPTH = 4
CASE_CLOSED_MAX_REPAIR_ATTEMPTS = 3
CASE_CLOSED_LLM_TIMEOUT_SECONDS = 120
CASE_CLOSED_DEFAULT_HALF_LIFE_SECONDS = 43200
CASE_CLOSED_DEFAULT_MAX_POINTS = 100
CASE_CLOSED_SESSION_LOCK_KEY = 7_250_004

# Automated AI session generation (Plot Twist and Case Closed): the 00:01 UTC scheduler only
# *enqueues* a cycle; a worker claims one job and runs Stage A -> approve -> Stage B ->
# validate -> publish before the session opens. One job per game per cycle (the unique key)
# and one advisory lock serialise the protected pipeline across games and workers.
AI_GAME_IDS: tuple[str, ...] = ("plot-twist", "case-closed")
# ``queued`` waits for a worker, ``running`` is claimed and holds the pipeline lock,
# ``retry_wait`` waits out a backoff; terminal states are ``published`` (session opened),
# ``failed`` (attempts exhausted) and ``cancelled`` (revoked, hidden or superseded).
AI_GENERATION_JOB_STATUSES: tuple[str, ...] = (
    "queued",
    "running",
    "retry_wait",
    "published",
    "failed",
    "cancelled",
)
# One key for both games: only one protected generation workflow may run at a time across the
# deployment, so simultaneous due ticks queue.
AI_GENERATION_LOCK_KEY = 7_250_005
# Attempts one cycle may spend before parking as failed. A crash mid-run is reclaimed as a
# fresh attempt after ``AI_GENERATION_STALE_SECONDS``; the next scheduled cycle starts clean.
AI_GENERATION_MAX_ATTEMPTS = 3
# How long a claimed job may sit untouched before another worker reclaims it as abandoned.
AI_GENERATION_STALE_SECONDS = 1800
# Base wait between attempts, multiplied by the attempt number so backoff grows.
AI_GENERATION_RETRY_BACKOFF_SECONDS = 120
# Admin session-history page limits; the same hard-ceiling convention as other history reads.
AI_GENERATION_HISTORY_MAX_LIMIT = 100
AI_GENERATION_HISTORY_DEFAULT_LIMIT = 50


# How old an open timing segment may be before its closed tail is discarded rather than
# credited (a browser dying mid-game never sends the close). Two minutes is two 30s heartbeat
# intervals plus grace -- a live game never has a segment older than ~30s -- so a quick
# logout-and-return still counts honestly.
ANAGRAM_TIMER_STALE_AFTER_SECONDS = 120
