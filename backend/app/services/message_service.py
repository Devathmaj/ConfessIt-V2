# app/services/message_service.py

from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from ..config import settings
from ..postgres import Database
from ..postgres import new_id, is_valid_id
from ..db_models import (
    ConversationRecord,
    MatchRecord,
    MessageRecord,
    MessageReportRecord,
    UserRecord,
)
from ..logger import get_logger
from ..limits import CONVERSATION_MESSAGE_MAX, CONVERSATION_PARTICIPANT_MESSAGE_MAX
from ..utils import isoformat_datetime
from fastapi import HTTPException, status

from ..models import UserDetails
from .redis_service import redis_service
from .auth_service import ACCOUNT_UNAVAILABLE_DETAIL
from .chat_encryption import ChatDecryptionError, ChatEncryptionError
from .chat_evidence_service import (
    REPORT_KIND_MATCHMAKING,
    capture_report_evidence,
)
from .chat_message_crypto import (
    decrypt_matchmaking_message,
    decrypt_matchmaking_preview,
    encrypt_matchmaking_message,
    encrypt_matchmaking_preview,
)
from .report_counters import MESSAGE, record_report_filed, record_removal

logger = get_logger(__name__)

# Readable transcripts: live (accepted) and moderator-closed (terminated) only.
READABLE_CONVERSATION_STATUSES = frozenset({"accepted", "terminated"})

# Only an accepted conversation accepts new messages; terminated is read-only.
WRITABLE_CONVERSATION_STATUS = "accepted"

# Which cap stopped a send: a participant's own cap vs the conversation's ceiling for both.
PARTICIPANT_LIMIT_REACHED = "participant"
CONVERSATION_LIMIT_REACHED = "conversation"

# Built from the limits so the user-facing text cannot drift from the enforced number.
MESSAGE_LIMIT_PARTICIPANT_DETAIL = (
    f"You have sent all {CONVERSATION_PARTICIPANT_MESSAGE_MAX} messages you can send "
    f"in this conversation"
)
MESSAGE_LIMIT_CONVERSATION_DETAIL = (
    f"This conversation has reached its limit of {CONVERSATION_MESSAGE_MAX} messages"
)


