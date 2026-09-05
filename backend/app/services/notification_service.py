# app/services/notification_service.py

from datetime import datetime, timezone
from ..postgres import Database
from sqlalchemy import select
from ..db_models import NotificationRecord

from ..models import Notification, NotificationContent
from ..utils import isoformat_datetime

def create_notification_service(user_id: str, heading: str, body: str, db: Database) -> str:
    """
    Create a new notification for a user.
    """
    notification = Notification(
        user_id=user_id,
        content=NotificationContent(heading=heading, body=body),
        timestamp=datetime.now(timezone.utc)
    )
    with db.sessions.begin() as session:
        row = NotificationRecord(
            user_id=user_id,
            heading=heading,
            body=body,
            timestamp=notification.timestamp,
            read=False,
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
            .where(NotificationRecord.user_id == user_id)
            .order_by(NotificationRecord.timestamp.desc())
            .limit(limit)
        ).all()
    
    # Convert str to string and handle serialization
    result = []
    for notification in notifications:
        result.append({
            "_id": str(notification.id),
            "user_id": notification.user_id,
            "content": {"heading": notification.heading, "body": notification.body},
            "timestamp": isoformat_datetime(notification.timestamp),
            "read": notification.read,
        })
    
    return result

def mark_notification_read_service(notification_id: str, user_id: str, db: Database):
    """
    Mark a notification as read.
    """
    with db.sessions.begin() as session:
        notification = session.scalar(select(NotificationRecord).where(
            NotificationRecord.id == notification_id,
            NotificationRecord.user_id == user_id,
        ))
        if notification:
            notification.read = True

def delete_notification_service(notification_id: str, user_id: str, db: Database):
    """
    Delete a notification.
    """
    with db.sessions.begin() as session:
        notification = session.scalar(select(NotificationRecord).where(
            NotificationRecord.id == notification_id,
            NotificationRecord.user_id == user_id,
        ))
        if notification:
            session.delete(notification)
