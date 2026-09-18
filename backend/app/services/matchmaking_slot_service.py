"""Which participant cohorts are allowed to match with each other.

A *cohort* is a ``(year, department)`` pair, and it replaced a single free-text ``class_name``
column. That change is the whole reason this module was rewritten rather than adjusted: with one
string, the only thing a slot could name was whatever an operator had typed, so "CSE 2nd Year"
and "2nd Year CSE" were two unrelated cohorts and a rule configured between them matched nobody.
Both halves are now columns, so a cohort can be selected, displayed and compared without parsing
anything.

There is exactly one implementation of "what cohorts exist" -- :func:`list_participant_cohorts` --
and the Love Notes filter reads through it too, rather than keeping its own copy of the same
query. They were two identical functions before this, and two identical functions are two places
for the answer to disagree.
"""

import uuid
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy import func, select

from ..postgres import Database
from ..db_models import MatchmakingSlotRecord, UserRecord

# The key a cohort is addressed by in a drag-and-drop UI and in a POST body. An object rather than
# a joined string because the two halves have to stay separable to be sent back separately.
# ``Any`` rather than ``str`` because ``year`` is an int: the pair is a heterogeneous mapping, and
# a ``Dict[str, str]`` annotation here is what let the whole read path look type-correct while
# handing ints to code that joined them as text.
CohortDict = Dict[str, Any]


def cohort_label(year: Optional[int], department: Optional[str]) -> str:
    """How a cohort reads in one line of UI text.

    Derived on the way out rather than stored, so there is no third column that can disagree with
    the other two. Either half missing renders as whatever there is, which is why it takes
    ``Optional``: the slot editor receives cohorts from an operator-typed request body, and a
    half-filled one should still produce something readable rather than "None".

    Each half is rendered individually rather than by joining the tuple, because the two halves are
    no longer the same type -- ``year`` is an int and ``department`` a str -- and ``" · ".join`` on
    the pair raises ``TypeError: sequence item 1: expected str instance, int found``. That is the
    reason this is written as two appends instead of a comprehension.
    """
    parts: List[str] = []
    if department:
        parts.append(department.strip())
    if year:
        # Truthy rather than ``is not None`` so a NULL half is skipped the same way it was as a
        # string, instead of being spelled "None" into the chip.
        parts.append(str(year))
    return " · ".join(parts) if parts else "Unspecified cohort"


async def list_participant_cohorts(db: Database) -> List[CohortDict]:
    """Every ``(year, department)`` pair at least one participant actually belongs to.

    Distinct, and derived from ``users`` rather than stored, so a cohort with nobody in it never
    appears in the configuration palette and an operator is not offered a pairing that would match
    no one.

    Ordered by year then department so the palette reads in the same order every time it is
    opened -- an unordered ``SELECT DISTINCT`` reshuffles whenever PostgreSQL picks a different
    plan, which makes a drag-and-drop configuration hard to reason about between saves.

    NULL is filtered out on both columns even though both are NOT NULL. A CHECK constraint added
    by ``apply_check_constraints`` lands ``NOT VALID``, so a row written before the columns were
    tightened can still hold one, and a cohort of ``{"year": null}`` would be a chip nothing could
    be dropped into.
    """
    with db.sessions() as session:
        rows = session.execute(
            select(UserRecord.year, UserRecord.department)
            .where(UserRecord.year.is_not(None), UserRecord.department.is_not(None))
            .distinct()
            .order_by(UserRecord.year.asc(), UserRecord.department.asc())
        ).all()
    return [{"year": year, "department": department} for year, department in rows]


def _serialise_slot(slot: MatchmakingSlotRecord) -> Dict[str, Any]:
    """One slot as the API returns it, with a pre-joined label beside the separable halves.

    The label is a convenience for the editor's chips and is derived here rather than in the
    browser, so the two sides of this codebase cannot render the same cohort differently.
    """
    return {
        "id": str(slot.id),
        "year_a": slot.year_a,
        "department_a": slot.department_a,
        "year_b": slot.year_b,
        "department_b": slot.department_b,
        "label_a": cohort_label(slot.year_a, slot.department_a),
        "label_b": cohort_label(slot.year_b, slot.department_b),
        "created_at": slot.created_at.isoformat() if slot.created_at else None,
        "updated_at": slot.updated_at.isoformat() if slot.updated_at else None,
    }


async def get_all_slots_service(db: Database) -> List[Dict[str, Any]]:
    """Every configured slot, both sides as ``(year, department)`` pairs."""
    with db.sessions() as session:
        slots = session.scalars(select(MatchmakingSlotRecord)).all()
        return [_serialise_slot(slot) for slot in slots]


def _normalize_department(value: str, field_name: str) -> str:
    """Trim a department and refuse an empty one.

    A blank department is refused rather than stored because a cohort is a *pair*: a slot with a
    year and no department describes everybody in that year across every department, which is a
    different rule wearing the same shape, and would be configured by accident the moment somebody
    cleared the input and pressed save. Department is free text, so nothing else can rule this
    out -- it has to be checked here.
    """
    normalized = (value or "").strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} is required for a matchmaking slot.",
        )
    return normalized


