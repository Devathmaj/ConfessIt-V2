# app/services/message_service.py

from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional
from sqlalchemy import and_, func, or_, select
from ..postgres import Database
from ..postgres import new_id, is_valid_id
from ..db_models import ConversationRecord, MessageRecord, MessageReportRecord, UserRecord
from ..logger import get_logger
from ..limits import CONVERSATION_MESSAGE_MAX, CONVERSATION_PARTICIPANT_MESSAGE_MAX
from ..utils import isoformat_datetime
from fastapi import HTTPException, status

from ..models import UserDetails
from .redis_service import redis_service
from .auth_service import ACCOUNT_UNAVAILABLE_DETAIL
from .report_counters import MESSAGE, record_report_filed, record_removal

logger = get_logger(__name__)

# Statuses whose transcript a participant may read. ``accepted`` is a live thread and
# ``terminated`` is a closed one; both are read-only-or-live and both were real conversations
# that people wrote in. Anything earlier in the lifecycle has no transcript to speak of.
READABLE_CONVERSATION_STATUSES = frozenset({"accepted", "terminated"})

# Statuses a message may be sent into. A terminated conversation is not one of them.
WRITABLE_CONVERSATION_STATUS = "accepted"

# The two ways a conversation runs out of room. Which one stopped the send is worth knowing:
# a participant at their own limit has a conversation they can still read and nothing more to
# put in it, whereas a conversation at its ceiling is finished for both of them.
PARTICIPANT_LIMIT_REACHED = "participant"
CONVERSATION_LIMIT_REACHED = "conversation"

