# app/services/notification_service.py

import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from ..postgres import Database, is_valid_id
from sqlalchemy import func, select
from ..db_models import NotificationRecord, UserRecord
from ..logger import get_logger

from ..utils import isoformat_datetime
from ..utils import ilike_contains
from .email_delivery_service import enqueue_notification_email
from .notification_links import participant_notification_link

logger = get_logger(__name__)

# Supported notification targets. Conversations use match IDs because API routes do.
NOTIFICATION_TARGETS = frozenset({"conversation", "love_note"})


def _column_width(column) -> int:
    """Return a string column's limit, or an effectively unbounded value for Text."""
    width = column.type.length
    return width if width is not None else sys.maxsize


# Read text limits from the model so clipping stays aligned with the schema.
NOTIFICATION_HEADING_MAX = _column_width(NotificationRecord.__table__.c.heading)
NOTIFICATION_BODY_MAX = _column_width(NotificationRecord.__table__.c.body)


def build_notification_heading(template: str, name: str) -> str:
    """Fit a named heading to its column while preserving template wording when possible."""
    placeholder = "{name}"
    if placeholder not in template:
        return template[:NOTIFICATION_HEADING_MAX]
    room = NOTIFICATION_HEADING_MAX - (len(template) - len(placeholder))
    if room <= 0:
        # The wording alone already fills the column. The name goes rather than the
        # wording: a heading that says what happened is worth more than one that
        # names somebody.
        return template.replace(placeholder, "")[:NOTIFICATION_HEADING_MAX]
    if len(name) <= room:
        return template.replace(placeholder, name)
    return template.replace(placeholder, name[: room - 1] + "…")


def _deliver_notification_email(
    recipient_regno: str,
    heading: str,
    body: str,
    db: Database,
    *,
    link: Optional[str] = None,
) -> None:
    """Queue an optional email after commit; failures do not affect the stored notice."""
    try:
        with db.sessions() as session:
            user = session.scalar(
                select(UserRecord).where(UserRecord.regno == recipient_regno)
            )
            if user is None or not bool(user.notifications_enabled):
                return
            email = user.email
            name = user.name

        payload = {
            "title": heading[:NOTIFICATION_HEADING_MAX],
            "body": body[:NOTIFICATION_BODY_MAX],
            "email": email,
            "name": name,
            "regno": recipient_regno,
        }
        if link:
            payload["link"] = link
        enqueue_notification_email(**payload)
    except Exception as exc:
        logger.warning(
            "Notification email was not queued: delivery preparation failed "
            "(error_type=%s)",
            type(exc).__name__,
        )


def create_notification_service(
    recipient_regno: str,
    heading: str,
    body: str,
    db: Database,
    *,
    target_kind: Optional[str] = None,
    target_id: Optional[str] = None,
) -> str:
    """Record a notification, optionally naming the thing it is about.

    ``target_kind`` and ``target_id`` are both omitted for an informational notice and both
    supplied otherwise; half a target is refused (a kind with no id opens nothing, an id with
    no kind is uninterpretable). Both are validated here rather than at the call sites so a
    caller cannot invent a kind the client does not know -- a failure is a typo in one of the
    eleven call sites, worth a 500 on a development build rather than a notice that quietly
    never works.

    An over-long heading or body is clipped to the column width instead: a wrong target kind is
    a programming error and deserves to be loud, while long text is ordinary data meeting a
    narrow column. Headings naming a participant should come from
    :func:`build_notification_heading`, which keeps the wording and shortens the name.

    Once the row commits, the same heading, body and target are handed to the email pipeline
    when the participant's ``notifications_enabled`` is true; email failure never rolls back
    or raises out of the stored notice.
    """
    if (target_kind is None) != (target_id is None):
        raise ValueError(
            "A notification target needs both a kind and an id, or neither. "
            f"Got kind={target_kind!r} id={target_id!r}."
        )
    if target_kind is not None and target_kind not in NOTIFICATION_TARGETS:
        raise ValueError(
            f"Unknown notification target kind {target_kind!r}. "
            f"Known kinds: {sorted(NOTIFICATION_TARGETS)}."
        )

    # One instant for the row, taken once. It used to be read back off a throwaway Pydantic
    # object built a line earlier for no other purpose, which meant there were two places to
    # keep in step and one more reason a notification write could fail.
    created_at = datetime.now(timezone.utc)

    with db.sessions.begin() as session:
        row = NotificationRecord(
            recipient_regno=recipient_regno,
            # Clipping here -- the one place every call site goes through -- so a long heading
            # (a name is 160 characters and the wording more) cannot fail the insert in a
            # request that has already committed something else.
            heading=heading[:NOTIFICATION_HEADING_MAX],
            body=body[:NOTIFICATION_BODY_MAX],
            created_at=created_at,
            is_read=False,
            target_kind=target_kind,
            target_id=target_id,
        )
        session.add(row)
        session.flush()
        notification_id = str(row.id)

    # The in-app notice is committed before the email is attempted. Delivery is
    # best-effort and gated by the participant's own notifications_enabled switch.
    # The email carries the same target the inbox opens, when there is one.
    _deliver_notification_email(
        recipient_regno,
        heading[:NOTIFICATION_HEADING_MAX],
        body[:NOTIFICATION_BODY_MAX],
        db,
        link=participant_notification_link(target_kind, target_id),
    )
    return notification_id

