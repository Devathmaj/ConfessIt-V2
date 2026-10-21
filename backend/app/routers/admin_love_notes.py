# app/routers/admin_love_notes.py

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, Response
from ..postgres import Database
from ..limits import PAGE_OFFSET_MAX

from ..dependencies import get_db
from ..accounts import AccountRef, Capability
from ..models import AccountDetails
from ..services.authorization import require_capability
from ..services.love_note_service import (
    LOVE_NOTES_MAX_LIMIT,
    get_admin_love_notes_data,
    regenerate_love_note_moderation_body,
    update_love_note_status_service,
    delete_love_note_service,
    get_love_note_reports_for_admin,
)
from ..services.pii_detection_service import PiiDetectionError, pii_error_to_http
from ..services.report_resolution_service import resolve_love_note_report

router = APIRouter(prefix="/admin/love-notes", tags=["Admin - Love Notes"])


@router.get("", response_model=Dict[str, Any])
def get_all_love_notes(
    skip: int = Query(0, ge=0, le=PAGE_OFFSET_MAX, description="How many matching notes to skip."),
    limit: int = Query(50, ge=1, le=LOVE_NOTES_MAX_LIMIT, description="How many matching notes to return. Clamped so one request can never materialise the whole table."),
    status: str = Query("all", description="Narrow to one moderation state: all, pending_review, approved or rejected."),
    order: str = Query("newest", description="Timeline order by arrival: newest or oldest first."),
    db: Database = Depends(get_db),
    admin: AccountDetails = Depends(require_capability(Capability.LOVE_NOTES_REVIEW))
) -> Dict[str, Any]:
    """
    Return one page of love notes for moderation as text only.

    No sender, no recipient, no image, no anonymity flag: the reviewer judges the words.
    The operational fields the screen needs to act -- id, status, arrival time and
    whether the note was already taken down -- come with it. The status filter is a SQL
    WHERE, the order a SQL ORDER BY, every page a SQL LIMIT/OFFSET; ``total`` is a COUNT
    under the same filter so the screen loads downwards without holding the notes it is
    not showing.
    """
    return get_admin_love_notes_data(db, skip=skip, limit=limit, status=status, order=order)


@router.put("/{note_id}/status")
def update_love_note_status(
    note_id: str,
    status_value: str = Query(..., alias="status", pattern="^(approved|rejected|pending_review)$"),
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.LOVE_NOTES_MODERATE))
) -> Dict[str, Any]:
    """
    Approve or reject a love note, and notify accordingly: approved notifies both
    sender and recipient; rejected only the sender.
    """
    return update_love_note_status_service(note_id, status_value, db)


@router.delete("/{note_id}")
def delete_love_note(
    note_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.LOVE_NOTES_DELETE))
) -> Dict[str, Any]:
    """Delete a love note from the system permanently."""
    return delete_love_note_service(note_id, db)


@router.post("/{note_id}/redact")
def regenerate_love_note_redaction(
    note_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.LOVE_NOTES_REVIEW))
) -> Dict[str, Any]:
    """
    Re-run PII redaction for one note whose moderation preview is missing.

    The preview is the only rendering of the note the moderation screens ever carry, and
    a note was still accepted when the provider was unavailable at send time (the
    sender's note is not held hostage to a preview). The response carries the redacted
    preview and the note id -- never the original body. A note that already has a preview
    is returned unchanged without a provider call; provider or configuration failures
    answer 503 with a generic message, so there is no response shape in which the raw
    body could escape.
    """
    try:
        return regenerate_love_note_moderation_body(note_id, db)
    except PiiDetectionError as exc:
        raise pii_error_to_http(exc)


# The report queue carries the reporting capabilities rather than the love-note ones, so
# reading reports and ruling on them stay separately grantable from approving a note.
# Nothing here shadows a handler above (FastAPI matches on method first, and no literal
# "/{note_id}" reader exists), but they are still declared last so that adding a
# ``GET /{note_id}`` later cannot silently swallow ``/reports`` as a note id.


@router.get("/reports")
def get_love_note_reports(
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by status: pending, dismissed (or the older reviewed), removed"),
    skip: int = Query(0, ge=0, le=PAGE_OFFSET_MAX),
    limit: int = Query(50, ge=1, le=200),
    q: Optional[str] = Query(None, max_length=100, description="Case-insensitive substring match on the reason, the note's redacted moderation preview, the reporter regno and the sender name/regno."),
    db: Database = Depends(get_db),
    admin: AccountDetails = Depends(require_capability(Capability.REPORTS_REVIEW)),
    response: Response = None
) -> Dict[str, Any]:
    """Every love note report, newest first, with the reported note and its sender."""
    # The reported note's signed URL does not expire; keep it out of every cache that would
    # outlive the moderation session that requested it, and out of every referrer that would
    # carry it sideways into a third party's logs.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return get_love_note_reports_for_admin(
        db,
        AccountRef.of(admin),
        status_filter=status_filter,
        skip=skip,
        limit=limit,
        query=q,
    )


@router.patch("/reports/{report_id}")
def update_love_note_report_status(
    report_id: str,
    status_value: str = Query(..., alias="status", pattern="^(reviewed|dismissed|removed)$"),
    db: Database = Depends(get_db),
    reviewer: AccountDetails = Depends(require_capability(Capability.REPORTS_MODERATE))
) -> Dict[str, Any]:
    """
    Rule on a love note report: ``dismissed`` or ``removed``.

    ``reviewed`` is the older spelling of ``dismissed`` and is still accepted; the screen
    only offers the two new words (see ``admin.update_confession_report`` for why).
    ``dismissed`` leaves the note exactly as it is and tells the reporter it was reviewed;
    ``removed`` marks the note removed so it leaves the recipient's inbox, has its image
    destroyed at the store, and tells both sender and reporter. Either way the note's own
    ``status`` is untouched -- approving, rejecting or deleting stays with the love-note
    endpoints above -- so ``status`` keeps meaning "judged as a love note" and
    ``removed_at`` keeps meaning "taken down for a report": two questions, two columns.
    """
    return resolve_love_note_report(db, report_id, status_value, AccountRef.of(reviewer))
