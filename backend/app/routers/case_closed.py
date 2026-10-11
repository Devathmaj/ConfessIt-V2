# app/routers/case_closed.py

import asyncio
import json
import time
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..config import settings
from ..dependencies import get_db
from ..limits import (
    CASE_CLOSED_CONVERSATION_MESSAGE_MAX,
    MESSAGE_BODY_MAX,
    REPORT_REASON_MAX,
    USER_DEPARTMENT_MAX,
    USER_REGNO_MAX,
    USER_YEAR_MAX,
    USER_YEAR_MIN,
)
from ..logger import get_logger
from ..models import (
    CaseClosedConfirmRequest,
    CaseClosedInvestigationSubmitRequest,
    CaseClosedProposeRequest,
    CouplePartnerPage,
    CoupleSelectionRequest,
)
from ..postgres import Database
from ..services import case_closed_gameplay
from ..services import case_closed_messaging
from ..services.authorization import require_participant
from ..services.case_closed_schemas import (
    CaseClosedCurrentState,
    CaseClosedResultView,
)
from ..services.couple_gameplay import COUPLE_PARTNERS_MAX_LIMIT
from ..services.redis_service import redis_service

logger = get_logger(__name__)

router = APIRouter(prefix="/games/case-closed", tags=["Case Closed"])


class CaseClosedMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=MESSAGE_BODY_MAX)

    @field_validator("body", mode="after")
    @classmethod
    def _validate_body_characters(cls, value: str) -> str:
        from ..models import _reject_unchangeable_characters

        return _reject_unchangeable_characters(value.strip(), "body")


class CaseClosedMessageReportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, max_length=USER_REGNO_MAX)
    reason: str = Field(min_length=1, max_length=REPORT_REASON_MAX)

    @field_validator("reason", mode="after")
    @classmethod
    def _validate_reason_characters(cls, value: str) -> str:
        from ..models import _reject_unchangeable_characters

        return _reject_unchangeable_characters(value.strip(), "reason")


@router.get("", response_model=CaseClosedCurrentState)
def current_state(
    db: Database = Depends(get_db),
    user=Depends(require_participant),
) -> CaseClosedCurrentState:
    """This caller's safe place in the active Case Closed event."""
    return CaseClosedCurrentState(**case_closed_gameplay.current_state(db, user))


@router.get("/partners", response_model=CouplePartnerPage)
def partner_candidates(
    db: Database = Depends(get_db),
    user=Depends(require_participant),
    skip: int = Query(0, ge=0, description="How many matching participants to skip."),
    limit: int = Query(
        50,
        ge=1,
        le=COUPLE_PARTNERS_MAX_LIMIT,
        description="How many matching participants to return. Clamped so one request can never materialise the whole participant table.",
    ),
    q: Optional[str] = Query(
        None,
        max_length=100,
        description="Case-insensitive substring match on the candidate's name, run in SQL.",
    ),
    gender_mode: Literal["different", "same"] = Query(
        "different",
        description="'different' hides the caller's own gender; 'same' shows only it. A discovery aid, never a rule about who may be chosen.",
    ),
    year: Optional[int] = Query(
        None, ge=USER_YEAR_MIN, le=USER_YEAR_MAX, description="Exact year of study, half of the cohort filter."
    ),
    department: Optional[str] = Query(
        None, max_length=USER_DEPARTMENT_MAX, description="Exact department, half of the cohort filter, case-insensitive."
    ),
) -> CouplePartnerPage:
    """One page of participants the caller may choose, excluding only themselves.

    The questionnaire's shared candidate list: the page, search and filters
    are its SQL ones (see ``couple_gameplay.list_partners``).
    """
    return case_closed_gameplay.list_partners(
        db,
        user,
        skip=skip,
        limit=limit,
        query=q,
        gender_mode=gender_mode,
        year=year,
        department=department,
    )


