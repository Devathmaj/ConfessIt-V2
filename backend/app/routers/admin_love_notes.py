# app/routers/admin_love_notes.py

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from ..postgres import Database

from ..dependencies import get_db
from ..accounts import AccountRef, Capability
from ..models import AccountDetails
from ..services.authorization import require_capability
from ..services.love_note_service import (
    get_all_love_notes_for_admin,
    update_love_note_status_service,
    delete_love_note_service,
    get_love_note_reports_for_admin,
    update_love_note_report_status_service,
)

router = APIRouter(prefix="/admin/love-notes", tags=["Admin - Love Notes"])


@router.get("", response_model=List[Dict[str, Any]])
async def get_all_love_notes(
    db: Database = Depends(get_db),
    admin: AccountDetails = Depends(require_capability(Capability.LOVE_NOTES_REVIEW))
) -> List[Dict[str, Any]]:
    """Return every love note with full sender and recipient details for admin review."""
    return await get_all_love_notes_for_admin(db, AccountRef.of(admin))


@router.put("/{note_id}/status")
async def update_love_note_status(
    note_id: str,
    status_value: str = Query(..., alias="status", regex="^(approved|rejected|pending_review)$"),
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.LOVE_NOTES_MODERATE))
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
    _: AccountDetails = Depends(require_capability(Capability.LOVE_NOTES_DELETE))
) -> Dict[str, Any]:
    """Delete a love note from the system permanently."""
    return await delete_love_note_service(note_id, db)


# The report queue carries the reporting capabilities rather than the love-note ones, so that
# reading reports and ruling on them stay separately grantable from approving a note. Nothing
# here shadows a handler above: FastAPI matches on method first, and neither ``GET
# /admin/love-notes/{note_id}`` nor any other literal-reading handler exists to catch these.
# They are still declared last so that adding a ``GET /{note_id}`` later cannot silently
# swallow ``/reports`` as a note id.


@router.get("/reports")
async def get_love_note_reports(
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status: pending, reviewed, dismissed"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Database = Depends(get_db),
    admin: AccountDetails = Depends(require_capability(Capability.REPORTS_REVIEW))
) -> Dict[str, Any]:
    """Every love note report, newest first, with the reported note and its sender."""
    return await get_love_note_reports_for_admin(
        db,
        AccountRef.of(admin),
        status_filter=status_filter,
        skip=skip,
        limit=limit,
    )


@router.patch("/reports/{report_id}")
async def update_love_note_report_status(
    report_id: str,
    status_value: str = Query(..., alias="status", regex="^(reviewed|dismissed)$"),
    db: Database = Depends(get_db),
    reviewer: AccountDetails = Depends(require_capability(Capability.REPORTS_MODERATE))
) -> Dict[str, Any]:
    """
    Close a love note report out as ``reviewed`` or ``dismissed``.

    Deliberately does not touch the note. Approving, rejecting or deleting the note stays
    with the love-note endpoints, so the report queue records that a human looked and the
    note list remains the single place a note's state is decided.
    """
    return await update_love_note_report_status_service(
        report_id, status_value, AccountRef.of(reviewer), db
    )
