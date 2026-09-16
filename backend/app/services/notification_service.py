# app/services/notification_service.py

from datetime import datetime, timezone
from typing import Optional
from ..postgres import Database
from sqlalchemy import select
from ..db_models import NotificationRecord

from ..utils import isoformat_datetime

# What a notification can point at, and what the target id means in each case.
#
# A kind is the name of the thing the id identifies, not the name of the table column it is
# read from -- the id is passed to the client, which puts it in a URL. ``conversation`` is a
# match id rather than a conversation id because every conversation route in the API is keyed
# by match, so that is the id the client needs.
#
# This is the whole list, and :func:`create_notification_service` refuses anything outside it.
# A target the client cannot resolve is worse than no target: the notification still looks
# clickable, and clicking it lands the reader on a page that quietly does not contain the thing
# the notification was about.
NOTIFICATION_TARGETS = frozenset({"conversation", "love_note"})


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

    ``target_kind`` and ``target_id`` are both omitted for an informational notice -- a match
    that was drawn, a report that was reviewed, content that was taken down -- and both
    supplied together otherwise. Half a target is refused rather than stored: a kind with no id
    opens nothing, and an id with no kind is a value the client has no way to interpret.

    Both are validated here rather than at the call sites so that a caller cannot invent a
    target kind the client does not know, and so the two always travel together. There are
    eleven call sites in four services and all of them pass literals, so a failure here means a
    typo in one of them and is worth a 500 on a development build rather than a notification
    that quietly never works.
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
            heading=heading,
            body=body,
            created_at=created_at,
            is_read=False,
            target_kind=target_kind,
            target_id=target_id,
        )
        session.add(row)
        session.flush()
        return str(row.id)

def get_user_notifications_service(user_id: str, db: Database, limit: int = 50):
    """
    Get notifications for a user, ordered by timestamp descending.
    """
    with db.sessions() as session:
        notifications = session.scalars(
            select(NotificationRecord)
            .where(NotificationRecord.recipient_regno == user_id)
            .order_by(NotificationRecord.created_at.desc())
            .limit(limit)
        ).all()

    # Convert str to string and handle serialization
    result = []
    for notification in notifications:
        result.append({
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
        })

    return result

def mark_notification_read_service(notification_id: str, user_id: str, db: Database):
    """
    Mark a notification as read.
    """
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
    """
    with db.sessions.begin() as session:
        notification = session.scalar(select(NotificationRecord).where(
            NotificationRecord.id == notification_id,
            NotificationRecord.recipient_regno == user_id,
        ))
        if notification:
            session.delete(notification)
