# app/services/conversation_service.py

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from ..postgres import Database
from ..postgres import new_id, is_valid_id
from ..db_models import ConversationRecord, MatchRecord, UserRecord
from ..logger import get_logger
from datetime import datetime, timezone

from ..models import UserDetails, Match, Conversation
from ..services.storage_service import storage_service
from ..config import settings
from .auth_service import ACCOUNT_UNAVAILABLE_DETAIL

# Logger
logger = get_logger(__name__)

def _user_data(row):
    if row is None:
        return None
    return {
        "id": str(row.id), "regno": row.regno, "name": row.name,
"email": row.email, "emoji": row.emoji, "bio": row.bio,
        "year": row.year, "department": row.department,
        "profile_picture_url": storage_service.profile_delivery_url(
            row.profile_picture_public_id, row.profile_picture_format
        ),
        "gender": row.gender, "interests": row.interests or [],
        "matchmaking_enabled": row.matchmaking_enabled, "notifications_enabled": row.notifications_enabled,
        "love_notes_receive_enabled": row.love_notes_receive_enabled, "love_notes_send_enabled": row.love_notes_send_enabled,
        "is_blocked": row.is_blocked,
        "last_login_at": row.last_login_at,
        "last_login_ip": str(row.last_login_ip) if row.last_login_ip else None,
        "last_login_user_agent": row.last_login_user_agent,
        "last_matchmaking_at": row.last_matchmaking_at,
        "last_confession_at": row.last_confession_at,
        # Rows here come from ``users`` and only from ``users``, so the table is fixed. It is
        # stated rather than left to the model default so that if a caller ever feeds this a
        # row from another table it fails validation instead of being silently relabelled as
        # a participant.
        "account_type": "users",
    }

def _match_data(row):
    if row is None:
        return None
    return {"id": str(row.id), "user_1_regno": row.user_1_regno,
            "user_2_regno": row.user_2_regno, "created_at": row.created_at,
            "expires_at": row.expires_at}

def _conversation_data(row):
    if row is None:
        return None
    return {"id": str(row.id), "match_id": str(row.match_id),
            "initiator_regno": row.initiator_regno, "receiver_regno": row.receiver_regno,
            "status": row.status, "requested_at": row.requested_at,
            "accepted_at": row.accepted_at, "created_at": row.created_at,
            "last_message_at": row.last_message_at,
            "last_message_preview": row.last_message_preview,
            "is_blocked": bool(row.blocked_by),
            "blocked_by": row.blocked_by[0] if row.blocked_by else None}

def _get_match(db, match_id):
    with db.sessions() as session:
        return _match_data(session.get(MatchRecord, str(match_id)))

def _get_conversation(db, match_id):
    with db.sessions() as session:
        row = session.scalar(select(ConversationRecord).where(ConversationRecord.match_id == str(match_id)))
        return _conversation_data(row)

def _get_user_by_regno(db, regno):
    with db.sessions() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
        return _user_data(row)

def _terminated_match_exists():
    """Whether the match row in scope has a conversation a moderator has closed.

    A terminated conversation outlives the match's four-hour window on purpose: the thread
    is the only place either participant can read what was said before it was shut, and the
    status is the record of that, not the clock. Matched on status rather than on
    ``terminated_at`` so rows terminated by an older build -- which rewrote ``expires_at``
    and so look lapsed -- are picked up by the same query.
    """
    return (
        select(ConversationRecord.id)
        .where(
            ConversationRecord.match_id == MatchRecord.id,
            ConversationRecord.status == "terminated",
        )
        .exists()
    )

def _partner_regno(match, regno: str) -> str:
    """The other participant of a match, which is never the caller themselves."""
    return match.user_2_regno if regno == match.user_1_regno else match.user_1_regno

def _ensure_partner_available(db, match, regno: str) -> bool:
    """Refuse a matchmaking surface when the other participant has been blocked.

    A match drawn before the block still sits in the table, so every place that hands a
    partner back to a client has to ask again: the draw query filters blocked accounts,
    but an existing match does not disappear when one of them is blocked. Returns True
    when the partner is blocked, for the callers that hide the row instead of raising.
    """
    partner_doc = _get_user_by_regno(db, _partner_regno(match, regno))
    return bool(partner_doc and partner_doc.get("is_blocked"))

