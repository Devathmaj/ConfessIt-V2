"""Absolute global scoring profiles: native results in, 0-100 base points out.

Where :mod:`league_scoring` converts a *placement within one opportunity*
(rank 1 of N, ties, solo consolation) into points, this module converts the
*result itself* -- guesses used, milliseconds measured, words placed,
predictions correct -- into a normalized global base score. The two paths
are independent by design:

* ``game`` path (placement-relative, policy v1): feeds per-game boards and
  preserves each game's established comparison rules, including Reaction
  Time's relative comparison on its own board.
* ``global`` path (absolute, policy v2 below): feeds the combined global
  boards. It never inspects the session field -- no rank, no participant
  count, no other subject's result, no min/max scaling, no tie positions.

Every profile is a pure function of the finalized native metrics the
adapters already persist on the award row's ``raw_metrics`` (plus small
counts the adapters add, e.g. couple totals). The same metrics under the
same profile version always produce the same base score, regardless of who
else played. Thresholds are explicit constants, frozen here -- runtime
scoring never adjusts them from leaderboard populations. If calibration
ever changes, bump ``GLOBAL_SCORING_VERSION`` and reprocess; old rows keep
their stamped version so history stays explainable.

Common scale (shared interpretation, per-game functions below):

* 0: no qualifying score / unsuccessful outcome / no valid result
* 1-20: minimal | 21-40: low | 41-60: moderate | 61-80: strong | 81-100: excellent
"""

from decimal import Decimal
from typing import Any

from .league_scoring import quantize_points

# Continues the SCORING_VERSION lineage in league_scoring (v1 = the
# placement-relative rule): v2 is the first absolute-profile edition. Old
# award rows predate the split and carry NULL versions, which boards read as
# the v1-relative policy through the documented legacy fallback.
GLOBAL_SCORING_VERSION = 2

# The game path keeps the placement-relative rule, unchanged in behavior;
# it is stamped separately so each half of a split award names its policy.
GAME_SCORING_VERSION = 1


# ---------------------------------------------------------------------------
# Wordle: solve first, then fewer guesses. WORDLE_MAX_ATTEMPTS is 6.
# ---------------------------------------------------------------------------

WORDLE_GLOBAL_BY_ATTEMPTS: dict[int, Decimal] = {
    1: Decimal("100"),
    2: Decimal("90"),
    3: Decimal("80"),
    4: Decimal("70"),
    5: Decimal("60"),
    6: Decimal("50"),
}


def wordle_global_base(raw: dict[str, Any]) -> Decimal:
    """Solved in N guesses -> fixed table; unsolved -> 0."""
    if not raw.get("solved"):
        return Decimal("0")
    try:
        attempts = int(raw.get("attempts") or 0)
    except (TypeError, ValueError):
        return Decimal("0")
    if attempts < 1:
        return Decimal("0")
    # The game caps at 6; a solve beyond that (should not exist) keeps the
    # floor tier rather than inventing a new one.
    return WORDLE_GLOBAL_BY_ATTEMPTS.get(attempts, WORDLE_GLOBAL_BY_ATTEMPTS[6])


# ---------------------------------------------------------------------------
# Anagram: solved, then attempts tier, then active time within the tier.
# ANAGRAM_MAX_ATTEMPTS is 5. Tiers never overlap: a slower solve with fewer
# attempts always outscores a faster solve with more attempts.
# ---------------------------------------------------------------------------

# Tier floors: attempt N starts at 100 - 10*N, time bonus adds 1..10.
ANAGRAM_TIER_FLOOR = {1: 90, 2: 80, 3: 70, 4: 60, 5: 50}

# Fixed active-time thresholds (seconds, from the game's own active-time
# accounting, which excludes time away from the page).
ANAGRAM_TIME_BONUS: tuple[tuple[float, int], ...] = (
    (30.0, 10),
    (90.0, 7),
    (180.0, 4),
    (float("inf"), 1),
)


def anagram_global_base(raw: dict[str, Any]) -> Decimal:
    """Solved only; tier by attempts, bonus by active seconds within tier."""
    if not raw.get("solved"):
        return Decimal("0")
    try:
        attempts = int(raw.get("attempts") or 0)
    except (TypeError, ValueError):
        return Decimal("0")
    if attempts < 1:
        return Decimal("0")
    floor = ANAGRAM_TIER_FLOOR.get(attempts, ANAGRAM_TIER_FLOOR[5])
    active = raw.get("active_time_seconds")
    try:
        seconds = float(active) if active is not None else float("inf")
    except (TypeError, ValueError):
        seconds = float("inf")
    bonus = 1
    for threshold, points in ANAGRAM_TIME_BONUS:
        if seconds <= threshold:
            bonus = points
            break
    return quantize_points(Decimal(floor + bonus))


