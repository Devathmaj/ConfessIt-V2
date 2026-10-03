# app/services/staff_notification_service.py

"""Role-based staff notifications: reports, love notes awaiting review, games.

Participant notices (``notifications``) are addressed to one regno. Staff
notices are addressed to a *role* -- every moderator on duty is owed the same
news about a fresh report, and every admin the same news about a game that
needs feeding -- so ``report`` and ``love_note`` events are stored once with
``audience='all'`` and read by whichever role asks, rather than fanned out
into one row per staff account.

``game`` alerts are the exception and are *computed live* on every read: a
missing featured pick or a pending manual-mode session stops being news the
moment an admin fixes it, and a stored row would linger as a stale alarm (or
need a sweeper to retract it). The list endpoint merges stored rows with the
live game alerts and sorts them newest-first.

Preferences (which categories an account wants) are stored per staff account
but deliberately *not* wired to delivery: the list returns every notice
regardless of the switches. The switches are persisted intent for a future
delivery layer, nothing more.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from ..accounts import AccountType, normalize_account_type
from ..db_models import (
    ACCOUNT_MODELS,
    AdminRecord,
    AnagramGameRecord,
    ConnectionsGameRecord,
    FeaturedGameRecord,
    GameConfigRecord,
    ModeratorRecord,
    StaffNotificationPreferenceRecord,
    StaffNotificationReadRecord,
    StaffNotificationRecord,
    TriviaDropQuestionRecord,
    WordleGameRecord,
)
from ..models import AccountDetails
from ..postgres import Database, is_valid_id
from ..utils import ilike_contains, isoformat_datetime

# The whole category vocabulary. ``report`` covers every report queue
# (confession/comment, love-note and chat-message reports); ``love_note`` is a
# note awaiting approval; ``game`` is anything an admin must feed.
STAFF_NOTIFICATION_CATEGORIES: tuple[str, ...] = ("report", "love_note", "game")

# What each role may see. Moderators rule on content, so reports and the
# love-note backlog; games are admin business (content modes, schedules,
# featured picks), so only an admin sees those.
MODERATOR_CATEGORIES: frozenset[str] = frozenset({"report", "love_note"})
ADMIN_CATEGORIES: frozenset[str] = frozenset({"report", "love_note", "game"})

STAFF_NOTIFICATIONS_MAX_LIMIT = 100


def _role_of(account: AccountDetails) -> str:
    return normalize_account_type(account.account_type)


def _visible_categories(role: str) -> frozenset[str]:
    if role == AccountType.ADMIN.value:
        return ADMIN_CATEGORIES
    if role == AccountType.MODERATOR.value:
        return MODERATOR_CATEGORIES
    return frozenset()


def create_staff_notification(
    db: Database,
    *,
    audience: str,
    category: str,
    heading: str,
    body: str,
    target_kind: Optional[str] = None,
    target_id: Optional[str] = None,
    dedup_key: Optional[str] = None,
) -> Optional[str]:
    """Store one staff notice. Idempotent on ``dedup_key``; never raises.

    A retried report filing must not double-notify the whole staff, so an
    event with a natural identity (a report id, a note id) carries it as
    ``dedup_key`` and a second insert for the same key returns the first row
    instead of writing another. Best-effort by contract: notification failure
    must never break the report or note write it annotates, so every failure
    (including a lost dedup race) answers None rather than raising.
    """
    if audience not in ("admin", "moderator", "all"):
        return None
    if category not in STAFF_NOTIFICATION_CATEGORIES:
        return None
    if (target_kind is None) != (target_id is None):
        return None
    try:
        with db.sessions.begin() as session:
            row = StaffNotificationRecord(
                audience=audience,
                category=category,
                heading=heading[:120],
                body=body[:500],
                created_at=datetime.now(timezone.utc),
                target_kind=target_kind,
                target_id=target_id,
                dedup_key=dedup_key,
            )
            session.add(row)
            session.flush()
            return str(row.id)
    except IntegrityError:
        # Lost a dedup race (or a retry): the notice is already stored, which
        # is the state the caller wanted. Read the winner back so the caller
        # still gets an id.
        try:
            with db.sessions() as session:
                winner = session.scalar(
                    select(StaffNotificationRecord).where(
                        StaffNotificationRecord.dedup_key == dedup_key
                    )
                )
                return str(winner.id) if winner is not None else None
        except Exception:
            return None
    except Exception:
        return None


def notify_report_filed(
    db: Database,
    *,
    kind: str,
    report_id: str,
    summary: str,
) -> Optional[str]:
    """One ``report`` notice to all staff that a report was filed.

    ``kind`` names the queue in human terms (``confession``, ``comment``,
    ``love note`` or ``chat message``); ``report_id`` is the idempotency key.
    """
    return create_staff_notification(
        db,
        audience="all",
        category="report",
        heading=f"New {kind} report",
        body=summary[:500],
        dedup_key=f"report:{report_id}",
    )


def notify_love_note_pending(db: Database, *, note_id: str) -> Optional[str]:
    """One ``love_note`` notice to all staff that a note awaits review."""
    return create_staff_notification(
        db,
        audience="all",
        category="love_note",
        heading="Love note awaiting approval",
        body=(
            "A love note was submitted and is waiting for approval. "
            "Open Love Notes review to approve or reject it."
        ),
        dedup_key=f"love-note-pending:{note_id}",
    )


# ---------------------------------------------------------------------------
# Live game alerts (computed, never stored)
# ---------------------------------------------------------------------------

def _game_alert(
    *,
    alert_id: str,
    heading: str,
    body: str,
    created_at: datetime,
) -> Dict[str, Any]:
    return {
        "id": alert_id,
        "audience": "admin",
        "category": "game",
        "heading": heading,
        "body": body,
        "created_at": isoformat_datetime(created_at),
        "is_read": False,
        "is_live": True,
        "target_kind": None,
        "target_id": None,
    }


def compute_game_alerts(db: Database, *, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Every game condition currently needing an admin's hand, newest first.

    Read-only: no rows are written, so an alert appears when its condition
    holds and vanishes the moment an admin fixes it, with no sweeper. The
    conditions mirror what the scheduler and the admin console already know:

    * no featured game set for today (UTC);
    * a Wordle/Anagram session stuck ``pending`` in ``manual`` content mode --
      the toggle between admin-fed and random lives in ``game_configs``, and
      ``manual`` means the admin must supply (or schedule) the word;
    * a Connections session ``pending`` -- Connections has no random mode, so
      every puzzle is admin-fed;
    * Trivia Drop with no live or scheduled question -- questions release
      individually, so an empty pipeline means nothing will ever play.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    today = moment.date()
    alerts: List[Dict[str, Any]] = []
    with db.sessions() as session:
        featured = session.scalar(
            select(FeaturedGameRecord.id).where(FeaturedGameRecord.game_date == today).limit(1)
        )
        if featured is None:
            alerts.append(
                _game_alert(
                    alert_id=f"game:featured-missing:{today.isoformat()}",
                    heading="No featured game set for today",
                    body=(
                        f"No game is featured for {today.isoformat()}. "
                        "Open the Games console to feature one."
                    ),
                    created_at=moment,
                )
            )
        for game_id, model, label in (
            ("wordle", WordleGameRecord, "Wordle"),
            ("anagram", AnagramGameRecord, "Anagram"),
        ):
            config = session.get(GameConfigRecord, game_id)
            content_mode = (config.content_mode if config is not None else None) or "manual"
            if content_mode != "manual":
                continue
            if config is not None and (config.is_hidden or not config.enabled):
                continue
            pending = session.scalar(
                select(model.id).where(model.status == "pending").limit(1)
            )
            if pending is not None:
                alerts.append(
                    _game_alert(
                        alert_id=f"game:{game_id}-pending:{today.isoformat()}",
                        heading=f"{label} is waiting for its word",
                        body=(
                            f"{label} runs on admin-fed words and its session "
                            "is pending. Open the Games console to supply it."
                        ),
                        created_at=moment,
                    )
                )
        connections_config = session.get(GameConfigRecord, "connections")
        if connections_config is None or (
            not connections_config.is_hidden and connections_config.enabled
        ):
            pending_connections = session.scalar(
                select(ConnectionsGameRecord.id)
                .where(ConnectionsGameRecord.status == "pending")
                .limit(1)
            )
            if pending_connections is not None:
                alerts.append(
                    _game_alert(
                        alert_id=f"game:connections-pending:{today.isoformat()}",
                        heading="Connections is waiting for its puzzle",
                        body=(
                            "Connections has no random mode -- every puzzle is "
                            "fed by an admin -- and its session is pending. "
                            "Open the Games console to supply the sixteen words."
                        ),
                        created_at=moment,
                    )
                )
        trivia_config = session.get(GameConfigRecord, "trivia-drop")
        if trivia_config is None or (
            not trivia_config.is_hidden and trivia_config.enabled
        ):
            pipeline = session.scalar(
                select(TriviaDropQuestionRecord.id)
                .where(TriviaDropQuestionRecord.status.in_(("active", "scheduled")))
                .limit(1)
            )
            if pipeline is None:
                alerts.append(
                    _game_alert(
                        alert_id=f"game:trivia-empty:{today.isoformat()}",
                        heading="Trivia Drop has no live question",
                        body=(
                            "Trivia Drop releases questions individually and "
                            "none is live or scheduled. Open the Games console "
                            "to add one."
                        ),
                        created_at=moment,
                    )
                )
    return alerts


# ---------------------------------------------------------------------------
# Preferences + master switch
# ---------------------------------------------------------------------------

def _default_preferences() -> Dict[str, bool]:
    return {"reports_enabled": True, "love_notes_enabled": True, "games_enabled": True}


def get_staff_preferences(db: Database, account: AccountDetails) -> Dict[str, bool]:
    """This staff account's category switches, defaulting to all-on."""
    with db.sessions() as session:
        row = session.get(
            StaffNotificationPreferenceRecord, (account.account_type, str(account.id))
        )
        if row is None:
            return _default_preferences()
        return {
            "reports_enabled": bool(row.reports_enabled),
            "love_notes_enabled": bool(row.love_notes_enabled),
            "games_enabled": bool(row.games_enabled),
        }


