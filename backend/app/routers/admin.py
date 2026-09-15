from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy import func, select

from ..postgres import new_id, is_valid_id
from fastapi import APIRouter, Depends, HTTPException, Query, status
from ..postgres import Database
from pydantic import BaseModel

from ..dependencies import get_db
from ..models import AccountDetails, AdminUserCreate, AdminUserUpdate
from ..db_models import (ACCOUNT_MODELS, ConfessionRecord, ConversationRecord, MatchRecord,
    MessageRecord, MessageReportRecord, UserRecord, find_account_in_table,
    resolve_account_by_id)
from ..accounts import AccountType, Capability, normalize_account_type
from ..services.authorization import require_capability
from ..services.admin_service import (
    serialize_datetime,
    serialize_user_doc,
    serialize_account_by_regno,
    _account_row_payload,
    collect_active_sessions,
    get_all_confessions_data,
    get_confession_reports_data,
    update_confession_report_status,
    get_all_conversations_data,
    get_admin_statistics_data,
)

router = APIRouter(prefix="/admin", tags=["Admin"])

# Every account the list can hold, newest-looking first. Ordered explicitly so the response
# is stable across calls: three unordered SELECTs concatenated would otherwise reshuffle the
# page whenever PostgreSQL chose a different plan, and the admin would have to re-find what
# they were looking at after an unrelated edit to an unrelated account.
_ACCOUNT_TABLE_ORDER = (AccountType.ADMIN, AccountType.MODERATOR, AccountType.USERS)

def _user_payload(user):
    if user is None: return None
    return {
        "id": str(user.id), "regno": user.regno, "name": user.name, "email": user.email,
        "emoji": user.emoji, "bio": user.bio, "class_name": user.class_name, "gender": user.gender,
        "profile_picture_url": user.profile_picture_url, "interests": user.interests or [],
        "account_type": AccountType.USERS.value,
        "matchmaking_enabled": user.matchmaking_enabled,
        "notifications_enabled": user.notifications_enabled, "love_notes_receive_enabled": user.love_notes_receive_enabled,
        "love_notes_send_enabled": user.love_notes_send_enabled,
        "last_login_at": user.last_login_at, "last_login_ip": str(user.last_login_ip) if user.last_login_ip else None,
        "last_login_user_agent": user.last_login_user_agent, "last_matchmaking_at": user.last_matchmaking_at,
        "created_at": user.created_at, "updated_at": user.updated_at,
        "is_blocked": user.is_blocked,
    }

def _conversation_payload(row):
    return {"id": str(row.id), "match_id": str(row.match_id), "initiator_regno": row.initiator_regno,
        "receiver_regno": row.receiver_regno, "status": row.status, "created_at": row.created_at,
        "requested_at": row.requested_at, "accepted_at": row.accepted_at, "terminated_at": row.terminated_at,
        "is_blocked": bool(row.blocked_by), "blocked_by": (row.blocked_by or [None])[0]}

def _match_payload(row):
    return {"id": str(row.id), "user_1_regno": row.user_1_regno, "user_2_regno": row.user_2_regno,
        "created_at": row.created_at, "expires_at": row.expires_at}


class ConversationTerminateRequest(BaseModel):
    reason: Optional[str] = None


@router.get("/confessions", response_model=List[Dict[str, Any]])
async def get_all_confessions(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.CONFESSIONS_REVIEW))
) -> List[Dict[str, Any]]:
    """Return every confession for admin review.

    Everything a reviewer acts on is here -- body, timestamps, reports, comments, engagement --
    with one exception: the sender of an anonymous confession. That identity is withheld for
    staff exactly as it is for users, and the redaction happens in the service so it is not
    merely absent from the screen but absent from the response.
    """
    return get_all_confessions_data(db)


@router.delete("/confessions/{confession_id}")
async def delete_confession(
    confession_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.CONFESSIONS_DELETE))
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


# The report queue is declared after ``/confessions/{confession_id}`` on purpose. FastAPI
# matches on method first, and the only sibling is a DELETE, so nothing here is shadowed
# today; keeping the literal path last means adding a ``GET /{confession_id}`` later cannot
# swallow it. These two carry the reporting capabilities rather than the confession ones, so
# that ruling on reports stays separately grantable from reviewing the posts themselves.


