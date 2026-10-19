# app/routers/messages.py

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status, Query
from ..config import cors_allow_origins, settings
from ..postgres import Database
from typing import Annotated, Optional
import json
import asyncio
import time

from ..dependencies import get_db
from ..models import UserDetails
from ..services.auth_service import ACCOUNT_UNAVAILABLE_DETAIL
from ..services.authorization import require_participant
from ..services.message_service import message_service
from ..services.redis_service import redis_service
from ..logger import get_logger
from pydantic import BaseModel, Field, field_validator
from .auth import _request_origin

from ..limits import (
    CONVERSATION_MESSAGE_MAX,
    MESSAGE_BODY_MAX,
    REPORT_REASON_MAX,
    USER_REGNO_MAX,
)

logger = get_logger(__name__)

router = APIRouter(
    prefix="/messages",
    tags=["Messages"]
)


class MessageCreate(BaseModel):
    conversation_id: str = Field(max_length=USER_REGNO_MAX)
    body: str = Field(max_length=MESSAGE_BODY_MAX)

    @field_validator("body", mode="after")
    @classmethod
    def _validate_body_characters(cls, value: str) -> str:
        from ..models import _reject_unchangeable_characters

        return _reject_unchangeable_characters(value, "body")


class MessageReport(BaseModel):
    message_id: str = Field(max_length=USER_REGNO_MAX)
    reason: str = Field(max_length=REPORT_REASON_MAX)


@router.post("/send", status_code=status.HTTP_201_CREATED)
def send_message(
    message_data: MessageCreate,
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db)
):
    """
    Send a message in a conversation
    
    Validates:
    - User is participant in conversation
    - Conversation is accepted
    - Conversation is not blocked
    - Conversation is not expired
    - User is within their own message allowance, and the conversation is within its own

    The response carries the message's `quota` so the client can show how much room is left
    without a second request.
    """
    return message_service.send_message(
        db=db,
        conversation_id=message_data.conversation_id,
        sender=current_user,
        body=message_data.body
    )


@router.get("/{conversation_id}")
def get_messages(
    conversation_id: str,
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db),
    # Upper-bounded by the conversation ceiling rather than by an arbitrary window: a
    # conversation cannot hold more than this many messages, so a larger `limit` could only
    # ever be a number that returns the same reply. Refusing it says so, where silently
    # clamping would leave the caller believing it had asked for a window it cannot have.
    limit: int = Query(CONVERSATION_MESSAGE_MAX, ge=1, le=CONVERSATION_MESSAGE_MAX),
    before: Optional[str] = Query(
        None,
        description="A message id anchoring the older page: the read answers the next page older than it, so the dialog grows upwards without shifting under concurrent sends.",
    ),
):
    """
    Get one thread window for the dialog, oldest first with the newest last.

    The dialog opens on the newest window and grows upwards: `before` names the oldest
    message on screen and the read answers the next older page, while `total` counts
    every visible message in the thread so the dialog knows how many older ones remain.
    An anchor is stable where an offset is not -- a message sent between two reads
    would shift every offset down and repeat a row, while a row id cannot move.

    `quota` is the message allowance as it stands for the caller, which is what the dialog
    reads to decide whether to offer the composer at all.
    """
    page = message_service.get_messages(
        db=db,
        conversation_id=conversation_id,
        user=current_user,
        limit=limit,
        before=before,
    )

    return {
        "conversation_id": conversation_id,
        "messages": page["messages"],
        "count": len(page["messages"]),
        "total": page["total"],
        "quota": message_service.conversation_quota(
            db=db, conversation_id=conversation_id, regno=current_user.regno
        ),
    }


@router.post("/report", status_code=status.HTTP_201_CREATED)
def report_message(
    report_data: MessageReport,
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db)
):
    """
    Report a message
    
    Validates:
    - Reporter is receiver of the message
    - No duplicate reports
    """
    return message_service.report_message(
        db=db,
        message_id=report_data.message_id,
        reporter=current_user,
        reason=report_data.reason
    )


