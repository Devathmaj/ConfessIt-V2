"""Purge expired chat messages and moderation evidence on the daily schedule.

Read and send paths enforce logical expiry; this service performs physical deletion using
stored deadlines. Reports and separately retained evidence survive message deletion.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, select, update

from ..config import settings
from ..db_models import (
    CaseClosedMessageRecord,
    ChatMessageEvidenceRecord,
    ConversationRecord,
    MessageRecord,
)
from ..logger import get_logger
from ..postgres import Database

logger = get_logger(__name__)


def _now(now: Optional[datetime] = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _delete_matchmaking_batch(session, now: datetime, batch_size: int) -> int:
    """Delete one bounded batch of expired matchmaking messages.

    A conversation preview is a copy of one specific message: when that message
    is purged, its preview must go too or an encrypted excerpt would outlive
    the retention rule it was copied under. Update and delete share this
    transaction, so a failure rolls both back.
    """
    ids = session.scalars(
        select(MessageRecord.id)
        .where(MessageRecord.delete_after <= now)
        .order_by(MessageRecord.delete_after.asc(), MessageRecord.id.asc())
        .limit(batch_size)
    ).all()
    if not ids:
        return 0
    session.execute(
        update(ConversationRecord)
        .where(ConversationRecord.last_message_preview_message_id.in_([str(value) for value in ids]))
        .values(
            last_message_preview_ciphertext=None,
            last_message_preview_nonce=None,
            last_message_preview_key_version=None,
            last_message_preview_message_id=None,
            last_message_preview_sender_regno=None,
            last_message_preview_delete_after=None,
        )
    )
    result = session.execute(delete(MessageRecord).where(MessageRecord.id.in_(ids)))
    count = result.rowcount
    return count if count is not None and count >= 0 else len(ids)


def _delete_case_closed_batch(session, now: datetime, batch_size: int) -> int:
    ids = session.scalars(
        select(CaseClosedMessageRecord.id)
        .where(CaseClosedMessageRecord.delete_after <= now)
        .order_by(CaseClosedMessageRecord.delete_after.asc(), CaseClosedMessageRecord.id.asc())
        .limit(batch_size)
    ).all()
    if not ids:
        return 0
    result = session.execute(
        delete(CaseClosedMessageRecord).where(CaseClosedMessageRecord.id.in_(ids))
    )
    count = result.rowcount
    return count if count is not None and count >= 0 else len(ids)


def _delete_evidence_batch(session, now: datetime, batch_size: int) -> int:
    ids = session.scalars(
        select(ChatMessageEvidenceRecord.id)
        .where(ChatMessageEvidenceRecord.expires_at <= now)
        .order_by(ChatMessageEvidenceRecord.expires_at.asc(), ChatMessageEvidenceRecord.id.asc())
        .limit(batch_size)
    ).all()
    if not ids:
        return 0
    result = session.execute(
        delete(ChatMessageEvidenceRecord).where(ChatMessageEvidenceRecord.id.in_(ids))
    )
    count = result.rowcount
    return count if count is not None and count >= 0 else len(ids)


def _drain(db: Database, now: datetime, batch_size: int, batch_deleter) -> tuple[int, int]:
    total = 0
    batches = 0
    while True:
        with db.sessions.begin() as session:
            deleted = batch_deleter(session, now, batch_size)
        if not deleted:
            break
        total += deleted
        batches += 1
        if deleted < batch_size:
            break
    return total, batches


def cleanup_expired_chat_messages(
    db: Database,
    *,
    now: Optional[datetime] = None,
    batch_size: Optional[int] = None,
) -> dict:
    """Purge expired ordinary chat rows and expired report evidence.

    Bounded batches keep each transaction short. Idempotent: a rerun finds the
    remaining backlog (or none) and is always safe. Partial failure is surfaced
    to the caller, which logs it and continues the scheduler pass; rows not
    reached stay for the next run.
    """
    moment = _now(now)
    size = max(1, int(batch_size or settings.CHAT_CLEANUP_BATCH_SIZE))

    mm_deleted, mm_batches = _drain(db, moment, size, _delete_matchmaking_batch)
    cc_deleted, cc_batches = _drain(db, moment, size, _delete_case_closed_batch)
    evidence_deleted, evidence_batches = _drain(db, moment, size, _delete_evidence_batch)

    summary = {
        "matchmaking_messages_deleted": mm_deleted,
        "case_closed_messages_deleted": cc_deleted,
        "reported_evidence_deleted": evidence_deleted,
        "batches": mm_batches + cc_batches + evidence_batches,
        "batch_size": size,
        "ran_at": moment.isoformat(),
    }
    logger.info(
        "Chat retention cleanup: matchmaking=%s case_closed=%s evidence=%s batches=%s",
        mm_deleted,
        cc_deleted,
        evidence_deleted,
        summary["batches"],
    )
    return summary
