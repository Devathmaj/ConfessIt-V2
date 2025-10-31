from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pymongo.database import Database
from pydantic import BaseModel

from ..dependencies import get_db
from ..models import AdminUserCreate, AdminUserUpdate, UserDetails
from ..services.auth_service import get_current_user
from ..services.supabase_service import supabase_service

router = APIRouter(prefix="/admin", tags=["Admin"])


def _require_admin(current_user: UserDetails = Depends(get_current_user)) -> UserDetails:
    """Ensure the requester has administrator privileges."""
    if current_user.user_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required."
        )
    return current_user


def _serialize_datetime(value: Optional[datetime]) -> Optional[str]:
    if isinstance(value, datetime):
        # Normalise to ISO format for consistent client handling
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return None


def _serialize_user_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    if not doc:
        return {}

    return {
        "id": str(doc.get("_id")),
        "Regno": doc.get("Regno"),
        "Name": doc.get("Name"),
        "email": doc.get("email"),
        "username": doc.get("username"),
        "emoji": doc.get("emoji"),
        "bio": doc.get("bio"),
        "which_class": doc.get("which_class"),
        "gender": doc.get("gender"),
        "profile_picture_id": doc.get("profile_picture_id"),
        "interests": doc.get("interests", []),
        "user_role": doc.get("user_role", "user"),
        "isMatchmaking": doc.get("isMatchmaking", False),
        "isNotifications": doc.get("isNotifications", True),
        "isLovenotesRecieve": doc.get("isLovenotesRecieve", True),
        "isLovenotesSend": doc.get("isLovenotesSend", False),
        "reported_count": doc.get("reported_count", 0),
        "last_login_time": _serialize_datetime(doc.get("last_login_time")),
        "last_login_ip": doc.get("last_login_ip"),
        "last_login_user_agent": doc.get("last_login_user_agent"),
        "last_matchmaking_time": _serialize_datetime(doc.get("last_matchmaking_time")),
        "is_blocked": doc.get("is_blocked", False),
    }


def _collect_active_sessions(db: Database, window_hours: int = 24) -> List[Dict[str, Any]]:
    now = datetime.utcnow()
    active_threshold = now - timedelta(hours=window_hours)

    tokens_collection = db["LoginTokens"]

    # Mark previously-active sessions that are past the window as expired.
    tokens_collection.update_many(
        {
            "metadata.status": "active",
            "consumed_at": {"$lt": active_threshold},
        },
        {"$set": {"metadata.status": "expired"}}
    )

    active_tokens = list(
        tokens_collection.find(
            {
                "used": True,
                "revoked": False,
                "consumed_at": {"$gte": active_threshold},
            }
        )
    )

    if not active_tokens:
        return []

    user_ids: List[ObjectId] = []
    for token in active_tokens:
        raw_user_id = token.get("user_id")
        if isinstance(raw_user_id, str) and ObjectId.is_valid(raw_user_id):
            user_ids.append(ObjectId(raw_user_id))

    user_map: Dict[str, Dict[str, Any]] = {}
    if user_ids:
        user_docs = db["UserDetails"].find({"_id": {"$in": user_ids}})
        user_map = {str(doc.get("_id")): doc for doc in user_docs}

    session_entries: Dict[str, Dict[str, Any]] = {}
    for token in active_tokens:
        metadata = (token.get("metadata") or {}).copy()
        status = metadata.get("status") or "active"
        if status != "active":
            # Skip expired/inactive sessions in the active list
            continue

        raw_user_id = token.get("user_id")
        user_doc = user_map.get(raw_user_id) if raw_user_id else None

        if "status" not in metadata:
            metadata["status"] = "active"
        if "device" not in metadata:
            metadata["device"] = token.get("consume_user_agent") or token.get("request_user_agent")

        last_seen_value = metadata.get("last_seen") or token.get("consumed_at") or datetime.utcnow()
        if isinstance(last_seen_value, str):
            try:
                parsed_last_seen = datetime.fromisoformat(last_seen_value.replace("Z", "+00:00"))
                if parsed_last_seen.tzinfo is not None:
                    parsed_last_seen = parsed_last_seen.astimezone(timezone.utc).replace(tzinfo=None)
            except ValueError:
                parsed_last_seen = datetime.utcnow()
        elif isinstance(last_seen_value, datetime):
            parsed_last_seen = last_seen_value
        else:
            parsed_last_seen = datetime.utcnow()

        metadata["last_seen"] = parsed_last_seen

        tokens_collection.update_one(
            {"_id": token["_id"]},
            {"$set": {"metadata": metadata}}
        )

        serialized_user = _serialize_user_doc(user_doc) if user_doc else {
            "id": raw_user_id,
            "Name": None,
            "email": None,
            "Regno": None,
            "username": None,
            "user_role": None,
            "is_blocked": False,
        }
        if not serialized_user.get("id"):
            serialized_user["id"] = raw_user_id or str(token.get("_id"))

        iso_last_seen = _serialize_datetime(parsed_last_seen.replace(tzinfo=timezone.utc))

        entry = {
            "session_id": str(token.get("_id")),
            "status": status,
            "last_seen": iso_last_seen,
            "ip": token.get("consume_ip") or token.get("request_ip"),
            "device": metadata.get("device") or token.get("consume_user_agent") or token.get("request_user_agent"),
            "user": serialized_user,
            "_last_seen_dt": parsed_last_seen,
        }

        account_key = serialized_user.get("id") or str(token.get("_id"))
        existing_entry = session_entries.get(account_key)
        if not existing_entry or entry["_last_seen_dt"] > existing_entry["_last_seen_dt"]:
            session_entries[account_key] = entry

    results: List[Dict[str, Any]] = []
    for value in session_entries.values():
        value.pop("_last_seen_dt", None)
        results.append(value)

    results.sort(key=lambda item: item.get("last_seen") or "", reverse=True)
    return results


