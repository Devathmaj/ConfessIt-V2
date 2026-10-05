"""Caps the application puts on a single conversation: text lengths, and message counts.

The text lengths are enforced twice on purpose: once by Pydantic, so the caller gets a 422
naming the field, and once by the database, so a write that does not come through the API
(a migration, a maintenance script, an endpoint added later) cannot store more than the
application claims to accept. Only the Pydantic half is a user-facing error; only the
database half is an invariant. Neither alone is sufficient, and a varchar column that is
narrower than the Pydantic cap is a 500 waiting to happen rather than a validation
message.

The message counts are a different kind of cap -- how much of a conversation may be written
at all, rather than how long one turn is -- and they are enforced in one place only,
``message_service``, because they are a statement about how many rows a conversation holds
and not about a column's width. They live here anyway because this module is the one place
a client-mirrored limit belongs, and because ``frontend/src/lib/limits.ts`` restates the
whole set so the browser can stop the input at the same point.

The values are product decisions rather than technical ones. They are sized to the content
each field actually holds: a confession is a short personal note, a comment is a reply to
one, and a chat message is a conversational turn.
"""

# A confession is a short personal note, not an essay. 500 characters is roughly a paragraph
# -- enough for the thing someone actually wants to say and short enough that a feed of them
# stays scannable, which is the entire reason the feed exists.
#
# Lowered from 2000, which was sized as if a confession were a diary entry. The cost of the old
# number was not storage; it was that the card rendering one confession could push everything
# below it off the screen.
#
# Longer feelings have a home already: a love note is a message to one person and is bounded
# separately at LOVE_NOTE_BODY_MAX. A public anonymous feed is the wrong place for a paragraph
# nobody chose to read.
CONFESSION_BODY_MAX = 500

# The message on a love note, and the only limit here that is a layout constraint rather than a
# readability one.
#
# A love note is *drawn*: the browser rasterises the message onto the template's PNG at the
# sender's chosen font and font size, and that image is the whole note. There is no markup and no
# scroll, so a message has to fit inside a fixed rectangle on the card or part of it is simply
# not there. This number is therefore the point past which the picture stops being able to show
# the note at a size anybody can read -- which is why it is far below the storage it could have.
#
# 1000 is set against the tightest template rather than the average one. The body box on
# template 3 is 895x328; at the 32px its JSON declares, that is about 10 lines of roughly 45
# characters, so ~450 characters at the intended size and a good deal more once the renderer is
# allowed to scale the type down. A note near this limit renders noticeably smaller than one
# near the start of it, which is the intended behaviour -- the sender sees the same scaling in
# the live preview before sending -- but it does mean the limit is a legibility ceiling rather
# than a comfortable one.
#
# Lowered from 2000, which was inherited from the confession and never checked against what a
# card could hold. At that length the only way to render a note was to shrink the type to
# roughly 12px, so the extra characters bought a love note nobody could read.
LOVE_NOTE_BODY_MAX = 1000

# A comment replies to a confession, so it is deliberately much shorter.
COMMENT_BODY_MAX = 500

# Chat turns. Sized to the same order as a confession; a real message is a sentence.
MESSAGE_BODY_MAX = 100

# How many messages one conversation may ever hold, counted across both participants.
#
# This is the ceiling on the transcript itself, and it is what makes the transcript readable:
# ``GET /messages/{conversation_id}`` returns the oldest N with no cursor, so a conversation
# long enough to overflow that window would hide its own newest messages from the two people
# in it. With a hard ceiling at or below the read window, every message a conversation holds
# is a message its participants can still fetch.
#
# A conversation also has a four-hour life, so this is a bound on what two people can put in
# one sitting rather than on how much anyone may ever say to each other.
CONVERSATION_MESSAGE_MAX = 200

# How many messages one participant may send into a single conversation.
#
# The conversation ceiling alone would not stop a conversation being entirely one person
# talking: 200 messages is a monologue, and the exchange is the point. This is the number
# that makes the conversation two-sided -- neither party can take the whole allowance, so
# the thread can only be finished when both have had their say.
#
# Twice this is the conversation ceiling, deliberately: the binding limit in normal use is
# this one, and the conversation cap is the backstop that holds even if a conversation was
# filled by a build that did not enforce it.
CONVERSATION_PARTICIPANT_MESSAGE_MAX = 100