class MessageService:
    """Service for handling message operations"""
    
    def validate_conversation_participant(
        self,
        db: Database,
        conversation_id: str,
        user_regno: str
    ) -> Dict[str, Any]:
        """Return the conversation if ``user_regno`` is a participant; raise HTTPException otherwise."""
        try:
            with db.sessions() as session:
                row = session.get(ConversationRecord, str(conversation_id))
                conversation = self._conversation_payload(row) if row else None
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid conversation ID"
            )
        
        if not conversation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found"
            )
        
        is_participant = (
            conversation.get("initiator_regno") == user_regno or
            conversation.get("receiver_regno") == user_regno
        )
        
        if not is_participant:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not a participant in this conversation"
            )
        
        # ``terminated`` is readable too: a closed thread still holds its written history; writing is refused in send_message().
        if conversation.get("status") not in READABLE_CONVERSATION_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Conversation must be accepted before sending messages"
            )
        
        # Block checks are send-only; reading stays allowed even when blocked (see send_message()).

        # Expiry is send-only too so get_messages() can still return history; enforced in send_message().
        
        return conversation

    @staticmethod
    def _conversation_payload(row):
        # Preview is encrypted content: stop decrypting once its retention deadline passes.
        preview = None
        delete_after = getattr(row, "last_message_preview_delete_after", None)
        if delete_after is not None and delete_after.tzinfo is None:
            delete_after = delete_after.replace(tzinfo=timezone.utc)
        if delete_after is not None and delete_after > datetime.now(timezone.utc):
            try:
                preview = decrypt_matchmaking_preview(row)
            except ChatDecryptionError as exc:
                logger.error(
                    "Conversation preview decryption failed for conversation %s (%s); "
                    "omitting preview",
                    row.id,
                    exc.code,
                )
        return {
            "id": str(row.id), "match_id": str(row.match_id), "initiator_regno": row.initiator_regno,
            "receiver_regno": row.receiver_regno, "status": row.status, "requested_at": row.requested_at,
            "accepted_at": row.accepted_at, "created_at": row.created_at,
            "last_message_at": row.last_message_at, "last_message_preview": preview,
            # One closer, not a list: only the participant who closed may reopen.
            "is_blocked": bool(row.blocked_by),
            "blocked_by": row.blocked_by[0] if row.blocked_by else None,
            "terminated_at": row.terminated_at,
        }

    def is_partner_blocked(self, db: Database, conversation: Dict[str, Any], regno: str) -> bool:
        """Whether the other participant's account is blocked; read per call, never cached, since the conversation row carries no account state."""
        other_regno = (
            conversation["receiver_regno"]
            if conversation["initiator_regno"] == regno
            else conversation["initiator_regno"]
        )
        with db.sessions() as session:
            return bool(
                session.scalar(select(UserRecord.is_blocked).where(UserRecord.regno == other_regno))
            )
    
    def _message_usage(self, session, conversation_id: str, regno: str) -> Dict[str, Any]:
        """How much of the message allowance is spent, on an open session; removed and purge-pending rows still count toward the ceiling."""
        total = session.scalar(
            select(func.count())
            .select_from(MessageRecord)
            .where(MessageRecord.conversation_id == str(conversation_id))
        ) or 0
        from_this_participant = session.scalar(
            select(func.count())
            .select_from(MessageRecord)
            .where(
                MessageRecord.conversation_id == str(conversation_id),
                MessageRecord.sender_regno == regno,
            )
        ) or 0
        return {"conversation_count": int(total), "participant_count": int(from_this_participant)}

    @staticmethod
    def _quota(usage: Dict[str, Any], *, messages_just_sent: int = 0) -> Dict[str, Any]:
        """Usage figures plus derived refusals, computed once so every surface agrees."""
        conversation_count = usage["conversation_count"] + messages_just_sent
        participant_count = usage["participant_count"] + messages_just_sent
        conversation_remaining = max(0, CONVERSATION_MESSAGE_MAX - conversation_count)
        participant_remaining = max(0, CONVERSATION_PARTICIPANT_MESSAGE_MAX - participant_count)

        # Participant allowance first: it binds in normal use and reads as the clearer refusal.
        if participant_remaining == 0:
            limit_reached = PARTICIPANT_LIMIT_REACHED
        elif conversation_remaining == 0:
            limit_reached = CONVERSATION_LIMIT_REACHED
        else:
            limit_reached = None

        return {
            "conversation_limit": CONVERSATION_MESSAGE_MAX,
            "participant_limit": CONVERSATION_PARTICIPANT_MESSAGE_MAX,
            "conversation_count": conversation_count,
            "participant_count": participant_count,
            "conversation_remaining": conversation_remaining,
            "participant_remaining": participant_remaining,
            "can_send": limit_reached is None,
            "limit_reached": limit_reached,
        }

    @staticmethod
    def _quota_refusal(quota: Dict[str, Any]) -> Optional[HTTPException]:
        """The refusal a send has to make, or ``None`` when there is room for it."""
        if quota["limit_reached"] == PARTICIPANT_LIMIT_REACHED:
            return HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=MESSAGE_LIMIT_PARTICIPANT_DETAIL,
            )
        if quota["limit_reached"] == CONVERSATION_LIMIT_REACHED:
            return HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=MESSAGE_LIMIT_CONVERSATION_DETAIL,
            )
        return None

    def conversation_quota(self, db: Database, conversation_id: str, regno: str) -> Dict[str, Any]:
        """The allowance as it stands for one participant; read-only and independent of status."""
        with db.sessions() as session:
            usage = self._message_usage(session, conversation_id, regno)
        return self._quota(usage)

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def _match_expiry(self, db: Database, match_id: str) -> datetime:
        """The match's UTC deadline from ``matches.expires_at``, the single authoritative expiry."""
        with db.sessions() as session:
            row = session.get(MatchRecord, str(match_id))
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found",
            )
        return self._aware(row.expires_at)

    def send_message(
        self,
        db: Database,
        conversation_id: str,
        sender: UserDetails,
        body: str
    ) -> Dict[str, Any]:
        """Send a message after enforcing participant, writable, unblocked, unexpired and quota."""
        # Validate sender is participant (reading allowed even when blocked)
        conversation = self.validate_conversation_participant(
            db, conversation_id, sender.regno
        )

        # Terminated is readable but not writable; checked here, not in the validator shared with the read path.
        if conversation.get("status") == "terminated":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This conversation has been terminated"
            )
        if conversation.get("status") != WRITABLE_CONVERSATION_STATUS:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Conversation must be accepted before sending messages"
            )

        # IMPORTANT: blocked conversations refuse sends, but reading stays allowed.
        if conversation.get("is_blocked", False):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot send messages: this conversation is blocked"
            )

        # Expiry is send-only, on the match's own ``expires_at``, so expired threads stay readable.
        match_expires_at = self._match_expiry(db, conversation["match_id"])
        if datetime.now(timezone.utc) >= match_expires_at:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Conversation has expired"
            )
        receiver_id = (
            conversation["receiver_regno"] 
            if conversation["initiator_regno"] == sender.regno 
            else conversation["initiator_regno"]
        )
        
        # The block is re-read from the account; the conversation row does not track it.
        if self.is_partner_blocked(db, conversation, sender.regno):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=ACCOUNT_UNAVAILABLE_DETAIL
            )

        # Moderation runs before persistence: a flagged message is born hidden, a provider
        # failure aborts before any row exists, and bodies/secrets are never logged.
        from .content_safety_service import (
            AUTOMATED_REMOVED_BY,
            AUTOMATED_REPORTER_DISPLAY_NAME,
            AUTOMATED_SOURCE,
            CONTENT_CHAT_MESSAGE,
            ContentSafetyError,
            moderate_content,
            moderation_error_to_http,
        )

        try:
            decision = moderate_content(CONTENT_CHAT_MESSAGE, body or "")
        except ContentSafetyError as exc:
            logger.error(
                "Content Safety moderation failed (%s); aborting chat send", exc.code
            )
            raise moderation_error_to_http(exc, content_type=CONTENT_CHAT_MESSAGE) from exc
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "Content Safety moderation failed unexpectedly: %s",
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Content moderation is temporarily unavailable. Please retry shortly.",
            ) from exc

        # Plaintext lives only in this local variable; the row stores the AES-256-GCM payload.
        message_doc = {
            "conversation_id": str(conversation_id),
            "sender_regno": sender.regno,
            "receiver_regno": receiver_id,
            "created_at": datetime.now(timezone.utc),
            "is_read": False
        }
        
        # The row is locked FOR UPDATE before counts are read: two racing sends could
        # otherwise both see 99 and both insert (per-conversation lock, no cross-thread
        # contention). A flagged send's hidden row, report and evidence land in this same
        # transaction; bookkeeping failure rolls the whole send back.
        automated_report_id: Optional[str] = None
        try:
            with db.sessions.begin() as session:
                conversation_row = session.scalar(
                    select(ConversationRecord)
                    .where(ConversationRecord.id == str(conversation_id))
                    .with_for_update()
                )
                if conversation_row and conversation_row.blocked_by:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Cannot send messages: this conversation is blocked"
                    )

                # Re-read the match under its own lock so a send racing expiry cannot commit into an expired chat.
                match_row = session.scalar(
                    select(MatchRecord)
                    .where(MatchRecord.id == str(conversation["match_id"]))
                    .with_for_update()
                )
                if match_row is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Conversation not found",
                    )
                match_expiry = self._aware(match_row.expires_at)
                if message_doc["created_at"] >= match_expiry:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Conversation has expired"
                    )
                delete_after = match_expiry + timedelta(
                    hours=settings.MATCHMAKING_CHAT_RETENTION_HOURS
                )

                quota = self._quota(self._message_usage(session, conversation_id, sender.regno))
                refusal = self._quota_refusal(quota)
                if refusal is not None:
                    raise refusal

                # Allocate the id before encrypting: it is the AAD binding ciphertext to this row.
                message_id = new_id()
                encrypted = encrypt_matchmaking_message(
                    message_id=message_id,
                    conversation_id=str(conversation_id),
                    sender_regno=sender.regno,
                    plaintext=body or "",
                )
                row = MessageRecord(
                    id=message_id,
                    conversation_id=str(conversation_id), sender_regno=sender.regno,
                    receiver_regno=receiver_id,
                    body_ciphertext=encrypted.ciphertext,
                    body_nonce=encrypted.nonce,
                    body_key_version=encrypted.key_version,
                    created_at=message_doc["created_at"],
                    delete_after=delete_after,
                    is_read=False,
                )
                if decision.should_hide:
                    # Born hidden via the same removed_at write a moderator uses; never published.
                    row.removed_at = message_doc["created_at"]
                    row.removed_by_regno = AUTOMATED_REMOVED_BY
                session.add(row)
                if conversation_row and not decision.should_hide:
                    # Only visible messages become the preview, which carries the retention deadline.
                    preview = encrypt_matchmaking_preview(
                        conversation_id=str(conversation_id),
                        message_id=message_id,
                        sender_regno=sender.regno,
                        plaintext=(body or "")[:100],
                    )
                    conversation_row.last_message_at = message_doc["created_at"]
                    conversation_row.last_message_preview_ciphertext = preview.ciphertext
                    conversation_row.last_message_preview_nonce = preview.nonce
                    conversation_row.last_message_preview_key_version = preview.key_version
                    conversation_row.last_message_preview_message_id = message_id
                    conversation_row.last_message_preview_sender_regno = sender.regno
                    conversation_row.last_message_preview_delete_after = delete_after
                session.flush()
                message_doc["id"] = message_id
                if decision.should_report:
                    # Idempotent: the check makes retries safe and the partial unique index backstops races.
                    existing = session.scalar(
                        select(MessageReportRecord.id).where(
                            MessageReportRecord.message_id == str(message_doc["id"]),
                            MessageReportRecord.source == AUTOMATED_SOURCE,
                        ).limit(1)
                    )
                    if existing is None:
                        report_row = MessageReportRecord(
                            message_id=str(message_doc["id"]),
                            reporter_regno=None,
                            reported_user_regno=sender.regno,
                            reason=decision.report_reason,
                            reported_at=message_doc["created_at"],
                            source=AUTOMATED_SOURCE,
                            status="removed",
                            reviewed_at=message_doc["created_at"],
                            reviewed_by_regno=AUTOMATED_REMOVED_BY,
                        )
                        session.add(report_row)
                        session.flush()
                        automated_report_id = str(report_row.id)
                        # Preserved for review under the evidence retention rule, separate from chat history.
                        capture_report_evidence(
                            session,
                            report_id=automated_report_id,
                            report_kind=REPORT_KIND_MATCHMAKING,
                            message_id=str(message_doc["id"]),
                            chat_id=str(conversation_id),
                            sender_ref=sender.regno,
                            plaintext=body or "",
                            message_created_at=message_doc["created_at"],
                            reported_at=message_doc["created_at"],
                        )
                        record_report_filed(
                            session, kind=MESSAGE, author_regno=sender.regno,
                        )
                        if decision.should_hide:
                            record_removal(
                                session, kind=MESSAGE, author_regno=sender.regno,
                            )
                    else:
                        automated_report_id = str(existing)
                # Reported after the insert rather than recounted, so the response describes this send's end state.
                quota = self._quota(quota, messages_just_sent=1)
        except HTTPException:
            raise
        except ChatEncryptionError as exc:
            # Encryption is configured at startup, so this is operational; never fall back to plaintext.
            logger.error(
                "Chat encryption failed for a new message (%s); send aborted", exc.code
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not securely store this message.",
            ) from exc
        except Exception as exc:
            # Moderation succeeded but bookkeeping rolled back; surface a retryable error, never a silent success.
            logger.error(
                "Automated moderation bookkeeping failed for new message: %s",
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not complete moderation for this message.",
            ) from exc

        # Best-effort notices: their failure must never unwind the committed report/hidden state.
        if automated_report_id is not None:
            try:
                from .staff_notification_service import notify_report_filed

                notify_report_filed(
                    db, kind="chat message", report_id=automated_report_id,
                    summary=(
                        "A chat message was automatically reported by "
                        "Azure Content Safety. Open Reports review to rule on it."
                    ),
                )
            except Exception:
                logger.warning(
                    "Failed to queue staff notification for automated message report %s",
                    automated_report_id,
                )
            if decision.should_hide:
                try:
                    # Neutral copy: no policy is cited because no human ruled on a report.
                    from .notification_service import (
                        create_notification_service as _notify,
                    )

                    _notify(
                        recipient_regno=sender.regno,
                        heading="Message removed",
                        body="We removed your chat message from ConfessIt.",
                        db=db,
                    )
                except Exception:
                    logger.warning(
                        "Failed to queue removal notice for message %s",
                        message_doc.get("id"),
                    )

        if decision.should_hide:
            # The row stays hidden for audit and is never published; the 422 drives the violation popup.
            logger.info(
                "Chat message %s withheld by content safety for %s -> %s "
                "(conversation %s, max=%s action=%s)",
                message_doc["id"], sender.regno, receiver_id, conversation_id,
                decision.max_severity, decision.action,
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Your chat message was removed because it violated our content policies.",
            )

        # Publish to Redis for real-time delivery (visible messages only).
        redis_message = {
            "id": str(message_doc["id"]),
            "conversation_id": conversation_id,
            "sender_regno": sender.regno,
            "sender_name": sender.name,
            "receiver_regno": receiver_id,
            "body": body,
            "created_at": isoformat_datetime(message_doc["created_at"]),
            "is_read": False
        }
        
        published = redis_service.publish_message(conversation_id, redis_message)
        if not published:
            # Persistence committed, so this is a realtime outage, not data loss; logged loudly.
            logger.error(
                f"Chat realtime outage: message {message_doc['id']} persisted "
                f"but Redis publish failed (conversation {conversation_id})"
            )

        logger.info(f"Message sent: {sender.regno} -> {receiver_id} in conversation {conversation_id}")
        
        return {
            "id": str(message_doc["id"]),
            "conversation_id": conversation_id,
            "sender_regno": sender.regno,
            "receiver_regno": receiver_id,
            "body": body,
            "created_at": isoformat_datetime(message_doc["created_at"]),
            "is_read": False,
            # Sent with the message so a client that just used its last allowance learns here.
            "quota": quota,
            # Stored either way; "unavailable" means an open socket needs a re-read to show it.
            "realtime": "delivered" if published else "unavailable",
        }
    
    def get_messages(
        self,
        db: Database,
        conversation_id: str,
        user: UserDetails,
        limit: int = CONVERSATION_MESSAGE_MAX,
        before: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        One thread window, oldest first with the newest last, plus the total.

        ``before`` anchors on a message id rather than an offset: concurrent sends shift offsets and would repeat a row.
        """
        # Participant-only: ``reports:review`` exposes a reported message plus context, not whole transcripts.
        conversation = self.validate_conversation_participant(db, conversation_id, user.regno)

        # A blocked partner makes the thread unreachable for reading as well as sending.
        if self.is_partner_blocked(db, conversation, user.regno):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=ACCOUNT_UNAVAILABLE_DETAIL
            )

        limit = max(1, min(int(limit or CONVERSATION_MESSAGE_MAX), CONVERSATION_MESSAGE_MAX))
        now = datetime.now(timezone.utc)
        # Logical expiry: rows past retention are unreachable even before the daily purge removes them.
        visible = [
            MessageRecord.conversation_id == str(conversation_id),
            MessageRecord.removed_at.is_(None),
            MessageRecord.delete_after > now,
        ]
        with db.sessions() as session:
            anchor = None
            if before is not None:
                anchor = session.get(MessageRecord, str(before))
                if (
                    anchor is None
                    or str(anchor.conversation_id) != str(conversation_id)
                    or self._aware(anchor.delete_after) <= now
                ):
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Message not found.",
                    )
            if anchor is not None:
                # Strictly older than the anchor, ties broken by id, so equal timestamps page cleanly.
                visible.append(or_(
                    MessageRecord.created_at < anchor.created_at,
                    and_(
                        MessageRecord.created_at == anchor.created_at,
                        MessageRecord.id < str(anchor.id),
                    ),
                ))
            total = session.scalar(
                select(func.count()).select_from(MessageRecord).where(
                    MessageRecord.conversation_id == str(conversation_id),
                    MessageRecord.removed_at.is_(None),
                    MessageRecord.delete_after > now,
                )
            ) or 0
            # Slice newest-first, then reverse so the reply reads oldest-first.
            newest_first = session.scalars(
                select(MessageRecord).where(*visible)
                .order_by(MessageRecord.created_at.desc(), MessageRecord.id.desc())
                .limit(limit)
            ).all()
            rows = list(reversed(newest_first))

        messages = []
        for row in rows:
            try:
                plaintext = decrypt_matchmaking_message(row)
            except ChatDecryptionError as exc:
                # Withhold a corrupted row rather than deliver a placeholder; log ids and the sanitized code only.
                logger.error(
                    "Chat message decryption failed for message %s in conversation %s (%s)",
                    row.id,
                    conversation_id,
                    exc.code,
                )
                continue
            messages.append({
                "id": str(row.id), "conversation_id": str(row.conversation_id),
                "sender_regno": row.sender_regno, "receiver_regno": row.receiver_regno,
                "body": plaintext,
                "created_at": row.created_at, "is_read": row.is_read,
            })
        returned_ids = [str(msg["id"]) for msg in messages]

        # Mark read only the returned ids: an older page must not clear newer unloaded messages.
        with db.sessions.begin() as session:
            if returned_ids:
                rows = session.scalars(select(MessageRecord).where(
                    MessageRecord.id.in_(returned_ids),
                    MessageRecord.receiver_regno == user.regno,
                    MessageRecord.is_read.is_(False),
                )).all()
                for row in rows:
                    row.is_read = True

        return {
            "messages": [
                {
                    "id": str(msg["id"]),
                    "conversation_id": conversation_id,
                    "sender_regno": msg["sender_regno"],
                    "receiver_regno": msg["receiver_regno"],
                    "body": msg["body"],
                    "created_at": isoformat_datetime(msg["created_at"]),
                    "is_read": msg.get("is_read", False),
                    "is_sender": msg["sender_regno"] == user.regno
                }
                for msg in messages
            ],
            "total": total,
        }
    
    def report_message(
        self,
        db: Database,
        message_id: str,
        reporter: UserDetails,
        reason: str
    ) -> Dict[str, Any]:
        """Report a message: receiver-only, readable thread, no duplicate reports."""
        # Authorise on metadata only; decrypt when committing so an unauthorised caller never yields plaintext.
        try:
            with db.sessions() as session:
                row = session.get(MessageRecord, str(message_id))
                message = {
                    "id": str(row.id), "conversation_id": str(row.conversation_id),
                    "sender_regno": row.sender_regno, "receiver_regno": row.receiver_regno,
                    "created_at": row.created_at, "is_read": row.is_read,
                    "delete_after": self._aware(row.delete_after),
                } if row else None
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid message ID"
            )
        
        if not message or message["delete_after"] <= datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Message not found"
            )
        
        if message["receiver_regno"] != reporter.regno:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only report messages sent to you"
            )
        
        # Terminated threads qualify: the report menu is offered on exactly the messages the reader can open.
        with db.sessions() as session:
            row = session.get(ConversationRecord, message["conversation_id"])
            conversation = self._conversation_payload(row) if row else None
        
        if not conversation or conversation.get("status") not in READABLE_CONVERSATION_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Can only report messages in an accepted or terminated conversation"
            )
        
        with db.sessions() as session:
            existing_report = session.scalar(select(MessageReportRecord).where(
                MessageReportRecord.message_id == str(message_id),
                MessageReportRecord.reporter_regno == reporter.regno,
            ))
        
        if existing_report:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="You have already reported this message"
            )
        
        report_doc = {
            "message_id": str(message_id),
            "reporter_regno": reporter.regno,
            "reported_user_regno": message["sender_regno"],
            "reason": reason,
            "reported_at": datetime.now(timezone.utc),
            "status": "pending"
        }
        
        try:
            with db.sessions.begin() as session:
                message_row = session.get(MessageRecord, report_doc["message_id"])
                if message_row is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Message not found",
                    )
                plaintext = decrypt_matchmaking_message(message_row)
                row = MessageReportRecord(
                    message_id=report_doc["message_id"], reporter_regno=report_doc["reporter_regno"],
                    reported_user_regno=report_doc["reported_user_regno"], reason=report_doc["reason"],
                    reported_at=report_doc["reported_at"], status=report_doc["status"],
                )
                session.add(row)
                session.flush()
                report_id = str(row.id)
                # Preserve only the reported message so the report stays reviewable after history is purged.
                capture_report_evidence(
                    session,
                    report_id=report_id,
                    report_kind=REPORT_KIND_MATCHMAKING,
                    message_id=report_doc["message_id"],
                    chat_id=message_row.conversation_id,
                    sender_ref=message_row.sender_regno,
                    plaintext=plaintext,
                    message_created_at=message_row.created_at,
                    reported_at=report_doc["reported_at"],
                )
                # Charged to the sender (the receiver filed); keyed by regno, see report_counters.
                record_report_filed(
                    session, kind=MESSAGE, author_regno=report_doc["reported_user_regno"]
                )
        except ChatDecryptionError as exc:
            logger.error(
                "Chat message decryption failed while filing report for message %s (%s)",
                message_id,
                exc.code,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="This message could not be reported.",
            ) from exc
        except IntegrityError as exc:
            # Two concurrent reports both read "no report yet" and both insert: the unique
            # constraint on (message_id, reporter_regno) is the authority, and the loser
            # trips it here, answering 409 like the check above. Only that constraint is
            # translated; other integrity failures stay faults.
            detail = str(exc.orig)
            if "message_id" not in detail or "reporter_regno" not in detail:
                raise
            logger.info(
                "Message %s was already reported by %s (duplicate report refused)",
                message_id,
                reporter.regno,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="You have already reported this message",
            ) from exc
        
        # Free-text reason and message body never reach the application log.
        logger.info("Message %s reported by %s", message_id, reporter.regno)

        # Best-effort staff notice: a failed write must not unwind the committed report.
        try:
            from .staff_notification_service import notify_report_filed

            notify_report_filed(
                db, kind="chat message", report_id=report_id,
                summary=f"A chat message was reported by {reporter.regno}. Open Reports review to rule on it.",
            )
        except Exception:
            logger.warning("Failed to queue staff notification for message report %s", message_id)

        return {
            "id": report_id,
            "message": "Message reported successfully"
        }
    
    def get_conversation_messages_count(
        self,
        db: Database,
        conversation_id: str
    ) -> int:
        """Get message count for a conversation"""
        with db.sessions() as session:
            return session.scalar(select(func.count()).select_from(MessageRecord).where(
                MessageRecord.conversation_id == str(conversation_id)
            )) or 0


# Global instance
message_service = MessageService()
