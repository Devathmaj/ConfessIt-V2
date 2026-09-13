# app/services/admin_service.py

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from sqlalchemy import func, or_, select, update

from ..postgres import new_id, is_valid_id
from ..postgres import Database
from ..db_models import (ConfessionCommentRecord, ConfessionRecord, ConversationRecord,
    LoginTokenRecord, LoveNoteRecord, MatchRecord, MessageRecord, ReportRecord, UserRecord)
from ..roles import STAFF_ROLE_VALUES, is_staff, normalize_role


def serialize_datetime(value: Optional[datetime]) -> Optional[str]:
    """Convert datetime to ISO format string."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def serialize_user_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize a user document to a JSON-friendly format."""
    if not doc:
        return {}

    return {
        "id": str(doc.get("id")),
        "regno": doc.get("regno"),
        "name": doc.get("name"),
        "email": doc.get("email"),
        "emoji": doc.get("emoji"),
        "bio": doc.get("bio"),
        "class_name": doc.get("class_name"),
        "gender": doc.get("gender"),
        "profile_picture_url": doc.get("profile_picture_url"),
        "interests": doc.get("interests", []),
        # Normalised rather than passed through: a profile document predating the role
        # constraint can still hold a value the app no longer accepts, and the admin list
        # would otherwise render a fourth role that grants nothing.
        "role": normalize_role(doc.get("role")),
        "matchmaking_enabled": doc.get("matchmaking_enabled", False),
        "notifications_enabled": doc.get("notifications_enabled", True),
        "love_notes_receive_enabled": doc.get("love_notes_receive_enabled", True),
        "love_notes_send_enabled": doc.get("love_notes_send_enabled", False),
        "reported_count": doc.get("reported_count", 0),
        "last_login_at": serialize_datetime(doc.get("last_login_at")),
        "last_login_ip": doc.get("last_login_ip"),
        "last_login_user_agent": doc.get("last_login_user_agent"),
        "last_matchmaking_at": serialize_datetime(doc.get("last_matchmaking_at")),
        "is_blocked": doc.get("is_blocked", False),
    }


def serialize_user_by_regno(db: Database, regno: Optional[str]) -> Dict[str, Any]:
    """Fetch and serialize a user by regno."""
    if not regno:
        return {"regno": None}

    with db.sessions() as session:
        user = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
        doc = None if not user else {
            "id": str(user.id), "regno": user.regno, "name": user.name, "email": user.email,
            "emoji": user.emoji, "bio": user.bio, "class_name": user.class_name,
            "gender": user.gender, "profile_picture_url": user.profile_picture_url,
            "interests": user.interests or [], "role": user.role,
            "matchmaking_enabled": user.matchmaking_enabled, "notifications_enabled": user.notifications_enabled,
            "love_notes_receive_enabled": user.love_notes_receive_enabled, "love_notes_send_enabled": user.love_notes_send_enabled,
            "reported_count": user.reported_count, "last_login_at": user.last_login_at,
            "last_login_ip": str(user.last_login_ip) if user.last_login_ip else None,
            "last_login_user_agent": user.last_login_user_agent,
            "last_matchmaking_at": user.last_matchmaking_at, "is_blocked": user.is_blocked,
        }
    if doc:
        return serialize_user_doc(doc)
    return {"regno": regno, "name": None, "email": None, "role": None}


def normalize_conversation_status(value: Optional[str]) -> str:
    """Normalize conversation status values."""
    if value == "accepted":
        return "approved"
    if value == "rejected":
        return "rejected"
    return "pending"


