# app/services/matchmaking_service.py

import logging
from fastapi import HTTPException, status
from pymongo.database import Database

from ..models import UserDetails

# Logger
logger = logging.getLogger(__name__)

def find_match_service(
    current_user: UserDetails,
    db: Database
) -> UserDetails:
    """
    Service to find a random user with a different gender who has matchmaking enabled.
    """
    query = {
        "Regno": {"$ne": current_user.Regno},
        "gender": {"$ne": current_user.gender},
        "isMatchmaking": True
    }

    count = db["UserDetails"].count_documents(query)
    if count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No potential matches found. Try again later!"
        )

    random_pipeline = [
        {"$match": query},
        {"$sample": {"size": 1}}
    ]

    match_cursor = db["UserDetails"].aggregate(random_pipeline)

    matched_user_doc = None
    for doc in match_cursor:
        matched_user_doc = doc
        break

    if not matched_user_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Could not find a match at this moment. Please try again."
        )

    return UserDetails(**matched_user_doc)