# Case Closed per-couple detective thread: double the matchmaking allowance
# (400 per thread, 200 per detective) so a full five-actions-each investigation
# plus accusation discussion fits without hitting the ceiling mid-case.
CASE_CLOSED_CONVERSATION_MESSAGE_MAX = 400
CASE_CLOSED_CONVERSATION_PARTICIPANT_MESSAGE_MAX = 200

# Report reasons are reviewed by an admin, so they need to be readable but not a data dump.
# Matches the existing reports.reason column.
REPORT_REASON_MAX = 1000

# The name a confession is addressed to. Matches users.name and
# confessions.recipient_name, which are both String(160).
RECIPIENT_NAME_MAX = 160

# A reaction is one of a fixed set of keys held in a JSONB column, so the service already
# rejects anything else. This bounds what a client can make it parse and log first.
REACTION_MAX = 24

# Admin-supplied profile fields, sized to the columns they are stored in.
# (USER_ROLE_MAX removed: the `role` column is gone -- the table a row lives in
# *is* its account type -- so a role length cap is dead code.)
USER_NAME_MAX = 160
USER_BIO_MAX = 500
USER_GENDER_MAX = 24
USER_REGNO_MAX = 100

# A participant's department. Free text and deliberately *not* a closed set: the departments that
# exist are a property of the institution this deployment serves, not of this codebase, so a list
# written here would be a guess that is wrong the day another one is introduced. It is a display
# field and a matchmaking grouping, not a filter anything is required to match against.
USER_DEPARTMENT_MAX = 80

# How many distinct departments/divisions may exist at once, counted as distinct
# (case-insensitive) ``users.department`` values.
#
# Departments are free text -- there is no departments table -- so this is a cap on how many
# *different* values the column holds, not on how many rows hold one. Joining an existing
# department never counts against it; only a value nobody already uses opens a new slot.
# Enforced in ``app/routers/admin.py`` on participant create and on any edit that moves a
# participant onto a brand-new department, behind the same advisory lock as the account caps
# so two concurrent creates cannot both open the 51st slot.
#
# Product knob, not a technical one: raise it to onboard more divisions, lower it to freeze
# the current set. Lowering it below the number already stored does not delete anything --
# it only refuses brand-new values until the count falls back under the new ceiling.
# Change this one number and the enforcement follows; there is no second copy to update.
MAX_DEPARTMENTS = 50

# The closed set of genders an account may carry, and the one place the list is written down.
#
# Closed rather than free text because gender is a *filter*, not a description: matchmaking
# pairs candidates by it, and a value outside the set is not a gender nobody has, it is a typo
# that silently excludes the account from every pool the opposite gender draws from. The old
# column accepted any string up to 24 characters, so "Male", "male " and "M" were three
# different people to that query and none of them matched each other.
#
# ``other`` is a real value rather than a null: "not one of the two" is the answer, and storing
# it as NULL would make it indistinguishable from an account that predates the column.
#
# Enforced three times, because each one covers a different hole: Pydantic so the caller gets a
# 422 naming the field, the frontend Select so there is no invalid value to send in the first
# place, and a CHECK constraint so a write that bypasses the API cannot store anything else.
USER_GENDER_VALUES: tuple[str, ...] = ("male", "female", "other")

# The range of years of study a participant may be in, and the one place the bounds are written
# down. A number rather than a closed list of names, because the same schema has to serve
# institutions that number a cohort differently: an undergraduate programme runs 1st-4th Year, a
# school runs Class 1 to Class 12. Storing the *name* would bake one institution's vocabulary into
# the column, and serving the other one would need a second column and a second set of endpoints.
# The bounds are chosen to hold the longer of the two.
#
# Still a closed range, closed for the same reason gender is, and it is worth saying out loud why:
# matchmaking groups participants by year, so "2", "02" and "second year" would be three cohorts
# that never match each other and no screen could tell them apart. Storing an int makes the first
# two unwritable rather than merely discouraged, which is strictly stronger than the closed list
# this replaced.
#
# Enforced in exactly the three places ``USER_GENDER_VALUES`` is: Pydantic (a 422 naming the
# field), the frontend select (no invalid value to offer in the first place), and a CHECK
# constraint (so a write that bypasses the API cannot store anything else).
#
# ``frontend/src/lib/accountFields.ts`` restates the bounds so the browser offers the same range
# the server accepts. Change both.
USER_YEAR_MIN = 1
USER_YEAR_MAX = 12

