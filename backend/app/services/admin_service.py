# app/services/admin_service.py

import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select, update

from ..postgres import new_id
from ..postgres import Database
from ..db_models import (ACCOUNT_MODELS, ConfessionCommentRecord, ConfessionRecord,
    LoginTokenRecord, LoveNoteRecord, ReportRecord, UserRecord,
    resolve_account_by_id)
from ..accounts import AccountType, is_valid_account_type, normalize_account_type
from ..services.report_resolution_service import pending_report_totals, resolve_status_filter
from ..services.storage_service import storage_service
from ..utils import ilike_contains
from .report_counters import tally_for_row

# Above how many upheld removals a participant lands in the flagged work queue.
# Mirrors FLAGGED_REMOVAL_THRESHOLD on the profile-review screen: strictly more than
# this, summed across the three content kinds, is flagged.
FLAGGED_REMOVAL_THRESHOLD = 5

# Hard ceiling for one page of the admin user list. The endpoint clamps anything larger,
# so no client -- present or future, well-meaning or not -- can ask for 10 000 rows and
# make the backend materialise them all at once. 102 is a multiple of three, which the
# profile-review screen's three-column card grid asks for so no row is left ragged.
ADMIN_USERS_MAX_LIMIT = 102

# Deep-OFFSET guard: a skip past this is refused (422) rather than executed, so a
# client cannot force the database to scan-and-discard an unbounded prefix on every
# page. Cursor pagination (created_at,id) is the path for deep traversal; OFFSET
# remains for the admin screens' shallow load-more. Counts are cached briefly (below)
# so a page does not pay COUNT(*) on every request.
ADMIN_MAX_SKIP = 5000
_COUNT_CACHE_TTL_SECONDS = 30.0
_count_cache: dict[tuple, tuple[float, int]] = {}


def _cached_count(key: tuple, compute) -> int:
    """Return a cached COUNT(*) for ``key``, recomputing at most every 30s.

    The key must capture the filter (status/query/sort); the value is the total
    under that filter. Stale-by-seconds is acceptable for an admin total beside
    a page, and it turns a per-page full-table COUNT into one per window.
    """
    now = time.monotonic()
    hit = _count_cache.get(key)
    if hit is not None and (now - hit[0]) < _COUNT_CACHE_TTL_SECONDS:
        return hit[1]
    total = int(compute() or 0)
    _count_cache[key] = (now, total)
    # Bound memory: drop expired entries opportunistically.
    if len(_count_cache) > 512:
        expired = [k for k, (ts, _) in _count_cache.items() if (now - ts) >= _COUNT_CACHE_TTL_SECONDS]
        for k in expired:
            _count_cache.pop(k, None)
    return total


def _clamp_skip(skip: int) -> int:
    """Clamp OFFSET input, refusing deep skips that are a DoS vector."""
    from fastapi import HTTPException as _HTTPException
    from fastapi import status as _status

    value = max(0, int(skip or 0))
    if value > ADMIN_MAX_SKIP:
        raise _HTTPException(
            status_code=_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"skip too deep (max {ADMIN_MAX_SKIP}); use cursor pagination",
        )
    return value

ADMIN_USERS_SORTS = frozenset({"name", "name_desc", "year", "year_desc", "removals_desc"})

ADMIN_USERS_FLAG_FILTERS = frozenset({"all", "flagged", "ordinary"})


def serialize_datetime(value: Optional[datetime]) -> Optional[str]:
    """Convert datetime to ISO format string (canonical: utils.isoformat_datetime).

    Thin wrapper preserving this module's historical contract (unparseable str
    -> None) over the single shared implementation, so the two cannot drift.
    """
    from ..utils import isoformat_datetime as _isoformat_datetime

    result = _isoformat_datetime(value)
    if result is None:
        return None
    if isinstance(value, str) and result == value:
        # utils passes unparseable strings through; this helper returns None.
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return result


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


# Hard ceiling for one page of the admin confession list. The endpoint clamps anything
# larger, so no client -- present or future, well-meaning or not -- can ask for every
# confession, with every comment, report and reactor row attached, and make the backend
# materialise them all at once.
CONFESSIONS_MAX_LIMIT = 100

CONFESSION_FILTERS = frozenset({"all", "reported", "anonymous", "with-comments"})

CONFESSION_SORTS = frozenset({"recent", "reports", "comments", "reactions"})


