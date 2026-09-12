import logging
from datetime import datetime, timezone

# Registration numbers and application IDs are stored as text values.


def isoformat_datetime(value):
    """Serialize PostgreSQL ISO timestamp strings and Python datetimes uniformly.

    Always emits an explicit UTC offset. A naive datetime would otherwise serialise to
    something like "2026-09-28T12:01:36" with no zone at all, and every browser reads that
    as *its own* local time rather than UTC -- so a stored-UTC instant would render several
    hours off for the recipient, and it is not fixable downstream because the information
    is simply absent. Coercing here keeps the wire format unambiguous, which is what lets
    the client's parseTimestamp assume a bare timestamp means UTC.

    Matches ``admin_service.serialize_datetime``, which already did this.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


