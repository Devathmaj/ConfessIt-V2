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
from .report_counters import REMOVAL_AUTO_BLOCK_THRESHOLD, tally_for_row

# Auto-block threshold for participants: strictly more than this, summed across the three content kinds.
FLAGGED_REMOVAL_THRESHOLD = REMOVAL_AUTO_BLOCK_THRESHOLD

# Hard ceiling for one admin user page; larger requests are clamped (102 fits the card grid).
ADMIN_USERS_MAX_LIMIT = 102

# Deep-OFFSET guard: skips past this are refused (422) so the database never
# scan-and-discards an unbounded prefix; use cursor pagination for deep traversal.
ADMIN_MAX_SKIP = 5000
_COUNT_CACHE_TTL_SECONDS = 30.0
_count_cache: dict[tuple, tuple[float, int]] = {}


def _cached_count(key: tuple, compute) -> int:
    """Return a cached COUNT(*), recomputed at most every 30s; seconds-stale is acceptable beside a page."""
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


def invalidate_admin_user_count_cache() -> None:
    """Drop cached account-page totals after a users-table write; the whole small cache is cleared so every filter shape stays correct."""
    _count_cache.clear()


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
    """Convert datetime to ISO string; thin wrapper over utils.isoformat_datetime keeping the unparseable-str -> None contract."""
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
    """Flatten an account row from any of the three tables into a dict; participant fields exist only when the row is a participant, and the table is reported as ``account_type``."""
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
            # Derived from the stored identity, not a column: a profile picture is a public
            # CDN object that authorises nothing. Participant-only: a staff row has neither column.
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
            # Six counts, grouped by content kind; participant-only because a staff row would
            # show "0 removals" -- reading as a clean record, not a non-applicable field.
            "report_history": tally_for_row(row),
        })
    return payload


