# app/routers/messages.py

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, status, Query
from ..postgres import Database
from typing import Annotated, Optional
import json
import asyncio

from ..dependencies import get_db
from ..models import UserDetails
from ..services.auth_service import ACCOUNT_UNAVAILABLE_DETAIL
from ..services.authorization import require_participant
from ..services.message_service import message_service
from ..services.redis_service import redis_service
from ..logger import get_logger
from pydantic import BaseModel, Field, validator

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

    @validator("body")
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
    limit: int = Query(CONVERSATION_MESSAGE_MAX, ge=1, le=CONVERSATION_MESSAGE_MAX)
):
    """
    Get messages for a conversation
    
    Validates:
    - User is participant in conversation
    - Marks messages as read

    Every message a conversation can hold fits in one response -- the ceiling on the
    conversation is the same as this endpoint's default window -- so `limit` trims the reply
    for a caller who asks for less and never hides the newest part of the thread.

    `quota` is the message allowance as it stands for the caller, which is what the dialog
    reads to decide whether to offer the composer at all.
    """
    messages = message_service.get_messages(
        db=db,
        conversation_id=conversation_id,
        user=current_user,
        limit=limit
    )
    
    return {
        "conversation_id": conversation_id,
        "messages": messages,
        "count": len(messages),
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


@router.websocket("/ws/{conversation_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    conversation_id: str,
    db: Database = Depends(get_db),
    token: Optional[str] = Query(None)
):
    """
    WebSocket endpoint for real-time messages
    
    Client connects with JWT token in query params
    Subscribes to Redis pub/sub for the conversation
    """
    await websocket.accept()
    
    try:
        # Authenticate user from token
        if not token:
            await websocket.send_json({"error": "Authentication required"})
            await websocket.close(code=4001)
            return
        
        # Import here to avoid circular dependency
        from ..services.auth_service import verify_token_service
        
        try:
            user = verify_token_service(token, db)
        except Exception as e:
            logger.error(f"WebSocket auth failed: {e}")
            await websocket.send_json({"error": "Invalid token"})
            await websocket.close(code=4001)
            return
        
        # Validate user is participant
        try:
            conversation = message_service.validate_conversation_participant(
                db, conversation_id, user.regno
            )
            # A terminated conversation is readable but finished, so no live stream is
            # opened into it. Refusing here rather than only on send keeps a socket from
            # staying connected and receiving anything a later change might publish.
            if conversation.get("status") == "terminated":
                logger.info(f"WebSocket rejected: conversation {conversation_id} is terminated")
                await websocket.send_json({"error": "This conversation has been terminated"})
                await websocket.close(code=4003)
                return
            if conversation.get("is_blocked", False):
                logger.warning(f"WebSocket rejected: conversation {conversation_id} is blocked")
                await websocket.send_json({"error": "Conversation is blocked"})
                await websocket.close(code=4003)
                return
            # The conversation row says nothing about account state, so a socket opened
            # after a block would otherwise stream live messages to the blocked side.
            if message_service.is_partner_blocked(db, conversation, user.regno):
                logger.warning(
                    f"WebSocket rejected: partner of {user.regno} in conversation {conversation_id} is blocked"
                )
                await websocket.send_json({"error": ACCOUNT_UNAVAILABLE_DETAIL})
                await websocket.close(code=4003)
                return
        except Exception as e:
            logger.error(f"WebSocket validation failed: {e}")
            await websocket.send_json({"error": str(e)})
            await websocket.close(code=4003)
            return
        
        logger.info(f"WebSocket connected: {user.regno} to conversation {conversation_id}")
        
        # Subscribe to Redis pub/sub
        pubsub = redis_service.subscribe_to_conversation(conversation_id)
        
        if not pubsub:
            await websocket.send_json({"error": "Failed to connect to message stream"})
            await websocket.close(code=5000)
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
            try:
                # Run the blocking Redis listen() in a thread pool
                import concurrent.futures
                executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                loop = asyncio.get_event_loop()
                
                def get_redis_message():
                    """Blocking function to get next message from Redis"""
                    for message in pubsub.listen():
                        if message['type'] == 'message':
                            return message
                    return None
                
                while True:
                    # Run blocking Redis call in thread pool
                    message = await loop.run_in_executor(executor, get_redis_message)
                    
                    if message is None:
                        break
                        
                    try:
                        data = json.loads(message['data'])
                        await websocket.send_json({
                            "type": "message",
                            "data": data
                        })
                    except json.JSONDecodeError:
                        logger.error(f"Invalid JSON from Redis: {message['data']}")
                    except Exception as e:
                        logger.error(f"Error sending message to WebSocket: {e}")
                        break
                        
            except Exception as e:
                logger.error(f"Redis listener error: {e}")
        
        # Start Redis listener task
        listener_task = asyncio.create_task(redis_listener())
        
        # Keep connection alive and handle ping/pong
        try:
            while True:
                # Wait for messages from client (like ping/pong)
                data = await websocket.receive_text()
                
                # Handle ping
                if data == "ping":
                    await websocket.send_text("pong")
                    
        except WebSocketDisconnect:
            logger.info(f"WebSocket disconnected: {user.regno}")
        finally:
            # Cleanup
            listener_task.cancel()
            redis_service.unsubscribe(pubsub, conversation_id)
            logger.info(f"WebSocket cleaned up: {user.regno}")
            
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        try:
            await websocket.close(code=5000)
        except:
            pass