def _load_admin_confession_relations(session, confessions):
    """Comments, reports and user rows for exactly the confessions handed in.

    One bulk read per related table rather than a query per row: a page of confessions
    with a hundred comments each must not cost a hundred queries to render. The author of
    an anonymous confession is never loaded at all, so the identity is absent from this
    process rather than filtered out of the payload later; commenters are a different
    population and are always resolved.

    Reactors are resolved too, with one exception applied later per confession: an
    anonymous confession's author reacting to their own post is the most likely reactor
    on any anonymous confession, and naming them in a reactor list would de-anonymise
    the post on the same screen that promises it cannot. That exception cannot be
    applied here by excluding the id from the query -- someone who has written a public
    confession is loaded legitimately for it, and dropping their id here would strip the
    name off that public post as well -- so the decision belongs where the confession
    is, which is why the build step below makes it per confession.
    """
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
    user_ids = {row.author_id for row in confessions if not row.anonymous}
    user_ids |= {row.user_id for row in comments}
    for row in confessions:
        for reactor_ids in (row.reactions or {}).values():
            user_ids.update(str(reactor) for reactor in reactor_ids)
    users = session.scalars(select(UserRecord).where(UserRecord.id.in_(user_ids))).all() if user_ids else []
    return comments, reports, users


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
    with db.sessions() as session:
        confessions = session.scalars(select(ConfessionRecord).order_by(ConfessionRecord.created_at.desc())).all()
        comments, reports, users = _load_admin_confession_relations(session, confessions)
    return _build_admin_confession_items(confessions, comments, reports, users)


def _build_admin_confession_items(confessions, comments, reports, users) -> List[Dict[str, Any]]:
    """Assemble staff-facing confession payloads from loaded rows.

    One builder for both readers -- the unfiltered list and the filtered page -- so the
    anonymity promise has exactly one implementation: an anonymous sender is never named,
    reactors resolve with the per-confession author exception, and removed rows arrive
    flagged rather than filtered. What differs between the readers is only which
    confessions are handed in.
    """
    reaction_keys = ["heart", "haha", "whoa", "heartbreak"]
    items: List[Dict[str, Any]] = []
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
                    # Automated reports carry reported_by_id IS NULL and the
                    # exact display name "Azure Content Safety"; the snapshot
                    # is the display contract, not the live users row.
                    "id": str(report.reported_by_id) if report.reported_by_id is not None else None,
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
                        "id": str(report.reported_by_id) if report.reported_by_id is not None else None,
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
                    "reported_by": [str(report.reported_by_id) for report in reports_by_content.get(comment_id, []) if report.reported_by_id is not None],
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
                "reported_by": [str(report.reported_by_id) for report in reports_by_content.get(confession_id, []) if report.reported_by_id is not None],
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


def _admin_confession_search_filters(query: Optional[str]) -> list:
    """A case-insensitive substring match on a confession and its sender, as SQL clauses.

    Done in SQL rather than in Python so a deployment with thousands of confessions never
    loads them all to discard all but twenty. Matches the body and the recipient name on
    the confession row, plus the sender's name, regno and email through a join that
    instantiates no user rows.

    The sender clauses apply only to non-anonymous confessions. Without that gate the
    search would be an oracle for the withheld identity: asking for an anonymous author's
    name would surface a row the response itself refuses to name, and the old client-side
    search never matched one either, because the sender fields arrive empty there.
    """
    cleaned = (query or "").strip()
    if not cleaned:
        return []
    sender_match = and_(
        ConfessionRecord.anonymous.is_(False),
        or_(
            ilike_contains(UserRecord.name, cleaned),
            ilike_contains(UserRecord.regno, cleaned),
            ilike_contains(UserRecord.email, cleaned),
        ),
    )
    return [or_(
        ilike_contains(ConfessionRecord.body, cleaned),
        ilike_contains(ConfessionRecord.recipient_name, cleaned),
        sender_match,
    )]


