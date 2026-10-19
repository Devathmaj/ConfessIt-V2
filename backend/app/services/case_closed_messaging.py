"""Case Closed detective thread: per-couple chat inside the game window.

Reuses the matchmaking chat's implementation shape (quota derivation,
FOR UPDATE quota lock, anchor paging, Redis pub/sub realtime) without
reusing its tables or its allowances:

- Scope is the couple's Case Closed session (``session_id``), which already
  pins the game event, so the game id, session id and couple travel together.
- Storage is ``case_closed_messages`` (one thread per session).
- Quotas are 400 per thread / 200 per detective (``CASE_CLOSED_*``), double
  the matchmaking 200/100 so a full investigation plus accusation debate fits.
- Every turn is moderated by the central Content Safety dispatcher before it is
  persisted: a flagged turn is born hidden (never delivered, never in the
  transcript) yet still counts against the sender's allowance, exactly like a
  matchmaking chat message.
- Realtime rides the same ``redis_service`` pub/sub infrastructure on a
  distinct channel namespace (``case-closed:{session_id}``), so matchmaking
  threads and detective threads never cross.
- The thread is session-aware and short-lived by design: the first accusation
  confirmation from either detective ends it (the case ending does too), and
  the websocket handshake, the send path and the live ping re-check all read
  the same ``thread_state``. The transcript stays readable after closure --
  there is simply no live connection to keep and no new turn to send.

Sending never consumes an investigation action. Reading never marks the game
as started: the timer still starts only on the first successful investigation.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select

from ..db_models import (
    CaseClosedGameRecord,
    CaseClosedMessageRecord,
    CaseClosedMessageReportRecord,
    CaseClosedSessionRecord,
    CoupleGameSessionRecord,
    UserRecord,
)
from ..limits import (
    CASE_CLOSED_CONVERSATION_MESSAGE_MAX,
    CASE_CLOSED_CONVERSATION_PARTICIPANT_MESSAGE_MAX,
    MESSAGE_BODY_MAX,
    REPORT_REASON_MAX,
)
from ..logger import get_logger
from ..postgres import Database, is_valid_id, new_id
from ..utils import isoformat_datetime
from .chat_encryption import ChatDecryptionError, ChatEncryptionError
from .chat_evidence_service import (
    REPORT_KIND_CASE_CLOSED,
    capture_report_evidence,
)
from .chat_message_crypto import (
    decrypt_case_closed_message,
    encrypt_case_closed_message,
)
from .auth_service import ACCOUNT_UNAVAILABLE_DETAIL
from .redis_service import redis_service
from .report_counters import MESSAGE, record_removal, record_report_filed

logger = get_logger(__name__)

PARTICIPANT_LIMIT_REACHED = "participant"
CONVERSATION_LIMIT_REACHED = "conversation"

MESSAGE_LIMIT_PARTICIPANT_DETAIL = (
    f"You have sent all {CASE_CLOSED_CONVERSATION_PARTICIPANT_MESSAGE_MAX} messages you can send "
    "in this detective thread"
)
MESSAGE_LIMIT_CONVERSATION_DETAIL = (
    f"This detective thread has reached its limit of {CASE_CLOSED_CONVERSATION_MESSAGE_MAX} messages"
)

# The thread lives exactly as long as the accusation is open. The first
# confirmation from either detective ends it -- even before both sides agree --
# because the decision has been made and further lobbying would be out of place.
# The end of the case ends it too. Reads stay open either way: the transcript is
# the pair's record, only new turns stop.
THREAD_CLOSED_ACCUSATION_DETAIL = "The detective thread closed when an accusation was confirmed"
THREAD_CLOSED_CASE_DETAIL = "The detective thread closed when the case ended"
THREAD_CLOSED_SESSION_DETAIL = "The Case Closed session has expired"


def channel_for_session(session_id: str) -> str:
    """Redis namespace for one couple's detective thread (same infra, own channel)."""
    return f"case-closed:{session_id}"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def thread_state(
    row: CaseClosedSessionRecord,
    *,
    now: Optional[datetime] = None,
    expires_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Whether the thread still accepts turns, and the reason it stopped.

    Session expiry is checked first: once the game window is over the whole
    thread is finished, even if this row has not yet been finalized by the
    scheduler. A lock from a confirmed accusation is deliberately separate --
    it closes writes immediately but leaves messages readable until the session
    itself expires.
    """
    moment = now or datetime.now(timezone.utc)
    # ``expires_at`` lets a caller that already resolved the pinned event supply
    # the authoritative deadline for a row created before the column existed.
    deadline = row.expires_at or expires_at
    if deadline is not None:
        if moment >= _aware(deadline):
            return {"chat_open": False, "closed_reason": THREAD_CLOSED_SESSION_DETAIL}
    if (
        row.locked_culprit_id is not None
        or row.confirmed_a is not None
        or row.confirmed_b is not None
    ):
        return {"chat_open": False, "closed_reason": THREAD_CLOSED_ACCUSATION_DETAIL}
    if row.finalized or row.status != "investigating":
        return {"chat_open": False, "closed_reason": THREAD_CLOSED_CASE_DETAIL}
    return {"chat_open": True, "closed_reason": None}


def _resolve_thread(session, user_id: str) -> tuple[CaseClosedGameRecord, CaseClosedSessionRecord]:
    """The caller's live Case Closed session (game + couple row)."""
    from . import couple_gameplay
    from .case_closed_gameplay import _latest_global_game

    game = _latest_global_game(session)
    if game is None:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="No Case Closed event is live.")
    questionnaire = couple_gameplay._latest_playable_game(session)
    if questionnaire is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Form a mutual pair first.")
    couple = session.scalar(
        select(CoupleGameSessionRecord).where(
            CoupleGameSessionRecord.couple_game_id == questionnaire.id,
            or_(
                CoupleGameSessionRecord.user_a_id == user_id,
                CoupleGameSessionRecord.user_b_id == user_id,
            ),
        )
    )
    if couple is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Form a mutual pair first.")
    user_a_id, user_b_id = sorted([str(couple.user_a_id), str(couple.user_b_id)])
    row = session.scalar(
        select(CaseClosedSessionRecord).where(
            CaseClosedSessionRecord.case_closed_game_id == game.id,
            CaseClosedSessionRecord.user_a_id == user_a_id,
            CaseClosedSessionRecord.user_b_id == user_b_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Enter the event first.")
    return game, row


def thread_closed_reason(db: Database, user) -> Optional[str]:
    """Why the caller's thread takes no new turns, or None while it is open.

    Cheap pre-flight for the send path: refusing here keeps a closed thread from
    spending a Content Safety round trip on a turn that can never be stored. The
    authoritative check still runs inside the send transaction's row lock.
    """
    with db.sessions() as session:
        game, row = _resolve_thread(session, str(user.id))
        return thread_state(
            row, expires_at=_session_expiry(row, game)
        )["closed_reason"]


def get_thread(db: Database, user) -> Dict[str, Any]:
    """The caller's detective thread identity (game + session + partner).

    Carries the thread's open/closed state as well, so both the transcript read
    (which the dialog renders from) and the websocket handshake can tell a live
    thread from a finished one without re-deriving it.
    """
    with db.sessions() as session:
        game, row = _resolve_thread(session, str(user.id))
        partner_id = str(row.user_b_id) if str(row.user_a_id) == str(user.id) else str(row.user_a_id)
        partner = session.get(UserRecord, partner_id)
        return {
            "game_id": str(game.id),
            "session_id": str(row.id),
            "partner": {"id": partner_id, "name": partner.name if partner is not None else "Partner"},
            "conversation_limit": CASE_CLOSED_CONVERSATION_MESSAGE_MAX,
            "participant_limit": CASE_CLOSED_CONVERSATION_PARTICIPANT_MESSAGE_MAX,
            **thread_state(row, expires_at=_session_expiry(row, game)),
        }


def _usage(session, session_id: str, user_id: str) -> Dict[str, int]:
    """How much of a detective thread's allowance is spent, removed rows included.

    Counts every row in the thread on purpose, exactly like matchmaking chat: a
    turn Azure Content Safety took down is still a turn that was written, and
    letting its removal hand the allowance back would make the ceiling depend on
    how many turns the system happened to flag.
    """
    total = session.scalar(
        select(func.count()).select_from(CaseClosedMessageRecord).where(
            CaseClosedMessageRecord.session_id == str(session_id)
        )
    ) or 0
    mine = session.scalar(
        select(func.count()).select_from(CaseClosedMessageRecord).where(
            CaseClosedMessageRecord.session_id == str(session_id),
            CaseClosedMessageRecord.sender_user_id == str(user_id),
        )
    ) or 0
    return {"conversation_count": int(total), "participant_count": int(mine)}


def _quota(usage: Dict[str, int], *, messages_just_sent: int = 0) -> Dict[str, Any]:
    conversation_count = usage["conversation_count"] + messages_just_sent
    participant_count = usage["participant_count"] + messages_just_sent
    conversation_remaining = max(0, CASE_CLOSED_CONVERSATION_MESSAGE_MAX - conversation_count)
    participant_remaining = max(0, CASE_CLOSED_CONVERSATION_PARTICIPANT_MESSAGE_MAX - participant_count)
    if participant_remaining == 0:
        limit_reached = PARTICIPANT_LIMIT_REACHED
    elif conversation_remaining == 0:
        limit_reached = CONVERSATION_LIMIT_REACHED
    else:
        limit_reached = None
    return {
        "conversation_limit": CASE_CLOSED_CONVERSATION_MESSAGE_MAX,
        "participant_limit": CASE_CLOSED_CONVERSATION_PARTICIPANT_MESSAGE_MAX,
        "conversation_count": conversation_count,
        "participant_count": participant_count,
        "conversation_remaining": conversation_remaining,
        "participant_remaining": participant_remaining,
        "can_send": limit_reached is None,
        "limit_reached": limit_reached,
    }


def _refusal(quota: Dict[str, Any]) -> Optional[HTTPException]:
    if quota["limit_reached"] == PARTICIPANT_LIMIT_REACHED:
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=MESSAGE_LIMIT_PARTICIPANT_DETAIL)
    if quota["limit_reached"] == CONVERSATION_LIMIT_REACHED:
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=MESSAGE_LIMIT_CONVERSATION_DETAIL)
    return None


