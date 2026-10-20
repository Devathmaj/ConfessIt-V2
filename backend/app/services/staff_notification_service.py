# app/services/staff_notification_service.py

"""Role-based staff notifications: reports, love notes awaiting review, games.

Participant notices (``notifications``) are addressed to one regno. Staff
notices are addressed to a *role* -- every moderator on duty is owed the same
news about a fresh report, and every admin the same news about a game that
needs feeding -- so ``report`` and ``love_note`` events are stored once with
``audience='all'`` and read by whichever role asks, rather than fanned out
into one row per staff account.

One event is news for admins alone: a participant blocked automatically at the
removal threshold (``notify_user_auto_blocked``). Clearing a block is an
admin-only capability, so the notice is stored with ``audience='admin'`` and a
moderator has no route on which it could be acted on. Unlike every other stored
notice, it is staged into the caller's transaction rather than written through
``create_staff_notification``'s own, because it must commit with the block it
reports -- see ``notify_user_auto_blocked``.

``game`` notices are stored like every other category. They are written where
the condition is discovered rather than computed on read: a pending Wordle,
Anagram or Connections session is recorded by the scheduler pass that evaluates
that game's period, a missing featured pick is recorded by the daily scheduler
pass, and an empty Trivia Drop pipeline is recorded at application start-up.
The ``dedup_key`` carries the session id or UTC date so repeated passes and
restarts cannot fan the same condition out into many rows.

Preferences (which categories an account wants) are stored per staff account,
created with the account, but deliberately *not* wired to the feed: the list
returns every notice regardless of the switches. They gate email delivery,
which asks the same role-first rules as :func:`should_deliver_staff_notification`
so a moderator can never be sent a game notice even if a database row says
``games_enabled = true``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import event, func, or_, select
from sqlalchemy.exc import IntegrityError

from ..accounts import AccountType, normalize_account_type
from ..db_models import (
    ACCOUNT_MODELS,
    AdminRecord,
    FeaturedGameRecord,
    GameConfigRecord,
    ModeratorRecord,
    StaffNotificationPreferenceRecord,
    StaffNotificationReadRecord,
    StaffNotificationRecord,
    TriviaDropQuestionRecord,
)
from ..logger import get_logger
from ..models import AccountDetails
from ..postgres import Database, get_postgres_client, is_valid_id
from ..utils import ilike_contains, isoformat_datetime
from .email_delivery_service import enqueue_notification_email
from .notification_links import staff_notification_link

logger = get_logger(__name__)

# The whole category vocabulary. ``report`` covers every report queue
# (confession/comment, love-note and chat-message reports) and the automatic
# block at the removal threshold, which shares the category because it leaves no
# queue entry of its own; ``love_note`` is a note awaiting approval; ``game`` is
# anything an admin must feed.
STAFF_NOTIFICATION_CATEGORIES: tuple[str, ...] = ("report", "love_note", "game")

# What each role may see. Moderators rule on content, so reports and the
# love-note backlog; games are admin business (content modes, schedules,
# featured picks), so only an admin sees those.
MODERATOR_CATEGORIES: frozenset[str] = frozenset({"report", "love_note"})
ADMIN_CATEGORIES: frozenset[str] = frozenset({"report", "love_note", "game"})

# Which stored switch records consent for each category. This mapping is only
# consulted after the role has admitted the category: the switch is a
# preference, not an authorisation boundary.
CATEGORY_PREFERENCE_COLUMNS: dict[str, str] = {
    "report": "reports_enabled",
    "love_note": "love_notes_enabled",
    "game": "games_enabled",
}

STAFF_NOTIFICATIONS_MAX_LIMIT = 100

# The widths of the stored notice columns. Delivery reuses the exact clipped
# values the inbox stores, so an email cannot say more than the notice it reports.
STAFF_NOTIFICATION_HEADING_MAX = 120
STAFF_NOTIFICATION_BODY_MAX = 500

# Which account tables an audience names. Delivery iterates these roles, never
# the other way around: a moderator's stored preference cannot widen this set.
STAFF_AUDIENCE_ROLES: dict[str, tuple[str, ...]] = {
    "admin": (AccountType.ADMIN.value,),
    "moderator": (AccountType.MODERATOR.value,),
    "all": (AccountType.ADMIN.value, AccountType.MODERATOR.value),
}


def _role_of(account: AccountDetails) -> str:
    return normalize_account_type(account.account_type)


def _visible_categories(role: str) -> frozenset[str]:
    if role == AccountType.ADMIN.value:
        return ADMIN_CATEGORIES
    if role == AccountType.MODERATOR.value:
        return MODERATOR_CATEGORIES
    return frozenset()


def staff_category_allowed_for_role(account_type: str, category: str) -> bool:
    """Whether this account type may receive this category at all.

    Role first, by design. A moderator's ``games_enabled`` column may say true
    -- creation writes all three switches on -- but a database row can be edited
    and must never be what authorises a game delivery. Game notices are admin
    business; reports and love notes are shared by both staff roles.
    """
    return category in _visible_categories(normalize_account_type(account_type))


def should_deliver_staff_notification(
    db: Database,
    *,
    account_type: str,
    account_id: str,
    category: str,
) -> bool:
    """Whether one staff account may be sent one category on any channel.

    This is the gate every delivery channel, including a game-config email
    sender, has to ask before composing a message. The role check runs first and
    independently of the preference row, so a moderator is refused ``game`` even
    when the database says ``games_enabled = true``. Only after the role admits
    the category are the account's master switch and its stored category switch
    read. A missing preferences row keeps the historical all-on default rather
    than silently muting an account; creation and the boot backfill mean a staff
    account should always have one.
    """
    role = normalize_account_type(account_type)
    if category not in _visible_categories(role):
        return False
    model = ACCOUNT_MODELS.get(role)
    if model not in (AdminRecord, ModeratorRecord):
        return False
    if not is_valid_id(account_id):
        return False
    with db.sessions() as session:
        account = session.get(model, str(account_id))
        if account is None or not bool(account.notifications_enabled):
            return False
        row = session.get(
            StaffNotificationPreferenceRecord, (role, str(account_id))
        )
        if row is None:
            return True
        return bool(getattr(row, CATEGORY_PREFERENCE_COLUMNS[category]))


def staff_notification_email_recipients(
    db: Database, *, audience: str, category: str
) -> list[dict[str, str]]:
    """The staff accounts an email for this stored notice may be sent to.

    The role check runs first, so ``game`` has no moderator recipients even when
    the database row says ``games_enabled = true``. For each permitted role the
    account's master switch is read, followed by its category preference. A
    missing preference row is all-on, matching the historical default and the
    backfill.
    """
    roles = STAFF_AUDIENCE_ROLES.get(audience, ())
    if category not in CATEGORY_PREFERENCE_COLUMNS:
        return []

    recipients: list[dict[str, str]] = []
    for role in roles:
        if not staff_category_allowed_for_role(role, category):
            continue
        model = ACCOUNT_MODELS.get(role)
        if model not in (AdminRecord, ModeratorRecord):
            continue
        with db.sessions() as session:
            accounts = [
                {
                    "id": str(account.id),
                    "email": account.email,
                    "name": account.name,
                    "regno": account.regno,
                }
                for account in session.scalars(select(model)).all()
            ]
        for account in accounts:
            if not should_deliver_staff_notification(
                db,
                account_type=role,
                account_id=account["id"],
                category=category,
            ):
                continue
            recipients.append(
                {
                    "email": account["email"],
                    "name": account["name"],
                    "regno": account["regno"],
                }
            )
    return recipients


def deliver_staff_notification_email(
    db: Database,
    *,
    audience: str,
    category: str,
    heading: str,
    body: str,
    game_id: Optional[str] = None,
) -> None:
    """Best-effort email fan-out for one freshly stored staff notice.

    Called only after the notice transaction commits and only for a newly
    inserted row, so retries and dedup winners do not send duplicate emails.
    Every recipient is attempted independently; one failing address or HTTP
    request must not suppress another recipient's notification.

    The email carries the review queue or game console the in-app notice is
    about. ``game_id`` narrows a game notice to its exact manage screen.
    """
    link = staff_notification_link(category, game_id=game_id)
    try:
        recipients = staff_notification_email_recipients(
            db, audience=audience, category=category
        )
    except Exception as exc:
        logger.warning(
            "Staff notification email recipients could not be read "
            "(category=%s, error_type=%s)",
            category,
            type(exc).__name__,
        )
        return

    for recipient in recipients:
        try:
            enqueue_notification_email(
                title=heading[:STAFF_NOTIFICATION_HEADING_MAX],
                body=body[:STAFF_NOTIFICATION_BODY_MAX],
                link=link,
                **recipient,
            )
        except Exception as exc:
            logger.warning(
                "Staff notification email was not queued "
                "(category=%s, error_type=%s)",
                category,
                type(exc).__name__,
            )


_PENDING_EMAILS_KEY = "staff_notification_pending_emails"
_EMAIL_LISTENER_KEY = "staff_notification_email_listener"


def _schedule_staff_notification_email(
    session,
    *,
    audience: str,
    category: str,
    heading: str,
    body: str,
    game_id: Optional[str] = None,
) -> None:
    """Send a staged staff notice after its transaction commits, never before.

    ``notify_user_auto_blocked`` writes into the caller's transaction because the
    alert must live or die with the block. Email is not transactional, so the
    payload is queued on the session and released by ``after_commit``; a rollback
    drops it. This keeps an admin from being told about a block that never
    happened while still covering the one direct-row insertion path.
    """
    pending = session.info.setdefault(_PENDING_EMAILS_KEY, [])
    pending.append(
        {
            "audience": audience,
            "category": category,
            "heading": heading,
            "body": body,
            "game_id": game_id,
        }
    )
    if session.info.get(_EMAIL_LISTENER_KEY):
        return
    session.info[_EMAIL_LISTENER_KEY] = True

    def _after_commit(committed_session):
        payloads = list(committed_session.info.pop(_PENDING_EMAILS_KEY, []))
        if not payloads:
            return
        try:
            db = get_postgres_client()
        except Exception as exc:
            logger.warning(
                "Staff notification email could not be delivered after commit "
                "(error_type=%s)",
                type(exc).__name__,
            )
            return
        for payload in payloads:
            deliver_staff_notification_email(db, **payload)

    def _after_rollback(rolled_back_session):
        rolled_back_session.info.pop(_PENDING_EMAILS_KEY, None)

    event.listen(session, "after_commit", _after_commit)
    event.listen(session, "after_rollback", _after_rollback)


def create_staff_notification(
    db: Database,
    *,
    audience: str,
    category: str,
    heading: str,
    body: str,
    created_at: Optional[datetime] = None,
    target_kind: Optional[str] = None,
    target_id: Optional[str] = None,
    dedup_key: Optional[str] = None,
    game_id: Optional[str] = None,
) -> Optional[str]:
    """Store one staff notice. Idempotent on ``dedup_key``; never raises.

    A retried report filing must not double-notify the whole staff, so an
    event with a natural identity (a report id, a note id) carries it as
    ``dedup_key`` and a second insert for the same key returns the first row
    instead of writing another. Best-effort by contract: notification failure
    must never break the report or note write it annotates, so every failure
    (including a lost dedup race) answers None rather than raising.

    ``created_at`` lets a scheduler event carry the instant the pass ran rather
    than the instant the insert happened; the two are the same for request-path
    notices, which leave it unset.

    A newly inserted notice is also offered to the email delivery layer after
    the row commits. The dedup winner path returns before that call, so a retry
    cannot email the same staff twice. ``game_id`` only picks the email link's
    destination; it is not stored, because the in-app feed opens the games
    dashboard for every game notice.
    """
    if audience not in ("admin", "moderator", "all"):
        return None
    if category not in STAFF_NOTIFICATION_CATEGORIES:
        return None
    if (target_kind is None) != (target_id is None):
        return None
    moment = created_at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    stored_heading = heading[:STAFF_NOTIFICATION_HEADING_MAX]
    stored_body = body[:STAFF_NOTIFICATION_BODY_MAX]
    try:
        with db.sessions.begin() as session:
            row = StaffNotificationRecord(
                audience=audience,
                category=category,
                heading=stored_heading,
                body=stored_body,
                created_at=moment,
                target_kind=target_kind,
                target_id=target_id,
                dedup_key=dedup_key,
            )
            session.add(row)
            session.flush()
            notification_id = str(row.id)
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

    deliver_staff_notification_email(
        db,
        audience=audience,
        category=category,
        heading=stored_heading,
        body=stored_body,
        game_id=game_id,
    )
    return notification_id


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


def notify_profile_picture_rejected(
    db: Database, *, regno: str, occurred_at: datetime
) -> Optional[str]:
    """One ``report``-category notice to all staff that an image was rejected.

    A rejected profile picture leaves no report row to open, so this notice is
    the whole trace a moderator gets: which account tried, and that Azure
    Content Safety refused the upload. ``occurred_at`` names the refused
    attempt (the consumed cooldown window), so a replayed rejection cannot
    notify the staff twice.
    """
    return create_staff_notification(
        db,
        audience="all",
        category="report",
        heading="Profile picture rejected by Azure Content Safety",
        body=(
            "Profile picture upload rejected by Azure Content Safety "
            f"for user {regno}."
        ),
        dedup_key=f"profile-picture-rejected:{regno}:{isoformat_datetime(occurred_at)}",
    )


def notify_user_auto_blocked(
    session, *, regno: str, name: str, removal_total: int
) -> None:
    """Stage one admin-only ``report`` notice about an automatic block.

    *Staged into the caller's session*, unlike every ``notify_*`` above: this
    notice must commit with the block it reports, or not at all. A block whose
    transaction rolls back must not leave an alert behind, and a committed
    block must not be able to lose its alert, so the row joins the same
    transaction as the ``is_blocked`` write and the session retirement (see
    ``report_counters._block_if_over_threshold``) -- the same reasoning that
    puts ``retire_account_sessions`` in the caller's transaction.

    Addressed to ``admin`` rather than ``all``: the block is the one moderation
    outcome a moderator cannot act on -- clearing it is ``users:update``, an
    admin-only capability -- so the notice is actionable for one role and
    noise for the other. It carries ``report`` all the same, like
    :func:`notify_profile_picture_rejected`: it is moderation news that leaves
    no report row of its own to open, not a fourth category.

    No ``dedup_key``: the block is written only on the ``is_blocked``
    transition, and a re-removal after an admin unblocks the account is a
    second, equally worth-notifying event rather than a duplicate of this one.

    ``removal_total`` is the participant's upheld-removal total as the blocking
    update left it, so the reader sees the figure that crossed the line without
    opening anything.
    """
    heading = "Participant automatically blocked"
    body = (
        f"User {regno} ({name}) was automatically blocked after "
        f"{removal_total} upheld removals."
    )[:STAFF_NOTIFICATION_BODY_MAX]
    session.add(
        StaffNotificationRecord(
            audience="admin",
            category="report",
            heading=heading,
            body=body,
            created_at=datetime.now(timezone.utc),
        )
    )
    _schedule_staff_notification_email(
        session,
        audience="admin",
        category="report",
        heading=heading,
        body=body,
    )


# ---------------------------------------------------------------------------
# Stored game notices
# ---------------------------------------------------------------------------

# The three games whose sessions can wait on an admin-supplied word/puzzle.
PENDING_GAME_LABELS: dict[str, str] = {
    "wordle": "Wordle",
    "anagram": "Anagram",
    "connections": "Connections",
}


def notify_game_pending(
    db: Database,
    *,
    game_id: str,
    session_id: str,
    created_at: datetime,
) -> Optional[str]:
    """One admin notice that a session is waiting for its content.

    The session id is the idempotency key: a scheduler pass that finds the same
    pending session again writes nothing, while the next pending session -- after
    the admin supplies this one -- gets its own notice.
    """
    label = PENDING_GAME_LABELS.get(game_id)
    if label is None:
        return None
    if game_id == "connections":
        heading = "Connections is waiting for its puzzle"
        body = (
            "Connections has no random mode -- every puzzle is fed by an admin -- "
            "and its session is pending. Open the Games console to supply the "
            "sixteen words."
        )
    else:
        heading = f"{label} is waiting for its word"
        body = (
            f"{label} runs on admin-fed words and its session is pending. "
            "Open the Games console to supply it."
        )
    return create_staff_notification(
        db,
        audience="admin",
        category="game",
        heading=heading,
        body=body,
        created_at=created_at,
        dedup_key=f"game:pending:{game_id}:{session_id}",
        game_id=game_id,
    )


def notify_featured_game_missing(
    db: Database, *, now: Optional[datetime] = None
) -> Optional[str]:
    """One admin notice per UTC date on which no game is featured.

    Called by the daily scheduler pass, after that pass has ensured the day's
    sessions. The query reads the real ``featured_games`` row for the date, so a
    pick scheduled ahead of time suppresses the notice; the date is the
    idempotency key, so repeated or catch-up passes write one row.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    today = moment.date()
    with db.sessions() as session:
        featured = session.scalar(
            select(FeaturedGameRecord.id)
            .where(FeaturedGameRecord.game_date == today)
            .limit(1)
        )
    if featured is not None:
        return None
    return create_staff_notification(
        db,
        audience="admin",
        category="game",
        heading="No featured game set for today",
        body=(
            f"No game is featured for {today.isoformat()}. "
            "Open the Games console to feature one."
        ),
        created_at=moment,
        dedup_key=f"game:featured-missing:{today.isoformat()}",
        game_id="featured",
    )


