"""Case Closed detective thread: per-couple chat inside the game window.

Reuses the matchmaking chat's implementation shape (quota derivation,
FOR UPDATE quota lock, anchor paging, Redis pub/sub realtime) without
reusing its tables or its allowances:

- Scope is the couple's Case Closed session (``session_id``), which already
  pins the game event, so the game id, session id and couple travel together.
- Storage is ``case_closed_messages`` (one thread per session).
- Quotas are 400 per thread / 200 per detective (``CASE_CLOSED_*``), double
  the matchmaking 200/100 so a full investigation plus accusation debate fits.
- Realtime rides the same ``redis_service`` pub/sub infrastructure on a
  distinct channel namespace (``case-closed:{session_id}``), so matchmaking
  threads and detective threads never cross.

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
    CaseClosedSessionRecord,
    CoupleGameSessionRecord,
    UserRecord,
)
from ..limits import (
    CASE_CLOSED_CONVERSATION_MESSAGE_MAX,
    CASE_CLOSED_CONVERSATION_PARTICIPANT_MESSAGE_MAX,
    MESSAGE_BODY_MAX,
)
from ..logger import get_logger
from ..postgres import Database
from ..utils import isoformat_datetime
from .redis_service import redis_service

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


def channel_for_session(session_id: str) -> str:
    """Redis namespace for one couple's detective thread (same infra, own channel)."""
    return f"case-closed:{session_id}"


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


def get_thread(db: Database, user) -> Dict[str, Any]:
    """The caller's detective thread identity (game + session + partner)."""
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
        }


def _usage(session, session_id: str, user_id: str) -> Dict[str, int]:
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


def conversation_quota(db: Database, user) -> Dict[str, Any]:
    with db.sessions() as session:
        _, row = _resolve_thread(session, str(user.id))
        return _quota(_usage(session, str(row.id), str(user.id)))


def send_message(db: Database, user, body: str) -> Dict[str, Any]:
    """Send one detective-thread turn (never charges an investigation action)."""
    text = (body or "").strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Message body is required.")
    if len(text) > MESSAGE_BODY_MAX:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Message is too long.")
    with db.sessions.begin() as session:
        game, row = _resolve_thread(session, str(user.id))
        # Serialize quota checks per thread: count-then-insert races past the
        # same check would both see 399 and both insert without this lock.
        locked = session.scalar(
            select(CaseClosedSessionRecord).where(CaseClosedSessionRecord.id == str(row.id)).with_for_update()
        )
        assert locked is not None
        quota = _quota(_usage(session, str(row.id), str(user.id)))
        refusal = _refusal(quota)
        if refusal is not None:
            raise refusal
        now = datetime.now(timezone.utc)
        record = CaseClosedMessageRecord(
            session_id=str(row.id), sender_user_id=str(user.id), body=text,
            created_at=now, is_read=False,
        )
        session.add(record)
        session.flush()
        message_id = str(record.id)
        quota = _quota(quota, messages_just_sent=1)
        game_id, session_id = str(game.id), str(row.id)
        sender_name = user.name if hasattr(user, "name") else str(user.id)

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
    if not redis_service.publish_message(channel_for_session(session_id), payload):
        logger.error(
            "Case Closed realtime outage: message %s persisted but Redis publish failed (session %s)",
            message_id, session_id,
        )
    logger.info("Case Closed message sent in session %s by %s", session_id, user.id)
    return {**payload, "quota": quota}


def get_messages(
    db: Database, user, limit: int = CASE_CLOSED_CONVERSATION_MESSAGE_MAX,
    before: Optional[str] = None,
) -> Dict[str, Any]:
    """One thread window, oldest first with the newest last, plus the total."""
    limit = max(1, min(int(limit or CASE_CLOSED_CONVERSATION_MESSAGE_MAX), CASE_CLOSED_CONVERSATION_MESSAGE_MAX))
    with db.sessions() as session:
        _, row = _resolve_thread(session, str(user.id))
        session_id = str(row.id)
        visible = [CaseClosedMessageRecord.session_id == session_id]
        anchor = None
        if before is not None:
            anchor = session.get(CaseClosedMessageRecord, str(before))
            if anchor is None or str(anchor.session_id) != session_id:
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
                CaseClosedMessageRecord.session_id == session_id
            )
        ) or 0
        newest_first = session.scalars(
            select(CaseClosedMessageRecord).where(*visible)
            .order_by(CaseClosedMessageRecord.created_at.desc(), CaseClosedMessageRecord.id.desc())
            .limit(limit)
        ).all()
        rows = list(reversed(newest_first))
        messages = [{
            "id": str(r.id), "game_id": None, "session_id": session_id,
            "sender_user_id": str(r.sender_user_id), "body": r.body,
            "created_at": isoformat_datetime(r.created_at), "is_read": bool(r.is_read),
            "is_sender": str(r.sender_user_id) == str(user.id),
        } for r in rows]
        returned_ids = [str(r.id) for r in rows]

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