def _partner_blocked(session, row: CaseClosedSessionRecord, user_id: str) -> bool:
    """Whether the other detective's account is blocked.

    The session row knows the couple but not account state, so this is a live
    read. The matchmaking chat path refuses a send to a blocked partner for the
    same reason: a block must stop new turns reaching them, not merely hide the
    thread from their next request.
    """
    partner_id = (
        str(row.user_b_id) if str(row.user_a_id) == str(user_id) else str(row.user_a_id)
    )
    return bool(
        session.scalar(
            select(UserRecord.is_blocked).where(UserRecord.id == partner_id)
        )
    )


def _session_expiry(row: CaseClosedSessionRecord, game: CaseClosedGameRecord) -> datetime:
    """The session's own end, from the stored explicit column or the event.

    New sessions copy the pinned event's exclusive end date into
    ``CaseClosedSessionRecord.expires_at``. The fallback exists only for a row
    created by an older build; it uses the same authoritative event deadline
    rather than inventing a duration. A missing deadline is treated as "not
    expired" (the game itself is unreachable in that state) so a data problem
    can never delete a live conversation early.
    """
    try:
        from .case_closed_gameplay import _event_deadline

        fallback = _event_deadline(game)
    except Exception:
        fallback = None
    value = row.expires_at or fallback
    if value is None:
        return datetime.max.replace(tzinfo=timezone.utc)
    return _aware(value)