# Hard ceiling for one inbox page. Notification rows are small, but an account that
# has been collecting them for years still must not have them all serialised at once.
NOTIFICATIONS_MAX_LIMIT = 100


def _serialise_notification(notification: NotificationRecord) -> Dict[str, Any]:
    """One notification row as the inbox reads it."""
    return {
        "id": str(notification.id),
        "recipient_regno": notification.recipient_regno,
        "heading": notification.heading,
        "body": notification.body,
        "created_at": isoformat_datetime(notification.created_at),
        "is_read": notification.is_read,
        # Nulls rather than omitted keys, so a client can tell "this notification has no
        # target" from "this build of the server did not send targets at all".
        "target_kind": notification.target_kind,
        "target_id": notification.target_id,
    }


def get_user_notifications_service(
    user_id: str,
    db: Database,
    limit: int = 50,
    *,
    skip: int = 0,
    query: Optional[str] = None,
) -> Dict[str, Any]:
    """One page of a reader's notifications, newest first, with the total beside it.

    The page is a SQL LIMIT/OFFSET and ``total`` a COUNT under the same filter, so an
    account with years of notices loads downwards instead of all at once. ``query``
    narrows in SQL to notices whose heading or body matches; ``id`` seconds the order
    so two notices stamped in the same instant still page deterministically.
    """
    skip = max(0, int(skip or 0))
    limit = max(1, min(int(limit or 50), NOTIFICATIONS_MAX_LIMIT))
    filters = [NotificationRecord.recipient_regno == user_id]
    cleaned = (query or "").strip()
    if cleaned:
        filters.append(
            ilike_contains(NotificationRecord.heading, cleaned)
            | ilike_contains(NotificationRecord.body, cleaned)
        )
    with db.sessions() as session:
        total = session.scalar(
            select(func.count()).select_from(NotificationRecord).where(*filters)
        ) or 0
        notifications = session.scalars(
            select(NotificationRecord)
            .where(*filters)
            .order_by(NotificationRecord.created_at.desc(), NotificationRecord.id.asc())
            .offset(skip).limit(limit)
        ).all()

    return {
        "notifications": [_serialise_notification(notification) for notification in notifications],
        "total": total,
        "skip": skip,
        "limit": limit,
    }

def mark_notification_read_service(notification_id: str, user_id: str, db: Database):
    """
    Mark a notification as read.

    A malformed id must not reach the database to find that out: compared against a uuid
    column, PostgreSQL raises ``invalid input syntax for type uuid`` on a non-uuid. A malformed
    id names no notification, the same answer this gives for one that is not the reader's.
    """
    if not is_valid_id(notification_id):
        return
    with db.sessions.begin() as session:
        notification = session.scalar(select(NotificationRecord).where(
            NotificationRecord.id == notification_id,
            NotificationRecord.recipient_regno == user_id,
        ))
        if notification:
            notification.is_read = True

def delete_notification_service(notification_id: str, user_id: str, db: Database):
    """
    Delete a notification.

    Guarded for the same reason as the read above: a malformed id names no
    notification, and it must not reach a uuid column to find that out.
    """
    if not is_valid_id(notification_id):
        return
    with db.sessions.begin() as session:
        notification = session.scalar(select(NotificationRecord).where(
            NotificationRecord.id == notification_id,
            NotificationRecord.recipient_regno == user_id,
        ))
        if notification:
            session.delete(notification)