# ---------------------------------------------------------------------------
# WebSocket hardening (SEC-003).
# ---------------------------------------------------------------------------
# Application-defined close codes (4000-4999): distinct from HTTP statuses so
# edge logs and client telemetry can tell a refused handshake from a failed
# request. 4001/4003/5000 predate this hardening and keep their meaning.
WS_CLOSE_AUTH_REQUIRED = 4001
WS_CLOSE_FORBIDDEN = 4003
WS_CLOSE_SERVER_ERROR = 5000
# Origin refused: the handshake carried an Origin outside the CORS allowlist.
WS_CLOSE_ORIGIN_REFUSED = 4403
# Handshake flood brake tripped (4429 mirrors HTTP 429).
WS_CLOSE_HANDSHAKE_FLOODED = 4429
# Silent socket reaped: the client stopped talking (see the idle timeout).
WS_CLOSE_IDLE = 4400

# Handshakes per minute per client IP, counted in Redis and shared by every
# worker. Generous on purpose: one socket per open dialog is legitimate, and
# shared campus NATs put many users behind one address -- this catches floods
# (reconnect loops, socket farms), not people. Zero or negative disables it.
WS_HANDSHAKE_LIMIT_PER_MINUTE = 30

# A socket that hears nothing for this long is dead: the client pings every 30
# seconds, so four missed intervals means the tab is suspended, the network is
# gone, or the peer never intends to talk. Reaping bounds a slow accumulation
# of silent sockets (each holding a Redis subscription) into a steady state.
WS_IDLE_TIMEOUT_SECONDS = 120.0


def _ws_client_ip(websocket: WebSocket) -> str:
    """The client-facing IP for this handshake, proxy-aware like the HTTP brake.

    Mirrors ``GlobalRateLimitMiddleware``: the socket peer by default, the
    leftmost ``X-Forwarded-For`` entry only when proxies are configured -- which
    in production is the edge (see ``TRUSTED_PROXY_HOSTS``).
    """
    ip = websocket.client.host if websocket.client else "unknown"
    if settings.TRUSTED_PROXY_HOSTS.strip():
        forwarded = websocket.headers.get("x-forwarded-for")
        if forwarded and forwarded.split(",")[0].strip():
            ip = forwarded.split(",")[0].strip()
    return ip


def _ws_handshake_allowed(ip: str) -> bool:
    """One fixed window per minute per IP for WebSocket handshakes (SEC-003).

    Same INCR/EXPIRE shape as the HTTP flood brake, and the same fail-open
    contract: a Redis outage must not become a chat outage, and the per-dialog
    auth gates below still hold.
    """
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


async def _socket_account_valid(
    session_id: str, account_id: str, account_type: str, db: Database
) -> bool:
    """Whether an accepted socket's session is still live.

    A WebSocket has no per-request dependency to re-run, so this is the one
    revalidation seam used both for frames the client sends and for messages
    about to be forwarded from Redis. Any failure is closed as auth-required;
    a database error never becomes permission to keep delivering.
    """
    from ..services.auth_service import revalidate_websocket_session

    try:
        return await asyncio.to_thread(
            revalidate_websocket_session,
            db,
            session_id=session_id,
            account_id=account_id,
            account_type=account_type,
        ) is not None
    except Exception as exc:
        logger.error("WebSocket auth revalidation failed: %s", exc)
        return False


