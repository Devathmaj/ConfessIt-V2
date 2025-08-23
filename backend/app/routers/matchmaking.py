# app/routes/matchmaking.py

from fastapi import APIRouter, Depends
from pymongo.database import Database
from typing import Annotated, List

from ..dependencies import get_db
from ..models import UserDetails
from ..services.auth_service import get_current_user
from ..services.matchmaking_service import (
    get_potential_matches_service,
    find_match_service
)

router = APIRouter(
    prefix="/matchmaking",
    tags=["Matchmaking"]
)

@router.get("/potential-matches", response_model=List[UserDetails])
def get_potential_matches(
    current_user: Annotated[UserDetails, Depends(get_current_user)],
    db: Database = Depends(get_db),
    limit: int = 20
):
    """
    Gets a list of random potential matches.
    """
    return get_potential_matches_service(current_user, db, limit)


@router.get("/find", response_model=UserDetails)
def find_match(
    current_user: Annotated[UserDetails, Depends(get_current_user)],
    db: Database = Depends(get_db)
):
    """
    Finds a random user with a different gender who has matchmaking enabled.
    """
    return find_match_service(current_user, db)
