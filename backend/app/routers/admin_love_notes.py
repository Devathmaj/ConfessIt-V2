# app/routers/admin_love_notes.py

from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query
from pymongo.database import Database

from ..dependencies import get_db
from ..models import UserDetails
from ..services.auth_service import get_current_user
from ..services.love_note_service import (
    get_all_love_notes_for_admin,
    update_love_note_status_service,
    delete_love_note_service,
)

router = APIRouter(prefix="/admin/love-notes", tags=["Admin - Love Notes"])


def _require_admin(current_user: UserDetails = Depends(get_current_user)) -> UserDetails:
    """Ensure the requester has administrator privileges."""
    from fastapi import HTTPException, status
    
    if current_user.user_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required."
        )
    return current_user


@router.get("", response_model=List[Dict[str, Any]])
async def get_all_love_notes(
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> List[Dict[str, Any]]:
    """Return every love note with full sender and recipient details for admin review."""
    return await get_all_love_notes_for_admin(db)


@router.put("/{note_id}/status")
async def update_love_note_status(
    note_id: str,
    status_value: str = Query(..., alias="status", regex="^(approved|rejected|pending_review)$"),
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> Dict[str, Any]:
    """
    Approve or reject a love note.
    
    This endpoint:
    - Updates the love note status
    - Sends notifications to sender and/or recipient based on the status
    - For approved: Notifies both sender and recipient
    - For rejected: Notifies only the sender
    """
    return await update_love_note_status_service(note_id, status_value, db)


@router.delete("/{note_id}")
async def delete_love_note(
    note_id: str,
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> Dict[str, Any]:
    """Delete a love note from the system permanently."""
    return await delete_love_note_service(note_id, db)