def serialize_user_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Serialise an account document to a JSON-friendly format; participant-only keys appear only for participants."""
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
            # Passed through rather than recomputed: denormalised columns written by report_counters,
            # and the number shown must be the stored one. ``{}`` for pre-field rows keeps the shape uniform.
            "report_history": doc.get("report_history") or {},
        })

    return payload


def count_active_sessions(db: Database, window_hours: int = 24) -> int:
    """How many distinct accounts held a live session inside the window; counts accounts, not tokens."""
    now = datetime.now(timezone.utc)
    active_threshold = now - timedelta(hours=window_hours)
    with db.sessions.begin() as session:
        # A session last seen before the window is expired here -- the one write that expires presence without readers.
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


# Hard ceiling for one admin confession page; clamped so no client can force a full-table materialisation.
CONFESSIONS_MAX_LIMIT = 100

CONFESSION_FILTERS = frozenset({"all", "reported", "anonymous", "with-comments"})

CONFESSION_SORTS = frozenset({"recent", "reports", "comments", "reactions"})


def _load_admin_confession_relations(session, confessions):
    """Comments, reports and user rows for exactly the confessions handed in, one bulk read per table.

    An anonymous confession's author is never loaded, so the identity is absent from this process rather than filtered out later. An anonymous author reacting to their own post must also stay unnamed (naming them would de-anonymise the post), but the exception is per confession -- a public confession's author is loaded legitimately -- so the check happens in the build step.
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

    An anonymous confession is anonymous to staff too: the author row is never loaded and ``sender_hidden`` marks the empty sender, so the identity cannot be recovered by reading the response.
    """
    with db.sessions() as session:
        confessions = session.scalars(select(ConfessionRecord).order_by(ConfessionRecord.created_at.desc())).all()
        comments, reports, users = _load_admin_confession_relations(session, confessions)
    return _build_admin_confession_items(confessions, comments, reports, users)


def _build_admin_confession_items(confessions, comments, reports, users) -> List[Dict[str, Any]]:
    """Assemble staff-facing confession payloads from loaded rows; one builder for both readers keeps the anonymity promise in one implementation."""
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

        # Reactors resolved to names so a reviewer can spot one account behind a pile of
        # reactions. Unresolved ids (anonymous author withheld, or a deleted participant
        # whose id remains in the JSONB) stay listed with a reason: privacy vs data fault.
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
                    # Automated reports: reported_by_id IS NULL, and the snapshot name
                    # "Azure Content Safety" is the display contract.
                    "id": str(report.reported_by_id) if report.reported_by_id is not None else None,
                    "name": report.reported_by_name,
                },
            }
            for report in confession_reports_docs
        ]

        # Comments under the confession
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
                            # As for the parent post: staff see removed comments, flagged.
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
                # Raw id list kept: denormalised counters can disagree with the array, so audit needs both.
                "reactions": {key: [str(user) for user in value] for key, value in reactions.items()},
                "reaction_users": reactor_entries,
                "heart_count": doc.heart_count,
                "haha_count": doc.haha_count,
                "whoa_count": doc.whoa_count,
                "heartbreak_count": doc.heartbreak_count,
                "comment_count": doc.comment_count,
                "comments": comment_entries,
                # Removed rows are flagged, not filtered: staff must see what is already removed and audit it.
                "content_removed": doc.removed_at is not None,
                "removed_at": serialize_datetime(doc.removed_at),
                "removed_by_regno": doc.removed_by_regno,
            }
        )

    return items


def _admin_confession_search_filters(query: Optional[str]) -> list:
    """Case-insensitive substring match on a confession and its sender, as SQL clauses.

    Matching in SQL keeps thousands of confessions from being loaded to discard all but twenty. Sender clauses apply only to non-anonymous confessions: matching an anonymous author's name would otherwise oracle the withheld identity.
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
        # "Reported" is any evidence: a report row on the post or a comment, or the counter filed.
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
        # Comment row, not the counter: a takedown decrements the counter while the row survives for audit.
        return [select(ConfessionCommentRecord.id).where(
            ConfessionCommentRecord.confession_id == ConfessionRecord.id
        ).exists()]
    return []


