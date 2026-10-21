# app/routers/notifications.py

from fastapi import APIRouter, Depends, Query, status
from ..postgres import Database
from ..limits import PAGE_OFFSET_MAX
from typing import Annotated, Any, Dict, Optional
from ..services.notification_service import NOTIFICATIONS_MAX_LIMIT

from ..dependencies import get_db
from ..models import UserDetails
from ..services.authorization import require_participant
from ..services.notification_service import (
    get_user_notifications_service,
    mark_notification_read_service,
    delete_notification_service
)

router = APIRouter(
    prefix="/notifications",
    tags=["Notifications"]
)

@router.get("")
def get_notifications(
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db),
    skip: int = Query(0, ge=0, le=PAGE_OFFSET_MAX, description="How many matching notices to skip."),
    limit: int = Query(50, ge=1, le=NOTIFICATIONS_MAX_LIMIT, description="How many matching notices to return. Clamped so one request can never serialise years of notices at once."),
    q: Optional[str] = Query(None, max_length=100, description="Case-insensitive substring match on the heading and body."),
):
    """One page of the reader's notifications, newest first, with the total count."""
    return get_user_notifications_service(current_user.regno, db, limit, skip=skip, query=q)

@router.put("/{notification_id}/read")
def mark_notification_read(
    notification_id: str,
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db)
):
    """Mark a notification as read."""
    mark_notification_read_service(notification_id, current_user.regno, db)
    return {"message": "Notification marked as read"}

@router.delete("/{notification_id}")
def delete_notification(
    notification_id: str,
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db)
):
    """Delete a notification."""
    delete_notification_service(notification_id, current_user.regno, db)
    return {"message": "Notification deleted"}