@router.get("/confessions/reports")
async def get_confession_reports(
    status_filter: Optional[str] = Query(
        None, alias="status", description="Filter by status: pending, reviewed, dismissed"
    ),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.REPORTS_REVIEW)),
) -> Dict[str, Any]:
    """Every report filed against a confession or one of its comments, newest first.

    Carries the reported content itself rather than an id, so the question a reviewer opens
    this screen to answer -- what did this actually say -- is answerable here. The author of
    an anonymous confession is withheld, as it is from the confession review screen: an
    anonymous post stays anonymous to staff through every door.
    """
    return get_confession_reports_data(db, status_filter=status_filter, skip=skip, limit=limit)


@router.patch("/confessions/reports/{report_id}")
async def update_confession_report(
    report_id: str,
    new_status: str = Query(..., alias="status", regex="^(reviewed|dismissed)$"),
    db: Database = Depends(get_db),
    reviewer: AccountDetails = Depends(require_capability(Capability.REPORTS_MODERATE)),
) -> Dict[str, Any]:
    """Close out a confession or comment report as reviewed or dismissed.

    Deliberately does not delete anything. Removing the reported post stays with
    ``DELETE /admin/confessions/{id}``, so this records that a human looked and the
    confession review screen remains the single place a post's fate is decided.
    """
    return update_confession_report_status(db, report_id, new_status, reviewer.regno)


@router.get("/users", response_model=List[Dict[str, Any]])
async def get_all_users(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.USERS_REVIEW))
) -> List[Dict[str, Any]]:
    """Return every account, from all three tables, for review.

    Staff and participants arrive through one endpoint because they are one population of
    accounts; the ``account_type`` on each document is which table it came from. Splitting
    the admin list per table would mean the search box on it would have to know which table
    to search, which is not a distinction a person administering the app should have to make.
    """
    with db.sessions() as session:
        docs: List[Dict[str, Any]] = []
        for account_type in _ACCOUNT_TABLE_ORDER:
            model = ACCOUNT_MODELS[account_type.value]
            rows = session.scalars(select(model).order_by(model.name.asc())).all()
            docs.extend(serialize_user_doc(_account_row_payload(account_type.value, row)) for row in rows)
        return docs


@router.post("/users", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: AdminUserCreate,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.USERS_CREATE))
) -> Dict[str, Any]:
    """Create an account in the table named by ``account_type``.

    The table is chosen by the payload, and the same payload shape creates a participant or
    a staff account: there is no separate admin-creation form or endpoint to fall out of
    step with this one. Participant-only fields are rejected by the payload validator for a
    staff account, so a caller cannot smuggle an email onto an admin row.
    """
    account_type = payload.account_type
    model = ACCOUNT_MODELS[account_type.value]
    normalized_regno = payload.regno.strip()

    # Uniqueness is enforced per table, by PostgreSQL, on the table being written to. It is
    # deliberately *not* enforced across the three tables any more: sign-in is category
    # scoped, so a regno is only ever looked up in the one table the person chose on the way
    # in, and a session is pinned to that row by primary key for its whole life. That means
    # the same regno can exist as a participant and as staff, and each sign-in page resolves
    # its own account -- which is a supported state, not corruption. Refusing it here would
    # have been a workaround for the ambiguity that no longer exists.
    with db.sessions() as session:
        if find_account_in_table(session, account_type.value, normalized_regno) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with this registration number already exists for this account type.",
            )
        # An address is unique within one account table and never across the three, for the
        # same reason regno is: the value identifies a person for contact, not a route into an
        # account, so nothing has to resolve one. The check is against the target table only.
        normalized_email = payload.email.strip().lower() if payload.email else None
        if normalized_email is not None:
            email_conflict = session.scalar(select(model.id).where(
                model.email == normalized_email
            ).limit(1))
            if email_conflict:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="An account with this email already exists for this account type.",
                )

    interests_value: Optional[List[str]] = None
    if payload.interests:
        normalized_interests = [item.strip() for item in payload.interests if item and item.strip()]
        if normalized_interests:
            interests_value = normalized_interests

    # Only the fields the target table actually has are passed to the constructor. Building
    # the kwargs from the payload with the participant keys removed -- rather than branching
    # into two constructors -- means a field added to AccountColumns lands in all three
    # tables at once, and a participant-only field can never reach a staff row.
    account_fields: Dict[str, Any] = {
        "regno": normalized_regno,
        "name": payload.name.strip(),
        "email": normalized_email,
        "profile_picture_url": payload.profile_picture_url.strip() if payload.profile_picture_url else None,
        "gender": payload.gender.strip(),
        "notifications_enabled": payload.notifications_enabled,
        "is_blocked": payload.is_blocked,
    }
    if account_type is AccountType.USERS:
        account_fields.update({
            "emoji": payload.emoji.strip() if payload.emoji else None,
            "bio": payload.bio.strip() if payload.bio else None,
            "class_name": payload.class_name.strip(),
            "interests": interests_value or [],
            "matchmaking_enabled": payload.matchmaking_enabled,
            "love_notes_receive_enabled": payload.love_notes_receive_enabled,
            "love_notes_send_enabled": payload.love_notes_send_enabled,
        })

    with db.sessions.begin() as session:
        user = model(**account_fields)
        session.add(user)
        session.flush()
        created_doc = _account_row_payload(account_type.value, user)
    return serialize_user_doc(created_doc)


