# app/services/notification_service.py

from datetime import datetime, timezone
from ..postgres import Database
from sqlalchemy import select
from ..db_models import NotificationRecord

from ..models import Notification
from ..utils import isoformat_datetime

def create_notification_service(recipient_regno: str, heading: str, body: str, db: Database) -> str:
    """
    Create a new notification for a user.
    """
    notification = Notification(
        recipient_regno=recipient_regno,
        heading=heading,
        body=body,
        created_at=datetime.now(timezone.utc)
    )
    with db.sessions.begin() as session:
        row = NotificationRecord(
            recipient_regno=recipient_regno,
            heading=heading,
            body=body,
            created_at=notification.created_at,
            is_read=False,
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
