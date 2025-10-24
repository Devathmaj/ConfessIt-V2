# app/routers/conversations.py

from fastapi import APIRouter, Depends, status
from pymongo.database import Database
from typing import Annotated

from ..dependencies import get_db
from ..models import UserDetails, ConversationCreate
from ..services.auth_service import get_current_user
from ..services.conversation_service import (
    request_conversation_service,
    get_conversation_status_service,
    accept_conversation_service,
    reject_conversation_service,
    get_current_conversation_service,
    get_supabase_token_service
)

router = APIRouter(
    prefix="/conversations",
    tags=["Conversations"]
)

@router.post("/request", status_code=status.HTTP_201_CREATED)
def request_conversation(
    conversation_data: ConversationCreate,
    current_user: Annotated[UserDetails, Depends(get_current_user)],
    db: Database = Depends(get_db)
):
    """
    Used to create a pending conversation request for a given match.
    """
    return request_conversation_service(current_user, conversation_data.matchId, db)

@router.get("/{match_id}/status")
def get_conversation_status(
    match_id: str,
    current_user: Annotated[UserDetails, Depends(get_current_user)],
    db: Database = Depends(get_db)
):
    """
    Used to get the status of a conversation for a given match.
    """
    return get_conversation_status_service(current_user, match_id, db)

@router.post("/{match_id}/accept", status_code=status.HTTP_200_OK)
def accept_conversation(
    match_id: str,
    current_user: Annotated[UserDetails, Depends(get_current_user)],
    db: Database = Depends(get_db)
):
    """
    Used to accept a pending conversation request.
    """
    return accept_conversation_service(current_user, match_id, db)

@router.post("/{match_id}/reject", status_code=status.HTTP_200_OK)
def reject_conversation(
    match_id: str,
    current_user: Annotated[UserDetails, Depends(get_current_user)],
    db: Database = Depends(get_db)
):
    """
    Used to reject a pending conversation request.
    """
    return reject_conversation_service(current_user, match_id, db)

@router.get("/{match_id}/supabase-token")
def get_supabase_token(
    match_id: str,
    current_user: Annotated[UserDetails, Depends(get_current_user)],
    db: Database = Depends(get_db)
):
    """
    Used to get a Supabase ephemeral token for accessing messages in a conversation.
    """
    return get_supabase_token_service(current_user, match_id, db)

@router.get("/current")
def get_current_conversation(
    current_user: Annotated[UserDetails, Depends(get_current_user)],
    db: Database = Depends(get_db)
):
    """
    Used to get the current active conversation for the authenticated user.
    """
    return get_current_conversation_service(current_user, db)