def _admin_confession_status_filters(filter_option: str) -> list:
    """The screen's filter picker as SQL clauses: reported, anonymous, or with-comments."""
    if filter_option == "reported":
        # A report row for the confession itself, or for any comment under it, or a
        # denormalised counter saying one was filed: the screen treats "reported" as any
        # evidence a report exists, and any one of the three is that evidence.
        confession_reported = select(ReportRecord.id).where(
            ReportRecord.confession_id == ConfessionRecord.id
        ).exists()
        comment_reported = select(ReportRecord.id).where(
            ReportRecord.comment_id.in_(
                select(ConfessionCommentRecord.id).where(
                    ConfessionCommentRecord.confession_id == ConfessionRecord.id
                )
            )
        ).exists()
        return [or_(
            ConfessionRecord.report_count > 0,
            confession_reported,
            comment_reported,
        )]
    if filter_option == "anonymous":
        return [ConfessionRecord.anonymous.is_(True)]
    if filter_option == "with-comments":
        # A comment row under the confession, not the denormalised counter: the counter is
        # decremented when a comment is taken down while the row survives for audit, so the
        # counter answers "how many are readable" and the row answers "is there a thread".
        return [select(ConfessionCommentRecord.id).where(
            ConfessionCommentRecord.confession_id == ConfessionRecord.id
        ).exists()]
    return []


def _admin_confession_ordering(sort: str):
    """The screen's sort picker as ORDER BY, with the id last so pages are stable.

    A deterministic tail matters because the count and the page are two queries: an
    unordered tail reshuffles between them, and a load-more list that reorders under the
    reader shows some confessions twice.
    """
    if sort == "reports":
        return [
            ConfessionRecord.report_count.desc(),
            ConfessionRecord.created_at.desc(),
            ConfessionRecord.id.asc(),
        ]
    if sort == "comments":
        return [
            ConfessionRecord.comment_count.desc(),
            ConfessionRecord.created_at.desc(),
            ConfessionRecord.id.asc(),
        ]
    if sort == "reactions":
        engagement = (
            func.coalesce(ConfessionRecord.heart_count, 0)
            + func.coalesce(ConfessionRecord.haha_count, 0)
            + func.coalesce(ConfessionRecord.whoa_count, 0)
            + func.coalesce(ConfessionRecord.heartbreak_count, 0)
        )
        return [engagement.desc(), ConfessionRecord.created_at.desc(), ConfessionRecord.id.asc()]
    return [ConfessionRecord.created_at.desc(), ConfessionRecord.id.asc()]