def _normalize_conversation_status(value: Optional[str]) -> str:
    if value == "accepted":
        return "approved"
    if value == "rejected":
        return "rejected"
    return "pending"


def _serialize_user_by_regno(db: Database, regno: Optional[str]) -> Dict[str, Any]:
    if not regno:
        return {"Regno": None}

    doc = db["UserDetails"].find_one({"Regno": regno})
    if doc:
        return _serialize_user_doc(doc)
    return {"Regno": regno, "Name": None, "email": None, "user_role": None}


class ConversationTerminateRequest(BaseModel):
    reason: Optional[str] = None


@router.get("/confessions", response_model=List[Dict[str, Any]])
async def get_all_confessions(
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> List[Dict[str, Any]]:
    """Return every confession with sender information for admin review."""
    confessions_collection = db["Confessions"]
    users_collection = db["UserDetails"]
    comments_collection = db["ConfessionComments"]
    reports_collection = db["Reports"]

    reaction_keys = ["heart", "haha", "whoa", "heartbreak"]
    items: List[Dict[str, Any]] = []

    confession_docs = list(confessions_collection.find().sort("timestamp", -1))
    confession_ids = [str(doc.get("_id")) for doc in confession_docs]

    comments_by_confession: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    comment_ids: List[str] = []
    if confession_ids:
        for comment_doc in comments_collection.find({"confession_id": {"$in": confession_ids}}):
            confession_id = comment_doc.get("confession_id")
            comments_by_confession[confession_id].append(comment_doc)
            comment_ids.append(str(comment_doc.get("_id")))

    reports_by_content: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    report_ids_to_fetch = confession_ids + comment_ids
    if report_ids_to_fetch:
        for report_doc in reports_collection.find({"content_id": {"$in": report_ids_to_fetch}}):
            content_id = report_doc.get("content_id")
            reports_by_content[content_id].append(report_doc)

    for doc in confession_docs:
        raw_user_id = doc.get("user_id")
        sender_doc = None
        if isinstance(raw_user_id, str) and ObjectId.is_valid(raw_user_id):
            sender_doc = users_collection.find_one({"_id": ObjectId(raw_user_id)})

        sender_info = {
            "id": str(sender_doc.get("_id")) if sender_doc else None,
            "name": sender_doc.get("Name") if sender_doc else None,
            "regno": sender_doc.get("Regno") if sender_doc else None,
            "email": sender_doc.get("email") if sender_doc else None,
        }

        confession_id = str(doc.get("_id"))
        reactions = {key: doc.get("reactions", {}).get(key, []) for key in reaction_keys}
        confession_reports_docs = sorted(
            reports_by_content.get(confession_id, []),
            key=lambda item: item.get("timestamp") or datetime.min,
            reverse=True
        )
        confession_reports = [
            {
                "id": str(report.get("_id")),
                "reason": report.get("reason"),
                "timestamp": _serialize_datetime(report.get("timestamp")),
                "reporter": {
                    "id": report.get("reported_by_id"),
                    "name": report.get("reported_by_name"),
                },
            }
            for report in confession_reports_docs
        ]

        comment_entries: List[Dict[str, Any]] = []
        comment_docs = sorted(
            comments_by_confession.get(confession_id, []),
            key=lambda item: item.get("timestamp") or datetime.min,
            reverse=True,
        )
        for comment_doc in comment_docs:
            comment_id = str(comment_doc.get("_id"))
            comment_reports_docs = sorted(
                reports_by_content.get(comment_id, []),
                key=lambda item: item.get("timestamp") or datetime.min,
                reverse=True,
            )
            comment_reports = [
                {
                    "id": str(report.get("_id")),
                    "reason": report.get("reason"),
                    "timestamp": _serialize_datetime(report.get("timestamp")),
                    "reporter": {
                        "id": report.get("reported_by_id"),
                        "name": report.get("reported_by_name"),
                    },
                }
                for report in comment_reports_docs
            ]

            comment_entries.append(
                {
                    "id": comment_id,
                    "message": comment_doc.get("message", ""),
                    "timestamp": _serialize_datetime(comment_doc.get("timestamp")),
                    "user": comment_doc.get("user_info", {}),
                    "report_count": comment_doc.get("report_count", 0),
                    "reported_by": [str(item) for item in comment_doc.get("reported_by", [])],
                    "like_count": comment_doc.get("like_count", 0),
                    "dislike_count": comment_doc.get("dislike_count", 0),
                    "reports": comment_reports,
                }
            )

        items.append(
            {
                "id": confession_id,
                "confession": doc.get("confession", ""),
                "confessing_to": doc.get("confessing_to"),
                "is_anonymous": doc.get("is_anonymous", True),
                "is_comment": doc.get("is_comment", True),
                "timestamp": _serialize_datetime(doc.get("timestamp")),
                "sender": sender_info,
                "report_count": doc.get("report_count", 0),
                "reported_by": [str(item) for item in doc.get("reported_by", [])],
                "reports": confession_reports,
                "reactions": {key: [str(user) for user in value] for key, value in reactions.items()},
                "heart_count": doc.get("heart_count", 0),
                "haha_count": doc.get("haha_count", 0),
                "whoa_count": doc.get("whoa_count", 0),
                "heartbreak_count": doc.get("heartbreak_count", 0),
                "comment_count": doc.get("comment_count", 0),
                "comments": comment_entries,
            }
        )

    return items


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


@router.get("/love-notes", response_model=List[Dict[str, Any]])
async def get_all_love_notes(
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> List[Dict[str, Any]]:
    """Return every love note with full sender and recipient details."""
    notes_collection = db["LoveNotes"]
    users_collection = db["UserDetails"]

    items: List[Dict[str, Any]] = []
    for doc in notes_collection.find().sort("created_at", -1):
        sender_doc = users_collection.find_one({"_id": doc.get("sender_id")})
        recipient_doc = users_collection.find_one({"_id": doc.get("recipient_id")})
        items.append(
            {
                "id": str(doc.get("_id")),
                "sender": {
                    "id": str(doc.get("sender_id")) if doc.get("sender_id") else None,
                    "name": sender_doc.get("Name") if sender_doc else None,
                    "regno": sender_doc.get("Regno") if sender_doc else None,
                    "email": sender_doc.get("email") if sender_doc else None,
                },
                "recipient": {
                    "id": str(doc.get("recipient_id")) if doc.get("recipient_id") else None,
                    "name": recipient_doc.get("Name") if recipient_doc else None,
                    "regno": recipient_doc.get("Regno") if recipient_doc else None,
                    "email": recipient_doc.get("email") if recipient_doc else None,
                },
                "image_url": doc.get("image_base64"),
                "message_text": doc.get("message_text", ""),
                "is_anonymous": doc.get("is_anonymous", False),
                "status": doc.get("status", "pending_review"),
                "created_at": _serialize_datetime(doc.get("created_at")),
                "read_at": _serialize_datetime(doc.get("read_at")),
            }
        )
    return items


@router.put("/love-notes/{note_id}/status")
async def update_love_note_status(
    note_id: str,
    status_value: str = Query(..., alias="status", regex="^(approved|rejected|pending_review)$"),
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> Dict[str, Any]:
    """Approve or reject a love note."""
    if not ObjectId.is_valid(note_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid love note id")

    result = db["LoveNotes"].update_one({"_id": ObjectId(note_id)}, {"$set": {"status": status_value}})
    if result.matched_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Love note not found")
    return {"id": note_id, "status": status_value}


@router.delete("/love-notes/{note_id}")
async def delete_love_note(
    note_id: str,
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> Dict[str, Any]:
    """Delete a love note from the system."""
    if not ObjectId.is_valid(note_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid love note id")

    result = db["LoveNotes"].delete_one({"_id": ObjectId(note_id)})
    if result.deleted_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Love note not found")
    return {"deleted": note_id}


@router.get("/users", response_model=List[Dict[str, Any]])
async def get_all_users(
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> List[Dict[str, Any]]:
    """Return every user profile for review."""
    users_collection = db["UserDetails"]
    return [
        _serialize_user_doc(doc)
        for doc in users_collection.find().sort("Name", 1)
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
    return _serialize_user_doc(created_doc)


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
    return _serialize_user_doc(updated_doc)


@router.get("/conversations", response_model=List[Dict[str, Any]])
async def list_conversations(
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> List[Dict[str, Any]]:
    """Return every conversation with participant and latest message metadata."""
    conversations_collection = db["conversations"]
    matches_collection = db["matches"]

    conversation_docs = list(conversations_collection.find().sort("createdAt", -1))

    match_ids: List[ObjectId] = []
    for doc in conversation_docs:
        match_id = doc.get("matchId")
        if isinstance(match_id, ObjectId):
            match_ids.append(match_id)

    match_map: Dict[str, Dict[str, Any]] = {}
    if match_ids:
        unique_match_ids = list({match_id for match_id in match_ids})
        match_docs = matches_collection.find({"_id": {"$in": unique_match_ids}})
        match_map = {str(item["_id"]): item for item in match_docs}

    results: List[Dict[str, Any]] = []
    for doc in conversation_docs:
        conversation_id = str(doc.get("_id"))
        match_id = doc.get("matchId")
        match_doc = match_map.get(str(match_id)) if isinstance(match_id, ObjectId) else None

        supabase_info = None
        supabase_conversation_id: Optional[str] = None
        if isinstance(match_id, ObjectId):
            supabase_info = supabase_service.get_conversation_by_match_id(str(match_id))
            supabase_conversation_id = supabase_info.get("id") if supabase_info else None

        latest_message = None
        if supabase_conversation_id:
            message = supabase_service.get_latest_message_for_conversation(supabase_conversation_id)
            if message:
                timestamp_value = message.get("timestamp") or message.get("created_at") or message.get("createdAt")
                if isinstance(timestamp_value, str):
                    try:
                        timestamp_value = datetime.fromisoformat(timestamp_value.replace("Z", "+00:00"))
                    except ValueError:
                        timestamp_value = None
                latest_message = {
                    "id": message.get("id"),
                    "sender_id": message.get("sender_id") or message.get("senderId"),
                    "text": message.get("text") or message.get("content") or message.get("message"),
                    "timestamp": _serialize_datetime(timestamp_value),
                }

        results.append(
            {
                "id": conversation_id,
                "status": doc.get("status"),
                "created_at": _serialize_datetime(doc.get("createdAt")),
                "requested_at": _serialize_datetime(doc.get("requestedAt")),
                "accepted_at": _serialize_datetime(doc.get("acceptedAt")),
                "terminated_at": _serialize_datetime(doc.get("terminatedAt")),
                "initiator": _serialize_user_by_regno(db, doc.get("initiatorId")),
                "receiver": _serialize_user_by_regno(db, doc.get("receiverId")),
                "match": None
                if not match_doc
                else {
                    "id": str(match_doc.get("_id")),
                    "user_1_regno": match_doc.get("user_1_regno"),
                    "user_2_regno": match_doc.get("user_2_regno"),
                    "created_at": _serialize_datetime(match_doc.get("created_at")),
                    "expires_at": _serialize_datetime(match_doc.get("expires_at")),
                },
                "supabase_conversation_id": supabase_conversation_id,
                "latest_message": latest_message,
                "is_blocked": supabase_info.get("is_blocked") if supabase_info else False,
                "blocked_by": supabase_info.get("blocked_by") if supabase_info else None,
            }
        )

    return results


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

    supabase_info = None
    supabase_conversation_id: Optional[str] = None
    if isinstance(match_id, ObjectId):
        supabase_info = supabase_service.get_conversation_by_match_id(str(match_id))
        supabase_conversation_id = supabase_info.get("id") if supabase_info else None

    messages: List[Dict[str, Any]] = []
    if supabase_conversation_id:
        raw_messages = supabase_service.get_messages_for_conversation(supabase_conversation_id)
        for message in raw_messages:
            timestamp_value = message.get("timestamp") or message.get("created_at") or message.get("createdAt")
            if isinstance(timestamp_value, str):
                try:
                    timestamp_value = datetime.fromisoformat(timestamp_value.replace("Z", "+00:00"))
                except ValueError:
                    timestamp_value = None
            messages.append(
                {
                    "id": message.get("id"),
                    "sender_id": message.get("sender_id") or message.get("senderId"),
                    "text": message.get("text") or message.get("content") or message.get("message"),
                    "timestamp": _serialize_datetime(timestamp_value),
                }
            )

    return {
        "id": conversation_id,
        "status": conversation_doc.get("status"),
        "created_at": _serialize_datetime(conversation_doc.get("createdAt")),
        "requested_at": _serialize_datetime(conversation_doc.get("requestedAt")),
        "accepted_at": _serialize_datetime(conversation_doc.get("acceptedAt")),
        "terminated_at": _serialize_datetime(conversation_doc.get("terminatedAt")),
        "initiator": _serialize_user_by_regno(db, conversation_doc.get("initiatorId")),
        "receiver": _serialize_user_by_regno(db, conversation_doc.get("receiverId")),
        "match": None
        if not match_doc
        else {
            "id": str(match_doc.get("_id")),
            "user_1_regno": match_doc.get("user_1_regno"),
            "user_2_regno": match_doc.get("user_2_regno"),
            "created_at": _serialize_datetime(match_doc.get("created_at")),
            "expires_at": _serialize_datetime(match_doc.get("expires_at")),
        },
        "supabase_conversation_id": supabase_conversation_id,
        "is_blocked": supabase_info.get("is_blocked") if supabase_info else False,
        "blocked_by": supabase_info.get("blocked_by") if supabase_info else None,
        "messages": messages,
    }


@router.post("/conversations/{conversation_id}/terminate", response_model=Dict[str, Any])
async def terminate_conversation(
    conversation_id: str,
    payload: ConversationTerminateRequest,
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> Dict[str, Any]:
    """Terminate a conversation and block it in Supabase."""
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
        supabase_service.terminate_conversation(str(match_id), payload.reason)

    return {
        "id": conversation_id,
        "status": "terminated",
        "terminated_at": _serialize_datetime(now),
    }


@router.get("/matchmaking", response_model=List[Dict[str, Any]])
async def get_matchmaking_overview(
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> List[Dict[str, Any]]:
    """Return every recorded match excluding administrators."""
    matches_collection = db["matches"]
    conversations_collection = db["conversations"]
    users_collection = db["UserDetails"]

    admin_regnos = {
        doc.get("Regno")
        for doc in users_collection.find({"user_role": "admin"}, {"Regno": 1})
        if doc.get("Regno")
    }

    match_filter: Dict[str, Any] = {}
    if admin_regnos:
        match_filter = {
            "$and": [
                {"user_1_regno": {"$nin": list(admin_regnos)}},
                {"user_2_regno": {"$nin": list(admin_regnos)}},
            ]
        }

    matches = list(matches_collection.find(match_filter).sort("created_at", -1))
    match_ids = [match.get("_id") for match in matches if match.get("_id")]

    regnos: set[str] = set()
    for match in matches:
        user_1 = match.get("user_1_regno")
        user_2 = match.get("user_2_regno")
        if user_1:
            regnos.add(user_1)
        if user_2:
            regnos.add(user_2)

    users_by_regno: Dict[str, Dict[str, Any]] = {}
    if regnos:
        users_by_regno = {
            doc.get("Regno"): doc
            for doc in users_collection.find({"Regno": {"$in": list(regnos)}})
        }

    conversations_by_match: Dict[str, Dict[str, Any]] = {}
    if match_ids:
        for convo in conversations_collection.find({"matchId": {"$in": match_ids}}).sort("createdAt", -1):
            match_id_value = convo.get("matchId")
            if not match_id_value:
                continue
            key = str(match_id_value)
            if key not in conversations_by_match:
                conversations_by_match[key] = convo

    results: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    for match in matches:
        match_id = match.get("_id")
        if not match_id:
            continue

        participants: List[Dict[str, Any]] = []
        skip_match = False
        for regno in [match.get("user_1_regno"), match.get("user_2_regno")]:
            if not regno:
                continue
            user_doc = users_by_regno.get(regno)
            if user_doc and user_doc.get("user_role") == "admin":
                skip_match = True
                break
            participants.append(_serialize_user_doc(user_doc) if user_doc else {"Regno": regno})
        if skip_match:
            continue

        expires_at_value = match.get("expires_at")
        expired = False
        if isinstance(expires_at_value, datetime):
            expires_at_value = expires_at_value.astimezone(timezone.utc)
            expired = expires_at_value <= now

        conversation_doc = conversations_by_match.get(str(match_id))
        raw_status = conversation_doc.get("status") if conversation_doc else None
        normalized_status = _normalize_conversation_status(raw_status)
        if expired:
            normalized_status = "expired"

        conversation_payload: Optional[Dict[str, Any]] = None
        if conversation_doc:
            conversation_payload = {
                "id": str(conversation_doc.get("_id")),
                "status": conversation_doc.get("status"),
                "requested_at": _serialize_datetime(conversation_doc.get("requestedAt")),
                "accepted_at": _serialize_datetime(conversation_doc.get("acceptedAt")),
                "initiator": conversation_doc.get("initiatorId"),
                "receiver": conversation_doc.get("receiverId"),
            }

        results.append(
            {
                "id": str(match_id),
                "created_at": _serialize_datetime(match.get("created_at")),
                "expires_at": _serialize_datetime(match.get("expires_at")),
                "expired": expired,
                "status": normalized_status,
                "participants": participants,
                "conversation": conversation_payload,
            }
        )

    return results


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

    response_status = _normalize_conversation_status(updated_conversation.get("status"))

    return {
        "match_id": match_id,
        "status": response_status,
        "conversation": {
            "id": str(updated_conversation.get("_id")),
            "status": updated_conversation.get("status"),
            "requested_at": _serialize_datetime(updated_conversation.get("requestedAt")),
            "accepted_at": _serialize_datetime(updated_conversation.get("acceptedAt")),
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
    users_collection = db["UserDetails"]
    confessions_collection = db["Confessions"]
    love_notes_collection = db["LoveNotes"]

    total_users = users_collection.count_documents({})
    total_confessions = confessions_collection.count_documents({})
    total_love_notes = love_notes_collection.count_documents({})
    pending_love_notes = love_notes_collection.count_documents({"status": "pending_review"})
    blocked_users = users_collection.count_documents({"is_blocked": True})

    active_session_entries = _collect_active_sessions(db)
    active_sessions = len(active_session_entries)

    return {
        "total_users": total_users,
        "active_sessions": active_sessions,
        "total_confessions": total_confessions,
        "total_love_notes": total_love_notes,
        "pending_love_notes": pending_love_notes,
        "blocked_users": blocked_users,
    }


@router.get("/stats/active-sessions", response_model=Dict[str, Any])
async def list_active_sessions(
    db: Database = Depends(get_db),
    _: UserDetails = Depends(_require_admin)
) -> Dict[str, Any]:
    """Return details about users with recent activity for the active sessions dialog."""
    sessions = _collect_active_sessions(db)

    unique_user_ids = {session.get("user", {}).get("id") for session in sessions if session.get("user")}
    unique_user_ids.discard(None)

    return {
        "count": len(sessions),
        "unique_user_count": len(unique_user_ids),
        "sessions": sessions,
        "window_hours": 24,
    }
