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
from sqlalchemy import and_, exists, func, or_, select

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


# ---------------------------------------------------------------------------
# The rule: who may match whom
# ---------------------------------------------------------------------------
#
# One definition of the rule, here, because matchmaking has to obey it on every draw and the
# obvious alternative -- asking ``is_cohort_match_service`` once per candidate -- is a query per
# row of the participant table inside the request that is supposed to be answering a click. These
# three functions are what the draw path calls: two to describe a cohort's standing, and one to
# fold the whole rule into the candidate query as a predicate.
#
# The rule in one sentence: a cohort is *in a slot* when some row names it on either side, and two
# cohorts may match when some row names both of them. Those two clauses are the entire model, and
# the second one applied to a cohort against itself is where self-matching comes from.


def slots_are_configured(db: Database) -> bool:
    """Whether any matchmaking slot row exists at all.

    This is the switch between the two different products the table can describe, and which one is
    in force is not a matter of taste:

    * **No rows.** Every cohort may match with every other. This is what a deployment that has never
      been configured means, and it is why the initial state is the empty table rather than a
      placeholder row -- see :func:`init_matchmaking_slot_service`, which used to write one
      representative pair and thereby restricted a fresh install to a single pair of cohorts.
    * **One row or more.** The rows are the whole rule. Every cohort not named by any of them is
      now unmatchable, including cohorts that had no slot a moment ago.

    That second clause is the sharp edge, and it is deliberate: the moment an operator creates their
    first slot, the cohorts they did not mention stop matching. Anything else would mean a
    half-configured screen silently still pairing people the operator can see are not configured
    to pair, which is the failure this is here to prevent. It is also why the admin screen has to
    show the unconfigured cohorts as such rather than leaving them to be discovered by a user
    wondering why their draws keep failing.

    Read per request rather than cached at import. It is an operator flipping a switch on a screen,
    and the next draw has to be the new rule without a restart.
    """
    with db.sessions() as session:
        return session.scalar(select(MatchmakingSlotRecord.id).limit(1)) is not None


def cohort_is_configured(db: Database, year: Optional[int], department: Optional[str]) -> bool:
    """Whether this cohort is named by any slot, on either side.

    A half-supplied cohort is reported as unconfigured rather than raising. The answer is only ever
    used to phrase a message about why a draw found nobody, and a cohort missing a half cannot be
    named by any row -- both halves of a row are NOT NULL -- so "no" is already the truth. Raising
    would turn a legacy row into a 500 on the one path that is trying to explain itself.
    """
    if not year or not department:
        return False

    with db.sessions() as session:
        return session.scalar(
            select(MatchmakingSlotRecord.id).where(_names_cohort(year, department)).limit(1)
        ) is not None


def candidate_shares_a_slot_predicate(year: Optional[int], department: Optional[str]):
    """Restrict a candidate query to partners this cohort may actually be matched with.

    Returns a SQL predicate rather than a set of cohorts to filter in Python, and the difference is
    the whole reason this function exists: the permitted partners of a cohort are exactly the people
    in the cohorts some row names alongside it, which is a property of the *candidate* rows and so
    belongs in the query that already reads them. Materialising the cohort list first and then
    filtering on it would mean a second query whose answer is stale before it is used, and would
    read as though a cohort list were an authorisation boundary when it is only a convenience for
    the editor's palette.

    The correlated subquery asks a single question of each candidate: is there a row naming both
    them and me? For a candidate in my own cohort that row need only name me once, which is what
    makes self-matching fall out of the same clause instead of needing a second one.

    Callers must only apply this when :func:`slots_are_configured` is true. The empty table means
    all-to-all, and this predicate would answer "nothing may match" for it, so the bypass is the
    caller's to make and is deliberately not hidden in here.
    """
    return exists(
        select(MatchmakingSlotRecord.id).where(
            and_(_names_cohort(year, department), _names_candidate_cohort())
        )
    )


def _names_cohort(year: Optional[int], department: Optional[str]):
    """A slot row naming ``(year, department)`` on either of its two sides.

    Both sides, because the row does not remember which one the operator typed first and there is
    no reason for that to matter -- the same order-independence :func:`is_cohort_match_service`
    applies when comparing two named cohorts.
    """
    return or_(
        and_(MatchmakingSlotRecord.year_a == year, MatchmakingSlotRecord.department_a == department),
        and_(MatchmakingSlotRecord.year_b == year, MatchmakingSlotRecord.department_b == department),
    )