# The closed set of interests a participant may hold, and the one place the list is written down.
#
# Closed for the same reason gender is: interests feed matchmaking scoring, and an interest
# outside the set has no counterpart on the other participant's profile to be compared against.
# It used to be free text, entered as a comma-separated string, which meant "music", "Music" and
# "musics" were three interests that never matched each other and no screen could tell the
# difference.
#
# Capitalised, and matched exactly. These strings are stored, displayed and compared, so they
# are values rather than labels: normalising case on the way in would make the stored value
# disagree with the one the browser offered, and the checkbox a participant unchecks would come
# back ticked.
#
# ``frontend/src/lib/interests.ts`` restates the whole set so the browser can render the same
# checkbox grid the server will accept. Change both.
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

# A participant cannot hold every interest at once and still be telling a matchmaker anything,
# and an unbounded array is a payload no screen has a sane way to render. The ceiling is the
# size of the whole vocabulary, so it is a backstop against a client sending the list twice
# over rather than a limit that ever rejects a real selection.
#
# There is deliberately no separate per-interest length cap. `INTEREST_MAX` used to sit here for
# the `ARRAY(String(80))` column, but with the vocabulary closed every value is one of twelve
# known strings, so the column's width is a database detail rather than something the API has to
# police -- and a second number to keep in step with the first is one more thing to drift.
# Widening an array element is a migration; refusing a value that is not in `INTEREST_VALUES` is
# a 422.
INTEREST_MAX_SELECTED = len(INTEREST_VALUES)


# Wordle. The answer is five letters, and the length is the whole shape of the game: the
# grid, the keyboard, the guess validation and the word list all assume it. One number, so
# the column, the payload validator and the word-list check cannot drift apart -- a word
# list written for one length and a column written for another would only meet at the
# insert, as a value the database accepts and no screen can render.
WORDLE_ANSWER_LENGTH = 5

# How many guesses one player may make against a single day's Wordle, and therefore the
# size of the whole game: six rows on the board, and it is the number a loss means the
# player reached. Written down once here because it appears three more places -- the CHECK
# constraint on ``wordle_user_games.attempt_count`` (named after the number so the
# constraint supersedes itself when the limit changes), the ``max_attempts`` field a
# participation answer carries, and the grid the frontend renders. It is a product decision:
# six is the Wordle norm, long enough that a player who is stuck still has a board to think
# about and short enough that a loss is a board fully played rather than a wall of grey.
WORDLE_MAX_ATTEMPTS = 6

# Anagram. The shortest word a *random* draw may return, and only that: an answer the
# administrator types is accepted at any length down to a single letter, because "cat" and
# "encyclopedia" are both perfectly playable anagrams and the length rule exists to stop the
# draw handing out a two-letter word nobody has to rearrange. Two numbers rather than one
# is the point -- a single minimum would either reject legal custom answers or let the draw
# produce trivia.
#
# The random draw filters ``backend/assets/anagram_words.txt`` by this before choosing, so
# the file itself is left exactly as committed.
ANAGRAM_RANDOM_MIN_LENGTH = 5

# The longest answer an administrator may type. Not a rule about the game -- an anagram of
# any length plays the same -- but the column has to end somewhere, and 100 is far past the
# longest entry in the word list (23 characters) while still being a bound a payload can be
# refused against rather than a value PostgreSQL truncates. It is enforced twice, like every
# other length here: a 400 from the service for a request that asked for it, and a CHECK
# constraint for a write that never went through the API.
ANAGRAM_ANSWER_MAX = 100