def _reject_if_partner_blocked(db, match, regno: str) -> None:
    if _ensure_partner_available(db, match, regno):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ACCOUNT_UNAVAILABLE_DETAIL,
        )

def _save_conversation(db, conversation_id, **changes):
    with db.sessions.begin() as session:
        row = session.get(ConversationRecord, str(conversation_id))
        if not row:
            return False
        for name, value in changes.items():
            if name == "is_blocked":
                row.blocked_by = list(row.blocked_by or [])
                if value and changes.get("blocked_by") and changes["blocked_by"] not in row.blocked_by:
                    row.blocked_by.append(changes["blocked_by"])
                elif not value:
                    row.blocked_by = []
            elif name == "blocked_by":
                if value and value not in (row.blocked_by or []):
                    row.blocked_by = list(row.blocked_by or []) + [value]
                elif value is None:
                    row.blocked_by = []
            elif name in {"status", "requested_at", "accepted_at", "terminated_at"}:
                setattr(row, name, value)
        return True

def request_conversation_service(current_user: UserDetails, match_id_str: str, db: Database):
    """
    Used to update a conversation from 'pending' to 'requested' and notify the receiver.
    This is called when the initiator clicks "Send Message Request".
    """
    try: # <--- FIX: Added main try block
        try: # Indented this block
            match_id = str(match_id_str)
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Match ID format.")

        # 1. Fetch the match document
        match_doc = _get_match(db, match_id)
        if not match_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found.")
        
        try: # Indented this block
            match = Match(**match_doc)
        except Exception as e:
            logger.error(f"Error parsing match document: {e}")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error processing match data.")

        # 2. Check if the match has expired
        current_time = datetime.now(timezone.utc)
        match_expires_at = match.expires_at
        
        if match_expires_at.tzinfo is None:
            match_expires_at = match_expires_at.replace(tzinfo=timezone.utc)
        
        if current_time > match_expires_at:
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="This match has expired.")

        # 3. Verify the current user is part of the match
        if current_user.regno not in [match.user_1_regno, match.user_2_regno]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not part of this match.")

        # 3b. A partner blocked after the match was drawn is not reachable anymore.
        _reject_if_partner_blocked(db, match, current_user.regno)

        # 4. Get the existing conversation (should exist with status 'pending')
        conversation_doc = _get_conversation(db, match_id)
        if not conversation_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No conversation found for this match.")

        conversation = Conversation(**conversation_doc)
        
        # 5. Verify current user is the initiator
        if conversation.initiator_regno != current_user.regno:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the initiator can send the message request.")

        # 6. Check if already requested
        if conversation.status != "pending":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Message request has already been sent.")

        # 7. Update conversation status to 'requested'
        requested_at = datetime.now(timezone.utc)
        _save_conversation(db, conversation.id, status="requested", requested_at=requested_at)
        
# 8. Create notification for the receiver
        from ..services.notification_service import create_notification_service
        receiver_user_doc = _get_user_by_regno(db, conversation.receiver_regno)
        if receiver_user_doc:
            receiver_user = UserDetails(**receiver_user_doc)
            # The target is the match, not the conversation. Every conversation route in the
            # API is keyed by match -- there is at most one conversation per match -- so the
            # match id is the one the client needs to reach the thing being announced.
            create_notification_service(
                recipient_regno=conversation.receiver_regno,
                heading=f"Message request from {current_user.name}",
                body=f"{current_user.name} wants to start a conversation with you!",
                db=db,
                target_kind="conversation",
                target_id=match_id,
            )
        
        logger.info(f"Conversation request sent from {current_user.regno} to {conversation.receiver_regno} for match {match_id}")

        return {"status": "success", "message": "Message request sent."}
    except HTTPException:
        # Deliberate rejections (expired match, blocked partner, wrong participant)
        # carry a status the client can act on. Letting the blanket handler below
        # swallow them would report every one of them as a server fault.
        raise
    except Exception as e: # <--- FIX: This except block is now correctly placed
        logger.error(f"Error creating conversation: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error creating conversation.")