def get_admin_confessions_data(
    db: Database,
    *,
    skip: int = 0,
    limit: int = 50,
    query: Optional[str] = None,
    filter_option: str = "all",
    sort: str = "recent",
) -> Dict[str, Any]:
    """One page of confessions for admin review, searched, filtered, ordered and counted in SQL.

    The search is a SQL WHERE across the confession and (through a non-instantiating
    join) its sender, the filter picker a SQL WHERE, the sort picker a SQL ORDER BY, and
    every page a SQL LIMIT/OFFSET: with thousands of confessions -- each carrying its
    comments, reports and reactor rows -- the backend reads and serialises at most
    ``limit`` of them, never the whole table, and ``total`` is a COUNT under the same
    WHERE so the screen can load more downwards without ever holding the confessions it
    is not showing.

    The per-row payload is identical to ``get_all_confessions_data``: the same redaction
    of anonymous senders, the same comments, reports and engagement snapshot. Only the
    row selection changed, from "everything, newest first" to one filtered page.

    ``skip`` is capped by ADMIN_MAX_SKIP (deep OFFSET refused) and ordering always
    ends in id.asc() so pages are stable; ``total`` is cached briefly.
    """
    skip = _clamp_skip(skip)
    limit = max(1, min(int(limit or 50), CONFESSIONS_MAX_LIMIT))
    if filter_option not in CONFESSION_FILTERS:
        from fastapi import HTTPException, status as _status

        raise HTTPException(status_code=_status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid filter")
    if sort not in CONFESSION_SORTS:
        from fastapi import HTTPException, status as _status

        raise HTTPException(status_code=_status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid sort")

    filters = _admin_confession_search_filters(query) + _admin_confession_status_filters(filter_option)
    ordering = _admin_confession_ordering(sort)

    with db.sessions() as session:
        base = (
            select(ConfessionRecord)
            .outerjoin(UserRecord, UserRecord.id == ConfessionRecord.author_id)
            .where(*filters)
        )
        cache_key = ("confessions", str(query or ""), filter_option, sort)
        total = _cached_count(
            cache_key,
            lambda: session.scalar(
                select(func.count()).select_from(ConfessionRecord)
                .outerjoin(UserRecord, UserRecord.id == ConfessionRecord.author_id)
                .where(*filters)
            ),
        )
        confessions = session.scalars(base.order_by(*ordering).offset(skip).limit(limit)).all()
        comments, reports, users = _load_admin_confession_relations(session, confessions)

    # Same payload as the unfiltered read, built by the one shared builder: which rows
    # are handed in is the only thing the paginated reader decides differently.
    items = _build_admin_confession_items(confessions, comments, reports, users)

    return {"confessions": items, "total": total, "skip": skip, "limit": limit}


def get_admin_confessions_hybrid_data(
    db: Database,
    *,
    query: str,
    filter_option: str = "all",
    sort: str = "recent",
    skip: int = 0,
    limit: int = 10,
    rate_key: str = "admin",
) -> Dict[str, Any]:
    """Hybrid keyword + semantic search for staff review, keyword first.

    Keyword is the existing lexical page unchanged (same search across body,
    recipient, and non-anonymous sender; same filter/sort/paging/total). The
    semantic half runs the shared embed + vector-query + threshold + dedupe
    pipeline capped at ``EMBEDDING_SEMANTIC_TOP_K`` (not paged), with staff
    visibility: removed rows are included and flagged, anonymous senders stay
    withheld, and the active status ``filter`` applies to both halves. Uses the
    admin rate-limit bucket. Degrades like the public path: semantic failures
    return keyword results with ``semantic.status`` naming the reason.
    """
    from concurrent.futures import ThreadPoolExecutor

    from ..config import settings as _settings
    from ..logger import get_logger as _get_logger
    from .semantic_candidates import get_semantic_candidates
    from .semantic_rate_limit import ADMIN_BUCKET

    _hybrid_logger = _get_logger(__name__)
    cleaned = (query or "").strip()
    if not cleaned:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Search query must not be blank.",
        )
    if len(cleaned) > 1000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Search query is too long (max 1000 characters).",
        )
    top_k = int(_settings.EMBEDDING_SEMANTIC_TOP_K)
    with ThreadPoolExecutor(max_workers=2) as pool:
        keyword_future = pool.submit(
            get_admin_confessions_data,
            db, skip=skip, limit=limit, query=cleaned,
            filter_option=filter_option, sort=sort,
        )
        semantic_future = pool.submit(
            get_semantic_candidates,
            sessions=db.sessions,
            query=cleaned,
            cap=top_k,
            include_removed=True,
            extra_filters=_admin_confession_status_filters(filter_option),
            fetch_extra=max(0, int(limit or 0)),
            bucket=ADMIN_BUCKET,
            rate_key=rate_key or "admin",
        )
        keyword_page = keyword_future.result()
        try:
            candidates, semantic_status = semantic_future.result()
        except Exception as exc:
            _hybrid_logger.warning("Admin hybrid semantic half failed: %s", type(exc).__name__)
            candidates, semantic_status = [], "unavailable"
    keyword_ids = {str(item["id"]) for item in keyword_page.get("confessions", [])}
    # Dedupe against the keyword page (same rule as the public path). The shared
    # pipeline over-fetched by the keyword page size so the top-K still fills.
    candidates = [cand for cand in candidates if cand[0] not in keyword_ids][:top_k]
    semantic_items: list = []
    if candidates:
        sem_ids = [cid for cid, _, _ in candidates]
        with db.sessions() as session:
            rows = session.scalars(
                select(ConfessionRecord).where(ConfessionRecord.id.in_(sem_ids))
            ).all()
        by_id = {str(row.id): row for row in rows}
        ordered = [by_id[cid] for cid in sem_ids if cid in by_id]
        if ordered:
            with db.sessions() as session:
                comments, reports, users = _load_admin_confession_relations(session, ordered)
            semantic_items = _build_admin_confession_items(ordered, comments, reports, users)
    return {
        "query": cleaned,
        "filter": filter_option,
        "sort": sort,
        "skip": keyword_page.get("skip", skip),
        "limit": keyword_page.get("limit", limit),
        "keyword": {
            "items": keyword_page.get("confessions", []),
            "total": keyword_page.get("total", 0),
            "skip": keyword_page.get("skip", skip),
            "limit": keyword_page.get("limit", limit),
        },
        "semantic": {"items": semantic_items, "status": semantic_status},
    }