@router.put("/users/{user_id}/block")
async def set_user_block_state(
    user_id: str,
    blocked: bool = Query(..., alias="blocked"),
    db: Database = Depends(get_db),
    current_admin: AccountDetails = Depends(require_capability(Capability.USERS_BLOCK))
) -> Dict[str, Any]:
    """Block or unblock an account in any of the three tables.

    Resolved by id across all tables rather than against ``users``, because a moderator can
    be blocked by an admin and the moderator's row is not in ``users``.
    """
    if not is_valid_id(user_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user id")

    if str(current_admin.id) == user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot change your own block status")

    with db.sessions.begin() as session:
        found = resolve_account_by_id(session, user_id)
        if found: found[1].is_blocked = blocked
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return {"id": user_id, "is_blocked": blocked}


@router.put("/users/{user_id}", response_model=Dict[str, Any])
async def update_user_details(
    user_id: str,
    update: AdminUserUpdate,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.USERS_UPDATE))
) -> Dict[str, Any]:
    """Edit an account that already exists, in whichever table it lives.

    The account is found by id across all three tables and edited in place. Its table is not
    editable: ``AdminUserUpdate`` has no ``account_type`` field, so a save cannot turn a
    moderator into a participant and strand the content that references it.
    """
    if not is_valid_id(user_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user id")

    update_payload = update.dict(exclude_unset=True)

    if not update_payload:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No changes provided")

    with db.sessions.begin() as session:
        found = resolve_account_by_id(session, user_id)
        if found:
            account_type, account = found
            # Participant-only keys are dropped for a staff account. The participant-shaped
            # form submits every field it knows about, and without this an edit to a
            # moderator's name would also try to write an email onto a table with no email
            # column -- and, more insidiously, would write one if the column were ever added
            # for an unrelated reason.
            # ``email`` is absent on purpose: it is a shared column, so a moderator's address
            # is editable the same way a participant's is. The rest still describe a
            # participant, and without this an edit to a moderator's name would also try to
            # write a bio onto a table that has no bio column -- and, more insidiously, would
            # write one if the column were ever added for an unrelated reason.
            participant_only = (
                "emoji", "bio", "class_name", "interests", "matchmaking_enabled",
                "love_notes_receive_enabled", "love_notes_send_enabled", "last_matchmaking_at",
            )
            if account_type != AccountType.USERS.value:
                update_payload = {
                    key: value for key, value in update_payload.items()
                    if key not in participant_only
                }
            for key, value in update_payload.items():
                setattr(account, key, value)
            updated_doc = _account_row_payload(account_type, account)
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return serialize_user_doc(updated_doc)


@router.get("/conversations", response_model=List[Dict[str, Any]])
async def list_conversations(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.CONVERSATIONS_REVIEW))
) -> List[Dict[str, Any]]:
    """Return every conversation with participant and latest message metadata."""
    return get_all_conversations_data(db)