def conversation_quota(db: Database, user) -> Dict[str, Any]:
    with db.sessions() as session:
        _, row = _resolve_thread(session, str(user.id))
        return _quota(_usage(session, str(row.id), str(user.id)))


def send_message(db: Database, user, body: str) -> Dict[str, Any]:
    """Send one detective-thread turn, moderated before it is persisted.

    Same principal as matchmaking chat (everything >1 flagged+hidden): a flagged
    turn is stored born hidden -- never delivered and never in the transcript --
    but still counts against the sender's allowance, because a removed turn is
    still a turn that was written. A provider failure is unknown, never safe, so
    it aborts before anything is stored. Sending never charges an investigation
    action.
    """
    text = (body or "").strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Message body is required.")
    if len(text) > MESSAGE_BODY_MAX:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Message is too long.")

    # The thread is over once either detective has confirmed (or the case has
    # ended). Refuse before moderation so a closed thread never spends a safety
    # call; the transaction below re-checks under the row lock.
    closed_reason = thread_closed_reason(db, user)
    if closed_reason is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=closed_reason)

    # A partner blocked after the thread opened stops new turns, exactly as in
    # matchmaking chat. Refused before moderation so a blocked pair spends no
    # safety call, then re-checked under the send transaction's row lock.
    with db.sessions() as session:
        _, row = _resolve_thread(session, str(user.id))
        if _partner_blocked(session, row, str(user.id)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=ACCOUNT_UNAVAILABLE_DETAIL,
            )

    # Central moderation, before persistence: a flagged turn must never reach
    # the recipient, and a provider failure must abort before any row exists.
    # Imported locally (like the matchmaking path) so tests can patch the module
    # attribute; never branch on raw severities, only on the decision flags.
    from .content_safety_service import (
        AUTOMATED_REMOVED_BY,
        AUTOMATED_SOURCE,
        CONTENT_CASE_CLOSED_MESSAGE,
        ContentSafetyError,
        moderate_content,
        moderation_error_to_http,
    )

    try:
        decision = moderate_content(CONTENT_CASE_CLOSED_MESSAGE, text)
    except ContentSafetyError as exc:
        logger.error("Content Safety moderation failed (%s); aborting Case Closed send", exc.code)
        raise moderation_error_to_http(exc, content_type=CONTENT_CASE_CLOSED_MESSAGE) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Content Safety moderation failed unexpectedly: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Content moderation is temporarily unavailable. Please retry shortly.",
        ) from exc

    automated_report_id: Optional[str] = None
    try:
        with db.sessions.begin() as session:
            game, row = _resolve_thread(session, str(user.id))
            # Serialize quota checks per thread: count-then-insert races past the
            # same check would both see 399 and both insert without this lock.
            locked = session.scalar(
                select(CaseClosedSessionRecord).where(CaseClosedSessionRecord.id == str(row.id)).with_for_update()
            )
            assert locked is not None
            now = datetime.now(timezone.utc)
            expires_at = _session_expiry(locked, game)
            state = thread_state(locked, now=now, expires_at=expires_at)
            if not state["chat_open"]:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=state["closed_reason"])
            if _partner_blocked(session, locked, str(user.id)):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=ACCOUNT_UNAVAILABLE_DETAIL,
                )
            quota = _quota(_usage(session, str(row.id), str(user.id)))
            refusal = _refusal(quota)
            if refusal is not None:
                raise refusal
            if now >= expires_at:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=THREAD_CLOSED_SESSION_DETAIL,
                )
            # Allocate the id first: it is part of the authenticated metadata, so
            # the ciphertext cannot later be moved to another row unnoticed.
            message_id = new_id()
            encrypted = encrypt_case_closed_message(
                message_id=message_id,
                session_id=str(row.id),
                sender_user_id=str(user.id),
                plaintext=text,
            )
            record = CaseClosedMessageRecord(
                id=message_id,
                session_id=str(row.id), sender_user_id=str(user.id),
                body_ciphertext=encrypted.ciphertext,
                body_nonce=encrypted.nonce,
                body_key_version=encrypted.key_version,
                created_at=now, delete_after=expires_at, is_read=False,
            )
            if decision.should_hide:
                # Born hidden: the same removed_at write a moderator's removal
                # performs, so the transcript read (removed_at IS NULL) excludes
                # it from the first commit. The quota read does not filter it.
                record.removed_at = now
                record.removed_by_regno = AUTOMATED_REMOVED_BY
            session.add(record)
            session.flush()
            if decision.should_report:
                # Idempotent: exactly one automated report per turn. A new turn
                # cannot already have one, but the check keeps retries safe.
                existing = session.scalar(
                    select(CaseClosedMessageReportRecord.id).where(
                        CaseClosedMessageReportRecord.message_id == message_id,
                        CaseClosedMessageReportRecord.source == AUTOMATED_SOURCE,
                    ).limit(1)
                )
                if existing is None:
                    report_row = CaseClosedMessageReportRecord(
                        message_id=message_id,
                        reporter_regno=None,
                        reported_user_regno=user.regno,
                        reason=decision.report_reason,
                        reported_at=now,
                        source=AUTOMATED_SOURCE,
                        status="removed",
                        reviewed_at=now,
                        reviewed_by_regno=AUTOMATED_REMOVED_BY,
                    )
                    session.add(report_row)
                    session.flush()
                    automated_report_id = str(report_row.id)
                    capture_report_evidence(
                        session,
                        report_id=automated_report_id,
                        report_kind=REPORT_KIND_CASE_CLOSED,
                        message_id=message_id,
                        chat_id=str(row.id),
                        sender_ref=str(user.id),
                        plaintext=text,
                        message_created_at=now,
                        reported_at=now,
                    )
                    record_report_filed(session, kind=MESSAGE, author_regno=user.regno)
                    if decision.should_hide:
                        record_removal(session, kind=MESSAGE, author_regno=user.regno)
                else:
                    automated_report_id = str(existing)
            quota = _quota(quota, messages_just_sent=1)
            game_id, session_id = str(game.id), str(row.id)
            sender_name = user.name if hasattr(user, "name") else str(user.id)
    except HTTPException:
        raise
    except ChatEncryptionError as exc:
        logger.error(
            "Chat encryption failed for a new Case Closed message (%s); send aborted",
            exc.code,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not securely store this message.",
        ) from exc
    except Exception as exc:
        # Bookkeeping failed after a successful moderation result: never treat it
        # as success. The transaction above rolled back, so no partial row
        # survives; surface a retryable error (no body/secrets logged).
        logger.error(
            "Automated moderation bookkeeping failed for new Case Closed message: %s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not complete moderation for this message.",
        ) from exc

    # Best-effort staff/author notices: failures never unwind the committed
    # moderation outcome (report + hidden state are already durable).
    if automated_report_id is not None:
        try:
            from .staff_notification_service import notify_report_filed

            notify_report_filed(
                db, kind="case closed message", report_id=automated_report_id,
                summary=(
                    "A Case Closed detective-thread message was automatically reported by "
                    "Azure Content Safety. Open Reports review to rule on it."
                ),
            )
        except Exception:
            logger.warning(
                "Failed to queue staff notification for automated Case Closed report %s",
                automated_report_id,
            )
        if decision.should_hide:
            try:
                from .notification_service import create_notification_service as _notify

                _notify(
                    recipient_regno=user.regno,
                    heading="Message removed",
                    body="We removed your Case Closed message from ConfessIt.",
                    db=db,
                )
            except Exception:
                logger.warning(
                    "Failed to queue removal notice for Case Closed message %s", message_id
                )

    if decision.should_hide:
        # Flagged+hidden: the row stays hidden for audit (report + removed_at
        # already committed) and is never published, so the partner never sees
        # it. The caller is told plainly so the frontend shows a violation popup.
        logger.info(
            "Case Closed message %s withheld by content safety for %s "
            "(session %s, max=%s action=%s)",
            message_id, user.id, session_id, decision.max_severity, decision.action,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Your chat message was removed because it violated our content policies.",
        )

    payload = {
        "id": message_id,
        "game_id": game_id,
        "session_id": session_id,
        "sender_user_id": str(user.id),
        "sender_name": sender_name,
        "body": text,
        "created_at": isoformat_datetime(now),
        "is_read": False,
    }
    published = redis_service.publish_message(channel_for_session(session_id), payload)
    if not published:
        logger.error(
            "Case Closed realtime outage: message %s persisted but Redis publish failed (session %s)",
            message_id, session_id,
        )
    logger.info("Case Closed message sent in session %s by %s", session_id, user.id)
    # `realtime` says whether the live echo went out: the message is stored
    # either way, so a client that reads the field knows whether the partner's
    # open socket will show it without the thread being read again.
    return {
        **payload,
        "quota": quota,
        "realtime": "delivered" if published else "unavailable",
    }