def _confession_report_search_filters(query: Optional[str]) -> list:
    """A case-insensitive substring match on a report and the content it points at.

    Done in SQL rather than in Python so a queue with thousands of reports never loads
    them all to discard all but twenty. Matches the reason and the reporter's snapshotted
    name on the report row, plus the reported body through correlated EXISTS subqueries --
    EXISTS rather than joins, so the COUNT beside the page cannot fan out and the page
    cannot repeat a row.

    The reported author is deliberately not searchable. For an anonymous confession that
    identity is withheld from staff everywhere, and a search that matched it would be an
    oracle for it: asking for the name would surface a row the response itself refuses to
    name. Commenters are named on screen, but their rows arrive through the reported
    comment, which the content search already reaches.
    """
    cleaned = (query or "").strip()
    if not cleaned:
        return []
    confession_body = select(ConfessionRecord.id).where(
        ConfessionRecord.id == ReportRecord.confession_id,
        ilike_contains(ConfessionRecord.body, cleaned),
    ).exists()
    comment_body = select(ConfessionCommentRecord.id).where(
        ConfessionCommentRecord.id == ReportRecord.comment_id,
        ilike_contains(ConfessionCommentRecord.body, cleaned),
    ).exists()
    return [or_(
        ilike_contains(ReportRecord.reason, cleaned),
        ilike_contains(ReportRecord.reported_by_name, cleaned),
        confession_body,
        comment_body,
    )]


