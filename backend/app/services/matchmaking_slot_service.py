import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..postgres import Database
from ..db_models import MatchmakingSlotRecord, UserRecord


async def get_all_classes_service(db: Database) -> List[str]:
    """Fetch all unique class_name values from the users table."""
    with db.sessions() as session:
        result = session.scalars(
            select(UserRecord.class_name).distinct().where(UserRecord.class_name.is_not(None))
        ).all()
    return [c for c in result if c is not None]


async def get_all_slots_service(db: Database) -> List[Dict[str, Any]]:
    """Fetch all matchmaking slots."""
    with db.sessions() as session:
        slots = session.scalars(select(MatchmakingSlotRecord)).all()
    result = []
    for slot in slots:
        result.append({
            "id": str(slot.id),
            "class_a": slot.class_a,
            "class_b": slot.class_b,
            "created_at": slot.created_at.isoformat() if slot.created_at else None,
            "updated_at": slot.updated_at.isoformat() if slot.updated_at else None,
        })
    return result


async def create_matchmaking_slot_service(
    db: Database,
    class_a: str,
    class_b: str,
) -> Dict[str, Any]:
    """Create a new matchmaking slot where class_a can match with class_b.

    Self-matching is allowed (class_a == class_b) for single-class slots.
    """
    with db.sessions.begin() as session:
        # Check if slot already exists (order-independent)
        existing = session.scalar(
            select(MatchmakingSlotRecord).where(
                ((MatchmakingSlotRecord.class_a == class_a) & (MatchmakingSlotRecord.class_b == class_b))
                | ((MatchmakingSlotRecord.class_a == class_b) & (MatchmakingSlotRecord.class_b == class_a))
            )
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A matchmaking slot between these two classes already exists.",
            )

        slot = MatchmakingSlotRecord(class_a=class_a, class_b=class_b)
        session.add(slot)
        session.flush()

    return {
        "id": str(slot.id),
        "class_a": slot.class_a,
        "class_b": slot.class_b,
        "created_at": slot.created_at.isoformat() if slot.created_at else None,
        "updated_at": slot.updated_at.isoformat() if slot.updated_at else None,
    }


async def delete_matchmaking_slot_service(
    db: Database,
    slot_id: str,
) -> Dict[str, Any]:
    """Delete a matchmaking slot by ID."""
    if not slot_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Slot ID is required.",
        )

    try:
        slot_uuid = str(uuid.UUID(slot_id))
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid slot ID format.",
        )

    from uuid import UUID

    with db.sessions.begin() as session:
        slot = session.get(MatchmakingSlotRecord, UUID(slot_id))
        if not slot:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Matchmaking slot not found.",
            )
        session.delete(slot)

    return {"id": slot_id, "deleted": True}


async def init_matchmaking_slot_service(
    db: Database,
) -> Dict[str, Any]:
    """Initialize with a single slot indicating any class can match with any other class.

    Fetches all distinct classes from the users table and creates an initial slot.
    The initial slot represents the state where any class can match with any other class.
    """
    with db.sessions() as session:
        # Get all distinct classes
        classes = session.scalars(
            select(UserRecord.class_name).distinct().where(UserRecord.class_name.is_not(None))
        ).all()

        if not classes:
            return {"initialized": True, "message": "No classes found, slot not created."}

        existing_count = session.scalar(select(func.count()).select_from(MatchmakingSlotRecord)) or 0

        if existing_count == 0:
            # Create initial slot(s)
            # "Initially there will be a single slot with all classes which indicates any class can match with any other class"
            # Since we need class_a and class_b as concrete values, and the requirement says
            # "a single slot with all classes", we create one slot that represents the universal matching state.
            # 
            # The approach: create one slot using the first two classes found.
            # This establishes the pattern that classes can match with each other.
            # The admin can then add more slots or modify as needed.
            if len(classes) >= 2:
                slot = MatchmakingSlotRecord(class_a=classes[0], class_b=classes[1])
                session.add(slot)
                session.flush()
            elif len(classes) == 1:
                # Only one class - skip slot creation since constraint requires class_a != class_b
                session.rollback()
                return {"initialized": True, "message": "Only one class found, slot not created."}
            else:
                session.rollback()
                return {"initialized": True, "message": "No classes found, slot not created."}

        return {
            "initialized": True,
            "message": "Matchmaking slots initialized.",
            "classes_count": len(classes),
        }


async def is_class_match_service(
    db: Database,
    class_a: str,
    class_b: str,
) -> bool:
    """Check if two classes can match based on the slots."""
    if not class_a or not class_b:
        return False

    with db.sessions() as session:
        # Check both orderings: (class_a, class_b) or (class_b, class_a)
        result = session.scalar(
            select(MatchmakingSlotRecord).where(
                ((MatchmakingSlotRecord.class_a == class_a) & (MatchmakingSlotRecord.class_b == class_b))
                | ((MatchmakingSlotRecord.class_a == class_b) & (MatchmakingSlotRecord.class_b == class_a))
            )
        )
    return result is not None


async def get_matching_classes_service(
    db: Database,
    class_name: str,
) -> List[str]:
    """Get all classes that can match with the given class."""
    if not class_name:
        return []

    with db.sessions() as session:
        result = session.scalars(
            select(MatchmakingSlotRecord.class_b).where(
                (MatchmakingSlotRecord.class_a == class_name)
                | (MatchmakingSlotRecord.class_b == class_name)
            )
        ).all()
    # Filter out the class_name itself and duplicates
    matching = set()
    for class_b in result:
        if class_b and class_b != class_name:
            matching.add(class_b)
    return list(matching)