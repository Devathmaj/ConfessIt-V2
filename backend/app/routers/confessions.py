import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Optional
from ..services.confession_service import ConfessionService
from ..models import Confession, ConfessionComment, UserDetails, ConfessionCreate, ReactionCreate, CommentCreate, ReportCreate, ConfessionUpdate
from ..services.auth_service import get_current_user, get_current_user_optional

router = APIRouter()

@router.post("/confessions", response_model=Confession, response_model_by_alias=False)
def create_confession(confession_data: ConfessionCreate, service: ConfessionService = Depends(), current_user: UserDetails = Depends(get_current_user)):
    return service.create_confession(confession_data, str(current_user.id))

@router.get("/confessions", response_model=List[Confession], response_model_by_alias=False)
def get_confessions(sort_by: str = Query('popularity', enum=['popularity', 'time', 'comments']), service: ConfessionService = Depends(), current_user: Optional[UserDetails] = Depends(get_current_user_optional)):
    user_id = str(current_user.id) if current_user else None
    return service.get_confessions(sort_by, user_id)

@router.get("/confessions/total_count", response_model=int)
def get_total_confessions_count(service: ConfessionService = Depends()):
    """
    Used to get the total number of confessions.
    """
    return service.get_total_confessions_count()

@router.put("/confessions/{confession_id}", response_model=Confession, response_model_by_alias=False)
def update_confession(confession_id: str, update_data: ConfessionUpdate, service: ConfessionService = Depends(), current_user: UserDetails = Depends(get_current_user)):
    """
    Used to update a confession's settings.
    """
    updated_confession = service.update_confession(confession_id, update_data, str(current_user.id))
    if not updated_confession:
        raise HTTPException(status_code=404, detail="Confession not found or you don't have permission to edit it.")
    return updated_confession

@router.post("/confessions/{confession_id}/react", response_model=Confession, response_model_by_alias=False)
def react_to_confession(confession_id: str, reaction_data: ReactionCreate, service: ConfessionService = Depends(), current_user: UserDetails = Depends(get_current_user)):
    updated_confession = service.react_to_confession(confession_id, reaction_data.reaction, str(current_user.id))
    if not updated_confession:
        raise HTTPException(status_code=404, detail="Confession not found")
    return updated_confession

@router.post("/confessions/{confession_id}/comment", response_model=ConfessionComment, response_model_by_alias=False)
def add_comment_to_confession(confession_id: str, comment_data: CommentCreate, service: ConfessionService = Depends(), current_user: UserDetails = Depends(get_current_user)):
    result = service.add_comment_to_confession(confession_id, comment_data, current_user)

    if isinstance(result, str):
        if result == "COMMENTS_DISABLED":
            raise HTTPException(status_code=403, detail="Comments are disabled for this confession.")
        if result == "COMMENT_LIMIT_REACHED":
            raise HTTPException(status_code=400, detail="Failed to add comment. You may have reached your comment limit for this post.")
        if result == "CONFESSION_NOT_FOUND":
            raise HTTPException(status_code=404, detail="Confession not found.")
    
    if not result:
        raise HTTPException(status_code=500, detail="Failed to add comment due to an unknown error.")

    return result

@router.post("/comments/{comment_id}/like", response_model=ConfessionComment, response_model_by_alias=False)
def like_comment(comment_id: str, service: ConfessionService = Depends(), current_user: UserDetails = Depends(get_current_user)):
    updated_comment = service.like_comment(comment_id, str(current_user.id))
    if not updated_comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    return updated_comment

@router.post("/comments/{comment_id}/dislike", response_model=ConfessionComment, response_model_by_alias=False)
def dislike_comment(comment_id: str, service: ConfessionService = Depends(), current_user: UserDetails = Depends(get_current_user)):
    updated_comment = service.dislike_comment(comment_id, str(current_user.id))
    if not updated_comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    return updated_comment

@router.post("/comments/{comment_id}/report", response_model=ConfessionComment, response_model_by_alias=False)
def report_comment(comment_id: str, report_data: ReportCreate, service: ConfessionService = Depends(), current_user: UserDetails = Depends(get_current_user)):
    updated_comment = service.report_comment(comment_id, report_data, current_user)
    if not updated_comment:
        raise HTTPException(status_code=400, detail="Failed to report comment. You may have already reported this comment.")
    return updated_comment

@router.post("/confessions/{confession_id}/report", response_model=Confession, response_model_by_alias=False)
def report_confession(confession_id: str, report_data: ReportCreate, service: ConfessionService = Depends(), current_user: UserDetails = Depends(get_current_user)):
    updated_confession = service.report_confession(confession_id, report_data, current_user)
    if not updated_confession:
        raise HTTPException(status_code=400, detail="Failed to report confession. You may have already reported this confession.")
    return updated_confession