def get_conversation_status_service(current_user: UserDetails, match_id_str: str, db: Database):
    """
    Used to get the status of a conversation for a given match.
    """
    try:
        try:
            match_id = str(match_id_str)
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Match ID format.")

        # 1. Fetch the match document
        match_doc = _get_match(db, match_id)
        if not match_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found.")
        
        match = Match(**match_doc)

        # 2. Check if the match has expired
        # Ensure both datetimes are timezone-aware for comparison
        current_time = datetime.now(timezone.utc)
        match_expires_at = match.expires_at
        
        # If the stored datetime is timezone-naive, assume it's UTC
        if match_expires_at.tzinfo is None:
            match_expires_at = match_expires_at.replace(tzinfo=timezone.utc)
        
        if current_time > match_expires_at:
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="This match has expired.")

        # 3. Verify the current user is part of the match
        if current_user.regno not in [match.user_1_regno, match.user_2_regno]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not part of this match.")

        # 3b. Do not describe a conversation with a partner who has since been blocked.
        _reject_if_partner_blocked(db, match, current_user.regno)

        # 4. Get conversation status
        conversation_doc = _get_conversation(db, match_id)
        if not conversation_doc:
            return {"status": "no_conversation", "message": "No conversation exists for this match."}

        conversation = Conversation(**conversation_doc)
        
        return {
            "status": "success",
            "conversation_status": conversation.status,
            "initiator_regno": conversation.initiator_regno,
            "receiver_regno": conversation.receiver_regno,
            "created_at": conversation.created_at,
            "accepted_at": conversation.accepted_at
        }
    except HTTPException:
        # Keep deliberate rejections (expired match, blocked partner, wrong participant)
        # as themselves instead of reporting every one of them as a server fault.
        raise
    except Exception as e: # <--- FIX: Added missing except block
        logger.error(f"Error in get_conversation_status_service: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error getting conversation status.")


def accept_conversation_service(current_user: UserDetails, match_id_str: str, db: Database):
    """
    Used to accept a pending conversation request.
    """
    try:
        try:
            match_id = str(match_id_str)
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Match ID format.")

        # 1. Fetch the match document
        match_doc = _get_match(db, match_id)
        if not match_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found.")
        
        match = Match(**match_doc)

        # 2. Check if the match has expired
        # Ensure both datetimes are timezone-aware for comparison
        current_time = datetime.now(timezone.utc)
        match_expires_at = match.expires_at
        
        # If the stored datetime is timezone-naive, assume it's UTC
        if match_expires_at.tzinfo is None:
            match_expires_at = match_expires_at.replace(tzinfo=timezone.utc)
        
        if current_time > match_expires_at:
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="This match has expired.")

        # 3. Verify the current user is part of the match
        if current_user.regno not in [match.user_1_regno, match.user_2_regno]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not part of this match.")

        # 3b. A partner blocked after the match was drawn cannot be accepted by anyone.
        _reject_if_partner_blocked(db, match, current_user.regno)

        # 4. Get the conversation
        conversation_doc = _get_conversation(db, match_id)
        if not conversation_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No conversation found for this match.")

        conversation = Conversation(**conversation_doc)

        # 5. Check if the current user is the receiver
        if conversation.receiver_regno != current_user.regno:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the receiver can accept the conversation.")

        # 6. Check if the conversation is already accepted
        if conversation.status == "accepted":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Conversation is already accepted.")
        
        # Check if the conversation is in 'requested' status (not 'pending')
        if conversation.status != "requested":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Conversation must be in 'requested' status to accept.")

        # 7. Update the conversation status
        accepted_at = datetime.now(timezone.utc)
        _save_conversation(db, conversation.id, status="accepted", accepted_at=accepted_at)
        
        # Create notification for the initiator
        from ..services.notification_service import create_notification_service
        initiator_user_doc = _get_user_by_regno(db, conversation.initiator_regno)
        receiver_user_doc = _get_user_by_regno(db, current_user.regno)
        if initiator_user_doc and receiver_user_doc:
            create_notification_service(
                recipient_regno=conversation.initiator_regno,
                heading=f"{current_user.name} accepted your message request",
                body=f"{current_user.name} accepted your message request. You can now start chatting!",
                db=db,
                target_kind="conversation",
                target_id=match_id,
            )
        
        logger.info(f"Conversation {conversation.id} accepted by {current_user.regno}")

        return {
            "status": "success",
            "message": "Conversation accepted successfully.",
            "conversation_id": str(conversation.id)
        }
    except HTTPException:
        # Keep deliberate rejections (expired match, blocked partner, wrong participant)
        # as themselves instead of reporting every one of them as a server fault.
        raise
    except Exception as e: # <--- FIX: Added missing except block
        logger.error(f"Error in accept_conversation_service: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error accepting conversation.")