def _names_candidate_cohort():
    """:func:`_names_cohort` for the cohort of whichever candidate row is being examined.

    Written against ``UserRecord`` rather than against a value, because the cohort being tested is
    the one on the candidate row and is only known once the row is. SQLAlchemy correlates the outer
    query in for it, which is what lets this stay a predicate instead of a per-candidate query.
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

    ``group_key`` is returned as a plain string or ``None``, never omitted: the editor groups boxes
    on it, and a key missing from the JSON would be indistinguishable from a pair that was never
    part of a named box, which is the NULL it is already meant to spell.
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

    Ordered, and by ``group_key`` first, because the editor reconstructs a list of boxes out of this
    and a list whose order depends on the query plan is a drag-and-drop screen whose boxes move
    between saves. Same argument as the ordering in :func:`list_participant_cohorts`: an unordered
    ``SELECT`` reshuffles whenever PostgreSQL picks a different plan, and an operator who cannot
    predict where their box will be next time cannot tell whether it moved or was replaced.

    ``group_key`` ascending with NULLs *last*, so a deployment that has never been saved through the
    current editor reads in the order the pairs were created, and the boxes the editor reconstructs
    from them sit together rather than being split by keyed boxes that happen to sort before them.
    ``created_at`` breaks ties within a box, and ``id`` breaks ties between rows written in the same
    statement, so the order is total rather than merely stable per-plan.
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


def create_matchmaking_slot_service(
    db: Database,
    year_a: int,
    department_a: str,
    year_b: int,
    department_b: str,
    group_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a slot permitting one cohort to match with another.

    A slot whose two sides are the same cohort is allowed and means that cohort may match within
    itself, so there is no "these must differ" rule here.

    ``group_key`` names the editor box this pair belongs to. It is optional, because a pair is a
    valid thing to store on its own and every row written before the column existed is one. It
    changes nothing about what may match -- see ``MatchmakingSlotRecord`` -- it only says which box
    to put the pair back into when the editor loads.

    A duplicate is a 409 rather than a unique-constraint error: the operator pressed save twice, and
    "this pairing is already configured" is an answer they can act on. The check and the insert are
    in one transaction, but the row is still not protected by a unique index -- see
    ``MatchmakingSlotRecord`` for why that trade is deliberate.

    The duplicate is scoped to ``group_key`` as well as to the pair, and that is what makes
    overlapping boxes expressible. Two boxes naming the same pair -- ``[A, B]`` and ``[A, B, C]``
    say, or ``[A, B, C]`` and ``[D, E, A]`` where both write A-B -- are a legitimate configuration,
    not a mistake: each box is a thing the operator built and expects to still be there after a
    reload. Refusing the second one would make the editor unable to represent overlapping boxes at
    all. The rule reads pairs and not boxes, so storing the pair twice costs nothing beyond the row,
    and it is still a 409 when the pair *and* the box both repeat -- which is what an actual double
    submit looks like.
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

    # The box this pair is being written into. A NULL group has to be matched with ``IS NULL`` and
    # every other value with ``=``, so this is built as a condition rather than folded into the
    # equality above -- which would also be wrong in the other direction, matching a pair that
    # exists in a *different* box and is precisely the case being allowed.
    group_matches = (
        (MatchmakingSlotRecord.group_key == group_key)
        if group_key is not None
        else MatchmakingSlotRecord.group_key.is_(None)
    )

    with db.sessions.begin() as session:
        # Order-independent: A-B and B-A are the same rule, and an operator who adds the second
        # should be told it exists rather than be shown two rows that do the same thing.
        #
        # Pair *and* box in one query, rather than fetching a row for the pair and comparing its
        # group in Python. The fetched row would be whichever one the planner returned, so a pair
        # stored in two boxes could be reported as a duplicate or as new depending on the order --
        # and which of those happens would change with the query plan.
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

    Not invented here. A box key is minted by the editor, so a request without one is a legitimate
    client -- a script, or a row written before the column existed -- and a request with one that is
    not a UUID is a client that got the shape wrong. Refusing the second is worth a round trip: a key
    that stored as something other than a UUID would come back as a different string and quietly
    fall into the legacy grouping path on the next load, which is the exact failure this column
    exists to remove.

    Empty string is read as absent rather than as an invalid key, because that is what a form field
    the operator never touched submits, and it means the same thing as not sending one.
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

    The claim that an empty table means all-to-all is now load-bearing rather than decorative:
    :func:`slots_are_configured` is what the matchmaking draw reads to decide whether to apply the
    slot predicate at all. That is exactly why the placeholder row had to go -- it would have made
    a never-configured deployment enforce a single pair of cohorts, permanently and invisibly.
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