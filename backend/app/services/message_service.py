# app/services/message_service.py

from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from sqlalchemy import func, select
from ..postgres import Database
from ..postgres import new_id, is_valid_id
from ..db_models import ConversationRecord, MessageRecord, MessageReportRecord, UserRecord
from ..logger import get_logger
from ..utils import isoformat_datetime
from fastapi import HTTPException, status

from ..models import UserDetails
from ..accounts import Capability, has_capability
from .redis_service import redis_service
from .auth_service import ACCOUNT_UNAVAILABLE_DETAIL

logger = get_logger(__name__)

# Statuses whose transcript a participant may read. ``accepted`` is a live thread and
# ``terminated`` is a closed one; both are read-only-or-live and both were real conversations
# that people wrote in. Anything earlier in the lifecycle has no transcript to speak of.
READABLE_CONVERSATION_STATUSES = frozenset({"accepted", "terminated"})

# Statuses a message may be sent into. A terminated conversation is not one of them.
WRITABLE_CONVERSATION_STATUS = "accepted"


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
        with db.sessions.begin() as session:
            conversation_row = session.get(ConversationRecord, str(conversation_id))
            if conversation_row and conversation_row.blocked_by:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cannot send messages: this conversation is blocked"
                )
            row = MessageRecord(
                conversation_id=str(conversation_id), sender_regno=sender.regno,
                receiver_regno=receiver_id, body=body, created_at=message_doc["created_at"], is_read=False,
            )
            session.add(row)
            if conversation_row:
                conversation_row.last_message_at = message_doc["created_at"]
                conversation_row.last_message_preview = body[:100]
            session.flush()
            message_doc["id"] = str(row.id)
        
        # Publish to Redis for real-time delivery
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
        
        redis_service.publish_message(conversation_id, redis_message)
        
        logger.info(f"Message sent: {sender.regno} -> {receiver_id} in conversation {conversation_id}")
        
        return {
            "id": str(message_doc["id"]),
            "conversation_id": conversation_id,
            "sender_regno": sender.regno,
            "receiver_regno": receiver_id,
            "body": body,
            "created_at": isoformat_datetime(message_doc["created_at"]),
            "is_read": False
        }
    
    def get_messages(
        self,
        db: Database,
        conversation_id: str,
        user: UserDetails,
        limit: int = 200
    ) -> List[Dict[str, Any]]:
        """
        Get messages for a conversation
        
        Enforces:
        - User is participant in conversation (unless a moderator)
        - Conversation is accepted
        """
        # A caller holding CONVERSATIONS_READ_MESSAGES -- the same capability that guards
        # GET /admin/conversations/{id} -- may read any thread. Keyed on the capability that
        # the caller's account table grants, rather than on the admin role, so the two paths
        # cannot disagree about who may read a transcript, and so a moderator gets it here
        # too. A participant's table grants nothing, so this is False for them without a
        # second branch naming them.
        may_read_any = has_capability(user.account_type, Capability.CONVERSATIONS_READ_MESSAGES)
        if not may_read_any:
            # Validate user is participant
            conversation = self.validate_conversation_participant(db, conversation_id, user.regno)

            # Reading a thread with a blocked partner is refused for the same reason sending
            # is: the conversation is not reachable from this account any more. Staff keep
            # their moderation read, so this stays inside the non-staff branch.
            if self.is_partner_blocked(db, conversation, user.regno):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=ACCOUNT_UNAVAILABLE_DETAIL
                )
        
        # Fetch messages
        with db.sessions() as session:
            rows = session.scalars(select(MessageRecord)
                .where(MessageRecord.conversation_id == str(conversation_id))
                .order_by(MessageRecord.created_at.asc()).limit(limit)).all()
            messages = [{"id": str(row.id), "conversation_id": row.conversation_id,
                "sender_regno": row.sender_regno, "receiver_regno": row.receiver_regno, "body": row.body,
                "created_at": row.created_at, "is_read": row.is_read} for row in rows]
        
        # Mark messages as read for receiver. A moderator's read must not clear the other
        # party's unread state, so this is skipped for exactly the readers above.
        if not may_read_any:
            with db.sessions.begin() as session:
                rows = session.scalars(select(MessageRecord).where(
                    MessageRecord.conversation_id == str(conversation_id),
                    MessageRecord.receiver_regno == user.regno,
                    MessageRecord.is_read.is_(False),
                )).all()
                for row in rows:
                    row.is_read = True
        
        # Format response
        return [
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
        ]
    
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
        
        logger.info(f"Message {message_id} reported by {reporter.regno} for: {reason}")
        
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