def collect_active_sessions(db: Database, window_hours: int = 24) -> List[Dict[str, Any]]:
    """Collect and return active user sessions within the specified time window."""
    now = datetime.now(timezone.utc)
    active_threshold = now - timedelta(hours=window_hours)
    with db.sessions.begin() as session:
        session.execute(update(LoginTokenRecord).where(
            LoginTokenRecord.session_status == "active",
            LoginTokenRecord.consumed_at < active_threshold,
        ).values(session_status="expired"))
        token_rows = session.scalars(select(LoginTokenRecord).where(
            LoginTokenRecord.used.is_(True), LoginTokenRecord.revoked.is_(False),
            LoginTokenRecord.consumed_at >= active_threshold,
        )).all()
        active_tokens = [{
            "id": str(row.id), "user_id": str(row.user_id), "session_status": row.session_status,
            "last_seen_at": row.last_seen_at, "consumed_at": row.consumed_at,
            "consume_ip": str(row.consume_ip) if row.consume_ip else None,
            "request_ip": str(row.request_ip) if row.request_ip else None,
            "device": row.device, "consume_user_agent": row.consume_user_agent,
            "request_user_agent": row.request_user_agent,
        } for row in token_rows]

    if not active_tokens:
        return []

    # Collect user IDs
    user_ids: List[str] = []
    for token in active_tokens:
        raw_user_id = token.get("user_id")
        if isinstance(raw_user_id, str) and is_valid_id(raw_user_id):
            user_ids.append(str(raw_user_id))

    # Fetch user documents
    user_map: Dict[str, Dict[str, Any]] = {}
    if user_ids:
        with db.sessions() as session:
            rows = session.scalars(select(UserRecord).where(UserRecord.id.in_(user_ids))).all()
            user_map = {str(user.id): {
                "id": str(user.id), "regno": user.regno, "name": user.name, "email": user.email,
                "emoji": user.emoji, "bio": user.bio, "class_name": user.class_name,
                "gender": user.gender, "profile_picture_url": user.profile_picture_url,
                "interests": user.interests or [], "role": user.role,
                "matchmaking_enabled": user.matchmaking_enabled, "notifications_enabled": user.notifications_enabled,
                "love_notes_receive_enabled": user.love_notes_receive_enabled, "love_notes_send_enabled": user.love_notes_send_enabled,
                "reported_count": user.reported_count, "last_login_at": user.last_login_at,
                "last_login_ip": str(user.last_login_ip) if user.last_login_ip else None,
                "last_login_user_agent": user.last_login_user_agent,
                "last_matchmaking_at": user.last_matchmaking_at, "is_blocked": user.is_blocked,
            } for user in rows}

    # Build session entries
    session_entries: Dict[str, Dict[str, Any]] = {}
    for token in active_tokens:
        status = token.get("session_status") or "active"
        
        if status != "active":
            continue

        raw_user_id = token.get("user_id")
        user_doc = user_map.get(raw_user_id) if raw_user_id else None

        last_seen_value = token.get("last_seen_at") or token.get("consumed_at") or datetime.now(timezone.utc)
        
        # Parse last_seen
        if isinstance(last_seen_value, str):
            try:
                parsed_last_seen = datetime.fromisoformat(last_seen_value.replace("Z", "+00:00"))
                if parsed_last_seen.tzinfo is not None:
                    parsed_last_seen = parsed_last_seen.astimezone(timezone.utc)
            except ValueError:
                parsed_last_seen = datetime.now(timezone.utc)
        elif isinstance(last_seen_value, datetime):
            parsed_last_seen = last_seen_value
        else:
            parsed_last_seen = datetime.now(timezone.utc)

        with db.sessions.begin() as session:
            token_row = session.get(LoginTokenRecord, token["id"])
            if token_row:
                token_row.last_seen_at = parsed_last_seen

        # Serialize user
        serialized_user = serialize_user_doc(user_doc) if user_doc else {
            "id": raw_user_id,
            "name": None,
            "email": None,
            "regno": None,
            "role": None,
            "is_blocked": False,
        }
        if not serialized_user.get("id"):
            serialized_user["id"] = raw_user_id or str(token.get("id"))

        iso_last_seen = serialize_datetime(parsed_last_seen.replace(tzinfo=timezone.utc))

        entry = {
            "session_id": str(token.get("id")),
            "status": status,
            "last_seen": iso_last_seen,
            "ip": token.get("consume_ip") or token.get("request_ip"),
            "device": token.get("device") or token.get("consume_user_agent") or token.get("request_user_agent"),
            "user": serialized_user,
            "_last_seen_dt": parsed_last_seen,
        }

        account_key = serialized_user.get("id") or str(token.get("id"))
        existing_entry = session_entries.get(account_key)
        if not existing_entry or entry["_last_seen_dt"] > existing_entry["_last_seen_dt"]:
            session_entries[account_key] = entry

    # Build results
    results: List[Dict[str, Any]] = []
    for value in session_entries.values():
        value.pop("_last_seen_dt", None)
        results.append(value)

    results.sort(key=lambda item: item.get("last_seen") or "", reverse=True)
    return results