@router.websocket("/ws/{conversation_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    conversation_id: str,
    db: Database = Depends(get_db),
    ticket: Optional[str] = Query(None)
):
    """Real-time messages for one conversation over Redis pub/sub.

    A browser ``WebSocket`` cannot set request headers, so the credential travels
    in the query string. It is deliberately *not* the access token: Uvicorn logs the
    accepted handshake path with its query string at INFO, which would put a
    30-minute bearer into stdout and every log store it reaches. Instead the client
    exchanges its access token over the authenticated HTTP API for a single-use
    ticket that expires in under a minute (``POST /auth/ws-ticket``); even a ticket
    copied from a log is useless after one handshake or 60 seconds.

    What else bounds exposure:

    * no subscription exists before auth -- a captured URL is a read of one dialog
      handshake, and the edge access log drops the query string (see
      nginx.prod.conf.example);
    * nothing is accepted before auth: origin, flood brake, ticket and entitlement
      gates all run pre-accept and refuse with a bare close, so an unauthenticated
      peer can never hold a socket, only fail a handshake;
    * a browser handshake always carries ``Origin``; one outside the CORS
      allowlist is refused, which keeps a leaked credential from being streamed to
      an attacker's page. Handshakes with no Origin (tests, curl, native clients)
      are let through -- there is no ambient authority to steal outside a browser,
      the same contract as the cookie-origin check.
    """
    # Import here to avoid circular dependency
    from ..services.auth_service import consume_websocket_ticket

    # --- Pre-accept gates: no socket exists until every one of these passes. ---
    # 1. Origin: a browser-driven handshake naming a foreign origin is refused.
    origin = websocket.headers.get("origin")
    if origin is not None:
        parsed = _request_origin(origin)
        allowed = {o.lower() for o in cors_allow_origins()}
        if parsed is None or parsed not in allowed:
            logger.warning("WebSocket refused: foreign origin %r", origin)
            await websocket.close(code=WS_CLOSE_ORIGIN_REFUSED)
            return

    # 2. Handshake flood brake, per client IP.
    ip = _ws_client_ip(websocket)
    if not _ws_handshake_allowed(ip):
        logger.warning("WebSocket refused: handshake flood from %s", ip)
        await websocket.close(code=WS_CLOSE_HANDSHAKE_FLOODED)
        return

    # 3. Ticket presence and validity. Consuming it is atomic: a replay after the
    # first handshake finds no row, and the same transaction re-reads the session
    # and account so a ticket minted just before a logout cannot still connect.
    if not ticket:
        await websocket.close(code=WS_CLOSE_AUTH_REQUIRED)
        return
    try:
        consumed = consume_websocket_ticket(db, ticket)
    except Exception as e:
        logger.error(f"WebSocket ticket redemption failed: {e}")
        await websocket.close(code=WS_CLOSE_AUTH_REQUIRED)
        return
    if consumed is None:
        await websocket.close(code=WS_CLOSE_AUTH_REQUIRED)
        return
    user, session_id = consumed
    account_id = str(user.id)
    account_type = user.account_type

    # 4. Entitlement: participant, and a dialog worth streaming.
    try:
        conversation = message_service.validate_conversation_participant(
            db, conversation_id, user.regno
        )
    except HTTPException:
        # Deliberate refusals (unknown dialog, non-participant, not accepted)
        # stay generic on the wire: the REST transcript answers the same way
        # rather than turning the socket into an existence oracle for dialog
        # ids. The detail is server-logged above by the validator's caller.
        logger.warning(
            "WebSocket refused: %s not entitled to conversation %s",
            user.regno, conversation_id,
        )
        await websocket.close(code=WS_CLOSE_FORBIDDEN)
        return
    except Exception as e:
        logger.error(f"WebSocket validation failed: {e}")
        await websocket.close(code=WS_CLOSE_FORBIDDEN)
        return
    # A terminated conversation is readable but finished, so no live stream is
    # opened into it. Refusing here rather than only on send keeps a socket from
    # staying connected and receiving anything a later change might publish.
    if conversation.get("status") == "terminated":
        logger.info(f"WebSocket rejected: conversation {conversation_id} is terminated")
        await websocket.close(code=WS_CLOSE_FORBIDDEN)
        return
    if conversation.get("is_blocked", False):
        logger.warning(f"WebSocket rejected: conversation {conversation_id} is blocked")
        await websocket.close(code=WS_CLOSE_FORBIDDEN)
        return
    # The conversation row says nothing about account state, so a socket opened
    # after a block would otherwise stream live messages to the blocked side.
    if message_service.is_partner_blocked(db, conversation, user.regno):
        logger.warning(
            f"WebSocket rejected: partner of {user.regno} in conversation {conversation_id} is blocked"
        )
        await websocket.close(code=WS_CLOSE_FORBIDDEN)
        return

    await websocket.accept()

    try:
        logger.info(f"WebSocket connected: {user.regno} to conversation {conversation_id}")

        # Subscribe to Redis pub/sub
        pubsub = redis_service.subscribe_to_conversation(conversation_id)

        if not pubsub:
            # Two very different refusals, and the difference is what an operator
            # reads in the log: this worker is already serving as many live
            # chats as it can -- in which case the open sockets carry on and only
            # this one is refused -- or Redis itself is not answering. Either way
            # the refusal is stated, not left to look like a working chat.
            at_capacity = redis_service.subscriber_capacity_reached()
            logger.warning(
                "WebSocket refused: %s could not subscribe to conversation %s%s",
                user.regno,
                conversation_id,
                " (live-chat capacity reached)" if at_capacity else "",
            )
            await websocket.send_json({
                "error": "Live updates are unavailable for this chat right now",
                "reason": "capacity" if at_capacity else "unavailable",
            })
            await websocket.close(code=WS_CLOSE_SERVER_ERROR)
            return

        # Send connection success
        await websocket.send_json({
            "type": "connected",
            "conversation_id": conversation_id,
            "user_regno": user.regno
        })

        # Listen for messages from Redis
        async def redis_listener():
            """Listen for messages from Redis and forward to WebSocket"""
            # The blocking Redis listen() runs on the loop's shared worker pool
            # (asyncio.to_thread), not on a per-connection executor: the old code
            # minted a ThreadPoolExecutor per socket and never shut it down, so
            # every dialog leaked a thread for the life of the process.
            def get_redis_message():
                """Blocking function to get next message from Redis"""
                for message in pubsub.listen():
                    if message['type'] == 'message':
                        return message
                return None

            try:
                while True:
                    message = await asyncio.to_thread(get_redis_message)

                    if message is None:
                        break

                    # The handshake is not a lifetime grant: a revoked, superseded,
                    # expired, or blocked session must stop receiving immediately,
                    # even before its next ping. Nothing is forwarded on a failed
                    # revalidation; the main loop closes the socket on that ping.
                    if not await _socket_account_valid(
                        session_id, account_id, account_type, db
                    ):
                        logger.info(
                            "WebSocket auth no longer valid; stopping delivery to %s",
                            user.regno,
                        )
                        return

                    try:
                        data = json.loads(message['data'])
                        await websocket.send_json({
                            "type": "message",
                            "data": data
                        })
                    except (json.JSONDecodeError, KeyError, TypeError):
                        logger.error(f"Invalid JSON from Redis: {message.get('data')}")
                    except Exception as e:
                        logger.error(f"Error sending message to WebSocket: {e}")
                        break

            except Exception as e:
                logger.error(f"Redis listener error: {e}")

        # Start Redis listener task
        listener_task = asyncio.create_task(redis_listener())

        # Keep connection alive and handle ping/pong, reaping silent sockets:
        # any text that is not a ping is ignored (clients cannot publish
        # through the socket -- sending is REST-only), and a socket that hears
        # nothing for WS_IDLE_TIMEOUT_SECONDS is closed rather than held.
        close_code = 1000
        try:
            while True:
                try:
                    data = await asyncio.wait_for(
                        websocket.receive_text(), timeout=WS_IDLE_TIMEOUT_SECONDS
                    )
                except asyncio.TimeoutError:
                    logger.info(
                        f"WebSocket idle timeout: {user.regno} on conversation {conversation_id}"
                    )
                    close_code = WS_CLOSE_IDLE
                    break

                # Every frame is a chance to notice the session ended: logout,
                # a newer login, expiry, or an account block. This deliberately
                # runs for non-ping text too, so a client cannot keep the socket
                # alive by sending something else.
                if not await _socket_account_valid(
                    session_id, account_id, account_type, db
                ):
                    logger.info(
                        f"WebSocket closed: session no longer valid for {user.regno}"
                    )
                    close_code = WS_CLOSE_AUTH_REQUIRED
                    break

                # Handle ping
                if data == "ping":
                    await websocket.send_text("pong")

        except WebSocketDisconnect:
            logger.info(f"WebSocket disconnected: {user.regno}")
            close_code = 1000
        finally:
            # Cleanup
            listener_task.cancel()
            try:
                await listener_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"WebSocket listener task failed: {e}")
            redis_service.unsubscribe(pubsub, conversation_id)
            try:
                await websocket.close(code=close_code)
            except Exception:
                pass
            logger.info(f"WebSocket cleaned up: {user.regno}")

    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        try:
            await websocket.close(code=WS_CLOSE_SERVER_ERROR)
        except Exception:
            pass
