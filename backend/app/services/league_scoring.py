"""League scoring: placement normalization, featured multipliers, and UTC periods."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

# Scoring-policy version stamped on opportunities and awards.
SCORING_VERSION = 1

# Competition types: individual (users) and couple (canonical pairs).
COMPETITION_INDIVIDUAL = "individual"
COMPETITION_COUPLE = "couple"
COMPETITION_TYPES = (COMPETITION_INDIVIDUAL, COMPETITION_COUPLE)

# Leaderboard periods in UTC.
PERIOD_DAILY = "daily"
PERIOD_WEEKLY = "weekly"
PERIOD_MONTHLY = "monthly"
PERIOD_ALL_TIME = "all_time"
PERIOD_VALUES = (PERIOD_DAILY, PERIOD_WEEKLY, PERIOD_MONTHLY, PERIOD_ALL_TIME)

# Competition type mappings per game.
INDIVIDUAL_GAMES = (
    "wordle",
    "anagram",
    "connections",
    "trivia-drop",
    "reaction-time",
)
COUPLE_GAMES = (
    "couple-questionnaire",
    "two-key-escape",
    "plot-twist",
    "case-closed",
)

GAME_COMPETITION_TYPES: dict[str, str] = {
    **{game_id: COMPETITION_INDIVIDUAL for game_id in INDIVIDUAL_GAMES},
    **{game_id: COMPETITION_COUPLE for game_id in COUPLE_GAMES},
}

# Featured opportunities award 2x base points.
FEATURED_MULTIPLIER = 2
NORMAL_MULTIPLIER = 1

# Base points for an uncontested opportunity.
SOLO_BASE_POINTS = Decimal("50.00")

_POINTS_QUANTUM = Decimal("0.01")


def quantize_points(value: Decimal) -> Decimal:
    """Round league points to two decimal places, deterministically."""
    return value.quantize(_POINTS_QUANTUM, rounding=ROUND_HALF_UP)


def base_points_for_rank(rank: int, participant_count: int) -> Decimal:
    """Calculate base points (100 first, 0 last, 50 if uncontested)."""
    if participant_count <= 0:
        raise ValueError("participant_count must be positive")
    if rank < 1 or rank > participant_count:
        raise ValueError("rank must lie within the ranked field")
    if participant_count == 1:
        return SOLO_BASE_POINTS
    return quantize_points(
        Decimal(100) * Decimal(participant_count - rank) / Decimal(participant_count - 1)
    )


def averaged_tie_points(ranks: list[int], participant_count: int) -> Decimal:
    """Calculate average base points shared across tied placement positions."""
    if not ranks:
        raise ValueError("a tie must occupy at least one position")
    total = sum(base_points_for_rank(rank, participant_count) for rank in ranks)
    return quantize_points(total / len(ranks))


def awarded_points(base: Decimal, is_featured: bool) -> tuple[Decimal, int]:
    """Apply snapshotted featured multiplier (2x or 1x) to base points."""
    multiplier = FEATURED_MULTIPLIER if is_featured else NORMAL_MULTIPLIER
    return quantize_points(base * multiplier), multiplier


def canonical_couple(user_a_id: str, user_b_id: str) -> tuple[str, str]:
    """Canonical sorted pair identifier for couple subjects."""
    if user_a_id <= user_b_id:
        return user_a_id, user_b_id
    return user_b_id, user_a_id


def competition_date(moment: datetime) -> date:
    """The UTC calendar day an aware instant belongs to for scoring."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).date()


def week_start(day: date) -> date:
    """The Monday (UTC) opening the week containing ``day``."""
    return day - timedelta(days=day.weekday())


def month_key(day: date) -> str:
    """The ``YYYY-MM`` label of the UTC month containing ``day``."""
    return f"{day.year:04d}-{day.month:02d}"


def period_bounds(period: str, anchor: Optional[date] = None) -> Optional[tuple[date, date]]:
    """Return inclusive UTC date window for a given period and anchor date."""
    if period not in PERIOD_VALUES:
        raise ValueError(f"Unknown leaderboard period {period!r}.")
    if period == PERIOD_ALL_TIME:
        return None
    day = anchor or datetime.now(timezone.utc).date()
    if period == PERIOD_DAILY:
        return day, day
    if period == PERIOD_WEEKLY:
        start = week_start(day)
        return start, start + timedelta(days=6)
    start = date(day.year, day.month, 1)
    if day.month == 12:
        end = date(day.year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(day.year, day.month + 1, 1) - timedelta(days=1)
    return start, end


def week_starts_between(start: date, end: date) -> list[date]:
    """Every Monday (UTC) of the weeks overlapping ``[start, end]``."""
    first = week_start(start)
    weeks = []
    current = first
    step = timedelta(days=7)
    while current <= end:
        weeks.append(current)
        current += step
    return weeks