def get_all_confessions_data(db: Database) -> List[Dict[str, Any]]:
    """Fetch all confessions with comments and reports."""
    reaction_keys = ["heart", "haha", "whoa", "heartbreak"]
    items: List[Dict[str, Any]] = []
    with db.sessions() as session:
        confessions = session.scalars(select(ConfessionRecord).order_by(ConfessionRecord.created_at.desc())).all()
        confession_ids = [row.id for row in confessions]
        comments = session.scalars(select(ConfessionCommentRecord).where(
            ConfessionCommentRecord.confession_id.in_(confession_ids)
        ).order_by(ConfessionCommentRecord.created_at.desc())).all() if confession_ids else []
        comment_ids = [row.id for row in comments]
        report_targets = []
        if confession_ids:
            report_targets.append(ReportRecord.confession_id.in_(confession_ids))
        if comment_ids:
            report_targets.append(ReportRecord.comment_id.in_(comment_ids))
        reports = session.scalars(select(ReportRecord).where(or_(*report_targets))).all() if report_targets else []
        user_ids = {row.author_id for row in confessions} | {row.user_id for row in comments}
        users = session.scalars(select(UserRecord).where(UserRecord.id.in_(user_ids))).all() if user_ids else []
    users_by_id = {user.id: user for user in users}
    comments_by_confession: Dict[str, List[ConfessionCommentRecord]] = defaultdict(list)
    for comment in comments:
        comments_by_confession[comment.confession_id].append(comment)
    reports_by_content: Dict[str, List[ReportRecord]] = defaultdict(list)
    for report in reports:
        target_id = report.comment_id or report.confession_id
        if target_id:
            reports_by_content[target_id].append(report)

    # Build confession items
    for doc in confessions:
        sender_doc = users_by_id.get(doc.author_id)

        sender_info = {
            "id": str(sender_doc.id) if sender_doc else None,
            "name": sender_doc.name if sender_doc else None,
            "regno": sender_doc.regno if sender_doc else None,
            "email": sender_doc.email if sender_doc else None,
        }

        confession_id = str(doc.id)
        reactions = {key: (doc.reactions or {}).get(key, []) for key in reaction_keys}
        
        # Process confession reports
        confession_reports_docs = sorted(
            reports_by_content.get(confession_id, []),
            key=lambda item: item.created_at or datetime.min,
            reverse=True
        )
        confession_reports = [
            {
                "id": str(report.id),
                "reason": report.reason,
                "created_at": serialize_datetime(report.created_at),
                "reporter": {
                    "id": str(report.reported_by_id),
                    "name": report.reported_by_name,
                },
            }
            for report in confession_reports_docs
        ]

        # Process comments
        comment_entries: List[Dict[str, Any]] = []
        comment_docs = sorted(
            comments_by_confession.get(confession_id, []),
            key=lambda item: item.created_at or datetime.min,
            reverse=True,
        )
        for comment_doc in comment_docs:
            comment_id = str(comment_doc.id)
            comment_reports_docs = sorted(
                reports_by_content.get(comment_id, []),
                key=lambda item: item.created_at or datetime.min,
                reverse=True,
            )
            comment_reports = [
                {
                    "id": str(report.id),
                    "reason": report.reason,
                    "created_at": serialize_datetime(report.created_at),
                    "reporter": {
                        "id": str(report.reported_by_id),
                        "name": report.reported_by_name,
                    },
                }
                for report in comment_reports_docs
            ]

            comment_entries.append(
                {
                    "id": comment_id,
                    "body": comment_doc.body,
                    "created_at": serialize_datetime(comment_doc.created_at),
                    "user": {"id": str(comment_doc.user_id), "name": (users_by_id.get(comment_doc.user_id).name if users_by_id.get(comment_doc.user_id) else "User"), "avatar": (users_by_id.get(comment_doc.user_id).emoji if users_by_id.get(comment_doc.user_id) else None)},
                    "report_count": comment_doc.report_count,
                    "reported_by": [str(report.reported_by_id) for report in reports_by_content.get(comment_id, [])],
                    "like_count": comment_doc.like_count,
                    "dislike_count": comment_doc.dislike_count,
                    "reports": comment_reports,
                }
            )

        items.append(
            {
                "id": confession_id,
                "body": doc.body,
                "recipient_name": doc.recipient_name,
                "anonymous": doc.anonymous,
                "comments_enabled": doc.comments_enabled,
                "created_at": serialize_datetime(doc.created_at),
                "sender": sender_info,
                "report_count": doc.report_count,
                "reported_by": [str(report.reported_by_id) for report in reports_by_content.get(confession_id, [])],
                "reports": confession_reports,
                "reactions": {key: [str(user) for user in value] for key, value in reactions.items()},
                "heart_count": doc.heart_count,
                "haha_count": doc.haha_count,
                "whoa_count": doc.whoa_count,
                "heartbreak_count": doc.heartbreak_count,
                "comment_count": doc.comment_count,
                "comments": comment_entries,
            }
        )

    return items


