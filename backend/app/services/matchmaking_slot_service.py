"""Define matching rules for participant cohorts, represented as ``(year, department)`` pairs.

The shared cohort query also serves Love Notes filters so both features use the same population.
"""

import uuid
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy import and_, exists, func, or_, select, text

from ..postgres import Database
from ..db_models import MatchmakingSlotRecord, UserRecord

# Cohorts stay structured objects so integer ``year`` and string ``department`` remain distinct.
CohortDict = Dict[str, Any]

# Dedicated advisory-lock key for slot writes; keep it distinct from lifecycle and pair-session keys.
_SLOT_WRITE_LOCK_KEY = 7_250_003


def cohort_label(year: Optional[int], department: Optional[str]) -> str:
    """Format a cohort for display without storing a duplicate label."""
    parts: List[str] = []
    if department:
        parts.append(department.strip())
    if year:
        # Truthy rather than ``is not None`` so a NULL half is skipped the same way it was as a
        # string, instead of being spelled "None" into the chip.
        parts.append(str(year))
    return " · ".join(parts) if parts else "Unspecified cohort"


# Matching rules live here and are folded into the candidate SQL query, avoiding per-candidate
# lookups. A cohort is configured if a slot names it; two cohorts may match if one slot names both.


def slots_are_configured(db: Database) -> bool:
    """Return whether explicit slot rules exist; an empty table means all cohorts may match.

    Read on each request so operator changes take effect without restarting the service.
    """
    with db.sessions() as session:
        return session.scalar(select(MatchmakingSlotRecord.id).limit(1)) is not None


def cohort_is_configured(db: Database, year: Optional[int], department: Optional[str]) -> bool:
    """Whether this cohort is named by any slot, on either side.

    A half-supplied cohort is answered as unconfigured rather than raising: the answer only ever
    phrases a message about why a draw found nobody, and both halves of a row are NOT NULL so a
    half cannot be named -- raising would turn a legacy row into a 500 on the one path trying
    to explain itself.
    """
    if not year or not department:
        return False

    with db.sessions() as session:
        return session.scalar(
            select(MatchmakingSlotRecord.id).where(_names_cohort(year, department)).limit(1)
        ) is not None


def candidate_shares_a_slot_predicate(year: Optional[int], department: Optional[str]):
    """Restrict a candidate query to partners this cohort may actually be matched with.

    A SQL predicate, not a cohort list to filter in Python: permitted partners are a property of
    the candidate rows and belong in the query already reading them, while a materialised list
    would be a second query stale before use that also reads like an authorisation boundary
    when it is only a convenience for the editor's palette.

    The correlated EXISTS asks each candidate: is there a row naming both them and me? For a
    candidate in my own cohort that row need only name me once, which is how self-matching
    falls out of the same clause. Callers must apply this only when
    :func:`slots_are_configured` is true -- the empty table means all-to-all, and here it would
    answer "nothing may match"; the bypass is deliberately the caller's.
    """
    return exists(
        select(MatchmakingSlotRecord.id).where(
            and_(_names_cohort(year, department), _names_candidate_cohort())
        )
    )


def _names_cohort(year: Optional[int], department: Optional[str]):
    """A slot row naming ``(year, department)`` on either side -- the row does not remember
    which one the operator typed first, and there is no reason for that to matter (the same
    order-independence :func:`is_cohort_match_service` applies).
    """
    return or_(
        and_(MatchmakingSlotRecord.year_a == year, MatchmakingSlotRecord.department_a == department),
        and_(MatchmakingSlotRecord.year_b == year, MatchmakingSlotRecord.department_b == department),
    )


def _names_candidate_cohort():
    """:func:`_names_cohort` for the cohort of whichever candidate row is being examined.

    Written against ``UserRecord`` rather than a value, because the cohort tested is only known
    once the row is; SQLAlchemy correlates the outer query in, keeping this a predicate instead
    of a per-candidate query.
    """
    return or_(
        and_(
            MatchmakingSlotRecord.year_a == UserRecord.year,
            MatchmakingSlotRecord.department_a == UserRecord.department,
        ),
        and_(
            MatchmakingSlotRecord.year_b == UserRecord.year,
            MatchmakingSlotRecord.department_b == UserRecord.department,
        ),
    )


