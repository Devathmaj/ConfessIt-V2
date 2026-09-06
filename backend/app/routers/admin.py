from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy import func, select

from ..postgres import new_id, is_valid_id
from fastapi import APIRouter, Depends, HTTPException, Query, status
from ..postgres import Database
from pydantic import BaseModel

from ..dependencies import get_db
from ..models import AdminUserCreate, AdminUserUpdate, UserDetails
from ..db_models import (ConfessionRecord, ConversationRecord, MatchRecord, MessageRecord,
    MessageReportRecord, UserRecord)
from ..services.auth_service import get_current_user
from ..services.admin_service import (
    serialize_datetime,
    serialize_user_doc,
    serialize_user_by_regno,
    normalize_conversation_status,
    collect_active_sessions,
    get_all_confessions_data,
    get_all_conversations_data,
    get_matchmaking_overview_data,
    get_admin_statistics_data,
)

router = APIRouter(prefix="/admin", tags=["Admin"])

def _user_payload(user):
    if user is None: return None
    return {"id": str(user.id), "regno": user.regno, "name": user.name, "email": user.email,
        "emoji": user.emoji, "bio": user.bio, "class_name": user.class_name, "gender": user.gender,
        "profile_picture_url": user.profile_picture_url, "interests": user.interests or [],
        "role": user.role, "matchmaking_enabled": user.matchmaking_enabled,
        "notifications_enabled": user.notifications_enabled, "love_notes_receive_enabled": user.love_notes_receive_enabled,
        "love_notes_send_enabled": user.love_notes_send_enabled, "reported_count": user.reported_count,
        "last_login_at": user.last_login_at, "last_login_ip": str(user.last_login_ip) if user.last_login_ip else None,
        "last_login_user_agent": user.last_login_user_agent, "last_matchmaking_at": user.last_matchmaking_at,
        "is_blocked": user.is_blocked}

def _conversation_payload(row):
    return {"id": str(row.id), "match_id": str(row.match_id), "initiator_regno": row.initiator_regno,
        "receiver_regno": row.receiver_regno, "status": row.status, "created_at": row.created_at,
        "requested_at": row.requested_at, "accepted_at": row.accepted_at, "terminated_at": row.terminated_at,
        "is_blocked": bool(row.blocked_by), "blocked_by": (row.blocked_by or [None])[0]}

def _match_payload(row):
    return {"id": str(row.id), "user_1_regno": row.user_1_regno, "user_2_regno": row.user_2_regno,
        "created_at": row.created_at, "expires_at": row.expires_at}


def _require_admin(current_user: UserDetails = Depends(get_current_user)) -> UserDetails:
    """Ensure the requester has administrator privileges."""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required."
        )
    return current_user


class ConversationTerminateRequest(BaseModel):
    reason: Optional[str] = None