@router.post("/selection", response_model=CaseClosedCurrentState)
def choose_partner(
    payload: CoupleSelectionRequest,
    db: Database = Depends(get_db),
    user=Depends(require_participant),
) -> CaseClosedCurrentState:
    return CaseClosedCurrentState(
        **case_closed_gameplay.choose_partner(db, user, payload.selected_user_id)
    )


@router.post("/start", response_model=CaseClosedCurrentState)
def start_event(
    db: Database = Depends(get_db),
    user=Depends(require_participant),
) -> CaseClosedCurrentState:
    return CaseClosedCurrentState(**case_closed_gameplay.enter_event(db, user))


@router.post("/investigate")
def investigate(
    payload: CaseClosedInvestigationSubmitRequest,
    db: Database = Depends(get_db),
    user=Depends(require_participant),
):
    """Investigate one available lead (deterministic, no LLM).

    Returns the safe session view plus the lead's discovery text.
    Retries of an already-visited lead replay the stored discovery.
    """
    result = case_closed_gameplay.investigate_lead(
        db, user, payload.lead_id, payload.state_version
    )
    replay = bool(result.pop("_replay", False))
    lead_id = result.pop("_lead_id", payload.lead_id)
    discovery = result.pop("_discovery_text", "")
    already_known = bool(result.pop("_already_known", False))
    state = CaseClosedCurrentState(**result)
    return {
        "state": state,
        "lead_id": lead_id,
        "discovery_text": discovery,
        "already_known": already_known,
        "replay": replay,
    }


@router.post("/accusation/propose", response_model=CaseClosedCurrentState)
def propose_accusation(
    payload: CaseClosedProposeRequest,
    db: Database = Depends(get_db),
    user=Depends(require_participant),
) -> CaseClosedCurrentState:
    return CaseClosedCurrentState(
        **case_closed_gameplay.propose_accusation(db, user, payload.suspect_id)
    )


@router.post("/accusation/confirm", response_model=CaseClosedCurrentState)
def confirm_accusation(
    payload: CaseClosedConfirmRequest,
    db: Database = Depends(get_db),
    user=Depends(require_participant),
) -> CaseClosedCurrentState:
    return CaseClosedCurrentState(
        **case_closed_gameplay.confirm_accusation(db, user, payload.suspect_id)
    )


@router.get("/result", response_model=CaseClosedResultView)
def get_result(
    db: Database = Depends(get_db),
    user=Depends(require_participant),
) -> CaseClosedResultView:
    return CaseClosedResultView(**case_closed_gameplay.get_result(db, user))


# ---------------------------------------------------------------------------
# Detective thread: per-couple chat inside the game window.
# ---------------------------------------------------------------------------
# Same transport as matchmaking chat (REST send + Redis pub/sub WS), own
# scope (the couple's Case Closed session) and own quotas (400 per thread,
# 200 per detective). Messaging never consumes an investigation action.
# The thread lives only while the accusation is open: the first confirmation
# from either detective (or the end of the case) closes it for writes, refuses
# new socket handshakes, and is re-checked on every live socket ping so no
# connection outlives the thread. The transcript stays readable.


@router.get("/messages/thread")
def message_thread(
    db: Database = Depends(get_db),
    user=Depends(require_participant),
) -> dict:
    """The caller's detective thread identity (game + session + partner)."""
    return case_closed_messaging.get_thread(db, user)


@router.get("/messages")
def get_thread_messages(
    db: Database = Depends(get_db),
    user=Depends(require_participant),
    limit: int = Query(CASE_CLOSED_CONVERSATION_MESSAGE_MAX, ge=1, le=CASE_CLOSED_CONVERSATION_MESSAGE_MAX),
    before: Optional[str] = Query(None, description="Message id anchoring the older page."),
) -> dict:
    """One thread window, oldest first with the newest last, plus quota.

    The open/closed state travels with the transcript so the dialog can render
    the closed composer from the authoritative source, not only from the
    caller's game-state poll.
    """
    thread = case_closed_messaging.get_thread(db, user)
    page = case_closed_messaging.get_messages(db, user, limit=limit, before=before)
    return {
        "game_id": thread["game_id"],
        "session_id": thread["session_id"],
        "messages": page["messages"],
        "count": len(page["messages"]),
        "total": page["total"],
        "quota": case_closed_messaging.conversation_quota(db, user),
        "chat_open": thread["chat_open"],
        "closed_reason": thread["closed_reason"],
    }