def list_participant_cohorts(db: Database) -> List[CohortDict]:
    """Every ``(year, department)`` pair at least one participant actually belongs to.

    Distinct and derived from ``users`` rather than stored, so a cohort with nobody in it never
    appears in the palette. Ordered by year then department so it reads the same every time --
    an unordered ``SELECT DISTINCT`` reshuffles whenever PostgreSQL picks a different plan,
    which makes the drag-and-drop palette hard to reason about between saves.

    NULL is filtered out on both columns even though both are NOT NULL: a CHECK constraint
    added by ``apply_check_constraints`` lands ``NOT VALID``, so pre-tightening rows can still
    hold one, and a ``{"year": null}`` chip is one nothing can be dropped into.
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

    The label is derived here rather than in the browser so both sides of the codebase render
    the same cohort. ``group_key`` is plain string or ``None``, never omitted: the editor groups
    boxes on it, and a missing key would be indistinguishable from a pair never part of a box.
    """
    return {
        "id": str(slot.id),
        "year_a": slot.year_a,
        "department_a": slot.department_a,
        "year_b": slot.year_b,
        "department_b": slot.department_b,
        "group_key": str(slot.group_key) if slot.group_key else None,
        "label_a": cohort_label(slot.year_a, slot.department_a),
        "label_b": cohort_label(slot.year_b, slot.department_b),
        "created_at": slot.created_at.isoformat() if slot.created_at else None,
        "updated_at": slot.updated_at.isoformat() if slot.updated_at else None,
    }


def get_all_slots_service(db: Database) -> List[Dict[str, Any]]:
    """Every configured slot, both sides as ``(year, department)`` pairs.

    Ordered by ``group_key`` ascending with NULLs *last* (so a never-legacy-saved deployment
    reads in creation order and boxes sit together instead of being split), then ``created_at``
    within a box, then ``id`` between rows written in the same statement, making the order
    total. The editor reconstructs boxes from this, and an order that depends on the query plan
    is a screen whose boxes move between saves -- same argument as in
    :func:`list_participant_cohorts`.
    """
    with db.sessions() as session:
        slots = session.scalars(
            select(MatchmakingSlotRecord).order_by(
                MatchmakingSlotRecord.group_key.asc().nulls_last(),
                MatchmakingSlotRecord.created_at.asc(),
                MatchmakingSlotRecord.id.asc(),
            )
        ).all()
        return [_serialise_slot(slot) for slot in slots]


def _normalize_department(value: str, field_name: str) -> str:
    """Trim a department and refuse an empty one.

    A cohort is a *pair*: a slot with a year and no department describes everybody in that year
    across every department -- a different rule wearing the same shape, configured by accident
    the moment somebody cleared the input and saved. Department is free text, so nothing else
    can rule this out.
    """
    normalized = (value or "").strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} is required for a matchmaking slot.",
        )
    return normalized


