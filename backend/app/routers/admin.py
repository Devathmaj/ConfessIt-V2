from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pymongo.database import Database
from pydantic import BaseModel

from ..dependencies import get_db
from ..models import AdminUserCreate, AdminUserUpdate, UserDetails
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


def _require_admin(current_user: UserDetails = Depends(get_current_user)) -> UserDetails:
    """Ensure the requester has administrator privileges."""
    if current_user.user_role != "admin":
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
    if not ObjectId.is_valid(confession_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid confession id")

    result = db["Confessions"].delete_one({"_id": ObjectId(confession_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Confession not found")
    return {"deleted": confession_id}


@router.get("/users", response_model=List[Dict[str, Any]])
async def get_all_users(
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> List[Dict[str, Any]]:
    """Return every user profile for review."""
    return [
        serialize_user_doc(doc)
        for doc in db["UserDetails"].find().sort("Name", 1)
    ]


@router.post("/users", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: AdminUserCreate,
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> Dict[str, Any]:
    """Allow admins to create a brand new user or admin account."""
    normalized_regno = payload.Regno.strip()
    normalized_email = payload.email.strip().lower()

    conflict = db["UserDetails"].find_one(
        {
            "$or": [
                {"Regno": normalized_regno},
                {"email": normalized_email},
            ]
        }
    )
    if conflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this registration number or email already exists."
        )

    username_value = payload.username.strip() if payload.username else None
    if username_value:
        username_conflict = db["UserDetails"].find_one({"username": username_value})
        if username_conflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="That username is already taken."
            )

    interests_value: Optional[List[str]] = None
    if payload.interests:
        normalized_interests = [item.strip() for item in payload.interests if item and item.strip()]
        if normalized_interests:
            interests_value = normalized_interests

    new_user_doc: Dict[str, Any] = {
        "Regno": normalized_regno,
        "Name": payload.Name.strip(),
        "email": normalized_email,
        "username": username_value,
        "emoji": payload.emoji.strip() if payload.emoji else None,
        "bio": payload.bio.strip() if payload.bio else None,
        "which_class": payload.which_class.strip(),
        "gender": payload.gender.strip(),
        "profile_picture_id": payload.profile_picture_id.strip() if payload.profile_picture_id else None,
        "interests": interests_value or [],
        "isMatchmaking": payload.isMatchmaking,
        "isNotifications": payload.isNotifications,
        "isLovenotesRecieve": payload.isLovenotesRecieve,
        "isLovenotesSend": payload.isLovenotesSend,
        "user_role": payload.user_role,
        "reported_count": 0,
        "is_blocked": False,
        "last_login_time": None,
        "last_login_ip": None,
        "last_matchmaking_time": None,
    }

    insert_result = db["UserDetails"].insert_one(new_user_doc)
    created_doc = db["UserDetails"].find_one({"_id": insert_result.inserted_id})
    return serialize_user_doc(created_doc)


@router.put("/users/{user_id}/block")
async def set_user_block_state(
    user_id: str,
    blocked: bool = Query(..., alias="blocked"),
    db: Database = Depends(get_db),
    current_admin: UserDetails = Depends(_require_admin)
) -> Dict[str, Any]:
    """Allow admins to block or unblock a user."""
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user id")

    if str(current_admin.id) == user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot change your own block status")

    result = db["UserDetails"].update_one({"_id": ObjectId(user_id)}, {"$set": {"is_blocked": blocked}})
    if result.matched_count == 0:
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
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user id")

    update_payload = {key: value for key, value in update.dict(exclude_unset=True).items()}

    if not update_payload:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No changes provided")

    result = db["UserDetails"].update_one({"_id": ObjectId(user_id)}, {"$set": update_payload})
    if result.matched_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    updated_doc = db["UserDetails"].find_one({"_id": ObjectId(user_id)})
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
    if not ObjectId.is_valid(conversation_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid conversation id")

    conversation_doc = db["conversations"].find_one({"_id": ObjectId(conversation_id)})
    if not conversation_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    match_id = conversation_doc.get("matchId")
    match_doc = None
    if isinstance(match_id, ObjectId):
        match_doc = db["matches"].find_one({"_id": match_id})

    # Get messages from MongoDB messages collection
    messages: List[Dict[str, Any]] = []
    raw_messages = db["messages"].find(
        {"conversation_id": ObjectId(conversation_id)},
        sort=[("timestamp", 1)]
    )
    for message in raw_messages:
        messages.append(
            {
                "id": str(message.get("_id")),
                "sender_id": message.get("sender_id"),
                "text": message.get("text"),
                "timestamp": serialize_datetime(message.get("timestamp")),
                "is_read": message.get("is_read", False),
            }
        )

    return {
        "id": conversation_id,
        "status": conversation_doc.get("status"),
        "created_at": serialize_datetime(conversation_doc.get("createdAt")),
        "requested_at": serialize_datetime(conversation_doc.get("requestedAt")),
        "accepted_at": serialize_datetime(conversation_doc.get("acceptedAt")),
        "terminated_at": serialize_datetime(conversation_doc.get("terminatedAt")),
        "initiator": serialize_user_by_regno(db, conversation_doc.get("initiatorId")),
        "receiver": serialize_user_by_regno(db, conversation_doc.get("receiverId")),
        "match": None
        if not match_doc
        else {
            "id": str(match_doc.get("_id")),
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
    if not ObjectId.is_valid(conversation_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid conversation id")

    conversation_doc = db["conversations"].find_one({"_id": ObjectId(conversation_id)})
    if not conversation_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    match_id = conversation_doc.get("matchId")
    now = datetime.now(timezone.utc)

    db["conversations"].update_one(
        {"_id": conversation_doc["_id"]},
        {"$set": {"status": "terminated", "terminatedAt": now}}
    )

    if isinstance(match_id, ObjectId):
        db["matches"].update_one(
            {"_id": match_id},
            {"$set": {"expires_at": now}}
        )

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
    if not ObjectId.is_valid(match_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid match id")

    match_obj = db["matches"].find_one({"_id": ObjectId(match_id)})
    if not match_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found")

    conversation_doc = db["conversations"].find_one({"matchId": ObjectId(match_id)})
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
        update_payload["acceptedAt"] = datetime.now(timezone.utc)
    else:
        update_payload["acceptedAt"] = None

    db["conversations"].update_one({"_id": conversation_doc["_id"]}, {"$set": update_payload})
    updated_conversation = db["conversations"].find_one({"_id": conversation_doc["_id"]})

    response_status = normalize_conversation_status(updated_conversation.get("status"))

    return {
        "match_id": match_id,
        "status": response_status,
        "conversation": {
            "id": str(updated_conversation.get("_id")),
            "status": updated_conversation.get("status"),
            "requested_at": serialize_datetime(updated_conversation.get("requestedAt")),
            "accepted_at": serialize_datetime(updated_conversation.get("acceptedAt")),
            "initiator": updated_conversation.get("initiatorId"),
            "receiver": updated_conversation.get("receiverId"),
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
    Archive old messages to compressed JSON files and remove from MongoDB
    
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
    user_regno: Optional[str] = Query(None, description="Filter by user Regno"),
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
    
    query = {}
    if status_filter:
        query["status"] = status_filter
    
    reports = list(
        db["message_reports"]
        .find(query)
        .sort("reported_at", -1)
        .skip(skip)
        .limit(limit)
    )
    
    total = db["message_reports"].count_documents(query)
    
    # Enrich with user and message details
    enriched_reports = []
    for report in reports:
        # Get reporter info
        reporter = db["UserDetails"].find_one({"Regno": report["reporter_id"]})
        
        # Get reported user info
        reported_user = db["UserDetails"].find_one({"Regno": report["reported_user_id"]})
        
        # Get message
        message = db["messages"].find_one({"_id": report["message_id"]})
        
        enriched_reports.append({
            "id": str(report["_id"]),
            "message_id": str(report["message_id"]),
            "message_text": message["text"] if message else "[Message not found]",
            "conversation_id": str(report["conversation_id"]),
            "reporter": {
                "regno": report["reporter_id"],
                "name": reporter["Name"] if reporter else "Unknown"
            },
            "reported_user": {
                "regno": report["reported_user_id"],
                "name": reported_user["Name"] if reported_user else "Unknown"
            },
            "reason": report["reason"],
            "reported_at": serialize_datetime(report["reported_at"]),
            "status": report.get("status", "pending")
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
        result = db["message_reports"].update_one(
            {"_id": ObjectId(report_id)},
            {"$set": {"status": new_status, "reviewed_at": datetime.utcnow()}}
        )
        
        if result.matched_count == 0:
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