def update_staff_preferences(
    db: Database, account: AccountDetails, payload: Dict[str, Any]
) -> Dict[str, bool]:
    """Patch this staff account's category switches.

    Admins may toggle all three; moderators only ``reports_enabled`` and
    ``love_notes_enabled`` -- a moderator has no game notices to switch, so
    writing ``games_enabled`` for one is refused rather than stored.
    Unknown keys are refused the same way.
    """
    role = _role_of(account)
    allowed = (
        {"reports_enabled", "love_notes_enabled", "games_enabled"}
        if role == AccountType.ADMIN.value
        else {"reports_enabled", "love_notes_enabled"}
    )
    for key in payload:
        if key not in allowed:
            from fastapi import HTTPException, status

            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown or disallowed preference: {key}",
            )
    values = {key: bool(payload[key]) for key in payload}
    with db.sessions.begin() as session:
        row = session.get(
            StaffNotificationPreferenceRecord, (account.account_type, str(account.id))
        )
        if row is None:
            row = StaffNotificationPreferenceRecord(
                account_type=account.account_type, account_id=str(account.id)
            )
            session.add(row)
        for key, value in values.items():
            setattr(row, key, value)
        session.flush()
        return {
            "reports_enabled": bool(row.reports_enabled),
            "love_notes_enabled": bool(row.love_notes_enabled),
            "games_enabled": bool(row.games_enabled),
        }


