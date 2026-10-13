"""Separately encrypted moderation evidence for reported chat messages.

Ordinary chat history is purged on its own deadline. A report is a moderation
record with a different lifetime, so the individual message it names is copied
here at filing time, encrypted again under its own AAD, and retained for
``REPORTED_MESSAGE_EVIDENCE_RETENTION_DAYS``. Only that one turn is preserved --
never the surrounding conversation -- and only the authorised report-review
endpoint decrypts it.

The table has no foreign key to the live chat tables: those rows are expected to
be deleted first. Reports themselves point at messages with ``ON DELETE SET
NULL``, so they survive the purge and still identify the evidence by report id.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import select

from ..config import settings
from ..db_models import ChatEvidenceAccessRecord, ChatMessageEvidenceRecord
from ..logger import get_logger
from ..postgres import new_id
from .chat_encryption import ChatDecryptionError
from .chat_message_crypto import decrypt_report_evidence, encrypt_report_evidence

logger = get_logger(__name__)

REPORT_KIND_MATCHMAKING = "matchmaking"
REPORT_KIND_CASE_CLOSED = "case_closed"


def capture_report_evidence(
    session,
    *,
    report_id: str,
    report_kind: str,
    message_id: str,
    chat_id: str,
    sender_ref: str,
    plaintext: str,
    message_created_at: datetime,
    reported_at: Optional[datetime] = None,
) -> ChatMessageEvidenceRecord:
    """Encrypt and queue one evidence row inside the caller's transaction.

    Called immediately after the report row is flushed, so a report can never
    commit without its evidence and evidence can never outlive a rolled-back
    report. Re-capturing the same report is a no-op (the automated path checks
    for an existing report first, and this unique report id is the backstop).
    """
    existing = session.scalar(
        select(ChatMessageEvidenceRecord).where(
            ChatMessageEvidenceRecord.report_id == str(report_id)
        )
    )
    if existing is not None:
        return existing

    reported = reported_at or datetime.now(timezone.utc)
    evidence_id = new_id()
    payload = encrypt_report_evidence(
        evidence_id=evidence_id,
        report_id=str(report_id),
        report_kind=report_kind,
        message_id=str(message_id),
        chat_id=str(chat_id),
        sender_ref=str(sender_ref),
        plaintext=plaintext or "",
    )
    row = ChatMessageEvidenceRecord(
        id=evidence_id,
        report_id=str(report_id),
        report_kind=report_kind,
        message_id=str(message_id),
        chat_id=str(chat_id),
        sender_ref=str(sender_ref),
        body_ciphertext=payload.ciphertext,
        body_nonce=payload.nonce,
        body_key_version=payload.key_version,
        message_created_at=message_created_at,
        reported_at=reported,
        expires_at=reported
        + timedelta(days=settings.REPORTED_MESSAGE_EVIDENCE_RETENTION_DAYS),
    )
    session.add(row)
    session.flush()
    return row


def evidence_for_reports(
    session,
    report_ids: Iterable[str],
    *,
    now: Optional[datetime] = None,
) -> dict[str, ChatMessageEvidenceRecord]:
    """Bulk-load unexpired evidence rows keyed by report id (one query, not one per row).

    ``expires_at`` is a logical deadline, not merely the next cleanup's to-do
    list: evidence whose retention window has passed is withheld immediately,
    even if the daily purge has not physically removed the row yet.
    """
    ids = {str(value) for value in report_ids if value}
    if not ids:
        return {}
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    rows = session.scalars(
        select(ChatMessageEvidenceRecord).where(
            ChatMessageEvidenceRecord.report_id.in_(ids),
            ChatMessageEvidenceRecord.expires_at > moment,
        )
    ).all()
    return {str(row.report_id): row for row in rows}


def decrypt_evidence(record: ChatMessageEvidenceRecord, *, report_id: str) -> Optional[str]:
    """Decrypt one evidence row, returning None and logging a sanitized error on failure."""
    try:
        return decrypt_report_evidence(record)
    except ChatDecryptionError as exc:
        logger.error(
            "Report evidence decryption failed for report %s (%s); withholding content",
            report_id,
            exc.code,
        )
        return None


def audit_evidence_access(
    db,
    *,
    reviewer_regno: str,
    report_kind_by_id: dict[str, str],
    evidence_by_report: dict[str, ChatMessageEvidenceRecord],
) -> None:
    """Persist one audit row per report whose evidence content was returned.

    A failure here fails the read: if access cannot be recorded, the private
    content is not handed out. Only ids and the reviewer are stored.
    """
    if not evidence_by_report:
        return
    now = datetime.now(timezone.utc)
    with db.sessions.begin() as session:
        for report_id, evidence in evidence_by_report.items():
            session.add(
                ChatEvidenceAccessRecord(
                    report_id=str(report_id),
                    report_kind=report_kind_by_id.get(str(report_id), evidence.report_kind),
                    evidence_id=str(evidence.id),
                    accessed_by_regno=reviewer_regno,
                    accessed_at=now,
                    action="report_queue_review",
                )
            )


__all__: list[str] = [
    "REPORT_KIND_CASE_CLOSED",
    "REPORT_KIND_MATCHMAKING",
    "audit_evidence_access",
    "capture_report_evidence",
    "decrypt_evidence",
    "evidence_for_reports",
]