def get_messages(
    db: Database, user, limit: int = CASE_CLOSED_CONVERSATION_MESSAGE_MAX,
    before: Optional[str] = None,
) -> Dict[str, Any]:
    """One thread window, oldest first with the newest last, plus the total."""
    limit = max(1, min(int(limit or CASE_CLOSED_CONVERSATION_MESSAGE_MAX), CASE_CLOSED_CONVERSATION_MESSAGE_MAX))
    now = datetime.now(timezone.utc)
    with db.sessions() as session:
        game, row = _resolve_thread(session, str(user.id))
        session_id = str(row.id)
        # Once the game session expires the transcript is no longer readable at
        # all, even if the next scheduler purge has not touched the rows yet.
        if now >= _session_expiry(row, game):
            return {"messages": [], "total": 0}
        # A removed turn is invisible on the participant read, which is the only
        # read there is (its quota weight survives in ``_usage``). ``delete_after``
        # is the session expiry captured when each turn was written.
        visible = [
            CaseClosedMessageRecord.session_id == session_id,
            CaseClosedMessageRecord.removed_at.is_(None),
            CaseClosedMessageRecord.delete_after > now,
        ]
        anchor = None
        if before is not None:
            anchor = session.get(CaseClosedMessageRecord, str(before))
            if (
                anchor is None
                or str(anchor.session_id) != session_id
                or _aware(anchor.delete_after) <= now
            ):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found.")
        if anchor is not None:
            visible.append(or_(
                CaseClosedMessageRecord.created_at < anchor.created_at,
                and_(
                    CaseClosedMessageRecord.created_at == anchor.created_at,
                    CaseClosedMessageRecord.id < str(anchor.id),
                ),
            ))
        total = session.scalar(
            select(func.count()).select_from(CaseClosedMessageRecord).where(
                CaseClosedMessageRecord.session_id == session_id,
                CaseClosedMessageRecord.removed_at.is_(None),
                CaseClosedMessageRecord.delete_after > now,
            )
        ) or 0
        newest_first = session.scalars(
            select(CaseClosedMessageRecord).where(*visible)
            .order_by(CaseClosedMessageRecord.created_at.desc(), CaseClosedMessageRecord.id.desc())
            .limit(limit)
        ).all()
        rows = list(reversed(newest_first))

    messages = []
    returned_ids = []
    for r in rows:
        try:
            plaintext = decrypt_case_closed_message(r)
        except ChatDecryptionError as exc:
            logger.error(
                "Case Closed message decryption failed for message %s in session %s (%s)",
                r.id,
                session_id,
                exc.code,
            )
            continue
        messages.append({
            "id": str(r.id), "game_id": None, "session_id": session_id,
            "sender_user_id": str(r.sender_user_id), "body": plaintext,
            "created_at": isoformat_datetime(r.created_at), "is_read": bool(r.is_read),
            "is_sender": str(r.sender_user_id) == str(user.id),
        })
        returned_ids.append(str(r.id))

    with db.sessions.begin() as session:
        if returned_ids:
            unread = session.scalars(select(CaseClosedMessageRecord).where(
                CaseClosedMessageRecord.id.in_(returned_ids),
                CaseClosedMessageRecord.sender_user_id != str(user.id),
                CaseClosedMessageRecord.is_read.is_(False),
            )).all()
            for r in unread:
                r.is_read = True
    return {"messages": messages, "total": int(total)}