@router.post("/messages", status_code=status.HTTP_201_CREATED)
def send_thread_message(
    payload: CaseClosedMessageCreate,
    db: Database = Depends(get_db),
    user=Depends(require_participant),
) -> dict:
    """Send one detective-thread turn. Never charges an investigation action."""
    return case_closed_messaging.send_message(db, user, payload.body)


@router.post("/messages/report", status_code=status.HTTP_201_CREATED)
def report_thread_message(
    payload: CaseClosedMessageReportCreate,
    db: Database = Depends(get_db),
    user=Depends(require_participant),
) -> dict:
    """Report a partner's detective-thread turn for moderation review."""
    return case_closed_messaging.report_message(db, user, payload.message_id, payload.reason)


WS_CLOSE_AUTH_REQUIRED = 4001
WS_CLOSE_FORBIDDEN = 4003
WS_CLOSE_SERVER_ERROR = 5000
WS_CLOSE_ORIGIN_REFUSED = 4403
WS_CLOSE_HANDSHAKE_FLOODED = 4429
WS_CLOSE_IDLE = 4400

WS_HANDSHAKE_LIMIT_PER_MINUTE = 30
WS_IDLE_TIMEOUT_SECONDS = 120.0


def _ws_client_ip(websocket: WebSocket) -> str:
    ip = websocket.client.host if websocket.client else "unknown"
    if settings.TRUSTED_PROXY_HOSTS.strip():
        forwarded = websocket.headers.get("x-forwarded-for")
        if forwarded and forwarded.split(",")[0].strip():
            ip = forwarded.split(",")[0].strip()
    return ip


