import logging
from fastapi import APIRouter, Depends, HTTPException, Query, status
from typing import Dict, List, Optional, Any
from ..dependencies import get_db
from ..postgres import Database
from ..services.confession_service import ConfessionService, FEED_MAX_LIMIT
from ..models import Confession, ConfessionComment, UserDetails, ConfessionCreate, ConfessionCooldown, ReactionCreate, CommentCreate, ReportCreate, ConfessionUpdate, SemanticSearchRequest, SemanticSearchResponse
from ..services.authorization import (
    get_current_participant_optional,
    require_participant,
)

router = APIRouter()


def get_confession_service(db: Database = Depends(get_db)) -> ConfessionService:
    """Build the per-request service on the shared engine.

    An explicit provider keeps the service on the app's single client; a bare
    class dependency would let it construct its own engine and pool.
    """
    return ConfessionService(db)


@router.post("/confessions", response_model=Confession, response_model_by_alias=False)
def create_confession(confession_data: ConfessionCreate, service: ConfessionService = Depends(get_confession_service), current_user: UserDetails = Depends(require_participant)):
    return service.create_confession(confession_data, str(current_user.id))

@router.get("/confessions", response_model=Dict[str, Any])
def get_confessions(
    sort_by: str = Query('time', enum=['popularity', 'time', 'comments']),
    skip: int = Query(0, ge=0, description="How many visible confessions to skip."),
    limit: int = Query(50, ge=1, le=FEED_MAX_LIMIT, description="How many visible confessions to return. Clamped so one request can never materialise the whole feed at once."),
    service: ConfessionService = Depends(get_confession_service),
    current_user: Optional[UserDetails] = Depends(get_current_participant_optional),
):
    """One page of the participant feed, sorted and counted in SQL.

    The sort picker is a SQL ORDER BY and every page a SQL LIMIT/OFFSET: the backend
    reads at most one page -- with only its comments, authors and reporter ids
    attached -- no matter how many confessions exist, and ``total`` counts the visible
    posts so the screen can load more downwards without holding the feed behind it.
    """
    user_id = str(current_user.id) if current_user else None
    return service.get_confessions(sort_by, user_id, skip=skip, limit=limit)

@router.post("/confessions/search", response_model=SemanticSearchResponse)
def semantic_search_confessions(
    payload: SemanticSearchRequest,
    service: ConfessionService = Depends(get_confession_service),
    current_user: Optional[UserDetails] = Depends(get_current_participant_optional),
):
    """Semantic search over confessions by meaning, not keywords.

    The query is embedded with text-embedding-3-small (1536 dims) and ranked
    by pgvector cosine distance ascending in PostgreSQL. Only confessions with
    a valid current embedding are eligible; hidden/removed posts are excluded
    in SQL. Returns the existing public confession shape (no raw embeddings,
    no author ids, no reports); anonymous presentation is preserved.

    When EMBEDDING_SEARCH_ENABLED is not true, no provider call is made and
    this answers 503. Missing credentials or bad provider state also answer
    503/429/502 with an actionable message and no secrets.
    """
    from ..services.embedding_service import EmbeddingError

    user_id = str(current_user.id) if current_user else None
    try:
        return service.search_confessions_semantic(
            payload.query, user_id, limit=payload.limit
        )
    except EmbeddingError as exc:
        if exc.code == "invalid_input":
            raise HTTPException(status_code=422, detail=exc.detail)
        if exc.code == "rate_limited":
            raise HTTPException(
                status_code=429,
                detail=exc.detail,
                headers={"Retry-After": "60"},
            )
        if exc.code == "disabled":
            raise HTTPException(status_code=503, detail=exc.detail)
        if exc.code in ("missing_config", "invalid_config"):
            raise HTTPException(status_code=503, detail=exc.detail)
        if exc.code in ("timeout", "network", "unavailable", "network_blocked"):
            raise HTTPException(status_code=503, detail=exc.detail)
        raise HTTPException(status_code=502, detail=exc.detail)


@router.get("/confessions/cooldown", response_model=ConfessionCooldown)
def get_confession_cooldown(service: ConfessionService = Depends(get_confession_service), current_user: UserDetails = Depends(require_participant)):
    """
    Used by the compose screen to show the wait before anything is typed.

    Advisory only. The write claims the window with a conditional UPDATE and re-derives the
    answer itself, so this cannot be raced into a second post -- it just lets the UI say so
    in advance instead of refusing after the fact.
    """
    return service.get_confession_cooldown(str(current_user.id))