def reject_conversation_service(current_user: UserDetails, match_id_str: str, db: Database):
    """
    Used to reject a pending conversation request.
    """
    try:
        try:
            match_id = str(match_id_str)
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Match ID format.")

        # 1. Fetch the match document
        match_doc = _get_match(db, match_id)
        if not match_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found.")
        
        match = Match(**match_doc)

        # 2. Check if the match has expired
        # Ensure both datetimes are timezone-aware for comparison
        current_time = datetime.now(timezone.utc)
        match_expires_at = match.expires_at
        
        # If the stored datetime is timezone-naive, assume it's UTC
        if match_expires_at.tzinfo is None:
            match_expires_at = match_expires_at.replace(tzinfo=timezone.utc)
        
        if current_time > match_expires_at:
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="This match has expired.")

        # 3. Verify the current user is part of the match
        if current_user.regno not in [match.user_1_regno, match.user_2_regno]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not part of this match.")

        # 4. Get the conversation
        conversation_doc = _get_conversation(db, match_id)
        if not conversation_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No conversation found for this match.")

        conversation = Conversation(**conversation_doc)

        # 5. Check if the current user is the receiver
        if conversation.receiver_regno != current_user.regno:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the receiver can reject the conversation.")

        # 6. Check if the conversation is already accepted or rejected
        if conversation.status != "requested":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Conversation is not in requested status.")

        # 7. Update the conversation status
        _save_conversation(db, conversation.id, status="rejected")
        
        # Create notification for the initiator
        from ..services.notification_service import create_notification_service
        initiator_user_doc = _get_user_by_regno(db, conversation.initiator_regno)
        if initiator_user_doc:
            create_notification_service(
                recipient_regno=conversation.initiator_regno,
                heading=f"{current_user.name} rejected your message request",
                body=f"{current_user.name} rejected your message request.",
                db=db,
                target_kind="conversation",
                target_id=match_id,
            )
        
        logger.info(f"Conversation {conversation.id} rejected by {current_user.regno}")

        return {"status": "success", "message": "Conversation rejected successfully."}
    except Exception as e: # <--- FIX: Added missing except block
        logger.error(f"Error in reject_conversation_service: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error rejecting conversation.")


