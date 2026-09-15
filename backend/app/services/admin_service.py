# app/services/admin_service.py

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select, update

from ..postgres import new_id, is_valid_id
from ..postgres import Database
from ..db_models import (ACCOUNT_MODELS, ConfessionCommentRecord, ConfessionRecord,
    ConversationRecord, LoginTokenRecord, LoveNoteRecord, MatchRecord, MessageRecord,
    ReportRecord, UserRecord, find_account_in_table, resolve_account,
    resolve_account_by_id)
from ..accounts import AccountType, normalize_account_type
from ..services.report_resolution_service import resolve_status_filter


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


def _account_row_payload(account_type: str, row: Any) -> Dict[str, Any]:
    """Flatten an account row from any of the three tables into a dict.

    The shared fields are read off whatever table the row came from; the participant fields
    are read only when the row is a participant, because a staff row does not have them to
    read. Building the dict this way -- rather than one serialiser per table -- is what lets
    the admin list render staff and participants through a single code path, and is why
    there is no ``role`` to fill in: the table is reported as ``account_type``.
    """
    payload: Dict[str, Any] = {
        "id": str(row.id),
        "regno": row.regno,
        "name": row.name,
        "email": row.email,
        "account_type": account_type,
        "gender": row.gender,
        "profile_picture_url": row.profile_picture_url,
        "notifications_enabled": row.notifications_enabled,
        "is_blocked": row.is_blocked,
        "last_login_at": serialize_datetime(row.last_login_at),
        "last_login_ip": str(row.last_login_ip) if row.last_login_ip else None,
        "last_login_user_agent": row.last_login_user_agent,
        "created_at": serialize_datetime(row.created_at),
        "updated_at": serialize_datetime(row.updated_at),
    }
    if account_type == AccountType.USERS.value:
        payload.update({
            "email": row.email,
            "emoji": row.emoji,
            "bio": row.bio,
            "class_name": row.class_name,
            "interests": row.interests or [],
            "matchmaking_enabled": row.matchmaking_enabled,
            "love_notes_receive_enabled": row.love_notes_receive_enabled,
            "love_notes_send_enabled": row.love_notes_send_enabled,
            "last_matchmaking_at": serialize_datetime(row.last_matchmaking_at),
        })
    return payload


