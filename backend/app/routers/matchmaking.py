# app/routes/matchmaking.py

from fastapi import APIRouter, Depends, status
from pymongo.database import Database
from typing import Annotated

from ..dependencies import get_db
from ..models import UserDetails
from ..services.auth_service import get_current_user
from ..services.matchmaking_service import find_match_service, check_matchmaking_cooldown_service

router = APIRouter(
    prefix="/matchmaking",
    tags=["Matchmaking"]
)

@router.get("/check")
def check_matchmaking_status(
    current_user: Annotated[UserDetails, Depends(get_current_user)],
    db: Database = Depends(get_db)
):
    """
    Checks if the user can initiate matchmaking based on a 4-hour cooldown.
    """
    return check_matchmaking_cooldown_service(current_user, db)


@router.post("/find", response_model=UserDetails, status_code=status.HTTP_200_OK)
def find_match(
    current_user: Annotated[UserDetails, Depends(get_current_user)],
    db: Database = Depends(get_db)
):
    """
    Finds a random user and consumes the user's matchmaking attempt for the cooldown period.
    This should only be called after a successful check and user confirmation.
    """
    return find_match_service(current_user, db)
