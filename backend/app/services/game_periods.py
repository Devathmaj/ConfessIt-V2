"""Session boundary and reset window calculations for game cadences in UTC."""

from datetime import date, datetime, timezone
from typing import Optional

from ..limits import RESET_CADENCE_DAYS, RESET_CADENCE_VALUES


def utc_today(now: Optional[datetime] = None) -> date:
    """Today's UTC calendar date -- the only "today" the games system uses."""
    return (now or datetime.now(timezone.utc)).date()


def validate_cadence(cadence: str) -> str:
    """Refuse anything outside the closed cadence vocabulary."""
    if cadence not in RESET_CADENCE_VALUES:
        raise ValueError(
            f"Unknown reset cadence {cadence!r}. "
            f"Expected one of {', '.join(RESET_CADENCE_VALUES)}."
        )
    return cadence


def monday_of(day: date) -> date:
    """The Monday (UTC) of the week holding ``day``."""
    from datetime import timedelta

    return day - timedelta(days=day.weekday())


def period_end(start: date, cadence: str) -> date:
    """Calculate exclusive reset date for a session starting on start under cadence."""
    from datetime import timedelta

    validate_cadence(cadence)
    if cadence == "weekly":
        return monday_of(start) + timedelta(days=7)
    return start + timedelta(days=RESET_CADENCE_DAYS[cadence])


def period_for_start(start: date, cadence: str) -> tuple[date, date]:
    """The ``(start, exclusive_end)`` pair for a session beginning on ``start``."""
    return start, period_end(start, cadence)


def is_reset_due(period_end_date: Optional[date], today: Optional[date] = None) -> bool:
    """Whether the session owning ``period_end_date`` has run past its boundary."""
    if period_end_date is None:
        return True
    day = today if today is not None else utc_today()
    return day >= period_end_date
