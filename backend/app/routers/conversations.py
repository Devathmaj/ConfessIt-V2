# app/routers/conversations.py

from fastapi import APIRouter, Depends, status
from ..postgres import Database
from typing import Annotated

from ..dependencies import get_db
from ..models import UserDetails, ConversationCreate
from ..services.authorization import require_participant
from ..services.conversation_service import (
    request_conversation_service,
    get_conversation_status_service,
    accept_conversation_service,
    reject_conversation_service,
    get_current_conversation_service,
    get_received_conversations_service,
    get_conversation_by_match_service,
    block_conversation_service,
    unblock_conversation_service
)

router = APIRouter(
    prefix="/conversations",
    tags=["Conversations"]
)

@router.post("/request", status_code=status.HTTP_201_CREATED)
def request_conversation(
    conversation_data: ConversationCreate,
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db)
):
    """Create a pending conversation request for a given match."""
    return request_conversation_service(current_user, conversation_data.match_id, db)

@router.get("/{match_id}/status")
def get_conversation_status(
    match_id: str,
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db)
):
    """Get the status of a conversation for a given match."""
    return get_conversation_status_service(current_user, match_id, db)

@router.post("/{match_id}/accept", status_code=status.HTTP_200_OK)
def accept_conversation(
    match_id: str,
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db)
):
    """Accept a pending conversation request."""
    return accept_conversation_service(current_user, match_id, db)

@router.post("/{match_id}/reject", status_code=status.HTTP_200_OK)
def reject_conversation(
    match_id: str,
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db)
):
    """Reject a pending conversation request."""
    return reject_conversation_service(current_user, match_id, db)

@router.get("/current")
def get_current_conversation(
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db)
):
    """Get current active conversation for authenticated user."""
    return get_current_conversation_service(current_user, db)

@router.get("/received")
def get_received_conversations(
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db)
):
    """Get conversations where the current user is the receiver."""
    return get_received_conversations_service(current_user, db)

@router.get("/{match_id}")
def get_conversation_by_match(
    match_id: str,
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db)
):
    """Get a specific conversation by match ID."""
    return get_conversation_by_match_service(current_user, match_id, db)

@router.post("/{match_id}/block", status_code=status.HTTP_200_OK)
def block_conversation(
    match_id: str,
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db)
):
    """Block a conversation."""
    return block_conversation_service(current_user, match_id, db)

@router.post("/{match_id}/unblock", status_code=status.HTTP_200_OK)
def unblock_conversation(
    match_id: str,
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db)
):
    """Unblock a conversation (blocker only)."""
    return unblock_conversation_service(current_user, match_id, db)
