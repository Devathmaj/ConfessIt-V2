# app/services/notification_service.py

from datetime import datetime, timezone
from pymongo.database import Database
from bson import ObjectId

from ..models import Notification, NotificationContent

def create_notification_service(user_id: str, heading: str, body: str, db: Database) -> str:
    """
    Create a new notification for a user.
    """
    notification = Notification(
        user_id=user_id,
        content=NotificationContent(heading=heading, body=body),
        timestamp=datetime.now(timezone.utc)
    )
    result = db.notifications.insert_one(notification.dict(by_alias=True))
    return str(result.inserted_id)

def get_user_notifications_service(user_id: str, db: Database, limit: int = 50):
    """
    Get notifications for a user, ordered by timestamp descending.
    """
    notifications = db.notifications.find(
        {"user_id": user_id}
    ).sort("timestamp", -1).limit(limit)
    
    # Convert ObjectId to string and handle serialization
    result = []
    for notification in notifications:
        notification_dict = dict(notification)
        notification_dict["_id"] = str(notification_dict["_id"])
        # Convert timestamp to ISO format string for JSON serialization
        if "timestamp" in notification_dict and notification_dict["timestamp"]:
            notification_dict["timestamp"] = notification_dict["timestamp"].isoformat()
        result.append(notification_dict)
    
    return result

def mark_notification_read_service(notification_id: str, user_id: str, db: Database):
    """
    Mark a notification as read.
    """
    db.notifications.update_one(
        {"_id": ObjectId(notification_id), "user_id": user_id},
        {"$set": {"read": True}}
    )

def delete_notification_service(notification_id: str, user_id: str, db: Database):
    """
    Delete a notification.
    """
    db.notifications.delete_one(
        {"_id": ObjectId(notification_id), "user_id": user_id}
    )