def get_current_conversation_service(current_user: UserDetails, db: Database):
    """
    Used to get the current active conversation for the authenticated user.
    ONLY returns matches where the current user is the INITIATOR (user_1).
    The matched user (user_2/receiver) should NOT see the match until they get a notification
    and should be able to continue matchmaking.
    """
    current_time = datetime.now(timezone.utc)

    # Only find matches where current user is user_1 (initiator)
    with db.sessions() as session:
        active_match_row = session.scalar(select(MatchRecord).where(
            MatchRecord.user_1_regno == current_user.regno,
            or_(MatchRecord.expires_at > current_time, _terminated_match_exists()),
        ).order_by(MatchRecord.created_at.desc()))
        current_match = _match_data(active_match_row)

    if not current_match:
        # No live or terminated match where user is initiator — fall back to the most
        # recent one of any state, so a lapsed conversation still has somewhere to be read
        # from. Ordered by when the match was drawn rather than by when it lapses: a
        # moderator-closed match used to be stamped as expiring immediately, which sorted
        # it behind older matches instead of in front of them.
        with db.sessions() as session:
            recent_match_row = session.scalar(select(MatchRecord).where(
                MatchRecord.user_1_regno == current_user.regno
            ).order_by(MatchRecord.created_at.desc()).limit(1))
            current_match = _match_data(recent_match_row)
        if not current_match:
            # No matches where user is initiator
            return {"status": "no_active_match", "message": "No active match found."}
        # mark as expired if expires_at <= now
        match = Match(**current_match)
        match_expires_at = match.expires_at
        if match_expires_at.tzinfo is None:
            match_expires_at = match_expires_at.replace(tzinfo=timezone.utc)
        is_expired = current_time > match_expires_at
    else:
        match = Match(**current_match)
        is_expired = False

    # A partner blocked after the match was drawn is reported as no match at all, so the
    # profile does not resurface on the matchmaking or inbox screens. Answering with the
    # existing empty shape keeps the client on its normal "nothing here" path.
    if _ensure_partner_available(db, match, current_user.regno):
        logger.info(
            f"Suppressed current match for {current_user.regno}: partner {match.user_2_regno} is blocked"
        )
        return {"status": "no_active_match", "message": "No active match found."}

    # Get the conversation for this match (may or may not exist)
    conversation_doc = _get_conversation(db, match.id)
    if not conversation_doc:
        # No conversation created yet; return match info and empty conversation placeholder
        # Return success so frontend shows the match in inbox even if expired
        other_user_regno = match.user_2_regno if current_user.regno == match.user_1_regno else match.user_1_regno
        other_user_doc = _get_user_by_regno(db, other_user_regno)
        other_user = UserDetails(**other_user_doc) if other_user_doc else None

        # ``_user_data`` already derived this from the row's stored identity. ``UserDetails``
        # carries the delivery URL, not the Cloudinary identity -- the identity columns exist
        # only on the database row -- so there is nothing to recompute here, and reaching for
        # them on the model would be reading an attribute that has never existed there.
        profile_picture_url = other_user.profile_picture_url if other_user else None

        return {
            "status": "success",
            "match": {
                "id": str(match.id),
                "expires_at": match.expires_at.isoformat(),
                "created_at": match.created_at.isoformat(),
                "is_expired": is_expired
            },
            "conversation": {
                "id": None,
                "status": "none",
                "initiator_regno": None,
                "receiver_regno": None,
                "created_at": None,
                "accepted_at": None
            },
            "other_user": {
                "regno": other_user.regno if other_user else None,
                "name": other_user.name if other_user else None,
                "profile_picture_url": profile_picture_url,
                "profile_picture": profile_picture_url,
                "year": other_user.year if other_user else None,
                "department": other_user.department if other_user else None,
                "bio": other_user.bio if other_user else None,
                "interests": other_user.interests if other_user else []
            },
            "is_initiator": False
        }

    # If conversation exists, return it (allow expired)
    conversation = Conversation(**conversation_doc)
    other_user_regno = match.user_2_regno if current_user.regno == match.user_1_regno else match.user_1_regno
    other_user_doc = _get_user_by_regno(db, other_user_regno)
    other_user = UserDetails(**other_user_doc) if other_user_doc else None

    # Derived once in ``_user_data``; see the note on the sibling branch above.
    profile_picture_url = other_user.profile_picture_url if other_user else None

    response_data = {
        "status": "success",
        "match": {
            "id": str(match.id),
            "expires_at": match.expires_at.isoformat(),
            "created_at": match.created_at.isoformat(),
            "is_expired": is_expired
        },
        "conversation": {
            "id": str(conversation.id),
            "status": conversation.status,
            "initiator_regno": conversation.initiator_regno,
            "receiver_regno": conversation.receiver_regno,
            "created_at": conversation.created_at.isoformat(),
            "accepted_at": conversation.accepted_at.isoformat() if conversation.accepted_at else None
        },
        "other_user": {
            "regno": other_user.regno if other_user else None,
            "name": other_user.name if other_user else None,
            "profile_picture_url": profile_picture_url,
            "profile_picture": profile_picture_url,
            "year": other_user.year if other_user else None,
                "department": other_user.department if other_user else None,
            "bio": other_user.bio if other_user else None,
            "interests": other_user.interests if other_user else []
        },
        "is_initiator": conversation.initiator_regno == current_user.regno,
        "is_blocked": conversation_doc.get("is_blocked", False),
        "blocked_by": conversation_doc.get("blocked_by", None)
    }
    
    return response_data


def get_conversation_by_match_id(match_id_str: str, db: Database):
    """
    Get conversation by match ID (kept for compatibility).
    """
    try:
        match_id = str(match_id_str)
    except Exception:
        return None
    
    conversation_doc = _get_conversation(db, match_id)
    if not conversation_doc:
        return None
    
    return conversation_doc