# ---------------------------------------------------------------------------
# Reaction Time (global path only): absolute milliseconds -> fixed brackets.
# Strictly monotonic decreasing: a faster valid reaction never scores less
# than a slower one. Invalid reactions never reach this function (the
# adapter only emits valid rows) and score 0 by construction.
# ---------------------------------------------------------------------------

# (upper bound ms exclusive, points). Covers every non-negative time.
REACTION_GLOBAL_BRACKETS: tuple[tuple[float, int], ...] = (
    (180.0, 100),
    (220.0, 90),
    (260.0, 80),
    (300.0, 70),
    (350.0, 60),
    (400.0, 50),
    (500.0, 40),
    (650.0, 30),
    (800.0, 20),
    (1000.0, 10),
    (float("inf"), 5),
)


def reaction_global_base(raw: dict[str, Any]) -> Decimal:
    """Best valid reaction time in ms -> fixed bracket (any valid >= 1)."""
    try:
        best = float(raw.get("best_ms"))
    except (TypeError, ValueError):
        return Decimal("0")
    if best is None or best < 0:
        return Decimal("0")
    for bound, points in REACTION_GLOBAL_BRACKETS:
        if best < bound:
            return Decimal(points)
    return Decimal("5")


# ---------------------------------------------------------------------------
# Trivia Drop: 80 accuracy + up to 20 speed, per question. Accuracy comes
# from trivia_accuracy() below (exact match for single-answer, partial
# credit for multi-answer, computed at finalization from the persisted key
# -- never from the session field). Entirely incorrect -> 0, and blanketing
# every option without exactly matching the key -> 0.
# ---------------------------------------------------------------------------

TRIVIA_ACCURACY_POINTS = Decimal("80")
TRIVIA_SPEED_POINTS = Decimal("20")


def trivia_accuracy(selected_options: Any, correct_options: Any, all_options: Any) -> float:
    """Deterministic partial credit for one submission (0..1).

    Inputs are the submitted option set, the answer-key set, and the full
    set of available options for the question:

    * exact match to the key -> 1.0 (covers single-answer correctness);
    * empty selection, empty key, or wholly wrong answer -> 0.0;
    * selecting *every* available option without exactly matching the key
      -> 0.0 (anti-select-all: blanketing the options must never pay, e.g.
      3-of-4 correct blanketed would otherwise score (3-1)/3 = 0.667);
    * otherwise ``max(0, (hits - false_positives) / key_size)``: partial
      selections without wrong picks keep proportional credit, and each
      additional incorrect pick strictly lowers the score, so fishing with
      extra options can never raise it.
    """
    try:
        picked = {int(option) for option in (selected_options or [])}
    except (TypeError, ValueError):
        return 0.0
    try:
        key = {int(option) for option in (correct_options or [])}
    except (TypeError, ValueError):
        return 0.0
    try:
        universe = {int(option) for option in (all_options or [])}
    except (TypeError, ValueError):
        universe = set()
    if not picked or not key:
        return 0.0
    if picked == key:
        return 1.0
    if universe and picked >= universe:
        return 0.0
    hits = len(picked & key)
    false_positives = len(picked - key)
    return max(0.0, (hits - false_positives) / len(key))

# Fixed absolute speed window: full bonus at instant answer, decaying
# linearly to zero at 30s. A slow correct answer keeps its 80.
TRIVIA_SPEED_WINDOW_MS = 30_000.0


def trivia_speed_factor(response_time_ms: Any) -> Decimal:
    """0..1 from absolute response ms (never session-relative)."""
    try:
        ms = float(response_time_ms)
    except (TypeError, ValueError):
        return Decimal("0")
    if ms < 0:
        return Decimal("0")
    return quantize_points(
        Decimal(max(0.0, 1.0 - ms / TRIVIA_SPEED_WINDOW_MS))
    )


def trivia_global_base(raw: dict[str, Any]) -> Decimal:
    """80 * accuracy + 20 * accuracy * speed_factor (0 when all wrong)."""
    try:
        accuracy = float(raw.get("accuracy", 1.0 if raw.get("is_correct") else 0.0))
    except (TypeError, ValueError):
        accuracy = 0.0
    if accuracy <= 0:
        return Decimal("0")
    if accuracy > 1:
        accuracy = 1.0
    speed = trivia_speed_factor(raw.get("response_time_ms"))
    return quantize_points(
        TRIVIA_ACCURACY_POINTS * Decimal(str(accuracy))
        + TRIVIA_SPEED_POINTS * Decimal(str(accuracy)) * speed
    )