def serialize_user_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Serialise an account document to a JSON-friendly format.

    Participant-only keys are present only for participant documents. The admin client
    reads them with fallbacks, so a staff row simply has nothing to show there -- which is
    the honest answer, since the column does not exist on that table.
    """
    if not doc:
        return {}

    is_participant = normalize_account_type(doc.get("account_type")) == AccountType.USERS.value

    payload: Dict[str, Any] = {
        "id": str(doc.get("id")),
        "regno": doc.get("regno"),
        "name": doc.get("name"),
        # Shared, so it sits with the common fields and every account can report one.
        "email": doc.get("email"),
        "account_type": normalize_account_type(doc.get("account_type")),
        "gender": doc.get("gender"),
        "profile_picture_url": doc.get("profile_picture_url"),
        "notifications_enabled": doc.get("notifications_enabled", True),
        "is_blocked": doc.get("is_blocked", False),
        "last_login_at": serialize_datetime(doc.get("last_login_at")),
        "last_login_ip": doc.get("last_login_ip"),
        "last_login_user_agent": doc.get("last_login_user_agent"),
        "created_at": serialize_datetime(doc.get("created_at")),
        "updated_at": serialize_datetime(doc.get("updated_at")),
    }

    if is_participant:
        payload.update({
            "emoji": doc.get("emoji"),
            "bio": doc.get("bio"),
            "class_name": doc.get("class_name"),
            "interests": doc.get("interests", []),
            "matchmaking_enabled": doc.get("matchmaking_enabled", False),
            "love_notes_receive_enabled": doc.get("love_notes_receive_enabled", True),
            "love_notes_send_enabled": doc.get("love_notes_send_enabled", False),
            "last_matchmaking_at": serialize_datetime(doc.get("last_matchmaking_at")),
        })

    return payload


def serialize_account_by_regno(db: Database, regno: Optional[str]) -> Dict[str, Any]:
    """Fetch and serialise a participant by regno.

    Always reads the ``users`` table, because every caller is naming the initiator or receiver
    of a conversation and those are participants by construction. Searching all three tables
    would be ambiguous for no benefit now that a regno may exist in more than one of them.

    Returns a document with a null ``name`` when no row holds the regno, which is what the
    review screens already expect for an account that no longer exists.
    """
    if not regno:
        return {"regno": None}

    with db.sessions() as session:
        found = find_account_in_table(session, AccountType.USERS.value, regno)
    if found:
        account_type, row = found
        return serialize_user_doc(_account_row_payload(account_type, row))
    return {"regno": regno, "name": None, "account_type": None}


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
            "id": str(row.id), "account_id": str(row.account_id),
            "account_type": normalize_account_type(row.account_type),
            "session_status": row.session_status,
            "last_seen_at": row.last_seen_at, "consumed_at": row.consumed_at,
            "consume_ip": str(row.consume_ip) if row.consume_ip else None,
            "request_ip": str(row.request_ip) if row.request_ip else None,
            "device": row.device, "consume_user_agent": row.consume_user_agent,
            "request_user_agent": row.request_user_agent,
        } for row in token_rows]

    if not active_tokens:
        return []

    # Fetch account documents. The token names the table the account was in when the link
    # was minted, and a session can belong to a participant or to staff, so the lookup is
    # per-token and cross-table rather than a single query against ``users`` -- which is
    # exactly what would silently drop every staff session from this list.
    account_map: Dict[str, Dict[str, Any]] = {}
    with db.sessions() as session:
        for token in active_tokens:
            account_id = token.get("account_id")
            if not isinstance(account_id, str) or not is_valid_id(account_id):
                continue
            # ``resolve_account`` takes the table it should read and returns just the row, so
            # the account kind here is the one recorded on the token -- not something derived
            # from the row. That keeps the reported ``account_type`` honest even if a row were
            # ever reachable through a different table.
            token_account_type = token["account_type"]
            row = resolve_account(session, token_account_type, account_id)
            if row is not None:
                account_map[account_id] = _account_row_payload(token_account_type, row)

    # Build session entries
    session_entries: Dict[str, Dict[str, Any]] = {}
    for token in active_tokens:
        status = token.get("session_status") or "active"
        
        if status != "active":
            continue

        raw_account_id = token.get("account_id")
        user_doc = account_map.get(raw_account_id) if raw_account_id else None

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

        # Serialize account
        serialized_user = serialize_user_doc(user_doc) if user_doc else {
            "id": raw_account_id,
            "name": None,
            "regno": None,
            "account_type": token.get("account_type"),
            "is_blocked": False,
        }
        if not serialized_user.get("id"):
            serialized_user["id"] = raw_account_id or str(token.get("id"))

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
    """Fetch all confessions with comments and reports.

    An anonymous confession is anonymous to staff too: the author row is never loaded, the
    sender fields are emitted empty, and ``sender_hidden`` is set so the client can render
    "no sender to show" instead of guessing from ``anonymous``. Review is still fully possible
    on such a post -- body, timestamps, reports, comments and engagement all come back -- it
    is only the identity that is withheld, and it is withheld here rather than in the client so
    it cannot be recovered by reading the response.

    A confession that was never anonymous is unaffected: it still names its author, and the
    review screen still decides for itself when to show that name.
    """
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
        # The author of an anonymous confession is not loaded at all, so the identity is
        # absent from this process rather than filtered out of the payload later. Commenters
        # are a different population and are always resolved.
        #
        # Reactors are a different population from the author and are resolved, with one
        # exception applied later: an anonymous confession's author reacting to their own post is
        # the most likely reactor on any anonymous confession, and naming them in a reactor list
        # would de-anonymise the post on the same screen that promises it cannot be.
        #
        # That exception cannot be applied here by excluding the id from the query, the way the
        # author block above does. Excluding it works only while the id appears nowhere else, and
        # someone who has written a public confession is loaded legitimately for it -- dropping
        # their id here would strip the name off that public post as well. An id is in this
        # process for reasons unrelated to any one confession, so the decision belongs where the
        # confession is, which is why it is made per confession below.
        user_ids = {row.author_id for row in confessions if not row.anonymous}
        user_ids |= {row.user_id for row in comments}
        for row in confessions:
            for reactor_ids in (row.reactions or {}).values():
                user_ids.update(str(reactor) for reactor in reactor_ids)
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
        sender_hidden = bool(doc.anonymous)
        sender_doc = None if sender_hidden else users_by_id.get(doc.author_id)

        sender_info = {
            "id": str(sender_doc.id) if sender_doc else None,
            "name": sender_doc.name if sender_doc else None,
            "regno": sender_doc.regno if sender_doc else None,
            "email": sender_doc.email if sender_doc else None,
        }

        confession_id = str(doc.id)
        reactions = {key: (doc.reactions or {}).get(key, []) for key in reaction_keys}

        # Who reacted, resolved to a name so the Engagement snapshot can answer "who is this"
        # without a second round trip per emoji. Built here rather than exposed as raw ids
        # because an id in a moderator's hands identifies nobody they can act on: the point is
        # to see whether one account is behind a pile of reactions.
        #
        # An id can fail to resolve for two reasons and neither is a bug: an anonymous post's
        # author is withheld on purpose, and a participant who has since been deleted is gone
        # from `users` while their id remains in the JSONB array. Both keep their place in the
        # list so it still adds up to the count beside it, and each reports which of the two it
        # is, so a reviewer can tell a privacy decision from a data fault.
        reactor_entries: Dict[str, List[Dict[str, Any]]] = {}
        for key in reaction_keys:
            entries = []
            for reactor_id in reactions[key]:
                reactor = users_by_id.get(str(reactor_id))
                is_anonymous_author = doc.anonymous and str(reactor_id) == str(doc.author_id)
                withheld_reason = None
                if is_anonymous_author:
                    withheld_reason = "anonymous_author"
                elif reactor is None:
                    withheld_reason = "unknown"
                entries.append(
                    {
                        "id": str(reactor_id),
                        "name": None if withheld_reason else reactor.name,
                        "withheld_reason": withheld_reason,
                    }
                )
            reactor_entries[key] = entries
        
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
                    # Same reasoning as the parent post: staff see removed comments, flagged.
                    "content_removed": comment_doc.removed_at is not None,
                    "removed_at": serialize_datetime(comment_doc.removed_at),
                    "removed_by_regno": comment_doc.removed_by_regno,
                }
            )

        items.append(
            {
                "id": confession_id,
                "body": doc.body,
                "recipient_name": doc.recipient_name,
                "anonymous": doc.anonymous,
                "sender_hidden": sender_hidden,
                "comments_enabled": doc.comments_enabled,
                "created_at": serialize_datetime(doc.created_at),
                "sender": sender_info,
                "report_count": doc.report_count,
                "reported_by": [str(report.reported_by_id) for report in reports_by_content.get(confession_id, [])],
                "reports": confession_reports,
                # The raw id list is kept: `heart_count` and friends are denormalised counters
                # and can disagree with the array, so anything auditing engagement needs both.
                "reactions": {key: [str(user) for user in value] for key, value in reactions.items()},
                "reaction_users": reactor_entries,
                "heart_count": doc.heart_count,
                "haha_count": doc.haha_count,
                "whoa_count": doc.whoa_count,
                "heartbreak_count": doc.heartbreak_count,
                "comment_count": doc.comment_count,
                "comments": comment_entries,
                # Not filtered out, flagged. This is the staff view: a moderator deciding
                # whether to remove something has to be able to see what is already removed,
                # and the same row is also how a removal gets audited after the fact.
                "content_removed": doc.removed_at is not None,
                "removed_at": serialize_datetime(doc.removed_at),
                "removed_by_regno": doc.removed_by_regno,
            }
        )

    return items


def get_confession_reports_data(
    db: Database,
    status_filter: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
) -> Dict[str, Any]:
    """Every report filed against a confession or a comment, newest first.

    One queue rather than two. The two targets live in the same table and are reviewed with
    the same judgement, so splitting them would only mean a reviewer reading half the queue on
    one screen and the other half somewhere they were not looking. Each row carries a
    ``target_type`` of ``"confession"`` or ``"comment"`` and, for a comment, the confession it
    was written under, because a comment reported out of context is not something anybody can
    act on.

    The content is carried, not referenced. The question a reviewer opens this screen to answer
    is "what did this actually say", and a row holding only an id sends them to a second screen
    to find out.

    The author of an anonymous confession is not resolved here, for the same reason
    ``get_all_confessions_data`` does not resolve it: an anonymous confession is anonymous to
    staff, and a report queue is another reader of the same promise. ``author`` comes back
    with its fields null and ``author_hidden`` set, so the client can say so rather than guess
    from the ``anonymous`` flag. Commenters are a different population and are always named.
    """
    filters = []
    if status_filter:
        filters.append(ReportRecord.status.in_(resolve_status_filter(status_filter)))

    with db.sessions() as session:
        reports = session.execute(
            select(ReportRecord)
            .where(*filters)
            .order_by(ReportRecord.created_at.desc())
            .offset(skip).limit(limit)
        ).scalars().all()
        total = session.scalar(
            select(func.count()).select_from(ReportRecord).where(*filters)
        ) or 0

        confession_ids = {report.confession_id for report in reports if report.confession_id}
        comment_ids = {report.comment_id for report in reports if report.comment_id}
        # One bulk read per target table rather than a query per row: this is the N+1 mistake
        # the confession admin list is called out for, and a queue grows with every report
        # filed rather than with the page being read.
        confessions = {
            str(row.id): row for row in session.scalars(
                select(ConfessionRecord).where(ConfessionRecord.id.in_(confession_ids))
            ).all()
        } if confession_ids else {}
        comments = {
            str(row.id): row for row in session.scalars(
                select(ConfessionCommentRecord).where(ConfessionCommentRecord.id.in_(comment_ids))
            ).all()
        } if comment_ids else {}

        # A comment's parent confession is needed for the context line even when the comment
        # itself is not reported, so these ids are collected before the author lookup.
        parent_ids = {comment.confession_id for comment in comments.values()}
        parents = {
            str(row.id): row for row in session.scalars(
                select(ConfessionRecord).where(ConfessionRecord.id.in_(parent_ids))
            ).all()
        } if parent_ids else {}

        # The author of an anonymous confession is deliberately absent from this set: the row
        # is never loaded, so the identity is not in this process to be filtered out later.
        author_ids = {row.author_id for row in confessions.values() if not row.anonymous}
        author_ids |= {row.user_id for row in comments.values()}
        authors = {
            user.id: user for user in session.scalars(
                select(UserRecord).where(UserRecord.id.in_(author_ids))
            ).all()
        } if author_ids else {}

        # The reporter's name is snapshotted on the report row, but the live row wins: a
        # participant who has since changed their name should appear under the name they have
        # now, not the one they filed under. The snapshot is the fallback for a reporter whose
        # row cannot be read at all.
        reporter_ids = {report.reported_by_id for report in reports}
        reporters = {
            user.id: user for user in session.scalars(
                select(UserRecord).where(UserRecord.id.in_(reporter_ids))
            ).all()
        } if reporter_ids else {}

    items: List[Dict[str, Any]] = []
    for report in reports:
        reporter = reporters.get(report.reported_by_id)
        is_comment = report.comment_id is not None
        if is_comment:
            comment = comments.get(str(report.comment_id))
            confession = parents.get(str(comment.confession_id)) if comment else None
            content = comment.body if comment else None
            content_created_at = comment.created_at if comment else None
            # A commenter is always named. The parent confession's anonymity governs the post,
            # not the people replying to it in the open.
            author_hidden = False
            author = authors.get(comment.user_id) if comment else None
            target_id = str(report.comment_id)
            confession_id = str(comment.confession_id) if comment else None
        else:
            confession = confessions.get(str(report.confession_id))
            comment = None
            content = confession.body if confession else None
            content_created_at = confession.created_at if confession else None
            author_hidden = bool(confession.anonymous) if confession else False
            author = None if author_hidden else authors.get(confession.author_id) if confession else None
            target_id = str(report.confession_id)
            confession_id = target_id
        # The reported row itself, whichever kind it was, so the removal flag below reads one
        # attribute instead of repeating the branch. It can be None even though the report
        # exists -- see the note on ``content_removed``.
        target = comment if is_comment else confession

        items.append({
            "id": str(report.id),
            "target_type": "comment" if is_comment else "confession",
            "target_id": target_id,
            "confession_id": confession_id,
            "parent_recipient_name": confession.recipient_name if confession else None,
            "anonymous": bool(confession.anonymous) if confession else False,
            "content": content,
            "content_created_at": serialize_datetime(content_created_at),
            "author": None if author_hidden else {
                "id": str(author.id) if author else None,
                "name": author.name if author else None,
                "regno": author.regno if author else None,
            },
            "author_hidden": author_hidden,
            "reporter": {
                "id": str(report.reported_by_id),
                # Prefer the live row: a participant who has since changed their name should
                # appear under the name they have now, not the one they filed under.
                "name": reporter.name if reporter else report.reported_by_name,
                "regno": reporter.regno if reporter else None,
            },
            "reason": report.reason,
            "reported_at": serialize_datetime(report.created_at),
            "status": report.status,
            "reviewed_at": serialize_datetime(report.reviewed_at),
            "reviewed_by_regno": report.reviewed_by_regno,
            # Whether the reported content is already taken down. The screen cannot infer it
            # from ``status``: a content admin can remove a confession without a report ever
            # existing, and a moderator has to see that before choosing between the two.
            #
            # ``target`` rather than the two branches above: the content row can be missing
            # even though the report exists, since deleting a confession cascades its reports
            # but a comment's parent going away does not take the comment report with it.
            "content_removed": bool(target.removed_at) if target is not None else False,
        })

    return {"reports": items, "total": total, "skip": skip, "limit": limit}





def get_all_conversations_data(db: Database) -> List[Dict[str, Any]]:
    """Fetch all conversations with participant and message metadata."""
    with db.sessions() as session:
        conversation_docs = session.scalars(select(ConversationRecord).order_by(ConversationRecord.created_at.desc())).all()
        match_ids = {row.match_id for row in conversation_docs}
        match_map = {str(row.id): row for row in session.scalars(select(MatchRecord).where(MatchRecord.id.in_(match_ids))).all()} if match_ids else {}
        message_rows = session.scalars(select(MessageRecord).where(
            MessageRecord.conversation_id.in_([row.id for row in conversation_docs])
        ).order_by(MessageRecord.created_at.desc())).all() if conversation_docs else []
    # Built from the remaining messages only. A removed message is still in ``message_rows``
        # and still counted in ``message_counts`` below -- staff need it -- but the preview has
        # to fall through to the newest message that is actually readable, otherwise ruling on
        # a report would take the text down and leave it sitting in the conversation list.
        latest_by_conversation = {}
    for message in message_rows:
        if message.removed_at is not None:
            continue
        latest_by_conversation.setdefault(message.conversation_id, message)

    # Two counters from the rows already loaded, not three more aggregate queries: the
    # transcript screen needs both numbers and this is the one place every message of every
    # listed conversation is in memory anyway.
    visible_counts: Dict[str, int] = defaultdict(int)
    message_counts: Dict[str, int] = defaultdict(int)
    for message in message_rows:
        message_counts[message.conversation_id] += 1
        if message.removed_at is None:
            visible_counts[message.conversation_id] += 1

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
                "initiator": serialize_account_by_regno(db, doc.initiator_regno),
                "receiver": serialize_account_by_regno(db, doc.receiver_regno),
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
                # How much of the thread a moderator has left to read. Computed here rather
                # than on the participant side, because the removed rows are filtered out of
                # the participant's transcript but not out of this one, and a count of visible
                # messages has to be a count of the same set the viewer can see.
                "visible_message_count": visible_counts.get(doc.id, 0),
                "removed_message_count": message_counts.get(doc.id, 0)
                - visible_counts.get(doc.id, 0),
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
        # Staff counted separately rather than folded into ``total_users``: the dashboard's
        # "users" figure is the participant population, and adding moderators to it would
        # quietly change what a number the staff reads every day means. Blocked accounts are
        # summed across all three tables so the count still answers "how many accounts are
        # blocked", which is a question about accounts and not about participants.
        blocked_accounts = blocked_users + sum(
            session.scalar(select(func.count()).select_from(model).where(
                model.is_blocked.is_(True)
            )) or 0
            for account_type, model in ACCOUNT_MODELS.items()
            if account_type != AccountType.USERS.value
        )
        total_staff = sum(
            session.scalar(select(func.count()).select_from(model)) or 0
            for account_type, model in ACCOUNT_MODELS.items()
            if account_type != AccountType.USERS.value
        )

    active_session_entries = collect_active_sessions(db)
    active_sessions = len(active_session_entries)

    return {
        "total_users": total_users,
        "total_staff": total_staff,
        "active_sessions": active_sessions,
        "total_confessions": total_confessions,
        "total_love_notes": total_love_notes,
        "pending_love_notes": pending_love_notes,
        "blocked_users": blocked_accounts,
    }