def notify_trivia_drop_empty(
    db: Database, *, now: Optional[datetime] = None
) -> Optional[str]:
    """One admin notice, at application start-up, that Trivia Drop has no question.

    Questions release individually, so the pipeline is empty exactly when
    nothing is live and nothing is scheduled. The UTC date is the idempotency
    key: several restarts in one day write one row, while a later day on which
    the pipeline is still empty can raise its own.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    today = moment.date()
    with db.sessions() as session:
        config = session.get(GameConfigRecord, "trivia-drop")
        if config is not None and (config.is_hidden or not config.enabled):
            return None
        pipeline = session.scalar(
            select(TriviaDropQuestionRecord.id)
            .where(TriviaDropQuestionRecord.status.in_(("active", "scheduled")))
            .limit(1)
        )
    if pipeline is not None:
        return None
    return create_staff_notification(
        db,
        audience="admin",
        category="game",
        heading="Trivia Drop has no live question",
        body=(
            "Trivia Drop releases questions individually and none is live or "
            "scheduled. Open the Games console to add one."
        ),
        created_at=moment,
        dedup_key=f"game:trivia-empty:{today.isoformat()}",
        game_id="trivia-drop",
    )


# ---------------------------------------------------------------------------
# Preferences + master switch
# ---------------------------------------------------------------------------

def _default_preferences() -> Dict[str, bool]:
    return {"reports_enabled": True, "love_notes_enabled": True, "games_enabled": True}


def get_staff_preferences(db: Database, account: AccountDetails) -> Dict[str, bool]:
    """This staff account's category switches.

    Creation writes the row, but a legacy account without one still reads as
    all-on rather than being silently muted.
    """
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
    writing ``games_enabled`` for one is refused rather than changed.
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
    """One page of this staff account's stored notices, newest first, with the total.

    Every category pages in SQL now, including ``game``: the scheduler and
    start-up checks write real rows, so read state and pagination apply the same
    way to a game notice as to a report. Preferences are returned beside the
    page but never filter it: the switches gate email delivery, while the page
    shows every stored notice regardless.
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
        total = (
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
    notifications = [
        _serialise_stored(row, is_read=str(row.id) in read_ids) for row in rows
    ]

    return {
        "notifications": notifications,
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

    Game notices are stored rows now, so they carry read state exactly like a
    report or love-note notice; there is no synthetic id to skip.
    """
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