def get_staff_notifications_enabled(db: Database, account: AccountDetails) -> bool:
    """This staff account's master switch (``notifications_enabled``)."""
    model = ACCOUNT_MODELS.get(_role_of(account))
    if model not in (AdminRecord, ModeratorRecord):
        return False
    with db.sessions() as session:
        row = session.get(model, str(account.id))
        return bool(row.notifications_enabled) if row is not None else False


def set_staff_notifications_enabled(
    db: Database, account: AccountDetails, *, enabled: bool
) -> bool:
    """Flip this staff account's master switch. Returns the stored value."""
    from fastapi import HTTPException, status

    model = ACCOUNT_MODELS.get(_role_of(account))
    if model not in (AdminRecord, ModeratorRecord):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only staff accounts have a notification switch.",
        )
    with db.sessions.begin() as session:
        row = session.get(model, str(account.id))
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Account not found."
            )
        row.notifications_enabled = bool(enabled)
        session.flush()
        return bool(row.notifications_enabled)


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

def _serialise_stored(
    row: StaffNotificationRecord, *, is_read: bool
) -> Dict[str, Any]:
    return {
        "id": str(row.id),
        "audience": row.audience,
        "category": row.category,
        "heading": row.heading,
        "body": row.body,
        "created_at": isoformat_datetime(row.created_at),
        "is_read": is_read,
        "is_live": False,
        "target_kind": row.target_kind,
        "target_id": row.target_id,
    }