def get_all_conversations_data(db: Database) -> List[Dict[str, Any]]:
    """Fetch all conversations with participant and message metadata."""
    with db.sessions() as session:
        conversation_docs = session.scalars(select(ConversationRecord).order_by(ConversationRecord.created_at.desc())).all()
        match_ids = {row.match_id for row in conversation_docs}
        match_map = {str(row.id): row for row in session.scalars(select(MatchRecord).where(MatchRecord.id.in_(match_ids))).all()} if match_ids else {}
        message_rows = session.scalars(select(MessageRecord).where(
            MessageRecord.conversation_id.in_([row.id for row in conversation_docs])
        ).order_by(MessageRecord.created_at.desc())).all() if conversation_docs else []
    latest_by_conversation = {}
    for message in message_rows:
        latest_by_conversation.setdefault(message.conversation_id, message)

    results: List[Dict[str, Any]] = []
    for doc in conversation_docs:
        conversation_id = str(doc.id)
        match_id = doc.match_id
        match_doc = match_map.get(str(match_id)) if match_id is not None else None

        # Get latest message
        latest_message = None
        latest_message_doc = latest_by_conversation.get(doc.id)
        if latest_message_doc:
            latest_message = {
                "id": str(latest_message_doc.id),
                "sender_regno": latest_message_doc.sender_regno,
                "body": latest_message_doc.body,
                "created_at": serialize_datetime(latest_message_doc.created_at),
            }

        results.append(
            {
                "id": conversation_id,
                "status": doc.status,
                "created_at": serialize_datetime(doc.created_at),
                "requested_at": serialize_datetime(doc.requested_at),
                "accepted_at": serialize_datetime(doc.accepted_at),
                "terminated_at": serialize_datetime(doc.terminated_at),
                "initiator": serialize_user_by_regno(db, doc.initiator_regno),
                "receiver": serialize_user_by_regno(db, doc.receiver_regno),
                "match": None
                if not match_doc
                else {
                    "id": str(match_doc.id),
                    "user_1_regno": match_doc.user_1_regno,
                    "user_2_regno": match_doc.user_2_regno,
                    "created_at": serialize_datetime(match_doc.created_at),
                    "expires_at": serialize_datetime(match_doc.expires_at),
                },
                "latest_message": latest_message,
                "is_blocked": bool(doc.blocked_by),
                "blocked_by": (doc.blocked_by or [None])[0],
            }
        )

    return results