def _ws_handshake_allowed(ip: str) -> bool:
    if WS_HANDSHAKE_LIMIT_PER_MINUTE <= 0:
        return True
    window = int(time.time() // 60)
    try:
        count = int(redis_service.client.incr(f"rl:ws:{ip}:{window}"))
        if count == 1:
            redis_service.client.expire(f"rl:ws:{ip}:{window}", 70)
    except Exception as exc:
        logger.warning("WS handshake limiter unavailable, allowing handshake: %s", exc)
        return True
    return count <= WS_HANDSHAKE_LIMIT_PER_MINUTE


def _thread_still_open(db: Database, user) -> bool:
    """Whether a live socket's thread still accepts turns.

    Re-checked on every ping (30s cadence, same request that keeps the socket
    alive) so a connection cannot outlive the thread just because the client
    has not noticed: the first confirmation, the case ending, or the event
    rolling over all close it. Any resolution failure counts as closed --
    there is nothing left to listen for, and REST stays the source of truth.
    """
    try:
        return bool(case_closed_messaging.get_thread(db, user)["chat_open"])
    except Exception:
        return False


@router.websocket("/ws")
async def thread_websocket(
    websocket: WebSocket,
    db: Database = Depends(get_db),
    token: Optional[str] = Query(None),
):
    """Live detective-thread turns over the shared Redis pub/sub.

    Same hardening as matchmaking chat (origin, flood brake, token,
    membership gates pre-accept; send-via-REST-only; idle reap). The only
    difference is entitlement: membership of the couple's Case Closed
    session rather than of a matchmaking conversation. The channel is
    ``case-closed:{session_id}`` (distinct namespace, same infrastructure).
    """
    from ..services.auth_service import verify_token_service
    from .auth import _request_origin

    origin = websocket.headers.get("origin")
    if origin is not None:
        from ..config import cors_allow_origins as _origins

        parsed = _request_origin(origin)
        allowed = {o.lower() for o in _origins()}
        if parsed is None or parsed not in allowed:
            logger.warning("Case Closed WS refused: foreign origin %r", origin)
            await websocket.close(code=WS_CLOSE_ORIGIN_REFUSED)
            return

    ip = _ws_client_ip(websocket)
    if not _ws_handshake_allowed(ip):
        logger.warning("Case Closed WS refused: handshake flood from %s", ip)
        await websocket.close(code=WS_CLOSE_HANDSHAKE_FLOODED)
        return

    if not token:
        await websocket.close(code=WS_CLOSE_AUTH_REQUIRED)
        return
    try:
        user = verify_token_service(token, db)
    except Exception as exc:
        logger.error("Case Closed WS auth failed: %s", exc)
        await websocket.close(code=WS_CLOSE_AUTH_REQUIRED)
        return

    try:
        thread = case_closed_messaging.get_thread(db, user)
    except HTTPException:
        logger.warning("Case Closed WS refused: no detective thread")
        await websocket.close(code=WS_CLOSE_FORBIDDEN)
        return
    except Exception as exc:
        logger.error("Case Closed WS thread lookup failed: %s", exc)
        await websocket.close(code=WS_CLOSE_FORBIDDEN)
        return

    if not thread["chat_open"]:
        # The thread is over: no handshake, so nothing persistent can be left
        # behind after the lock. 4003 is a no-retry code on the client.
        logger.info("Case Closed WS refused: thread closed for session %s", thread["session_id"])
        await websocket.close(code=WS_CLOSE_FORBIDDEN)
        return

    channel_id = case_closed_messaging.channel_for_session(thread["session_id"])
    await websocket.accept()

    try:
        logger.info("Case Closed WS connected: %s to session %s", user.regno, thread["session_id"])
        pubsub = redis_service.subscribe_to_conversation(channel_id)
        if not pubsub:
            await websocket.send_json({"error": "Failed to connect to message stream"})
            await websocket.close(code=WS_CLOSE_SERVER_ERROR)
            return

        await websocket.send_json({
            "type": "connected",
            "game_id": thread["game_id"],
            "session_id": thread["session_id"],
        })

        async def redis_listener():
            def get_redis_message():
                for message in pubsub.listen():
                    if message["type"] == "message":
                        return message
                return None

            try:
                while True:
                    message = await asyncio.to_thread(get_redis_message)
                    if message is None:
                        break
                    try:
                        data = json.loads(message["data"])
                        await websocket.send_json({"type": "message", "data": data})
                    except (json.JSONDecodeError, KeyError, TypeError):
                        logger.error("Invalid JSON from Redis: %s", message.get("data"))
                    except Exception as exc:
                        logger.error("Error sending message to Case Closed WS: %s", exc)
                        break
            except Exception as exc:
                logger.error("Case Closed Redis listener error: %s", exc)

        listener_task = asyncio.create_task(redis_listener())
        close_code = 1000
        try:
            while True:
                try:
                    data = await asyncio.wait_for(
                        websocket.receive_text(), timeout=WS_IDLE_TIMEOUT_SECONDS
                    )
                except asyncio.TimeoutError:
                    logger.info("Case Closed WS idle timeout")
                    close_code = WS_CLOSE_IDLE
                    break
                if data == "ping":
                    if not _thread_still_open(db, user):
                        logger.info("Case Closed WS closing: thread ended mid-connection")
                        close_code = WS_CLOSE_FORBIDDEN
                        break
                    await websocket.send_text("pong")
        except WebSocketDisconnect:
            close_code = 1000
        finally:
            listener_task.cancel()
            try:
                await listener_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.error("Case Closed WS listener task failed: %s", exc)
            redis_service.unsubscribe(pubsub, channel_id)
            try:
                await websocket.close(code=close_code)
            except Exception:
                pass
    except Exception as exc:
        logger.error("Case Closed WS error: %s", exc)
        try:
            await websocket.close(code=WS_CLOSE_SERVER_ERROR)
        except Exception:
            pass
