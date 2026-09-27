# app/routers/admin_love_notes.py

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, Response
from ..postgres import Database

from ..dependencies import get_db
from ..accounts import AccountRef, Capability
from ..models import AccountDetails
from ..services.authorization import require_capability
from ..services.love_note_service import (
    LOVE_NOTES_MAX_LIMIT,
    get_admin_love_notes_data,
    update_love_note_status_service,
    delete_love_note_service,
    get_love_note_reports_for_admin,
)
from ..services.report_resolution_service import resolve_love_note_report

router = APIRouter(prefix="/admin/love-notes", tags=["Admin - Love Notes"])


@router.get("", response_model=Dict[str, Any])
async def get_all_love_notes(
    skip: int = Query(0, ge=0, description="How many matching notes to skip."),
    limit: int = Query(50, ge=1, le=LOVE_NOTES_MAX_LIMIT, description="How many matching notes to return. Clamped so one request can never materialise the whole table."),
    status: str = Query("all", description="Narrow to one moderation state: all, pending_review, approved or rejected."),
    order: str = Query("newest", description="Timeline order by arrival: newest or oldest first."),
    db: Database = Depends(get_db),
    admin: AccountDetails = Depends(require_capability(Capability.LOVE_NOTES_REVIEW))
) -> Dict[str, Any]:
    """
    Return one page of love notes for moderation as text only.

    No sender, no recipient, no image, no anonymity flag: the reviewer judges the words. The
    operational fields the screen needs to act -- id, status, arrival time, and whether the
    note was already taken down -- come with it.

    The status filter is a SQL WHERE, the timeline order a SQL ORDER BY, and every page a
    SQL LIMIT/OFFSET: the backend reads at most one page no matter how many notes exist,
    and ``total`` is a COUNT under the same filter so the screen can load more downwards
    without holding the notes it is not showing.
    """
    return await get_admin_love_notes_data(db, skip=skip, limit=limit, status=status, order=order)


@router.put("/{note_id}/status")
async def update_love_note_status(
    note_id: str,
    status_value: str = Query(..., alias="status", pattern="^(approved|rejected|pending_review)$"),
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
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status: pending, dismissed (or the older reviewed), removed"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Database = Depends(get_db),
    admin: AccountDetails = Depends(require_capability(Capability.REPORTS_REVIEW)),
    response: Response = None
) -> Dict[str, Any]:
    """Every love note report, newest first, with the reported note and its sender."""
    # The reported note's signed URL does not expire; keep it out of every cache that would
    # outlive the moderation session that requested it.
    response.headers["Cache-Control"] = "no-store"
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
    status_value: str = Query(..., alias="status", pattern="^(reviewed|dismissed|removed)$"),
    db: Database = Depends(get_db),
    reviewer: AccountDetails = Depends(require_capability(Capability.REPORTS_MODERATE))
) -> Dict[str, Any]:
    """
    Rule on a love note report: ``dismissed`` or ``removed``.

    ``reviewed`` is the older spelling of ``dismissed`` and is still accepted; the screen only
    offers the two new words. See ``admin.update_confession_report`` for why.

    ``dismissed`` leaves the note exactly as it is and tells the reporter it was reviewed.
    ``removed`` marks the note removed so it leaves the recipient's inbox and has its image
    destroyed at the store, and tells both the sender and the reporter.

    The note's own ``status`` is untouched either way. Approving, rejecting or deleting a note
    stays with the love-note endpoints above, so ``status`` keeps meaning "judged as a love
    note" and ``removed_at`` keeps meaning "taken down for a report" -- two questions, two
    columns, and no screen left guessing which one it is looking at.
    """
    return resolve_love_note_report(db, report_id, status_value, AccountRef.of(reviewer))
