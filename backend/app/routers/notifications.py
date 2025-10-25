# app/routers/notifications.py

from fastapi import APIRouter, Depends, status
from pymongo.database import Database
from typing import Annotated

from ..dependencies import get_db
from ..models import UserDetails
from ..services.auth_service import get_current_user
from ..services.notification_service import (
    get_user_notifications_service,
    mark_notification_read_service,
    delete_notification_service
)

router = APIRouter(
    prefix="/notifications",
    tags=["Notifications"]
)

@router.get("/")
def get_notifications(
    current_user: Annotated[UserDetails, Depends(get_current_user)],
    db: Database = Depends(get_db)
):
    """
    Get all notifications for the current user.
    """
    notifications = get_user_notifications_service(current_user.Regno, db)
    return {"notifications": notifications}

@router.put("/{notification_id}/read")
def mark_notification_read(
    notification_id: str,
    current_user: Annotated[UserDetails, Depends(get_current_user)],
    db: Database = Depends(get_db)
):
    """
    Mark a notification as read.
    """
    mark_notification_read_service(notification_id, current_user.Regno, db)
    return {"message": "Notification marked as read"}

@router.delete("/{notification_id}")
def delete_notification(
    notification_id: str,
    current_user: Annotated[UserDetails, Depends(get_current_user)],
    db: Database = Depends(get_db)
):
    """
    Delete a notification.
    """
    delete_notification_service(notification_id, current_user.Regno, db)
    return {"message": "Notification deleted"}