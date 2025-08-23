import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Optional
from ..services.confession_service import ConfessionService
from ..models import Confession, ConfessionComment, UserDetails, ConfessionCreate, ReactionCreate, CommentCreate
from ..services.auth_service import get_current_user, get_current_user_optional

router = APIRouter()

@router.post("/confessions", response_model=Confession)
def create_confession(confession_data: ConfessionCreate, service: ConfessionService = Depends(), current_user: UserDetails = Depends(get_current_user)):
    return service.create_confession(confession_data, str(current_user.id))

@router.get("/confessions", response_model=List[Confession])
def get_confessions(sort_by: str = Query('popularity', enum=['popularity', 'time', 'comments']), service: ConfessionService = Depends(), current_user: Optional[UserDetails] = Depends(get_current_user_optional)):
    user_id = str(current_user.id) if current_user else None
    return service.get_confessions(sort_by, user_id)

@router.post("/confessions/{confession_id}/react", response_model=Confession)
def react_to_confession(confession_id: str, reaction_data: ReactionCreate, service: ConfessionService = Depends(), current_user: UserDetails = Depends(get_current_user)):
    updated_confession = service.react_to_confession(confession_id, reaction_data.reaction, str(current_user.id))
    if not updated_confession:
        raise HTTPException(status_code=404, detail="Confession not found")
    return updated_confession

@router.post("/confessions/{confession_id}/comment", response_model=ConfessionComment)
def add_comment_to_confession(confession_id: str, comment_data: CommentCreate, service: ConfessionService = Depends(), current_user: UserDetails = Depends(get_current_user)):
    new_comment = service.add_comment_to_confession(confession_id, comment_data, current_user)
    if not new_comment:
        raise HTTPException(status_code=400, detail="Failed to add comment. You may have reached your comment limit for this post.")
    return new_comment

# New endpoints for liking and disliking comments
@router.post("/comments/{comment_id}/like", response_model=ConfessionComment)
def like_comment(comment_id: str, service: ConfessionService = Depends(), current_user: UserDetails = Depends(get_current_user)):
    updated_comment = service.like_comment(comment_id, str(current_user.id))
    if not updated_comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    return updated_comment

@router.post("/comments/{comment_id}/dislike", response_model=ConfessionComment)
def dislike_comment(comment_id: str, service: ConfessionService = Depends(), current_user: UserDetails = Depends(get_current_user)):
    updated_comment = service.dislike_comment(comment_id, str(current_user.id))
    if not updated_comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    return updated_comment