# Built from the limits rather than written out, so the sentence a user reads cannot drift
# away from the number the service is actually enforcing.
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
        """
        Validate that user is a participant in the conversation
        
        Returns conversation data if valid, raises HTTPException otherwise
        """
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
        
        # Check if user is participant
        is_participant = (
            conversation.get("initiator_regno") == user_regno or
            conversation.get("receiver_regno") == user_regno
        )
        
        if not is_participant:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not a participant in this conversation"
            )
        
        # Check conversation status. ``terminated`` is accepted here for the same reason
        # expiry is: a thread a moderator closed is finished, not unreadable, and the two
        # people in it still need to read what they said before it was shut. Writing is
        # refused separately in send_message().
        if conversation.get("status") not in READABLE_CONVERSATION_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Conversation must be accepted before sending messages"
            )
        
        # NOTE: Block check removed from here - reading messages should be allowed even when blocked
        # The block check is only enforced in send_message() to prevent sending new messages
        
        # NOTE: Expiry check removed from here for the same reason. An expired conversation is
        # read-only, not unreadable: get_messages() shares this validator and must still be
        # able to return the history. Enforced in send_message() instead.
        
        return conversation

    @staticmethod
    def _conversation_payload(row):
        return {
            "id": str(row.id), "match_id": str(row.match_id), "initiator_regno": row.initiator_regno,
            "receiver_regno": row.receiver_regno, "status": row.status, "requested_at": row.requested_at,
            "accepted_at": row.accepted_at, "created_at": row.created_at,
            "last_message_at": row.last_message_at, "last_message_preview": row.last_message_preview,
            "is_blocked": bool(row.blocked_by), "blocked_by": row.blocked_by or [],
            "terminated_at": row.terminated_at,
        }

    def is_partner_blocked(self, db: Database, conversation: Dict[str, Any], regno: str) -> bool:
        """Whether an admin has blocked the other participant of a conversation.

        The conversation row says nothing about account state, so a chat that was accepted
        before the block still has to be re-checked. Read on demand rather than cached: the
        answer decides whether a message may be read or sent at all.
        """
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
        """How much of a conversation's message allowance is spent, read on an open session.

        Counts every row in the conversation, removed ones included. A message a moderator
        took down is still a message that was written: it is still in
        the transcript the other party sees a hole where it was, and letting its removal hand
        the allowance back would mean the ceiling depended on how many reports succeeded.

        Messages remain in the database permanently; old rows are never deleted.
        """
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
        """The usage figures as sent to the client, with the refusals derived from them.

        The derivation happens once, here, so that "how much is left", "may this person send"
        and "which limit stopped them" cannot be three different readings of the same two
        counts on three different surfaces. ``messages_just_sent`` lets the send path report
        the state *after* its own insert without counting the table a second time.
        """
        conversation_count = usage["conversation_count"] + messages_just_sent
        participant_count = usage["participant_count"] + messages_just_sent
        conversation_remaining = max(0, CONVERSATION_MESSAGE_MAX - conversation_count)
        participant_remaining = max(0, CONVERSATION_PARTICIPANT_MESSAGE_MAX - participant_count)

        # The participant's own allowance is checked first because it is the limit that binds
        # in normal use, and it is the more useful thing to be told: the conversation may
        # have room in it that this person cannot use.
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
        """The message allowance of a conversation, as it stands for one participant.

        Read-only, and deliberately independent of the conversation's status: a thread that
        is terminated, expired or blocked is just as finished at 200 messages as at 1, and the
        read that returns this is the one the chat dialog opens with. The caller must already
        be a participant -- ``get_messages`` has established that by the time the router asks
        for this.
        """
        with db.sessions() as session:
            usage = self._message_usage(session, conversation_id, regno)
        return self._quota(usage)

    def send_message(
        self,
        db: Database,
        conversation_id: str,
        sender: UserDetails,
        body: str
    ) -> Dict[str, Any]:
        """
        Send a message in a conversation

        Enforces:
        - Sender is participant
        - Conversation is accepted
        - Conversation is not blocked
        - Conversation is not expired (4 hours)
        - Sender is within their own message allowance, and the conversation is within its own
        """
        # Validate sender is participant (allows reading but not sending if blocked)
        conversation = self.validate_conversation_participant(
            db, conversation_id, sender.regno
        )

        # A thread a moderator closed takes no new messages. Checked here rather than in the
        # shared validator because that one is also on the read path, where a terminated
        # conversation is deliberately still open.
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

        # IMPORTANT: Check if conversation is blocked (prevents sending, but reading is OK)
        if conversation.get("is_blocked", False):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot send messages: this conversation is blocked"
            )

        # Expiry is a send-only restriction (4 hours from creation). Kept out of
        # validate_conversation_participant() so an expired conversation stays readable.
        created_at = conversation.get("created_at")
        if created_at:
            expiry_time = created_at + timedelta(hours=4)
            if datetime.now(timezone.utc) > expiry_time:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Conversation has expired"
                )
        
        # Determine receiver
        receiver_id = (
            conversation["receiver_regno"] 
            if conversation["initiator_regno"] == sender.regno 
            else conversation["initiator_regno"]
        )
        
        # A conversation accepted before the receiver was blocked stops here. The block is
        # re-read rather than inferred from the conversation row, which knows nothing about
        # the account state.
        if self.is_partner_blocked(db, conversation, sender.regno):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=ACCOUNT_UNAVAILABLE_DETAIL
            )

        # Central moderation: one call handles config + analyze + per-item policy,
        # exactly like confessions and their comments. Runs BEFORE persistence so a
        # flagged message is born hidden (never observable in the transcript and
        # never published to Redis) and a provider failure is unknown, never safe:
        # abort before any row exists or anything is delivered. The chat path
        # carries no local endpoint/key/timeout handling and never branches on raw
        # severities -- only on the decision flags. Never log body/secrets.
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

        # Create message document
        message_doc = {
            "conversation_id": str(conversation_id),
            "sender_regno": sender.regno,
            "receiver_regno": receiver_id,
            "body": body,
            "created_at": datetime.now(timezone.utc),
            "is_read": False
        }
        
        # Insert into PostgreSQL
        #
        # The conversation row is taken FOR UPDATE before the counts are read. The cap is
        # enforced by counting rows that already exist, so without the lock two sends racing
        # past the same check would both see 99 and both insert -- the same reason
        # matchmaking claims its cooldown window with a conditional UPDATE rather than a read
        # followed by a write. The lock is on the conversation, so sends into *different*
        # conversations do not contend.
        #
        # A flagged message is stored born hidden (the same removed_at write a moderator's
        # removal performs) together with its automated report, in this one transaction, so
        # a report/hide bookkeeping failure rolls the whole send back rather than leaving a
        # visible message that should have been hidden or a hidden one with no audit trail.
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

                quota = self._quota(self._message_usage(session, conversation_id, sender.regno))
                refusal = self._quota_refusal(quota)
                if refusal is not None:
                    raise refusal

                row = MessageRecord(
                    conversation_id=str(conversation_id), sender_regno=sender.regno,
                    receiver_regno=receiver_id, body=body, created_at=message_doc["created_at"], is_read=False,
                )
                if decision.should_hide:
                    # Born hidden: the same removed_at write a moderator's removal performs,
                    # so every participant read (removed_at IS NULL) excludes it from the
                    # first commit and it is never published.
                    row.removed_at = message_doc["created_at"]
                    row.removed_by_regno = AUTOMATED_REMOVED_BY
                session.add(row)
                if conversation_row and not decision.should_hide:
                    # A hidden message must not become the conversation preview: that preview
                    # is a denormalised copy of the newest *visible* message and is read by
                    # both participants and staff.
                    conversation_row.last_message_at = message_doc["created_at"]
                    conversation_row.last_message_preview = body[:100]
                session.flush()
                message_doc["id"] = str(row.id)
                if decision.should_report:
                    # Idempotent: exactly one automated report per message. A new message
                    # cannot already have one, but the check makes retries safe and the
                    # partial unique index backstops races. Manual reports are untouched.
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
                        record_report_filed(
                            session, kind=MESSAGE, author_regno=sender.regno,
                        )
                        if decision.should_hide:
                            record_removal(
                                session, kind=MESSAGE, author_regno=sender.regno,
                            )
                    else:
                        automated_report_id = str(existing)
                # Reported after the insert rather than recounted, so the response describes the
                # state this send actually left behind.
                quota = self._quota(quota, messages_just_sent=1)
        except HTTPException:
            raise
        except Exception as exc:
            # Report/hide bookkeeping failed after a successful moderation result: never
            # silently treat as successful. The transaction above rolled back, so no partial
            # visible message survives; surface a retryable error (no body/secrets logged).
            logger.error(
                "Automated moderation bookkeeping failed for new message: %s",
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not complete moderation for this message.",
            ) from exc

        # Best-effort staff/author notices: failures never unwind the committed moderation
        # outcome (report + hidden state are already durable).
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
                    # Same neutral copy the direct-hide path sends: no policy is cited
                    # because no human ruled on a report.
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
            # Flagged+hidden: the row stays hidden for audit (report + removed_at already
            # committed), and it is never published to Redis, so the recipient never sees it.
            # The caller is told plainly so the frontend shows a violation popup.
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

        # Publish to Redis for real-time delivery (only reached for a visible message).
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
        
        if not redis_service.publish_message(conversation_id, redis_message):
            # Persistence already committed above; this is a realtime outage, not
            # data loss. Logged loudly so a chat outage cannot hide behind the
            # boolean publish_message returns.
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
            # Sent with the message so a client that has just used up its allowance learns
            # it here rather than by being refused on the next send.
            "quota": quota,
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
        One thread window for the dialog, oldest first with the newest last, plus the total.

        The dialog opens on the newest window and grows upwards: ``before`` names the
        oldest message on screen and the read answers the next older page, so the
        reader's place never moves. A skip offset would shift under concurrent sends --
        a new message landing between two reads would push every row one slot down and
        the second page would repeat one -- while an anchor names a row that cannot
        move. ``total`` counts every visible message in the thread, loaded or not, so
        the dialog knows how many older ones remain.

        A conversation cannot hold more than CONVERSATION_MESSAGE_MAX messages -- the
        send path refuses past it -- so no window here ever needs to consider more.

        Enforces:
        - User is participant in conversation
        - Conversation is accepted
        """
        # Participant-only. There is no staff bypass here: a moderator is not a party to a
        # private thread, and ``reports:review`` -- which they do hold -- governs what they
        # may see *about* a conversation they have been asked to rule on, not a transcript
        # of every thread in the database. The report queue carries the reported message and
        # its surrounding context; nothing else is readable by staff.
        conversation = self.validate_conversation_participant(db, conversation_id, user.regno)

        # Reading a thread with a blocked partner is refused for the same reason sending is:
        # the conversation is not reachable from this account any more.
        if self.is_partner_blocked(db, conversation, user.regno):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=ACCOUNT_UNAVAILABLE_DETAIL
            )

        limit = max(1, min(int(limit or CONVERSATION_MESSAGE_MAX), CONVERSATION_MESSAGE_MAX))
        visible = [
            MessageRecord.conversation_id == str(conversation_id),
            MessageRecord.removed_at.is_(None),
        ]
        with db.sessions() as session:
            anchor = None
            if before is not None:
                anchor = session.get(MessageRecord, str(before))
                if anchor is None or str(anchor.conversation_id) != str(conversation_id):
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Message not found.",
                    )
            if anchor is not None:
                # Strictly older than the anchor, ties broken by id: two messages stamped
                # in the same instant still page without repeating or skipping one.
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
                )
            ) or 0
            # Newest first for the slice, then back to oldest-first for the reply: the
            # dialog renders latest-last, and the slice has to be taken from the newest
            # end for the opening window to be the newest one.
            newest_first = session.scalars(
                # A removed message is invisible on the participant read, which is the only
                # read there is.
                select(MessageRecord).where(*visible)
                .order_by(MessageRecord.created_at.desc(), MessageRecord.id.desc())
                .limit(limit)
            ).all()
            rows = list(reversed(newest_first))
            messages = [{"id": str(row.id), "conversation_id": row.conversation_id,
                "sender_regno": row.sender_regno, "receiver_regno": row.receiver_regno, "body": row.body,
                "created_at": row.created_at, "is_read": row.is_read} for row in rows]
            returned_ids = [str(row.id) for row in rows]

        # Mark messages as read for receiver, scoped to the ids actually returned above.
        # Reading an older page must not mark newer, unloaded messages as though they were
        # seen: the unread badge answers what is unread, and a page that cleared it for
        # rows nobody opened would be lying about that.
        with db.sessions.begin() as session:
            if returned_ids:
                rows = session.scalars(select(MessageRecord).where(
                    MessageRecord.id.in_(returned_ids),
                    MessageRecord.receiver_regno == user.regno,
                    MessageRecord.is_read.is_(False),
                )).all()
                for row in rows:
                    row.is_read = True

        # Format response
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
        """
        Report a message
        
        Enforces:
        - Reporter is the receiver of the message
        - Message exists in an accepted conversation
        - No duplicate reports
        """
        # Get message
        try:
            with db.sessions() as session:
                row = session.get(MessageRecord, str(message_id))
                message = {"id": str(row.id), "conversation_id": row.conversation_id,
                    "sender_regno": row.sender_regno, "receiver_regno": row.receiver_regno,
                    "body": row.body, "created_at": row.created_at, "is_read": row.is_read} if row else None
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid message ID"
            )
        
        if not message:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Message not found"
            )
        
        # Validate reporter is receiver
        if message["receiver_regno"] != reporter.regno:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only report messages sent to you"
            )
        
        # The thread has to be one whose transcript the reader can see at all. Reporting is
        # a note to a moderator about something already written, so a terminated thread
        # qualifies: the report menu is offered on exactly the messages the reader can open.
        with db.sessions() as session:
            row = session.get(ConversationRecord, message["conversation_id"])
            conversation = self._conversation_payload(row) if row else None
        
        if not conversation or conversation.get("status") not in READABLE_CONVERSATION_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Can only report messages in an accepted or terminated conversation"
            )
        
        # Check for duplicate report
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
        
        # Create report
        report_doc = {
            "message_id": str(message_id),
            "reporter_regno": reporter.regno,
            "reported_user_regno": message["sender_regno"],
            "reason": reason,
            "reported_at": datetime.now(timezone.utc),
            "status": "pending"
        }
        
        with db.sessions.begin() as session:
            row = MessageReportRecord(
                message_id=report_doc["message_id"], reporter_regno=report_doc["reporter_regno"],
                reported_user_regno=report_doc["reported_user_regno"], reason=report_doc["reason"],
                reported_at=report_doc["reported_at"], status=report_doc["status"],
            )
            session.add(row)
            session.flush()
            report_id = str(row.id)
            # Charged to the sender, not the receiver: the receiver filed the complaint. By
            # regno because that is how ``messages`` names its author -- see report_counters.
            record_report_filed(
                session, kind=MESSAGE, author_regno=report_doc["reported_user_regno"]
            )
        
        logger.info(f"Message {message_id} reported by {reporter.regno} for: {reason}")

        # Staff-facing notice (role-based, best-effort): a failed write here must
        # never unwind the report that was just committed.
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