def _admin_confession_ordering(sort: str):
    """The screen's sort picker as ORDER BY, with the id last so the separate count/page queries stay ordered alike."""
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
    """One page of confessions for admin review: searched, filtered, ordered and counted in SQL.

    SQL-side everything keeps the backend to ``limit`` reads per page regardless of table size, with ``total`` a COUNT under the same WHERE. The payload is identical to ``get_all_confessions_data`` via the shared builder; ``skip`` is capped (deep OFFSET refused) and ordering ends in id.asc() so pages are stable.
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

    The keyword half is the existing lexical page unchanged; the semantic half runs the shared pipeline with staff visibility (removed rows flagged, anonymous senders withheld, the active filter applied to both halves) and degrades to keyword results with ``semantic.status`` naming the reason.
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
    # Dedupe against the keyword page (same rule as the public path); the pipeline over-fetched to fill top-K.
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
    """Case-insensitive substring match on a report and the content it points at, as SQL clauses.

    EXISTS subqueries keep the COUNT from fanning out and the page from repeating rows. The reported author is deliberately not searchable: for an anonymous confession that identity is withheld from staff everywhere, and matching it would oracle it.
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

    One queue for both targets (same table, same judgement), each row carrying ``target_type`` and the reported content itself so a reviewer never needs a second screen. An anonymous confession's author is not resolved here -- a report queue is another reader of the anonymity promise -- and ``query`` narrows in SQL with the total counted under the same filter.
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
        # One bulk read per target table, not a query per row (the N+1 mistake).
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

        # Parent confessions are collected even for unreported comments, for the context line.
        parent_ids = {comment.confession_id for comment in comments.values()}
        parents = {
            str(row.id): row for row in session.scalars(
                select(ConfessionRecord).where(ConfessionRecord.id.in_(parent_ids))
            ).all()
        } if parent_ids else {}

        # Anonymous authors (post or parent) are never loaded: a comment report on an
        # anonymous confession must not become a way to de-anonymise it. The payload
        # reports ``author: None`` with ``anonymous: true`` rather than a missing key.
        author_ids = {row.author_id for row in confessions.values() if not row.anonymous}
        author_ids |= {row.author_id for row in parents.values() if not row.anonymous}
        author_ids |= {row.user_id for row in comments.values()}
        authors = {
            user.id: user for user in session.scalars(
                select(UserRecord).where(UserRecord.id.in_(author_ids))
            ).all()
        } if author_ids else {}

        # The live reporter row wins over the filed-time snapshot (names change); the snapshot
        # is the fallback when the row is unreadable -- automated reports carry
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
            # A commenter is always named: the parent's anonymity governs the post, not its open replies.
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
        # Whichever target kind it was, so the removal flag below reads one attribute; may be None.
        target = comment if is_comment else confession

        # The post a reported comment sits on: whether a remark is a threat or a joke often
        # turns on what it answers. Comment reports only; a confession report's parent is its content.
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
                # Denormalised on the confession already; situates the comment in its thread.
                "comment_count": confession.comment_count,
                "anonymous": bool(confession.anonymous),
                "content_removed": bool(confession.removed_at),
                # Withheld for an anonymous parent; the key is present so the client can tell "anonymous" from "author gone".
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
                # Live row wins over the snapshot (names change); automated reports have only the snapshot.
                "name": reporter.name if reporter else report.reported_by_name,
                "regno": reporter.regno if reporter else None,
            },
            "reason": report.reason,
            "reported_at": serialize_datetime(report.created_at),
            "status": report.status,
            "reviewed_at": serialize_datetime(report.reviewed_at),
            "reviewed_by_regno": report.reviewed_by_regno,
            # Already-taken-down flag; not inferable from ``status`` (content can be removed with
            # no report ever filed). Reads ``target`` -- the ``is not None`` is defensive against
            # a future non-cascading delete becoming a 500 on the moderation screen.
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
        # Staff are counted separately: the dashboard's "users" figure is the participant
        # population. Blocked accounts are summed across all three tables -- the question is about accounts.
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

    # Counted via the shared queue helper so the dashboard figure cannot drift from the queues; only `pending` rows are work.
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
    """Append a case-insensitive substring match on identity columns to ``filters``; done in SQL so ten thousand participants are never loaded to discard all but twenty."""
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
    """ORDER BY for one account table, honouring ``sort`` only where the column exists; staff tables fall back to name order."""
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

    Every filter is a WHERE and every page a LIMIT/OFFSET on the database, so at most ``limit`` participant rows are read regardless of population; ``total`` is a COUNT with the same WHERE. Without ``account_type`` the (tiny, capped) staff matches come back whole alongside the one participant page; skip capped against deep-OFFSET.
    """
    skip = _clamp_skip(skip)
    limit = max(1, min(int(limit or 50), ADMIN_USERS_MAX_LIMIT))
    if sort not in ADMIN_USERS_SORTS:
        sort = "name"
    if flagged not in ADMIN_USERS_FLAG_FILTERS:
        flagged = "all"

    wanted = [normalize_account_type(account_type)] if account_type else list(ACCOUNT_MODELS)
    # An unrecognised account_type would resolve to users by normalisation; refuse so a typo cannot answer from the participant table.
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

    # Stable cross-table order for the combined shape: staff first by table, then the participant page as the database returned it.
    if not account_type:
        order = {AccountType.ADMIN.value: 0, AccountType.MODERATOR.value: 1, AccountType.USERS.value: 2}
        docs.sort(key=lambda doc: (order.get(doc.get("account_type"), 9), str(doc.get("name") or "")))

    return {"users": docs, "total": total, "skip": skip, "limit": limit}
