# app/services/admin_service.py

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select, update

from ..postgres import new_id
from ..postgres import Database
from ..db_models import (ACCOUNT_MODELS, ConfessionCommentRecord, ConfessionRecord,
    LoginTokenRecord, LoveNoteRecord, ReportRecord, UserRecord,
    resolve_account_by_id)
from ..accounts import AccountType, normalize_account_type
from ..services.report_resolution_service import pending_report_totals, resolve_status_filter
from ..services.storage_service import storage_service
from .report_counters import tally_for_row


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
            "gender": row.gender,
            # Derived here rather than read from a column: the row holds the picture's identity,
            # and the address the admin client renders is built from it on the way out. Nothing
            # is authorised by this -- a profile picture is a public Cloudinary object -- so the
            # same URL goes to every account that can see the profile. Participant-only, because
            # the upload route is: a staff row has neither column to derive from.
            "profile_picture_url": storage_service.profile_delivery_url(
                row.profile_picture_public_id, row.profile_picture_format
            ),
            "bio": row.bio,
            "year": row.year,
            "department": row.department,
            "interests": row.interests or [],
            "matchmaking_enabled": row.matchmaking_enabled,
            "love_notes_receive_enabled": row.love_notes_receive_enabled,
            "love_notes_send_enabled": row.love_notes_send_enabled,
            "last_matchmaking_at": serialize_datetime(row.last_matchmaking_at),
            "last_confession_at": serialize_datetime(row.last_confession_at),
            "last_profile_picture_at": serialize_datetime(row.last_profile_picture_at),
            # Six counts, grouped by the kind of content they are about. Participant-only
            # because only a participant has content anyone can report: a staff row would show
            # "0 removals", which reads as a clean record rather than as a field that does not
            # apply. Maintained by app.services.report_counters.
            "report_history": tally_for_row(row),
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
            "gender": doc.get("gender"),
            "profile_picture_url": doc.get("profile_picture_url"),
            "bio": doc.get("bio"),
            "year": doc.get("year"),
            "department": doc.get("department"),
            "interests": doc.get("interests", []),
            "matchmaking_enabled": doc.get("matchmaking_enabled", False),
            "love_notes_receive_enabled": doc.get("love_notes_receive_enabled", True),
            "love_notes_send_enabled": doc.get("love_notes_send_enabled", False),
            "last_matchmaking_at": serialize_datetime(doc.get("last_matchmaking_at")),
            "last_confession_at": serialize_datetime(doc.get("last_confession_at")),
            "last_profile_picture_at": serialize_datetime(doc.get("last_profile_picture_at")),
            # Passed through rather than recomputed: these are denormalised columns written by
            # report_counters at filing and removal time, and the number an administrator is
            # shown has to be the stored one. ``{}`` rather than omitted for a participant
            # whose row predates the fields, so the client always has all three kinds to read.
            "report_history": doc.get("report_history") or {},
        })

    return payload


def count_active_sessions(db: Database, window_hours: int = 24) -> int:
    """How many accounts held a live session at some point inside the window.

    Counts distinct accounts rather than tokens, because one account signing in twice is one
    person, not two. The dashboard reads this as a single figure and no longer needs the
    session rows behind it -- who was online, from where, on what -- so nothing is resolved
    per token here: no account lookup, no per-row write, and nothing session-shaped leaves
    this function.
    """
    now = datetime.now(timezone.utc)
    active_threshold = now - timedelta(hours=window_hours)
    with db.sessions.begin() as session:
        # A session that was last seen before the window is over, whatever it says about
        # itself, so it stops being counted as live. This is the one write the count needs,
        # and it is why an account's presence expires even when nobody is reading the list.
        session.execute(update(LoginTokenRecord).where(
            LoginTokenRecord.session_status == "active",
            LoginTokenRecord.consumed_at < active_threshold,
        ).values(session_status="expired"))
        account_ids = session.scalars(
            select(LoginTokenRecord.account_id)
            .where(
                LoginTokenRecord.session_status == "active",
                LoginTokenRecord.used.is_(True),
                LoginTokenRecord.revoked.is_(False),
                LoginTokenRecord.consumed_at >= active_threshold,
            )
            .distinct()
        ).all()

    return len(account_ids)


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
                    "user": {"id": str(comment_doc.user_id), "name": (users_by_id.get(comment_doc.user_id).name if users_by_id.get(comment_doc.user_id) else "User")},
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
        #
        # The same rule applies to a *parent* confession reached through a reported comment. A
        # comment report points at a post, and showing that post's author would make reporting a
        # comment on an anonymous confession a way to de-anonymise it -- which is exactly the
        # promise this codebase refuses to break anywhere else. So a non-anonymous parent's
        # author is loaded and an anonymous one's is not, and the payload below reports the
        # difference as ``author: None`` with ``anonymous: true`` rather than as a missing key.
        author_ids = {row.author_id for row in confessions.values() if not row.anonymous}
        author_ids |= {row.author_id for row in parents.values() if not row.anonymous}
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

        # The confession a reported comment sits on. Without it a moderator is judging a remark
        # with no idea what it was a remark *about* -- and whether a comment is a threat or a
        # joke often turns entirely on the post it answers. Present only for comment reports:
        # for a confession report the parent is the reported content itself, already in
        # ``content``, and repeating it would double the card for no information.
        parent_confession = None
        if is_comment and confession is not None:
            parent_author = (
                None if confession.anonymous else authors.get(str(confession.author_id))
            )
            parent_confession = {
                "id": str(confession.id),
                "body": confession.body,
                "recipient_name": confession.recipient_name,
                "created_at": serialize_datetime(confession.created_at),
                # Denormalised on the confession already, so this costs no extra read, and it is
                # what turns the comment from "a remark" into "the fourth of nine".
                "comment_count": confession.comment_count,
                "anonymous": bool(confession.anonymous),
                "content_removed": bool(confession.removed_at),
                # Withheld for an anonymous parent, as above. The key is still present so the
                # client can tell "anonymous" from "author gone".
                "author": None if parent_author is None else {
                    "id": str(parent_author.id),
                    "name": parent_author.name,
                    "regno": parent_author.regno,
                },
            }

        items.append({
            "id": str(report.id),
            "target_type": "comment" if is_comment else "confession",
            "target_id": target_id,
            "confession_id": confession_id,
            "parent_recipient_name": confession.recipient_name if confession else None,
            "parent_confession": parent_confession,
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
            # ``target`` rather than the two branches above, so the flag reads one attribute
            # instead of repeating the branch. The ``is not None`` is defensive: both targets are
            # reachable only through a cascade that would have taken the report with them, so in
            # practice the row is always there. It costs one attribute check and keeps a future
            # non-cascading delete from turning into a 500 on the moderation screen.
            "content_removed": bool(target.removed_at) if target is not None else False,
        })

    return {"reports": items, "total": total, "skip": skip, "limit": limit}


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

    active_sessions = count_active_sessions(db)

    # Read through the same helper the report queues are counted by, so the dashboard's
    # backlog figure cannot drift from the queues behind it. Only `pending` rows are counted:
    # a report that has already been ruled on is not work anyone is waiting on.
    report_queues = pending_report_totals(db)
    pending_reports = sum(report_queues.values())

    return {
        "total_users": total_users,
        "total_staff": total_staff,
        "active_sessions": active_sessions,
        "total_confessions": total_confessions,
        "total_love_notes": total_love_notes,
        "pending_love_notes": pending_love_notes,
        "pending_reports": pending_reports,
        "pending_report_queues": report_queues,
        "blocked_users": blocked_accounts,
    }
