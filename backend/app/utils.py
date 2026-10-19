import logging
import re
from datetime import datetime, timezone

from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError

# Registration numbers and application IDs are stored as text values.

# LIKE metacharacters that must be escaped when user input is interpolated into
# a LIKE/ILIKE pattern: ``%`` (any run), ``_`` (one character) and the escape
# character itself. Without this a search for "100%" matches everything
# containing "100", and a search for "%" matches the whole table.
_LIKE_ESCAPE_RE = re.compile(r"([\\%_])")


def escape_like_literal(value: str) -> str:
    """Escape LIKE wildcards so user input matches literally.

    Prefixes every ``\\``, ``%`` and ``_`` with the backslash escape character,
    for use with ``LIKE ... ESCAPE '\\'`` (see :func:`ilike_contains`). A plain
    search term passes through unchanged, so behaviour only changes for inputs
    that previously acted as wildcards.
    """
    return _LIKE_ESCAPE_RE.sub(r"\\\1", value)


def ilike_contains(column, cleaned: str):
    """A case-insensitive substring match that treats user input literally.

    ``cleaned`` must already be stripped (and empty-checked by the caller); it
    is escaped so ``%``, ``_`` and ``\\`` match themselves, wrapped in ``%`` for
    substring semantics, and matched with an explicit ``ESCAPE '\\'`` so the
    backslashes are honoured on every backend rather than by accident.
    """
    return column.ilike(f"%{escape_like_literal(cleaned)}%", escape="\\")


def validation_error_detail(exc: ValidationError) -> list:
    """The error list FastAPI itself would have produced for this payload.

    A payload checked twice -- once by the model the API publishes and again by
    the stricter model the service uses -- has a failure the framework never
    sees. The second check raises ``ValidationError``, which is not an
    ``HTTPException``, and an exception nobody handles is a 500 for a payload the
    client got wrong.

    The shape is the one FastAPI's own request validation answers with -- a list
    of ``{type, loc, msg, input}`` -- so a client that reads the first error out
    of the list needs no second shape to understand, and the field the payload
    got wrong is named in the ``loc`` the way it is for a request validation
    error.
    """
    details = []
    for error in exc.errors():
        location = error.get("loc") or ()
        details.append({
            "type": str(error.get("type") or "value_error"),
            "loc": ["body", *[str(part) for part in location]],
            "msg": str(error.get("msg") or "is invalid"),
            "input": jsonable_encoder(error.get("input")),
        })
    return details


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