def get_confession_reports_data(
    db: Database,
    status_filter: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    query: Optional[str] = None,
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

    ``query`` narrows in SQL to reports whose reason, reporter name or reported content
    matches, with the total counted under the same filter so the screen pages the question
    it asked rather than the queue behind it. Ordered by (created_at desc, id asc) so
    equal timestamps cannot reshuffle between COUNT and page; skip capped; total cached.
    """
    skip = _clamp_skip(skip)
    limit = max(1, min(int(limit or 50), 200))
    filters = []
    if status_filter:
        filters.append(ReportRecord.status.in_(resolve_status_filter(status_filter)))
    filters.extend(_confession_report_search_filters(query))

    with db.sessions() as session:
        reports = session.execute(
            select(ReportRecord)
            .where(*filters)
            .order_by(ReportRecord.created_at.desc(), ReportRecord.id.asc())
            .offset(skip).limit(limit)
        ).scalars().all()
        cache_key = ("reports", str(status_filter or ""), str(query or ""))
        total = _cached_count(
            cache_key,
            lambda: session.scalar(
                select(func.count()).select_from(ReportRecord).where(*filters)
            ),
        )

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
        # row cannot be read at all -- including automated reports, which carry
        # reported_by_id IS NULL and display exactly "Azure Content Safety".
        reporter_ids = {report.reported_by_id for report in reports if report.reported_by_id is not None}
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
                "id": str(report.reported_by_id) if report.reported_by_id is not None else None,
                # Prefer the live row: a participant who has since changed their name should
                # appear under the name they have now, not the one they filed under.
                # Automated reports have no live row; the snapshot ("Azure Content
                # Safety") is the display contract.
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
            "removed_at": serialize_datetime(target.removed_at) if target is not None else None,
            "removed_by_regno": target.removed_by_regno if target is not None else None,
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


def _total_removals_expr(model: Any):
    """SQL expression for a participant's upheld removals summed across the three kinds."""
    return (
        func.coalesce(model.confession_removal_count, 0)
        + func.coalesce(model.love_note_removal_count, 0)
        + func.coalesce(model.message_removal_count, 0)
    )


def _apply_account_search(model: Any, filters: list, query: Optional[str], *, include_department: bool) -> None:
    """Append a case-insensitive substring match on identity columns to ``filters``.

    Done in SQL rather than in Python so a deployment with ten thousand participants never
    loads them all to discard all but twenty. ``q`` matches name, regno and email on every
    table, plus department on ``users`` where the column exists.
    """
    cleaned = (query or "").strip()
    if not cleaned:
        return
    clauses = [
        ilike_contains(model.name, cleaned),
        ilike_contains(model.regno, cleaned),
        ilike_contains(model.email, cleaned),
    ]
    if include_department:
        clauses.append(ilike_contains(model.department, cleaned))
    filters.append(or_(*clauses))


def _account_ordering(model: Any, account_type: str, sort: str):
    """ORDER BY for one account table, honouring ``sort`` only where the column exists.

    Staff tables have no year and no removal counters, so a participant sort requested
    against one falls back to name order rather than erroring: the screen keeps one sort
    control for a list that spans tables with different shapes.
    """
    if sort == "name_desc":
        return [model.name.desc(), model.id.asc()]
    if sort == "removals_desc" and account_type == AccountType.USERS.value:
        return [_total_removals_expr(model).desc(), model.name.asc(), model.id.asc()]
    if sort in ("year", "year_desc") and account_type == AccountType.USERS.value:
        ordering = model.year.desc() if sort == "year_desc" else model.year.asc()
        return [ordering, model.name.asc(), model.id.asc()]
    return [model.name.asc(), model.id.asc()]


def get_admin_users_data(
    db: Database,
    *,
    skip: int = 0,
    limit: int = 50,
    query: Optional[str] = None,
    account_type: Optional[str] = None,
    year: Optional[int] = None,
    department: Optional[str] = None,
    sort: str = "name",
    flagged: str = "all",
) -> Dict[str, Any]:
    """One page of accounts for the profile-review screen, filtered and counted in SQL.

    Every filter here is a WHERE and every page is a LIMIT/OFFSET on the database itself:
    with ten thousand participants the backend reads and serialises at most ``limit``
    participant rows (plus the handful of staff rows, which are capped per table), never
    the whole population. The ``total`` beside the page is a COUNT with the same WHERE, so
    the screen can page without ever holding the rows it is not showing.

    ``skip``/``limit`` apply to the ``users`` page. Staff tables are tiny by construction
    (their caps are an order of magnitude below the participant one), so when no
    ``account_type`` is given the staff matches come back whole -- still behind a LIMIT --
    alongside the one participant page. A caller that wants strict per-table paging asks
    per ``account_type`` instead. Skip capped against deep-OFFSET DoS.
    """
    skip = _clamp_skip(skip)
    limit = max(1, min(int(limit or 50), ADMIN_USERS_MAX_LIMIT))
    if sort not in ADMIN_USERS_SORTS:
        sort = "name"
    if flagged not in ADMIN_USERS_FLAG_FILTERS:
        flagged = "all"

    wanted = [normalize_account_type(account_type)] if account_type else list(ACCOUNT_MODELS)
    # An unrecognised account_type resolves to users by normalisation; refuse it instead so a
    # typo cannot silently answer out of the participant table.
    if account_type and not is_valid_account_type(account_type):
        from fastapi import HTTPException, status as _status

        raise HTTPException(status_code=_status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid account_type")

    cleaned_department = (department or "").strip() or None

    docs: List[Dict[str, Any]] = []
    total = 0
    with db.sessions() as session:
        for current_type in wanted:
            model = ACCOUNT_MODELS[current_type]
            is_participants = current_type == AccountType.USERS.value
            filters: list = []
            _apply_account_search(model, filters, query, include_department=is_participants)
            if is_participants:
                if year is not None:
                    filters.append(model.year == year)
                if cleaned_department:
                    filters.append(func.lower(model.department) == cleaned_department.lower())
                if flagged != "all":
                    removals = _total_removals_expr(model)
                    if flagged == "flagged":
                        filters.append(removals > FLAGGED_REMOVAL_THRESHOLD)
                    else:
                        filters.append(removals <= FLAGGED_REMOVAL_THRESHOLD)

            count = session.scalar(select(func.count()).select_from(model).where(*filters)) or 0
            total += count

            ordering = _account_ordering(model, current_type, sort)
            if is_participants or account_type:
                rows = session.scalars(
                    select(model).where(*filters).order_by(*ordering).offset(skip).limit(limit)
                ).all()
            else:
                # Staff when the caller asked for everything: bounded but complete, so the
                # admin/moderator sections never lose a row behind a participant page.
                rows = session.scalars(
                    select(model).where(*filters).order_by(*ordering).limit(ADMIN_USERS_MAX_LIMIT)
                ).all()
            docs.extend(serialize_user_doc(_account_row_payload(current_type, row)) for row in rows)

    # Stable cross-table order for the combined ("everything") shape: staff first by table,
    # then the participant page in the order the database returned it.
    if not account_type:
        order = {AccountType.ADMIN.value: 0, AccountType.MODERATOR.value: 1, AccountType.USERS.value: 2}
        docs.sort(key=lambda doc: (order.get(doc.get("account_type"), 9), str(doc.get("name") or "")))

    return {"users": docs, "total": total, "skip": skip, "limit": limit}