def update_conversation_status(match_id_str: str, status: str, db: Database):
    """
    Update the conversation status in PostgreSQL.
    """
    try:
        match_id = str(match_id_str)
    except Exception:
        return False
    
    conversation = _get_conversation(db, match_id)
    return bool(conversation and _save_conversation(db, conversation["id"], status=status))


def get_received_conversations_service(current_user: UserDetails, db: Database):
    """
    Get conversations where the current user is the RECEIVER (user_2).
    This shows message requests that the user has received.
    """
    current_time = datetime.now(timezone.utc)
    
# Find matches where current user is user_2 (receiver). A moderator-closed thread is
    # included whatever the clock says, so the receiver does not lose the conversation out
    # of their message box the moment it is terminated.
    with db.sessions() as session:
        received_matches = [_match_data(row) for row in session.scalars(select(MatchRecord).where(
            MatchRecord.user_2_regno == current_user.regno,
            or_(MatchRecord.expires_at > current_time, _terminated_match_exists()),
        )).all()]
    
    if not received_matches:
        return {"status": "no_received_conversations", "conversations": []}
    
    conversations = []
    
    for match_doc in received_matches:
        match = Match(**match_doc)
        
        # Get conversation for this match
        conversation_doc = _get_conversation(db, match.id)
        if not conversation_doc:
            continue
        
        conversation = Conversation(**conversation_doc)
        
        # Only include if status is 'requested' or later (not 'pending')
        if conversation.status == 'pending':
            continue
        
        # Get the initiator's info
        initiator_user_doc = _get_user_by_regno(db, match.user_1_regno)
        if not initiator_user_doc:
            continue
        
        # A request that arrived before the initiator was blocked is dropped rather than
        # offered: the blocked profile must not appear in the receiver's request list.
        if initiator_user_doc.get("is_blocked"):
            logger.info(
                f"Hid message request for {current_user.regno}: initiator {match.user_1_regno} is blocked"
            )
            continue
        
        initiator_user = UserDetails(**initiator_user_doc)
        # Already derived by ``_user_data``; see the note on the sibling branch above.
        profile_picture_url = initiator_user.profile_picture_url
        
        match_expires_at = match.expires_at
        if match_expires_at.tzinfo is None:
            match_expires_at = match_expires_at.replace(tzinfo=timezone.utc)
        is_expired = current_time > match_expires_at
        
        conversation_data = {
            "match": {
                "id": str(match.id),
                "expires_at": match.expires_at.isoformat(),
                "created_at": match.created_at.isoformat(),
                "is_expired": is_expired
            },
            "conversation": {
                "id": str(conversation.id),
                "status": conversation.status,
                "initiator_regno": conversation.initiator_regno,
                "receiver_regno": conversation.receiver_regno,
                "created_at": conversation.created_at.isoformat(),
                "accepted_at": conversation.accepted_at.isoformat() if conversation.accepted_at else None
            },
            "other_user": {
                "regno": initiator_user.regno,
                "name": initiator_user.name,
                "profile_picture_url": profile_picture_url,
                "profile_picture": profile_picture_url,
                "year": initiator_user.year,
                "department": initiator_user.department,
                "bio": initiator_user.bio,
                "interests": initiator_user.interests or []
            },
            "is_initiator": False,
            "is_blocked": conversation_doc.get("is_blocked", False),
            "blocked_by": conversation_doc.get("blocked_by", None)
        }
        
        conversations.append(conversation_data)
    
    return {"status": "success", "conversations": conversations}