@router.get("/confessions", response_model=List[Dict[str, Any]])
async def get_all_confessions(
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> List[Dict[str, Any]]:
    """Return every confession with sender information for admin review."""
    return get_all_confessions_data(db)


@router.delete("/confessions/{confession_id}")
async def delete_confession(
    confession_id: str,
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> Dict[str, Any]:
    """Remove a confession permanently."""
    if not is_valid_id(confession_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid confession id")

    with db.sessions.begin() as session:
        confession = session.get(ConfessionRecord, str(confession_id))
        if confession: session.delete(confession)
    if not confession:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Confession not found")
    return {"deleted": confession_id}


@router.get("/users", response_model=List[Dict[str, Any]])
async def get_all_users(
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> List[Dict[str, Any]]:
    """Return every user profile for review."""
    with db.sessions() as session:
        users = session.scalars(select(UserRecord).order_by(UserRecord.name.asc())).all()
        return [serialize_user_doc(_user_payload(user)) for user in users]


@router.post("/users", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: AdminUserCreate,
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> Dict[str, Any]:
    """Allow admins to create a brand new user or admin account."""
    normalized_regno = payload.regno.strip()
    normalized_email = payload.email.strip().lower()

    with db.sessions() as session:
        conflict = session.scalar(select(UserRecord.id).where(
            (UserRecord.regno == normalized_regno) | (UserRecord.email == normalized_email)
        ).limit(1))
    if conflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this registration number or email already exists."
        )

    interests_value: Optional[List[str]] = None
    if payload.interests:
        normalized_interests = [item.strip() for item in payload.interests if item and item.strip()]
        if normalized_interests:
            interests_value = normalized_interests

    with db.sessions.begin() as session:
        user = UserRecord(
            regno=normalized_regno, name=payload.name.strip(), email=normalized_email,
            emoji=payload.emoji.strip() if payload.emoji else None,
            bio=payload.bio.strip() if payload.bio else None, class_name=payload.class_name.strip(),
            gender=payload.gender.strip(),
            profile_picture_url=payload.profile_picture_url.strip() if payload.profile_picture_url else None,
            interests=interests_value or [], matchmaking_enabled=payload.matchmaking_enabled,
            notifications_enabled=payload.notifications_enabled, love_notes_receive_enabled=payload.love_notes_receive_enabled,
            love_notes_send_enabled=payload.love_notes_send_enabled, role=payload.role,
        )
        session.add(user)
        session.flush()
        created_doc = _user_payload(user)
    return serialize_user_doc(created_doc)


@router.put("/users/{user_id}/block")
async def set_user_block_state(
    user_id: str,
    blocked: bool = Query(..., alias="blocked"),
    db: Database = Depends(get_db),
    current_admin: UserDetails = Depends(_require_admin)
) -> Dict[str, Any]:
    """Allow admins to block or unblock a user."""
    if not is_valid_id(user_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user id")

    if str(current_admin.id) == user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot change your own block status")

    with db.sessions.begin() as session:
        user = session.get(UserRecord, str(user_id))
        if user: user.is_blocked = blocked
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return {"id": user_id, "is_blocked": blocked}


@router.put("/users/{user_id}", response_model=Dict[str, Any])
async def update_user_details(
    user_id: str,
    update: AdminUserUpdate,
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> Dict[str, Any]:
    """Allow admins to edit a user's profile details."""
    if not is_valid_id(user_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user id")

    update_payload = {key: value for key, value in update.dict(exclude_unset=True).items()}

    if not update_payload:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No changes provided")

    with db.sessions.begin() as session:
        user = session.get(UserRecord, str(user_id))
        if user:
            for key, value in update_payload.items():
                setattr(user, key, value)
            updated_doc = _user_payload(user)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return serialize_user_doc(updated_doc)


@router.get("/conversations", response_model=List[Dict[str, Any]])
async def list_conversations(
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> List[Dict[str, Any]]:
    """Return every conversation with participant and latest message metadata."""
    return get_all_conversations_data(db)


@router.get("/conversations/{conversation_id}", response_model=Dict[str, Any])
async def get_conversation_detail(
    conversation_id: str,
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> Dict[str, Any]:
    """Return a single conversation with full participant info and message transcript."""
    if not is_valid_id(conversation_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid conversation id")

    with db.sessions() as session:
        conversation_row = session.get(ConversationRecord, str(conversation_id))
        conversation_doc = _conversation_payload(conversation_row) if conversation_row else None
    if not conversation_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    match_id = conversation_doc.get("match_id")
    match_doc = None
    if match_id is not None:
        with db.sessions() as session:
            match_row = session.get(MatchRecord, match_id)
            match_doc = _match_payload(match_row) if match_row else None

    # Get messages from the PostgreSQL messages table
    messages: List[Dict[str, Any]] = []
    with db.sessions() as session:
        raw_messages = session.scalars(select(MessageRecord).where(
            MessageRecord.conversation_id == str(conversation_id)
        ).order_by(MessageRecord.created_at.asc())).all()
    for message in raw_messages:
        messages.append(
            {
                "id": str(message.id),
                "sender_regno": message.sender_regno,
                "body": message.body,
                "created_at": serialize_datetime(message.created_at),
                "is_read": message.is_read,
            }
        )

    return {
        "id": conversation_id,
        "status": conversation_doc.get("status"),
        "created_at": serialize_datetime(conversation_doc.get("created_at")),
        "requested_at": serialize_datetime(conversation_doc.get("requested_at")),
        "accepted_at": serialize_datetime(conversation_doc.get("accepted_at")),
        "terminated_at": serialize_datetime(conversation_doc.get("terminated_at")),
        "initiator": serialize_user_by_regno(db, conversation_doc.get("initiator_regno")),
        "receiver": serialize_user_by_regno(db, conversation_doc.get("receiver_regno")),
        "match": None
        if not match_doc
        else {
            "id": str(match_doc.get("id")),
            "user_1_regno": match_doc.get("user_1_regno"),
            "user_2_regno": match_doc.get("user_2_regno"),
            "created_at": serialize_datetime(match_doc.get("created_at")),
            "expires_at": serialize_datetime(match_doc.get("expires_at")),
        },
        "is_blocked": conversation_doc.get("is_blocked", False),
        "blocked_by": conversation_doc.get("blocked_by"),
        "messages": messages,
    }


@router.post("/conversations/{conversation_id}/terminate", response_model=Dict[str, Any])
async def terminate_conversation(
    conversation_id: str,
    payload: ConversationTerminateRequest,
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> Dict[str, Any]:
    """Terminate a conversation."""
    if not is_valid_id(conversation_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid conversation id")

    with db.sessions() as session:
        conversation_row = session.get(ConversationRecord, str(conversation_id))
        conversation_doc = _conversation_payload(conversation_row) if conversation_row else None
    if not conversation_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    match_id = conversation_doc.get("match_id")
    now = datetime.now(timezone.utc)

    with db.sessions.begin() as session:
        conversation = session.get(ConversationRecord, conversation_doc["id"])
        if conversation:
            conversation.status = "terminated"
            conversation.terminated_at = now
        if match_id is not None:
            match = session.get(MatchRecord, match_id)
            if match: match.expires_at = now

    return {
        "id": conversation_id,
        "status": "terminated",
        "terminated_at": serialize_datetime(now),
    }


@router.get("/matchmaking", response_model=List[Dict[str, Any]])
async def get_matchmaking_overview(
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> List[Dict[str, Any]]:
    """Return every recorded match excluding administrators."""
    return get_matchmaking_overview_data(db)


@router.put("/matchmaking/{match_id}/status")
async def update_matchmaking_status(
    match_id: str,
    status_value: str = Query(..., alias="status", regex="^(approved|rejected|pending)$"),
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> Dict[str, Any]:
    """Moderate a matchmaking outcome by updating the linked conversation state."""
    if not is_valid_id(match_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid match id")

    with db.sessions() as session:
        match_row = session.get(MatchRecord, str(match_id))
        match_obj = _match_payload(match_row) if match_row else None
    if not match_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found")

    with db.sessions() as session:
        conversation_row = session.scalar(select(ConversationRecord).where(ConversationRecord.match_id == str(match_id)))
        conversation_doc = _conversation_payload(conversation_row) if conversation_row else None
    if not conversation_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found for match")

    status_mapping = {
        "approved": "accepted",
        "rejected": "rejected",
        "pending": "pending",
    }
    new_status = status_mapping[status_value]

    update_payload: Dict[str, Any] = {"status": new_status}
    if new_status == "accepted":
        update_payload["accepted_at"] = datetime.now(timezone.utc)
    else:
        update_payload["accepted_at"] = None

    with db.sessions.begin() as session:
        updated_conversation_row = session.get(ConversationRecord, conversation_doc["id"])
        if updated_conversation_row:
            updated_conversation_row.status = new_status
            updated_conversation_row.accepted_at = update_payload["accepted_at"]
            updated_conversation = _conversation_payload(updated_conversation_row)
        else:
            updated_conversation = None
    if not updated_conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found for match")

    response_status = normalize_conversation_status(updated_conversation.get("status"))

    return {
        "match_id": match_id,
        "status": response_status,
        "conversation": {
            "id": str(updated_conversation.get("id")),
            "status": updated_conversation.get("status"),
            "requested_at": serialize_datetime(updated_conversation.get("requested_at")),
            "accepted_at": serialize_datetime(updated_conversation.get("accepted_at")),
            "initiator": updated_conversation.get("initiator_regno"),
            "receiver": updated_conversation.get("receiver_regno"),
        },
    }


@router.get("/stats", response_model=Dict[str, Any])
async def get_admin_statistics(
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> Dict[str, Any]:
    """Provide high-level metrics for the admin dashboard."""
    return get_admin_statistics_data(db)


@router.get("/stats/active-sessions", response_model=Dict[str, Any])
async def list_active_sessions(
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> Dict[str, Any]:
    """Return details about users with recent activity for the active sessions dialog."""
    sessions = collect_active_sessions(db)

    unique_user_ids = {session.get("user", {}).get("id") for session in sessions if session.get("user")}
    unique_user_ids.discard(None)

    return {
        "count": len(sessions),
        "unique_user_count": len(unique_user_ids),
        "sessions": sessions,
        "window_hours": 24,
    }


# ----------------------
# Message Management
# ----------------------

@router.post("/messages/archive")
async def archive_old_messages(
    hours_old: int = Query(4, ge=1, le=168, description="Archive messages older than this many hours"),
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> Dict[str, Any]:
    """
    Archive old messages to compressed JSON files and remove from PostgreSQL
    
    Default: Archives messages older than 4 hours
    """
    from ..services.message_archival_service import message_archival_service
    
    result = message_archival_service.archive_old_messages(db, hours_old=hours_old)
    return result


@router.get("/messages/archives")
async def list_message_archives(
    _: UserDetails = Depends(_require_admin)
) -> Dict[str, Any]:
    """List all archived message files"""
    from ..services.message_archival_service import message_archival_service
    
    archives = message_archival_service.list_archives()
    
    total_size_kb = sum(archive["size_kb"] for archive in archives)
    
    return {
        "archives": archives,
        "count": len(archives),
        "total_size_kb": round(total_size_kb, 2),
        "total_size_mb": round(total_size_kb / 1024, 2)
    }


@router.get("/messages/archives/{date}")
async def get_archived_messages(
    date: str,
    user_regno: Optional[str] = Query(None, description="Filter by user regno"),
    conversation_id: Optional[str] = Query(None, description="Filter by conversation ID"),
    _: UserDetails = Depends(_require_admin)
) -> Dict[str, Any]:
    """
    Get archived messages for a specific date
    
    Date format: YYYY-MM-DD
    """
    from ..services.message_archival_service import message_archival_service
    
    messages = message_archival_service.get_archived_messages(
        date=date,
        user_regno=user_regno,
        conversation_id=conversation_id
    )
    
    return {
        "date": date,
        "count": len(messages),
        "messages": messages
    }


@router.get("/messages/reports")
async def get_message_reports(
    status_filter: Optional[str] = Query(None, description="Filter by status: pending, reviewed, dismissed"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> Dict[str, Any]:
    """Get all message reports with optional status filter"""
    
    filters = []
    if status_filter:
        filters.append(MessageReportRecord.status == status_filter)
    with db.sessions() as session:
        reports = session.scalars(select(MessageReportRecord).where(*filters)
            .order_by(MessageReportRecord.reported_at.desc()).offset(skip).limit(limit)).all()
        total = session.scalar(select(func.count()).select_from(MessageReportRecord).where(*filters)) or 0
    
    # Enrich with user and message details
    enriched_reports = []
    with db.sessions() as session:
        regnos = {report.reporter_regno for report in reports} | {report.reported_user_regno for report in reports}
        users = {user.regno: user for user in session.scalars(select(UserRecord).where(UserRecord.regno.in_(regnos))).all()} if regnos else {}
        message_ids = {report.message_id for report in reports}
        messages = {str(message.id): message for message in session.scalars(select(MessageRecord).where(MessageRecord.id.in_(message_ids))).all()} if message_ids else {}
    for report in reports:
        reporter = users.get(report.reporter_regno)
        reported_user = users.get(report.reported_user_regno)
        message = messages.get(str(report.message_id))
        
        enriched_reports.append({
            "id": str(report.id),
            "message_id": str(report.message_id),
            "body": message.body if message else "[Message not found]",
            "conversation_id": str(message.conversation_id) if message else None,
            "reporter": {
                "regno": report.reporter_regno,
                "name": reporter.name if reporter else "Unknown"
            },
            "reported_user": {
                "regno": report.reported_user_regno,
                "name": reported_user.name if reported_user else "Unknown"
            },
            "reason": report.reason,
            "reported_at": serialize_datetime(report.reported_at),
            "status": report.status
        })
    
    return {
        "reports": enriched_reports,
        "total": total,
        "skip": skip,
        "limit": limit
    }


@router.patch("/messages/reports/{report_id}")
async def update_message_report_status(
    report_id: str,
    new_status: str = Query(..., regex="^(reviewed|dismissed)$"),
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> Dict[str, Any]:
    """Update the status of a message report"""
    
    try:
        with db.sessions.begin() as session:
            report = session.get(MessageReportRecord, str(report_id))
            if report:
                report.status = new_status
                report.reviewed_at = datetime.now(timezone.utc)
                report.reviewed_by_regno = _.regno if _ else None
        if not report:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Report not found"
            )
        
        return {"message": f"Report status updated to {new_status}"}
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid report ID: {str(e)}"
        )