# ---------------------------------------------------------------------------
# Connections: absolute partial progress. 4 per placed word (cap 64) + 9 per
# complete group (cap 36). One full group = 16 + 9 = 25; solved = 100.
# ---------------------------------------------------------------------------

CONNECTIONS_PER_WORD = Decimal("4")
CONNECTIONS_WORDS_CAP = Decimal("64")
CONNECTIONS_PER_GROUP = Decimal("9")
CONNECTIONS_GROUPS_CAP = Decimal("36")


def connections_global_base(raw: dict[str, Any]) -> Decimal:
    """Partial progress pays without requiring a full solve."""
    try:
        words = int(raw.get("correct_words") or 0)
    except (TypeError, ValueError):
        words = 0
    try:
        groups = int(raw.get("correct_groups") or 0)
    except (TypeError, ValueError):
        groups = 0
    words = max(0, words)
    groups = max(0, groups)
    return quantize_points(
        min(CONNECTIONS_PER_WORD * words, CONNECTIONS_WORDS_CAP)
        + min(CONNECTIONS_PER_GROUP * groups, CONNECTIONS_GROUPS_CAP)
    )


# ---------------------------------------------------------------------------
# Couple Questionnaire: 100 * correct / total scoreable predictions.
# Denominator is the session's eligible question set (two prediction
# opportunities per shared question, one per exclusive question), read from
# the session's drawn question rows -- never from submissions. Unanswered
# or never-submitted questions count as zero without shrinking the
# denominator. No scoreable questions (or no correct predictions) -> 0.
# ---------------------------------------------------------------------------


def couple_global_base(raw: dict[str, Any]) -> Decimal:
    """Proportional credit: partial sessions keep earned points."""
    try:
        correct = int(raw.get("correct_predictions") or 0)
    except (TypeError, ValueError):
        correct = 0
    try:
        total = int(raw.get("total_questions") or 0)
    except (TypeError, ValueError):
        total = 0
    if total <= 0 or correct <= 0:
        return Decimal("0")
    if correct > total:
        correct = total
    return quantize_points(Decimal("100") * Decimal(correct) / Decimal(total))


# ---------------------------------------------------------------------------
# Two-Key Escape: binary. Completed escape -> 100 regardless of time;
# anything else never reaches an award row (unfinished runs are absent
# subjects, not zero-point subjects).
# ---------------------------------------------------------------------------

TWO_KEY_ESCAPE_SUCCESS_POINTS = Decimal("100")


def two_key_escape_global_base(raw: dict[str, Any]) -> Decimal:
    """Every completed escape earns the same base; time is irrelevant."""
    return TWO_KEY_ESCAPE_SUCCESS_POINTS


# ---------------------------------------------------------------------------
# Plot Twist: tiered. The ending's validated reward tier sets the base and
# each discovered treasure adds a bounded bonus, capped at 100. Unfinished
# stories never reach an award row. The adapter precomputes the base with
# the same rule (plot_twist_gameplay.score_for_outcome) and carries it in
# ``base_points``; this profile clamps it so AI-authored tiers can never
# bypass the ceiling even if a stored row predates a tier change.
# ---------------------------------------------------------------------------


def plot_twist_global_base(raw: dict[str, Any]) -> Decimal:
    """Clamp the session's deterministic base into the 0-100 scale."""
    try:
        base = int(raw.get("base_points") if raw.get("base_points") is not None else 0)
    except (TypeError, ValueError):
        base = 0
    return quantize_points(Decimal(str(max(0, min(100, base)))))


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_GLOBAL_BASE_FUNCTIONS = {
    "wordle": wordle_global_base,
    "anagram": anagram_global_base,
    "reaction-time": reaction_global_base,
    "trivia-drop": trivia_global_base,
    "connections": connections_global_base,
    "couple-questionnaire": couple_global_base,
    "two-key-escape": two_key_escape_global_base,
    "plot-twist": plot_twist_global_base,
}


def global_base_for(game_id: str, raw_metrics: Any) -> Decimal:
    """Absolute 0-100 base for one finalized native result.

    Pure in the finalized metrics: never rank, field size, or other
    subjects. Unknown games raise (fail-closed: no invented scores).
    """
    try:
        function = _GLOBAL_BASE_FUNCTIONS[game_id]
    except KeyError:
        from .league_adapters import competition_type_for

        competition_type_for(game_id)
        raise
    raw = raw_metrics if isinstance(raw_metrics, dict) else {}
    return function(raw)