# How many guesses one player may make against a single Anagram puzzle, and therefore the
# size of the whole game: five tiles of patience. Written down once here because it appears
# three more places -- the CHECK constraint on ``anagram_user_games.attempt_count`` (named
# after the number so the constraint supersedes itself when the limit changes), the
# ``max_attempts`` field a participation answer carries, and the attempts-remaining line the
# player-facing screen renders. Five rather than Wordle's six is the product decision: an
# anagram guess costs a full rearrangement rather than a typed word, so the budget is a
# touch shorter.
ANAGRAM_MAX_ATTEMPTS = 5

# Couple Prediction Game. The question bank is static application content: fifty
# templates sharing one shape -- a question naming ``X`` and exactly four options.
# One number for the count, so the seed, the sequence CHECK and any future player
# surface cannot drift apart; one number for the options, so a question with three
# or five is unwritable rather than merely discouraged.
COUPLE_QUESTIONS_COUNT = 50
COUPLE_OPTIONS_PER_QUESTION = 4

# The longest question template or option the bank may hold. Not a rule about the
# game -- a prediction plays the same at any length -- but the columns have to end
# somewhere, and these sit far past the longest seeded entry while still being a
# bound a write can be refused against rather than a value PostgreSQL truncates.
# Enforced twice, like every other length here: the service refuses an over-long
# template with a readable error, and a CHECK guards a write that never went
# through the API.
COUPLE_QUESTION_TEXT_MAX = 300
COUPLE_OPTION_TEXT_MAX = 200

# A mutual session draws this many of the fifty bank questions, and splits them
# into this many shared plus this many exclusive per player: 5 shared + 10 each
# is 25 unique per session and 15 answered per participant. One number each, so
# the draw, the assignment CHECKs and the player surface cannot drift apart -- a
# session holding any other shape is a state no screen can render.
COUPLE_SESSION_QUESTIONS = 25
COUPLE_SESSION_SHARED_QUESTIONS = 5
COUPLE_SESSION_EXCLUSIVE_PER_PLAYER = 10
# Each participant's share, derived rather than restated: shared plus their own
# exclusives. Written as the sum so the relationship stays visible.
COUPLE_ANSWERS_PER_PARTICIPANT = COUPLE_SESSION_SHARED_QUESTIONS + COUPLE_SESSION_EXCLUSIVE_PER_PLAYER

# --- Game sessions, featured games and availability ------------------------------
#
# The three games share one session lifecycle, but each game owns its own cadence:
# there is deliberately no global reset setting. A game's ``reset_cadence`` says how
# its sessions count calendar days -- daily, every 2/3/5 days, or weekly
# (Monday-Sunday UTC) -- and the featured game is only a spotlight that never drives
# session creation. All three values below are mirrored by CHECK constraints on
# ``game_configs``; every list here, the payload Literal and the constraint agree.
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

# The only reset cadences a game may carry. Stored as the strings below; each game
# has its own row in ``game_configs`` so Wordle can reset daily while Anagram runs
# every five days and the Couple Questionnaire runs weekly.
RESET_CADENCE_VALUES: tuple[str, ...] = (
    "daily",
    "every_2_days",
    "every_3_days",
    "every_5_days",
    "weekly",
)

# Days in each cadence, for every cadence except weekly. Weekly is always
# Monday-Sunday UTC regardless of when its session started, so it has no fixed
# length here -- see ``game_periods.period_end``.
RESET_CADENCE_DAYS: dict[str, int] = {
    "daily": 1,
    "every_2_days": 2,
    "every_3_days": 3,
    "every_5_days": 5,
}

# How an admin-supplied Wordle/Anagram word reaches a session: ``random`` draws from
# the committed word list at the reset boundary with no approval step, ``manual``
# waits for an administrator to provide (or schedule) the word and otherwise leaves
# the session pending and unplayable. The Couple Questionnaire carries no mode.
GAME_CONTENT_MODES: tuple[str, ...] = ("random", "manual")