def report_message(db: Database, user, message_id: str, reason: str) -> Dict[str, Any]:
    """File a complaint against a partner's detective-thread turn.

    Mirrors matchmaking chat: only the participant who *received* a turn may
    report it, one report per participant per turn. A report records the
    complaint and nothing else -- nothing is hidden here. A moderator rules on
    it from the admin report queue, exactly as for a matchmaking chat message.

    Authorisation reads the couple row directly rather than ``_resolve_thread``:
    a report has to stay possible after the event closed, which is the same
    window in which the transcript itself is still readable.
    """
    if not is_valid_id(message_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid message ID")
    text = (reason or "").strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A reason is required.")
    if len(text) > REPORT_REASON_MAX:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Reason is too long.")

    now = datetime.now(timezone.utc)
    with db.sessions() as session:
        message = session.get(CaseClosedMessageRecord, str(message_id))
        if message is None or _aware(message.delete_after) <= now:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
        row = session.get(CaseClosedSessionRecord, str(message.session_id))
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
        if row.expires_at is not None and _aware(row.expires_at) <= now:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
        participants = {str(row.user_a_id), str(row.user_b_id)}
        if str(user.id) not in participants:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not part of this detective thread",
            )
        if str(message.sender_user_id) == str(user.id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only report messages sent to you",
            )
        sender = session.get(UserRecord, str(message.sender_user_id))
        sender_regno = sender.regno if sender is not None else None
        duplicate = session.scalar(
            select(CaseClosedMessageReportRecord.id).where(
                CaseClosedMessageReportRecord.message_id == str(message_id),
                CaseClosedMessageReportRecord.reporter_regno == user.regno,
            ).limit(1)
        )
        if duplicate is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="You have already reported this message",
            )
        try:
            plaintext = decrypt_case_closed_message(message)
        except ChatDecryptionError as exc:
            logger.error(
                "Case Closed message decryption failed while filing report for %s (%s)",
                message_id,
                exc.code,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="This message could not be reported.",
            ) from exc

    with db.sessions.begin() as session:
        report_row = CaseClosedMessageReportRecord(
            message_id=str(message_id),
            reporter_regno=user.regno,
            reported_user_regno=sender_regno,
            reason=text,
            reported_at=now,
            status="pending",
        )
        session.add(report_row)
        session.flush()
        report_id = str(report_row.id)
        capture_report_evidence(
            session,
            report_id=report_id,
            report_kind=REPORT_KIND_CASE_CLOSED,
            message_id=str(message_id),
            chat_id=str(message.session_id),
            sender_ref=str(message.sender_user_id),
            plaintext=plaintext,
            message_created_at=message.created_at,
            reported_at=now,
        )
        record_report_filed(session, kind=MESSAGE, author_regno=sender_regno)

    try:
        from .staff_notification_service import notify_report_filed

        notify_report_filed(
            db, kind="case closed message", report_id=report_id,
            summary=(
                f"A Case Closed detective-thread message was reported by {user.regno}. "
                "Open Reports review to rule on it."
            ),
        )
    except Exception:
        logger.warning("Failed to queue staff notification for Case Closed report %s", message_id)

    logger.info("Case Closed message %s reported by %s", message_id, user.regno)
    return {"id": report_id, "message": "Message reported successfully"}