def create_matchmaking_slot_service(
    db: Database,
    year_a: int,
    department_a: str,
    year_b: int,
    department_b: str,
    group_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a slot permitting one cohort to match with another.

    A slot whose two sides are the same cohort is allowed and means that cohort may match
    within itself. ``group_key`` names the editor box, is optional (a pair is valid on its own,
    and rows written before the column existed have none) and changes nothing about what may
    match. A duplicate is a 409 rather than a unique-constraint error, because "this pairing is
    already configured" is an answer the operator can act on; the check and insert run under
    the slot-write advisory lock below, which makes that 409 hold under concurrency too -- no
    unique index can express this invariant (order-independent pairs, NULL group semantics).

    The duplicate is scoped to the pair *and* the box, which is what makes overlapping boxes
    expressible: two boxes naming the same pair (``[A, B]`` and ``[A, B, C]``) are a legitimate
    configuration, each a thing the operator built, and refusing the second would make the
    editor unable to represent overlapping boxes at all.
    """
    department_a = _normalize_department(department_a, "department_a")
    department_b = _normalize_department(department_b, "department_b")
    group_key = _normalize_group_key(group_key)

    pair_matches = (
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

    # The box this pair is being written into. NULL groups must be matched with ``IS NULL`` and
    # every other value with ``=``, so this stays a separate condition rather than being folded
    # into the equality above -- which would also be wrong in the other direction, matching the
    # pair living in a *different* box, precisely the case being allowed.
    group_matches = (
        (MatchmakingSlotRecord.group_key == group_key)
        if group_key is not None
        else MatchmakingSlotRecord.group_key.is_(None)
    )

    with db.sessions.begin() as session:
        # ENG-005: serialize slot writes -- the duplicate check below is read-then-write, so two
        # concurrent creates of one pair+box both pass it and both land, and no unique index can
        # back it instead (order-independent pair, NULL-sensitive group). Administrator-only and
        # rare, so the lock is held for one short transaction.
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _SLOT_WRITE_LOCK_KEY},
        )
        # Order-independent: A-B and B-A are the same rule, and an operator who adds the second
        # is told it exists. Pair *and* box matched in one query rather than fetching a row and
        # comparing its group in Python -- the fetched row would be whichever the planner
        # returned, so a pair stored in two boxes could be reported as duplicate or new
        # depending on the query plan.
        existing = session.scalar(
            select(MatchmakingSlotRecord).where(and_(pair_matches, group_matches)).limit(1)
        )
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"A matchmaking slot between {cohort_label(year_a, department_a)} and "
                    f"{cohort_label(year_b, department_b)} already exists in this slot."
                ),
            )

        slot = MatchmakingSlotRecord(
            year_a=year_a,
            department_a=department_a,
            year_b=year_b,
            department_b=department_b,
            group_key=group_key,
        )
        session.add(slot)
        session.flush()
        return _serialise_slot(slot)


def _normalize_group_key(group_key: Optional[str]) -> Optional[str]:
    """Validate a slot box key, or pass ``None`` through as "not part of a named box".

    A request without one is a legitimate client (a script, or a row from before the column
    existed); a non-UUID one got the shape wrong, and storing it would fall into the legacy
    grouping path on the next load. Empty string reads as absent, as an untouched form field
    submits.
    """
    if group_key is None:
        return None
    candidate = group_key.strip()
    if not candidate:
        return None
    try:
        return str(uuid.UUID(candidate))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="group_key must be a UUID identifying the slot box this pair belongs to.",
        )


def delete_matchmaking_slot_service(
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


def init_matchmaking_slot_service(
    db: Database,
) -> Dict[str, Any]:
    """Report the slot configuration's state on boot. Writes nothing.

    "Every cohort may match with every other" is a fact about *no rows existing*, so the empty
    table is the initialised state. This used to write a single representative pair "to give the
    editor something to show", which was wrong twice over: the editor reconstructs slots from
    the rows it is given, so a fresh install opened on a slot of two cohorts out of thirty-six,
    and saving from there *replaced* the intended all-to-all with that one pair -- while a lone
    pair also reads as a rule permitting exactly that pair. That is load-bearing now:
    :func:`slots_are_configured` is what the matchmaking draw consults to decide whether to
    apply the slot predicate at all, so the placeholder row would have restricted a
    never-configured deployment permanently and invisibly.

    Kept as a function because it is the boot-time report of "is anything configured", and that
    is a question worth a log line.
    """
    with db.sessions() as session:
        existing_count = session.scalar(select(func.count()).select_from(MatchmakingSlotRecord)) or 0

    cohorts = list_participant_cohorts(db)
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


def is_cohort_match_service(
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


def get_matching_cohorts_service(
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