# Session lifecycle states. ``active`` is playable (when configured), ``pending`` is
# a manual-mode session still waiting for its word, ``ended`` is history that keeps
# its user progress attached. At most one active-or-pending row exists per game.
SESSION_STATUS_VALUES: tuple[str, ...] = ("active", "pending", "ended")

# The farthest ahead an administrator may schedule a manual word, in days from
# today (UTC). Scheduling is deliberately bounded: without a horizon the schedule
# table becomes an unbounded promise the reset loop must keep forever. Enforced
# server-side on every schedule write; the admin UI names the same number.
MAX_FUTURE_SCHEDULE_DAYS = 30

# Reaction Time. One featured game per date, five submissions per user per date.
# The budget is fixed at the application level: there is no per-day knob for
# it, so it lives here next to WORDLE_MAX_ATTEMPTS rather than in a settings
# table an administrator could edit. A valid reaction is a click *after* green,
# measured client-side with a high-resolution clock; the bounds below keep a
# fabricated payload honest without judging skill -- a reaction under a
# millisecond is not a reaction, and anything past ten seconds is a click on a
# screen that was left open rather than a measurement. A false start's elapsed
# time (start click until the early click) is informational only and never a
# score, so its ceiling is generous.
REACTION_TIME_MAX_SUBMISSIONS = 5
REACTION_TIME_MIN_VALID_MS = 1
REACTION_TIME_MAX_VALID_MS = 10_000
REACTION_TIME_MAX_ELAPSED_MS = 60_000

# The lifecycle states of one Reaction Time submission. ``pending`` is an
# attempt whose start click already consumed one of the five submissions but
# whose result has not been recorded yet (the red wait or the green screen);
# ``valid`` is a click after green carrying a reaction time; ``too_early`` is a
# click before green carrying only informational elapsed time.
REACTION_TIME_STATUS_VALUES: tuple[str, ...] = ("pending", "valid", "too_early")

# Connections. Sixteen words in four hidden groups of four, configured manually
# by an administrator -- there is no random-content mode. One number for the
# total, one for the group count and one for the group size, so the puzzle
# validation, the submission validation, the scoring and the player surface
# cannot drift apart: a puzzle holding any other shape is a state no screen
# can render.
CONNECTIONS_WORDS_TOTAL = 16
CONNECTIONS_GROUPS_COUNT = 4
CONNECTIONS_WORDS_PER_GROUP = 4

# The longest word a Connections puzzle may hold. Not a rule about the game --
# a grouping plays the same at any length -- but the columns have to end
# somewhere, and 50 is far past any reasonable single word or short phrase
# while still being a bound a payload can be refused against rather than a
# value PostgreSQL truncates. Enforced twice, like every other length here: a
# 400 from the service for a request that asks for more, and a CHECK guarding
# a write that never went through the API.
CONNECTIONS_WORD_MAX = 50

# Trivia Drop. Each question carries exactly four options with one or more of
# them correct; the single-answer vs multiple-answer distinction is derived
# from the correct count, never configured. One number for the option count so
# the question validation, the submission validation, the admin surface and
# the player surface cannot drift apart. The text ceilings are bounds a
# payload can be refused against rather than values PostgreSQL truncates:
# enforced twice, like every other length here -- a 400 from the service for
# a request that asks for more, and a CHECK guarding a write that never went
# through the API.
TRIVIA_OPTIONS_COUNT = 4
TRIVIA_QUESTION_TEXT_MAX = 500
TRIVIA_OPTION_TEXT_MAX = 200

# Two-Key Escape. A square grid maze played by one couple with complementary
# roles: the Navigator moves one cell at a time, the Key Operator uses keys
# that each unlock one locked cell and block one traversable cell.
#
# The grid size is the only gameplay knob an administrator may set; the
# number of locks on the board is derived from it by the single mapping
# below, kept here rather than restated on the frontend so the two cannot
# drift apart. Keys are reusable -- every use costs the ten-second cooldown,
# never a consumable charge -- because single-use keys plus relocking can
# permanently strand a lock the couple still needs, an unwinnable state no
# warning can fix. The cooldown is enforced server-side; no movement is ever
# required between key uses.
TWO_KEY_ESCAPE_GRID_SIZES: tuple[int, ...] = (12, 13, 14, 15)
TWO_KEY_ESCAPE_DEFAULT_GRID_SIZE = 12
TWO_KEY_ESCAPE_KEY_CHARGES: dict[int, int] = {12: 12, 13: 13, 14: 14, 15: 15}
TWO_KEY_ESCAPE_KEY_COOLDOWN_SECONDS = 10
TWO_KEY_ESCAPE_ROLES: tuple[str, ...] = ("navigator", "key_operator")