def get_conversation_by_match_service(current_user: UserDetails, match_id_str: str, db: Database):
    """
    Get a specific conversation by match_id.
    Works for both initiators and receivers.
    """
    try:
        try:
            match_id = str(match_id_str)
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Match ID format.")

        # Fetch the match document
        match_doc = _get_match(db, match_id)
        if not match_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found.")
        
        match = Match(**match_doc)

        # Verify the current user is part of the match
        if current_user.regno not in [match.user_1_regno, match.user_2_regno]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not part of this match.")

        # The match may predate the block, so the other profile is re-checked here rather
        # than trusting the draw. This is the lookup the chat dialog opens on.
        _reject_if_partner_blocked(db, match, current_user.regno)

        # Get conversation for this match
        conversation_doc = _get_conversation(db, match_id)
        if not conversation_doc:
            return {"status": "no_conversation", "message": "No conversation exists for this match."}
        
        conversation = Conversation(**conversation_doc)
        
        # Determine if current user is initiator
        is_initiator = current_user.regno == match.user_1_regno
        
        # Get the other user's info
        other_user_regno = match.user_2_regno if is_initiator else match.user_1_regno
        other_user_doc = _get_user_by_regno(db, other_user_regno)
        if not other_user_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Other user not found.")
        
        other_user = UserDetails(**other_user_doc)
        # Already derived by ``_user_data``; see the note on the sibling branch above.
        profile_picture_url = other_user.profile_picture_url
        
        # Check if match is expired
        current_time = datetime.now(timezone.utc)
        match_expires_at = match.expires_at
        if match_expires_at.tzinfo is None:
            match_expires_at = match_expires_at.replace(tzinfo=timezone.utc)
        is_expired = current_time > match_expires_at
        
        conversation_data = {
            "status": "success",
            "match": {
                "id": str(match.id),
                "expires_at": match.expires_at.isoformat(),
                "created_at": match.created_at.isoformat(),
                "is_expired": is_expired
            },
            "conversation": {
                "id": str(conversation.id),
                "status": conversation.status,
                "initiator_regno": conversation.initiator_regno,
                "receiver_regno": conversation.receiver_regno,
                "created_at": conversation.created_at.isoformat(),
                "accepted_at": conversation.accepted_at.isoformat() if conversation.accepted_at else None
            },
            "other_user": {
                "regno": other_user.regno,
                "name": other_user.name,
                "profile_picture_url": profile_picture_url,
                "year": other_user.year,
                "department": other_user.department,
                "bio": other_user.bio,
                "interests": other_user.interests or []
            },
            "is_initiator": is_initiator,
            "is_blocked": conversation_doc.get("is_blocked", False),
            "blocked_by": conversation_doc.get("blocked_by", None)
        }
        
        return conversation_data
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get_conversation_by_match_service: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error getting conversation.")


def block_conversation_service(current_user: UserDetails, match_id_str: str, db: Database):
    """
    Block a conversation. Updates PostgreSQL only.
    """
    try:
        try:
            match_id = str(match_id_str)
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid match ID.")
        
        # Get the match
        match_doc = _get_match(db, match_id)
        if not match_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found.")
        
        match = Match(**match_doc)
        
        # Verify user is part of the match
        if current_user.regno not in [match.user_1_regno, match.user_2_regno]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not part of this match.")
        
        # Get the conversation
        conversation_doc = _get_conversation(db, match_id)
        if not conversation_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
        
        # Update conversation to blocked status
        _save_conversation(db, conversation_doc["id"], is_blocked=True, blocked_by=current_user.regno)
        
        logger.info(f"User {current_user.regno} blocked conversation for match {match_id}")
        
        return {
            "status": "success",
            "message": "Conversation blocked successfully.",
            "blocked_by": current_user.regno
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error blocking conversation: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error blocking conversation.")


def unblock_conversation_service(current_user: UserDetails, match_id_str: str, db: Database):
    """
    Unblock a conversation. Only the user who blocked can unblock.
    """
    try:
        try:
            match_id = str(match_id_str)
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid match ID.")
        
        # Get the match
        match_doc = _get_match(db, match_id)
        if not match_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found.")
        
        match = Match(**match_doc)
        
        # Verify user is part of the match
        if current_user.regno not in [match.user_1_regno, match.user_2_regno]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not part of this match.")
        
        # Get the conversation
        conversation_doc = _get_conversation(db, match_id)
        if not conversation_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
        
        # Check if user is the one who blocked
        if conversation_doc.get("blocked_by") != current_user.regno:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only unblock if you were the one who blocked.")
        
        # Update conversation to unblocked
        _save_conversation(db, conversation_doc["id"], is_blocked=False, blocked_by=None)
        
        logger.info(f"User {current_user.regno} unblocked conversation for match {match_id}")
        
        return {
            "status": "success",
            "message": "Conversation unblocked successfully."
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error unblocking conversation: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error unblocking conversation.")
