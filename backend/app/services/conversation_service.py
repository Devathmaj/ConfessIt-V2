# app/services/conversation_service.py

from fastapi import HTTPException, status
from sqlalchemy import or_, select, update
from ..postgres import Database
from ..postgres import new_id, is_valid_id, get_row
from ..db_models import ConversationRecord, MatchRecord, UserRecord
from ..logger import get_logger
from datetime import datetime, timezone

from ..models import UserDetails, Match, Conversation
from ..services.storage_service import storage_service
from ..config import settings
from .auth_service import ACCOUNT_UNAVAILABLE_DETAIL
from .chat_encryption import ChatDecryptionError
from .chat_message_crypto import decrypt_matchmaking_preview
from .notification_service import build_notification_heading

# Logger
logger = get_logger(__name__)

def _user_data(row):
    if row is None:
        return None
    return {
        "id": str(row.id), "regno": row.regno, "name": row.name,
        "email": row.email, "bio": row.bio,
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
        "last_profile_picture_at": row.last_profile_picture_at,
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

def _conversation_preview(row):
    """Decrypt the stored preview only while its own retention deadline is open.

    The preview is message content, encrypted under the same policy as the body
    it copies. Once ``last_message_preview_delete_after`` passes it must stop
    being readable even if the daily cleanup has not cleared the columns yet.
    A row that fails authentication is omitted rather than shown stale.
    """
    delete_after = getattr(row, "last_message_preview_delete_after", None)
    if delete_after is None:
        return None
    if delete_after.tzinfo is None:
        delete_after = delete_after.replace(tzinfo=timezone.utc)
    if delete_after <= datetime.now(timezone.utc):
        return None
    try:
        return decrypt_matchmaking_preview(row)
    except ChatDecryptionError as exc:
        logger.error(
            "Conversation preview decryption failed for conversation %s (%s); "
            "omitting preview",
            row.id,
            exc.code,
        )
        return None

def _conversation_data(row):
    if row is None:
        return None
    return {"id": str(row.id), "match_id": str(row.match_id),
            "initiator_regno": row.initiator_regno, "receiver_regno": row.receiver_regno,
            "status": row.status, "requested_at": row.requested_at,
            "accepted_at": row.accepted_at, "created_at": row.created_at,
            "last_message_at": row.last_message_at,
            "last_message_preview": _conversation_preview(row),
            "is_blocked": bool(row.blocked_by),
            # The participant who closed the chat, and the only one who may reopen
            # it. One closer at a time: the block is a close, not a mute, so there
            # is nothing a list of closers would add.
            "blocked_by": row.blocked_by[0] if row.blocked_by else None}

def _get_match(db, match_id):
    """The match, or None when the id names no match at all.

    A malformed id must not reach the database: it goes straight into a uuid
    column, and PostgreSQL raises ``invalid input syntax for type uuid`` on a
    value that is not a uuid. A malformed id names no match, which is what every
    caller here already answers 404 for.
    """
    with db.sessions() as session:
        return _match_data(get_row(session, MatchRecord, match_id))

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
                # The closed flag and the closer are one fact, not two. Closing a
                # chat names the participant who closed it, and opening it again
                # clears that name with it -- a chat nobody closed is not closed.
                if value and changes.get("blocked_by"):
                    row.blocked_by = [changes["blocked_by"]]
                elif not value:
                    row.blocked_by = []
            elif name == "blocked_by":
                # The one closer. The column is an array for compatibility with the
                # rows already written, but it holds a single regno: the
                # participant who closed the chat. A second block replaces it
                # rather than joining it -- see ``block_conversation_service``,
                # which refuses a second close outright.
                row.blocked_by = [value] if value else []
            elif name in {"status", "requested_at", "accepted_at", "terminated_at"}:
                # This write is unconditional, so it cannot decide a race. The decision
                # paths no longer come here -- they go through
                # :func:`_transition_conversation`, whose UPDATE carries the status it
                # expects to leave -- and a new caller must not either.
                setattr(row, name, value)
        return True

def _transition_conversation(db, conversation_id, *, from_status: str, **changes) -> bool:
    """Move a conversation out of ``from_status``, and report whether this call moved it.

    Every status change here used to be a read followed by a write: read the row, check
    ``status``, then set it. Two requests that read before either wrote both pass the check
    and both write, so a double-clicked accept -- or an accept racing a reject -- let the
    second decision silently overwrite the first and the initiator was told both things.
    The predicate is re-checked by PostgreSQL after the row lock is taken, so of two racers
    exactly one sees ``rowcount == 1``; the other knows it decided nothing and answers 409
    instead of pretending otherwise.

    The callers keep their own status checks: those give a retry the answer it deserves
    (409 "already accepted") without a write. This is only the part that has to be atomic.

    Returns False when the row does not exist or is no longer in ``from_status``.
    """
    with db.sessions.begin() as session:
        result = session.execute(
            update(ConversationRecord)
            .where(
                ConversationRecord.id == str(conversation_id),
                ConversationRecord.status == from_status,
            )
            .values(**changes)
        )
        return result.rowcount == 1


def _notify_best_effort(db, *, recipient_regno, heading, body, match_id):
    """Tell the other participant about a conversation decision, without being able to undo it.

    The conversation status is committed before this runs -- by
    ``_transition_conversation``, in its own transaction -- and it is the state the request
    is actually about. A notice that cannot be written is a missing inbox entry; it is not
    a failed request.

    Letting the write raise would answer 500 for an action that has already been applied, and
    the initiator would be left believing the request never went out: the retry is refused as
    "already requested", so the thread sits in a state neither participant can move. The same
    holds for any other database failure here -- a full pool, a deadlock -- which is why this
    is a blanket guard rather than a special case for one long name.

    The target is the match, not the conversation. Every conversation route in the API is keyed
    by match -- there is at most one conversation per match -- so the match id is the one the
    client needs to reach the thing being announced.
    """
    from ..services.notification_service import create_notification_service

    try:
        create_notification_service(
            recipient_regno=recipient_regno,
            heading=heading,
            body=body,
            db=db,
            target_kind="conversation",
            target_id=match_id,
        )
    except Exception:
        logger.warning(
            "Failed to write the conversation notification for match %s; the "
            "conversation status is already committed",
            match_id,
            exc_info=True,
        )

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

        # 7. Take the transition. Two clicks -- or a retry of a request that timed out --
        # can both read 'pending' before either writes, and both would then create a
        # request and a notice. The UPDATE re-checks the status after PostgreSQL takes the
        # row lock, so exactly one call moves the row and only that call tells the receiver.
        requested_at = datetime.now(timezone.utc)
        if not _transition_conversation(
            db,
            conversation.id,
            from_status="pending",
            status="requested",
            requested_at=requested_at,
        ):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Message request has already been sent.")
        
# 8. Create notification for the receiver
        receiver_user_doc = _get_user_by_regno(db, conversation.receiver_regno)
        if receiver_user_doc:
            receiver_user = UserDetails(**receiver_user_doc)
            _notify_best_effort(
                db,
                recipient_regno=conversation.receiver_regno,
                # The name is shortened rather than the heading: the wording is what
                # says what happened, and the client matches the heading on it to
                # tell a conversation notice from a standalone one.
                heading=build_notification_heading(
                    "Message request from {name}", current_user.name
                ),
                body=f"{current_user.name} wants to start a conversation with you!",
                match_id=match_id,
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

        # 7. Take the transition. The checks above answer a plain retry, but they cannot
        # decide between two requests that both read 'requested' before either wrote: both
        # pass, both write, and the second contradicts the first -- an accept and a reject
        # racing would tell the initiator both things. The UPDATE re-checks the status
        # after PostgreSQL takes the row lock, so exactly one call moves the row and only
        # that call sends its notice.
        accepted_at = datetime.now(timezone.utc)
        if not _transition_conversation(
            db,
            conversation.id,
            from_status="requested",
            status="accepted",
            accepted_at=accepted_at,
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Conversation is no longer in 'requested' status.",
            )
        
        # Create notification for the initiator
        initiator_user_doc = _get_user_by_regno(db, conversation.initiator_regno)
        receiver_user_doc = _get_user_by_regno(db, current_user.regno)
        if initiator_user_doc and receiver_user_doc:
            _notify_best_effort(
                db,
                recipient_regno=conversation.initiator_regno,
                heading=build_notification_heading(
                    "{name} accepted your message request", current_user.name
                ),
                body=f"{current_user.name} accepted your message request. You can now start chatting!",
                match_id=match_id,
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

        # 7. Take the transition, for the same reason as the accept path: the check above
        # cannot decide between two requests that both read 'requested' before either
        # wrote, and a reject racing an accept must not be able to contradict it.
        if not _transition_conversation(
            db, conversation.id, from_status="requested", status="rejected"
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Conversation is no longer in 'requested' status.",
            )
        
        # Create notification for the initiator
        initiator_user_doc = _get_user_by_regno(db, conversation.initiator_regno)
        if initiator_user_doc:
            _notify_best_effort(
                db,
                recipient_regno=conversation.initiator_regno,
                heading=build_notification_heading(
                    "{name} rejected your message request", current_user.name
                ),
                body=f"{current_user.name} rejected your message request.",
                match_id=match_id,
            )
        
        logger.info(f"Conversation {conversation.id} rejected by {current_user.regno}")

        return {"status": "success", "message": "Conversation rejected successfully."}
    except HTTPException:
        # Keep deliberate rejections (expired match, wrong participant, wrong
        # receiver, non-requested status) as themselves -- see
        # accept_conversation_service above -- instead of reporting every one
        # of them as a server fault.
        raise
    except Exception as e:
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
    Block a conversation: close it. Updates PostgreSQL only.

    **Blocking a conversation is closing it.** The thread stays readable and
    neither participant can send into it again, until the participant who closed
    it reopens it. There is one closer at a time, and there is no mutual-blocking
    semantics: a chat is not something two participants each hold shut, so there
    is no list of closers to keep.

    Which is why a second close is refused rather than accepted. The chat is
    already shut, so the close the caller asked for has already happened; what a
    second one could add is a second entry in the closers list, and then the
    question of who may reopen it stops being a fact about the chat and starts
    being a question about list order -- the second closer is refused the reopen
    that is theirs, and the first closer's reopen undoes the second one's close.

    Refusing it also keeps the close from becoming a lock the other participant
    can put on: whoever closed the chat is the one who may reopen it.
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

        # Already closed: by the caller or by the other participant. Either way the
        # chat is shut, and the participant who closed it keeps the right to reopen
        # it. The bundled UI never gets here -- it hides the block button on a
        # closed chat -- so this is the API answering a close of a closed chat.
        if conversation_doc.get("is_blocked"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This conversation is already closed.",
            )
        
        # Close it, and name the closer.
        _save_conversation(db, conversation_doc["id"], is_blocked=True, blocked_by=current_user.regno)
        
        logger.info(f"User {current_user.regno} closed conversation for match {match_id}")
        
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
    Unblock a conversation: reopen it. Only the user who closed it can reopen it.

    The closer is the one participant whose regno is on the conversation, and the
    payload carries exactly that one -- so this is a plain identity check against
    the participant who closed the chat, not a question about list order.
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