def two_key_escape_key_charges(grid_size: int) -> int:
    """How many key charges a maze of ``grid_size`` grants, one charge per key."""
    return TWO_KEY_ESCAPE_KEY_CHARGES[grid_size]


# Plot Twist. An asynchronous cooperative storytelling game for couples.
# Stories are AI-generated into versioned packages before play; the engine
# executes published packages deterministically and never calls the model
# during gameplay. The bounds below keep generated packages playable and
# scores bounded: scene/decision ceilings bound validation and the UI, the
# repair cap bounds generation loops, and reward tiers map endings to the
# 0-100 league base every other game honours.
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
# How often one couple may revisit the same scene through choices. Reached
# over and over, a loop of compatible scenes plays forever instead of
# ending: past this many visits the choice leading back is no longer
# offered (the least-visited way forward always remains, so the story can
# never strand). Linear stories never notice -- every scene is seen once.
PLOT_TWIST_MAX_SCENE_VISITS = 2
# The decision budget when a package does not name its own: reaching it on
# a non-terminal scene concludes the story at the best qualifying ending
# rather than letting an unnoticed loop run past it.
PLOT_TWIST_DEFAULT_MAX_DECISIONS = 24
# The large majority of decision scenes must belong to one participant:
# past this fraction of shared (either/joint) scenes a package reads as a
# solo story with an audience, not a two-sided one.
PLOT_TWIST_SHARED_SCENE_FRACTION = 0.5
PLOT_TWIST_MAX_INTERCHANGEABLE_PER_SLOT = 6
PLOT_TWIST_MAX_REPAIR_ATTEMPTS = 3
PLOT_TWIST_MAX_BATCH_SCENES = 8
# Reward tiers map an ending to league base points. The engine caps the final
# base at 100 however the tiers combine, so AI values can never bypass it.
PLOT_TWIST_REWARD_TIERS: dict[str, int] = {
    "participation": 40,
    "bronze": 60,
    "silver": 80,
    "gold": 100,
}
PLOT_TWIST_TREASURE_BONUS_EACH = 5
PLOT_TWIST_TREASURE_BONUS_MAX = 15
# Generation transport: per-request timeout and overall attempt caps so a
# stalled provider fails visibly instead of hanging an admin request.
PLOT_TWIST_LLM_TIMEOUT_SECONDS = 120
PLOT_TWIST_SESSION_LOCK_KEY = 7_250_003

# Case Closed. Cooperative mystery game: one immutable published case per
# event, five investigation actions per partner, three initial leads,
# ~15-20 DAG lead nodes, joint accusation with hidden correctness until
# event finalization. Deterministic gameplay; LLM only pre-publication.
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


# How old an open timing segment may be before it is treated as dead rather than played.
#
# The active-play timer is authoritative in ``anagram_user_games``: ``active_time_seconds``
# holds finished segments and ``timer_started_at`` opens the current one. A segment normally
# closes on pause, heartbeat, guess or completion -- but a browser that dies mid-game sends
# none of those, so the segment stays open across the dead period. When a segment older
# than this is closed, its tail is discarded rather than counted: the alternative is
# crediting hours a crashed tab never played.
#
# Two minutes is two heartbeat intervals plus grace. The client heartbeats every 30 seconds
# while the game runs visibly, so a live game never has a segment older than about half a
# minute; anything past two minutes means the heartbeats stopped, which means the page is
# gone, hidden without pausing, or dead. Short enough that a quick logout and return inside
# the window still counts honestly, long enough that a slow network never discards real
# play.
ANAGRAM_TIMER_STALE_AFTER_SECONDS = 120