@router.get("/confessions/{confession_id}/comments", response_model=Dict[str, Any])
def get_confession_comments(
    confession_id: str,
    sort: str = Query('latest', enum=['latest', 'relevant'], description="Thread order: newest first, or most approved first."),
    skip: int = Query(0, ge=0, description="How many visible comments to skip."),
    limit: int = Query(10, ge=1, le=FEED_MAX_LIMIT, description="How many visible comments to return."),
    service: ConfessionService = Depends(get_confession_service),
    current_user: Optional[UserDetails] = Depends(get_current_participant_optional),
):
    """One thread page for the dialog, with the total beside it.

    The feed embeds only the opening window of each thread; this is what the "show
    more" beneath it reads, in either order. A missing or taken-down confession reads
    as absent here, exactly as the feed treats it.
    """
    user_id = str(current_user.id) if current_user else None
    page = service.get_confession_comments(confession_id, user_id, skip=skip, limit=limit, sort=sort)
    if page is None:
        raise HTTPException(status_code=404, detail="Confession not found.")
    return page


@router.get("/confessions/total_count", response_model=int)
def get_total_confessions_count(service: ConfessionService = Depends(get_confession_service)):
    """
    Used to get the total number of confessions.
    """
    return service.get_total_confessions_count()

@router.put("/confessions/{confession_id}", response_model=Confession, response_model_by_alias=False)
def update_confession(confession_id: str, update_data: ConfessionUpdate, service: ConfessionService = Depends(get_confession_service), current_user: UserDetails = Depends(require_participant)):
    """
    Used to update a confession's settings.
    """
    updated_confession = service.update_confession(confession_id, update_data, str(current_user.id))
    if not updated_confession:
        raise HTTPException(status_code=404, detail="Confession not found or you don't have permission to edit it.")
    return updated_confession

@router.post("/confessions/{confession_id}/react", response_model=Confession, response_model_by_alias=False)
def react_to_confession(confession_id: str, reaction_data: ReactionCreate, service: ConfessionService = Depends(get_confession_service), current_user: UserDetails = Depends(require_participant)):
    updated_confession = service.react_to_confession(confession_id, reaction_data.reaction, str(current_user.id))
    if not updated_confession:
        raise HTTPException(status_code=404, detail="Confession not found")
    return updated_confession

@router.post("/confessions/{confession_id}/comment", response_model=ConfessionComment, response_model_by_alias=False)
def add_comment_to_confession(confession_id: str, comment_data: CommentCreate, service: ConfessionService = Depends(get_confession_service), current_user: UserDetails = Depends(require_participant)):
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
def like_comment(comment_id: str, service: ConfessionService = Depends(get_confession_service), current_user: UserDetails = Depends(require_participant)):
    updated_comment = service.like_comment(comment_id, str(current_user.id))
    if not updated_comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    return updated_comment

@router.post("/comments/{comment_id}/dislike", response_model=ConfessionComment, response_model_by_alias=False)
def dislike_comment(comment_id: str, service: ConfessionService = Depends(get_confession_service), current_user: UserDetails = Depends(require_participant)):
    updated_comment = service.dislike_comment(comment_id, str(current_user.id))
    if not updated_comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    return updated_comment

@router.post("/comments/{comment_id}/report", response_model=ConfessionComment, response_model_by_alias=False)
def report_comment(comment_id: str, report_data: ReportCreate, service: ConfessionService = Depends(get_confession_service), current_user: UserDetails = Depends(require_participant)):
    updated_comment = service.report_comment(comment_id, report_data, current_user)
    if not updated_comment:
        raise HTTPException(status_code=400, detail="Failed to report comment. You may have already reported this comment.")
    return updated_comment

@router.post("/confessions/{confession_id}/report", response_model=Confession, response_model_by_alias=False)
def report_confession(confession_id: str, report_data: ReportCreate, service: ConfessionService = Depends(get_confession_service), current_user: UserDetails = Depends(require_participant)):
    updated_confession = service.report_confession(confession_id, report_data, current_user)
    if not updated_confession:
        raise HTTPException(status_code=400, detail="Failed to report confession. You may have already reported this confession.")
    return updated_confession