def list_staff_notifications(
    db: Database,
    account: AccountDetails,
    *,
    limit: int = 50,
    skip: int = 0,
    query: Optional[str] = None,
    category: Optional[str] = None,
    unread_only: bool = False,
) -> Dict[str, Any]:
    """One page of this staff account's notices, newest first, with the total.

    Stored ``report``/``love_note`` rows addressed to this role (or to
    ``all``) plus -- for admins -- the live ``game`` alerts, merged and
    sorted newest-first. Preferences are returned beside the page but never
    filter it: the switches record intent for a future delivery layer, and
    the page shows everything regardless.
    """
    from fastapi import HTTPException, status

    role = _role_of(account)
    visible = _visible_categories(role)
    if not visible:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only staff accounts have notifications.",
        )
    if category is not None and category not in STAFF_NOTIFICATION_CATEGORIES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown category {category!r}.",
        )
    if category is not None and category not in visible:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This role has no notifications in that category.",
        )
    skip = max(0, int(skip or 0))
    limit = max(1, min(int(limit or 50), STAFF_NOTIFICATIONS_MAX_LIMIT))
    wanted = (visible & {category}) if category else set(visible)

    cleaned = (query or "").strip()
    filters: list = [
        or_(
            StaffNotificationRecord.audience == "all",
            StaffNotificationRecord.audience == role,
        ),
        StaffNotificationRecord.category.in_(sorted(wanted)),
    ]
    if cleaned:
        filters.append(
            ilike_contains(StaffNotificationRecord.heading, cleaned)
            | ilike_contains(StaffNotificationRecord.body, cleaned)
        )
    if unread_only:
        # A SQL NOT EXISTS, so the page and the total count the same unread
        # rows rather than paging everything and filtering in Python (which
        # would under-fill the page and over-count the total).
        filters.append(
            ~select(StaffNotificationReadRecord.notification_id)
            .where(
                StaffNotificationReadRecord.notification_id == StaffNotificationRecord.id,
                StaffNotificationReadRecord.account_type == account.account_type,
                StaffNotificationReadRecord.account_id == str(account.id),
            )
            .exists()
        )
    with db.sessions() as session:
        total_stored = (
            session.scalar(
                select(func.count())
                .select_from(StaffNotificationRecord)
                .where(*filters)
            )
            or 0
        )
        rows = session.scalars(
            select(StaffNotificationRecord)
            .where(*filters)
            .order_by(
                StaffNotificationRecord.created_at.desc(),
                StaffNotificationRecord.id.asc(),
            )
            .offset(skip)
            .limit(limit)
        ).all()
        read_ids = set(
            session.scalars(
                select(StaffNotificationReadRecord.notification_id).where(
                    StaffNotificationReadRecord.account_type == account.account_type,
                    StaffNotificationReadRecord.account_id == str(account.id),
                )
            ).all()
        )
    stored = [_serialise_stored(row, is_read=str(row.id) in read_ids) for row in rows]

    live: List[Dict[str, Any]] = []
    if "game" in wanted and role == AccountType.ADMIN.value:
        live = compute_game_alerts(db)
        if cleaned:
            lowered = cleaned.lower()
            live = [
                alert
                for alert in live
                if lowered in alert["heading"].lower() or lowered in alert["body"].lower()
            ]

    # Stored rows page in SQL; live game alerts are a handful of computed
    # conditions, always complete. Merged newest-first by timestamp string
    # (all ISO-8601 UTC), stored first on ties.
    merged = sorted(
        stored + (live if skip == 0 else []),
        key=lambda entry: str(entry["created_at"]),
        reverse=True,
    )
    total = total_stored + (len(live) if skip == 0 else 0)

    return {
        "notifications": merged,
        "total": total,
        "skip": skip,
        "limit": limit,
        "preferences": get_staff_preferences(db, account),
        "notifications_enabled": get_staff_notifications_enabled(db, account),
        "categories": sorted(visible),
    }


def mark_staff_notification_read(
    db: Database, account: AccountDetails, *, notification_id: str
) -> None:
    """Record that this staff account has read one stored notice.

    Live game alerts carry synthetic ids and hold no read state -- they clear
    themselves when fixed -- so reading one is a no-op rather than an error.
    """
    if notification_id.startswith("game:"):
        return
    role = _role_of(account)
    visible = _visible_categories(role)
    if not visible:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only staff accounts have notifications.",
        )
    if not is_valid_id(notification_id):
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found."
        )
    with db.sessions.begin() as session:
        row = session.get(StaffNotificationRecord, str(notification_id))
        if row is None:
            from fastapi import HTTPException, status

            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found."
            )
        if row.audience not in ("all", role) or row.category not in visible:
            from fastapi import HTTPException, status

            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found."
            )
        session.merge(
            StaffNotificationReadRecord(
                notification_id=str(row.id),
                account_type=account.account_type,
                account_id=str(account.id),
                read_at=datetime.now(timezone.utc),
            )
        )


def unread_staff_count(db: Database, account: AccountDetails) -> Dict[str, Any]:
    """How many stored notices this staff account has not read, by category."""
    role = _role_of(account)
    visible = _visible_categories(role)
    if not visible:
        return {"unread": 0, "by_category": {}}
    with db.sessions() as session:
        rows = session.scalars(
            select(StaffNotificationRecord).where(
                or_(
                    StaffNotificationRecord.audience == "all",
                    StaffNotificationRecord.audience == role,
                ),
                StaffNotificationRecord.category.in_(sorted(visible)),
            )
        ).all()
        read_ids = set(
            session.scalars(
                select(StaffNotificationReadRecord.notification_id).where(
                    StaffNotificationReadRecord.account_type == account.account_type,
                    StaffNotificationReadRecord.account_id == str(account.id),
                )
            ).all()
        )
    by_category: Dict[str, int] = {name: 0 for name in sorted(visible)}
    for row in rows:
        if str(row.id) not in read_ids:
            by_category[row.category] = by_category.get(row.category, 0) + 1
    return {"unread": sum(by_category.values()), "by_category": by_category}