@router.get("/conversations/{conversation_id}", response_model=Dict[str, Any])
async def get_conversation_detail(
    conversation_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.CONVERSATIONS_READ_MESSAGES))
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
        "initiator": serialize_account_by_regno(db, conversation_doc.get("initiator_regno")),
        "receiver": serialize_account_by_regno(db, conversation_doc.get("receiver_regno")),
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
    _: AccountDetails = Depends(require_capability(Capability.CONVERSATIONS_TERMINATE))
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


@router.get("/stats", response_model=Dict[str, Any])
async def get_admin_statistics(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.STATS_READ))
) -> Dict[str, Any]:
    """Provide high-level metrics for the admin dashboard."""
    return get_admin_statistics_data(db)


@router.get("/stats/active-sessions", response_model=Dict[str, Any])
async def list_active_sessions(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.SESSIONS_READ))
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
    _: AccountDetails = Depends(require_capability(Capability.MESSAGES_ARCHIVE))
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
    _: AccountDetails = Depends(require_capability(Capability.MESSAGES_ARCHIVE))
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
    _: AccountDetails = Depends(require_capability(Capability.MESSAGES_ARCHIVE))
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
    status_filter: Optional[str] = Query(
        None, alias="status", description="Filter by status: pending, reviewed, dismissed"
    ),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.REPORTS_REVIEW))
) -> Dict[str, Any]:
    """Every reported chat message, newest first, with the message and both parties.

    Shares its ``{reports, total, skip, limit}`` envelope and its status/audit fields with the
    confession and love-note queues, because the three are rendered by one screen. The rows
    themselves stay their own shapes -- ``message_id`` and ``body`` here, ``confession_id`` and
    ``content`` there -- because a message is not a confession and flattening the difference
    would mean the payload lied about one of them.

    The wire name is ``status``, aliased onto ``status_filter`` so the parameter does not
    shadow the ``fastapi.status`` module used for HTTP codes in this router. It used to arrive
    as ``status_filter``, which was the odd one out among the queues; nothing called this
    endpoint before the unified Reports Review screen, so renaming it cost nothing and leaves
    all three queues speaking the same word for the same filter.
    """
    filters = []
    if status_filter:
        filters.append(MessageReportRecord.status == status_filter)
    with db.sessions() as session:
        reports = session.scalars(select(MessageReportRecord).where(*filters)
            .order_by(MessageReportRecord.reported_at.desc()).offset(skip).limit(limit)).all()
        total = session.scalar(select(func.count()).select_from(MessageReportRecord).where(*filters)) or 0

        # Two bulk reads rather than two per row: a queue grows with every report filed, so
        # the per-row lookup would be a cost paid on every page a moderator opens.
        regnos = {report.reporter_regno for report in reports} | {report.reported_user_regno for report in reports}
        users = {user.regno: user for user in session.scalars(select(UserRecord).where(UserRecord.regno.in_(regnos))).all()} if regnos else {}
        message_ids = {report.message_id for report in reports}
        messages = {str(message.id): message for message in session.scalars(select(MessageRecord).where(MessageRecord.id.in_(message_ids))).all()} if message_ids else {}

    enriched_reports = []
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
            "status": report.status,
            # Carried so the unified screen can show who closed a report and when without
            # a second request, matching the other two queues.
            "reviewed_at": serialize_datetime(report.reviewed_at),
            "reviewed_by_regno": report.reviewed_by_regno,
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
    new_status: str = Query(..., alias="status", regex="^(reviewed|dismissed)$"),
    db: Database = Depends(get_db),
    reviewer: AccountDetails = Depends(require_capability(Capability.REPORTS_MODERATE))
) -> Dict[str, Any]:
    """Close out a chat-message report as reviewed or dismissed.

    Returns ``{id, status}`` rather than a sentence, to match the other two close-out
    endpoints. Nothing is deleted here: a reported message is removed through the ordinary
    conversation endpoints, so the queue records that a human looked and does not become a
    second way to destroy history.
    """
    if not is_valid_id(report_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid report id")

    with db.sessions.begin() as session:
        report = session.get(MessageReportRecord, str(report_id))
        if report is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
        report.status = new_status
        report.reviewed_at = datetime.now(timezone.utc)
        report.reviewed_by_regno = reviewer.regno

    return {"id": str(report_id), "status": new_status}