async def create_matchmaking_slot_service(
    db: Database,
    year_a: int,
    department_a: str,
    year_b: int,
    department_b: str,
) -> Dict[str, Any]:
    """Create a slot permitting one cohort to match with another.

    A slot whose two sides are the same cohort is allowed and means that cohort may match within
    itself, so there is no "these must differ" rule here.

    A duplicate pair is a 409 rather than a unique-constraint error: the operator pressed save
    twice, and "this pairing is already configured" is an answer they can act on. The check and
    the insert are in one transaction, but the row is still not protected by a unique index --
    see ``MatchmakingSlotRecord`` for why that trade is deliberate.
    """
    department_a = _normalize_department(department_a, "department_a")
    department_b = _normalize_department(department_b, "department_b")

    with db.sessions.begin() as session:
        # Order-independent: A-B and B-A are the same rule, and an operator who adds the second
        # should be told it exists rather than be shown two rows that do the same thing.
        existing = session.scalar(
            select(MatchmakingSlotRecord).where(
                (
                    (MatchmakingSlotRecord.year_a == year_a)
                    & (MatchmakingSlotRecord.department_a == department_a)
                    & (MatchmakingSlotRecord.year_b == year_b)
                    & (MatchmakingSlotRecord.department_b == department_b)
                )
                | (
                    (MatchmakingSlotRecord.year_a == year_b)
                    & (MatchmakingSlotRecord.department_a == department_b)
                    & (MatchmakingSlotRecord.year_b == year_a)
                    & (MatchmakingSlotRecord.department_b == department_a)
                )
            )
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"A matchmaking slot between {cohort_label(year_a, department_a)} and "
                    f"{cohort_label(year_b, department_b)} already exists."
                ),
            )

        slot = MatchmakingSlotRecord(
            year_a=year_a,
            department_a=department_a,
            year_b=year_b,
            department_b=department_b,
        )
        session.add(slot)
        session.flush()
        return _serialise_slot(slot)


async def delete_matchmaking_slot_service(
    db: Database,
    slot_id: str,
) -> Dict[str, Any]:
    """Delete a matchmaking slot by id."""
    if not slot_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Slot ID is required.",
        )

    try:
        uuid.UUID(str(slot_id))
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
    """Report the slot configuration's state on boot. Writes nothing.

    The intent is "every cohort may match with every other", and that is a fact about *no rows
    existing* rather than about some particular row -- there is no row that says "all of them"
    without enumerating every cohort that will ever be created. An empty table is therefore the
    initialised state, and the configuration screen reads it as one slot holding every cohort.

    This used to write a single representative pair "to give the editor something to show", on the
    understanding that saving would expand it into the full all-to-all set. That was wrong twice
    over. The editor does not treat a lone pair as unconfigured: it reconstructs slots from the
    rows it is given, so a deployment that had never been configured opened on a slot containing
    exactly two cohorts out of thirty-six, and saving from there *replaced* the intended
    all-to-all with the one pair. And a lone pair is not a harmless placeholder -- it reads as a
    rule permitting that pair and nothing else, which is the opposite of what it stood for, so the
    moment anything consults these rows to decide who may match it would have restricted
    matchmaking to one pair of cohorts on a fresh install.

    Kept as a function because it is the boot-time report of "is anything configured", and that is
    a question worth a log line, but it holds no write path at all.
    """
    with db.sessions() as session:
        existing_count = session.scalar(select(func.count()).select_from(MatchmakingSlotRecord)) or 0

    cohorts = await list_participant_cohorts(db)
    return {
        "initialized": True,
        "configured": existing_count > 0,
        "message": (
            f"{existing_count} matchmaking slots configured."
            if existing_count
            else "No matchmaking slots configured; every cohort may match with every other."
        ),
        "slots_count": existing_count,
        "cohorts_count": len(cohorts),
    }


async def is_cohort_match_service(
    db: Database,
    year_a: int,
    department_a: str,
    year_b: int,
    department_b: str,
) -> bool:
    """Whether a slot has been configured permitting these two cohorts to match.

    Both orderings count, because the row that records a rule does not remember which side the
    operator typed first and there is no reason for that to matter.
    """
    if not year_a or not department_a or not year_b or not department_b:
        return False

    with db.sessions() as session:
        result = session.scalar(
            select(MatchmakingSlotRecord).where(
                (
                    (MatchmakingSlotRecord.year_a == year_a)
                    & (MatchmakingSlotRecord.department_a == department_a)
                    & (MatchmakingSlotRecord.year_b == year_b)
                    & (MatchmakingSlotRecord.department_b == department_b)
                )
                | (
                    (MatchmakingSlotRecord.year_a == year_b)
                    & (MatchmakingSlotRecord.department_a == department_b)
                    & (MatchmakingSlotRecord.year_b == year_a)
                    & (MatchmakingSlotRecord.department_b == department_a)
                )
            )
        )
    return result is not None


async def get_matching_cohorts_service(
    db: Database,
    year: int,
    department: str,
) -> List[CohortDict]:
    """Every cohort this one has been configured to match with.

    Excludes itself, so a self-pair -- which is how a cohort is permitted to match within itself
    -- does not come back as its own match. Deduplicated, because two overlapping slots that both
    name the same partner are one partner.
    """
    if not year or not department:
        return []

    with db.sessions() as session:
        rows = session.execute(
            select(
                MatchmakingSlotRecord.year_a, MatchmakingSlotRecord.department_a,
                MatchmakingSlotRecord.year_b, MatchmakingSlotRecord.department_b,
            ).where(
                (MatchmakingSlotRecord.year_a == year)
                & (MatchmakingSlotRecord.department_a == department)
                | (MatchmakingSlotRecord.year_b == year)
                & (MatchmakingSlotRecord.department_b == department)
            )
        ).all()

    matching: set[tuple[int, str]] = set()
    for year_a, department_a, year_b, department_b in rows:
        for side_year, side_department in ((year_a, department_a), (year_b, department_b)):
            if side_year is None or side_department is None:
                continue
            if (side_year, side_department) == (year, department):
                continue
            matching.add((side_year, side_department))
    return [{"year": value[0], "department": value[1]} for value in sorted(matching)]