def get_matchmaking_overview_data(db: Database) -> List[Dict[str, Any]]:
    """Fetch all matches excluding staff accounts."""
    with db.sessions() as session:
        # Staff are excluded from matchmaking as participants, so any match that names one
        # is not something the moderation queue should offer to approve or reject. The set
        # is the staff roles rather than the admin role alone, so a moderator cannot appear
        # in a match list it is being asked to moderate.
        staff_regnos = set(session.scalars(
            select(UserRecord.regno).where(UserRecord.role.in_(STAFF_ROLE_VALUES))
        ).all())
        match_query = select(MatchRecord)
        if staff_regnos:
            match_query = match_query.where(
                MatchRecord.user_1_regno.not_in(staff_regnos), MatchRecord.user_2_regno.not_in(staff_regnos)
            )
        matches = session.scalars(match_query.order_by(MatchRecord.created_at.desc())).all()
        match_ids = [match.id for match in matches]

    # Fetch users
    regnos: set[str] = set()
    for match in matches:
        user_1 = match.user_1_regno
        user_2 = match.user_2_regno
        if user_1:
            regnos.add(user_1)
        if user_2:
            regnos.add(user_2)

    with db.sessions() as session:
        user_rows = session.scalars(select(UserRecord).where(UserRecord.regno.in_(regnos))).all() if regnos else []
        users_by_regno = {user.regno: {
            "id": str(user.id), "regno": user.regno, "name": user.name, "email": user.email,
            "emoji": user.emoji, "bio": user.bio, "class_name": user.class_name, "gender": user.gender,
            "profile_picture_url": user.profile_picture_url, "interests": user.interests or [],
            "role": user.role, "matchmaking_enabled": user.matchmaking_enabled,
            "notifications_enabled": user.notifications_enabled, "love_notes_receive_enabled": user.love_notes_receive_enabled,
            "love_notes_send_enabled": user.love_notes_send_enabled, "reported_count": user.reported_count,
            "last_login_at": user.last_login_at, "last_login_ip": str(user.last_login_ip) if user.last_login_ip else None,
            "last_login_user_agent": user.last_login_user_agent, "last_matchmaking_at": user.last_matchmaking_at,
            "is_blocked": user.is_blocked,
        } for user in user_rows}

    # Fetch conversations
    with db.sessions() as session:
        conversation_rows = session.scalars(select(ConversationRecord).where(
            ConversationRecord.match_id.in_(match_ids)
        ).order_by(ConversationRecord.created_at.desc())).all() if match_ids else []
    conversations_by_match = {}
    for convo in conversation_rows:
        conversations_by_match.setdefault(str(convo.match_id), {
            "id": str(convo.id), "match_id": str(convo.match_id), "status": convo.status,
            "requested_at": convo.requested_at, "accepted_at": convo.accepted_at,
            "initiator_regno": convo.initiator_regno, "receiver_regno": convo.receiver_regno,
        })

    results: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    for match in matches:
        match_id = str(match.id)

        participants: List[Dict[str, Any]] = []
        skip_match = False
        for regno in [match.user_1_regno, match.user_2_regno]:
            if not regno:
                continue
            user_doc = users_by_regno.get(regno)
            if user_doc and is_staff(user_doc.get("role")):
                skip_match = True
                break
            participants.append(serialize_user_doc(user_doc) if user_doc else {"regno": regno})
        
        if skip_match:
            continue

        expires_at_value = match.expires_at
        expired = False
        if isinstance(expires_at_value, datetime):
            expires_at_value = expires_at_value.astimezone(timezone.utc)
            expired = expires_at_value <= now

        conversation_doc = conversations_by_match.get(str(match_id))
        raw_status = conversation_doc.get("status") if conversation_doc else None
        normalized_status = normalize_conversation_status(raw_status)
        if expired:
            normalized_status = "expired"

        conversation_payload: Optional[Dict[str, Any]] = None
        if conversation_doc:
            conversation_payload = {
                "id": str(conversation_doc.get("id")),
                "status": conversation_doc.get("status"),
                "requested_at": serialize_datetime(conversation_doc.get("requested_at")),
                "accepted_at": serialize_datetime(conversation_doc.get("accepted_at")),
                "initiator": conversation_doc.get("initiator_regno"),
                "receiver": conversation_doc.get("receiver_regno"),
            }

        results.append(
            {
                "id": match_id,
                "created_at": serialize_datetime(match.created_at),
                "expires_at": serialize_datetime(match.expires_at),
                "expired": expired,
                "status": normalized_status,
                "participants": participants,
                "conversation": conversation_payload,
            }
        )

    return results


def get_admin_statistics_data(db: Database) -> Dict[str, Any]:
    """Calculate high-level admin statistics."""
    with db.sessions() as session:
        total_users = session.scalar(select(func.count()).select_from(UserRecord)) or 0
        total_confessions = session.scalar(select(func.count()).select_from(ConfessionRecord)) or 0
        total_love_notes = session.scalar(select(func.count()).select_from(LoveNoteRecord)) or 0
        pending_love_notes = session.scalar(select(func.count()).select_from(LoveNoteRecord).where(
            LoveNoteRecord.status == "pending_review"
        )) or 0
        blocked_users = session.scalar(select(func.count()).select_from(UserRecord).where(
            UserRecord.is_blocked.is_(True)
        )) or 0

    active_session_entries = collect_active_sessions(db)
    active_sessions = len(active_session_entries)

    return {
        "total_users": total_users,
        "active_sessions": active_sessions,
        "total_confessions": total_confessions,
        "total_love_notes": total_love_notes,
        "pending_love_notes": pending_love_notes,
        "blocked_users": blocked_users,
    }
