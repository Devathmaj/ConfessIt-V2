"""A slot box is stored as pairs, and the grouping that puts it back together has to be stored too.

``matchmaking_slots`` holds pairs because the pairing rule is stated on pairs and a pair of indexed
columns is the only shape that rule can be asked against cheaply. The editor, though, shows *boxes*,
and a box is an entity an operator builds: [A, B, C] says those three may match each other and each
may match within itself. So a box is expanded into its pairs on save and has to be reconstructed from
them on load.

That reconstruction was being *guessed* -- grouped by each pair's first cohort -- and the guess was
wrong in both directions at once. One box of A, B and C came back as a box of A, B, C plus a stray box
of B and C, because B-C's first cohort is B rather than A; a box of four came back as three boxes.
Nothing in matchmaking noticed, because the rule reads pairs and the pairs were always right. What
broke was the save that *follows* a load: the editor deletes the old configuration before writing the
new one, so the duplicated pairs were refused with a 409 after the delete had already happened, and
the operator was left with a truncated configuration and an error.

``group_key`` stores the grouping instead of inferring it. These tests cover the three properties
that makes it work and the one it must not change:

* a box survives save -> load with the same cohorts, for any size;
* a cohort may be in several boxes, and overlapping boxes are a legal configuration rather than a
  duplicate to be refused;
* the pairing rule does not read ``group_key``, so grouping is presentation and nothing more.
"""

import uuid
from itertools import combinations

import pytest
from sqlalchemy import inspect

from app.db_models import MatchmakingSlotRecord
from app.services.matchmaking_slot_service import (
    cohort_is_configured,
    create_matchmaking_slot_service,
    get_all_slots_service,
)

COHORT_A = (2, "CSE")
COHORT_B = (2, "ECE")
COHORT_C = (3, "CSE")
COHORT_D = (3, "ECE")
COHORT_E = (4, "CSE")

# Fixed rather than random so the ordering assertions are about the sort and not about which UUID
# happened to be generated. Built with ``UUID(int=...)`` rather than written out as literals so the
# string form is a real canonical UUID and not a hand-typed approximation of one -- the column
# refuses anything else, and a test that bypassed that would be testing a value the server rejects.
BOX_ONE = str(uuid.UUID(int=1))
BOX_TWO = str(uuid.UUID(int=2))


def runs_of(keys):
    """Collapse a list into its consecutive runs, so adjacency can be asserted directly."""
    runs = []
    for key in keys:
        if not runs or runs[-1] != key:
            runs.append(key)
    return runs


def stored_pairs(db):
    """The pairs in the table, canonicalised and ignoring ``group_key``.

    This is precisely the input the pairing rule reads, which is what makes comparing two
    configurations through it a statement about matchmaking rather than about bookkeeping.
    """
    with db.sessions() as session:
        rows = session.query(
            MatchmakingSlotRecord.year_a,
            MatchmakingSlotRecord.department_a,
            MatchmakingSlotRecord.year_b,
            MatchmakingSlotRecord.department_b,
        ).all()
    return sorted(
        tuple(sorted(((year_a, f"{department_a}"), (year_b, f"{department_b}"))))
        for year_a, department_a, year_b, department_b in rows
    )


def add_user(db, regno: str, year: int, department: str, gender: str = "other") -> None:
    with db.sessions.begin() as session:
        session.add(
            UserRecord(
                regno=regno,
                name=f"Person {regno}",
                email=f"{regno}@example.com",
                year=year,
                department=department,
                gender=gender,
            )
        )


async def save_box(db, box, group_key=None) -> None:
    """Write one box the way the editor does: a clique of pairs, plus a self-pair for one cohort."""
    if len(box) >= 2:
        for cohort_a, cohort_b in combinations(box, 2):
            await create_matchmaking_slot_service(db, *cohort_a, *cohort_b, group_key)
    elif len(box) == 1:
        (cohort,) = box
        await create_matchmaking_slot_service(db, *cohort, *cohort, group_key)


# --------------------------------------------------------------------------- the column


@pytest.mark.asyncio
async def test_a_group_key_is_stored_and_returned(db, admin_client):
    """The one thing the column is for: getting back what was put in.

    A key that came back as a different string would not fail loudly. It would fall into the legacy
    grouping path on the next load, and the operator would see their box of three split into two
    again -- the original bug, back again, with the fix installed.
    """
    group_key = str(uuid.uuid4())

    created = await create_matchmaking_slot_service(
        db, *COHORT_A, *COHORT_B, group_key=group_key
    )

    assert created["group_key"] == group_key
    listed = await get_all_slots_service(db)
    assert [slot["group_key"] for slot in listed] == [group_key]


@pytest.mark.asyncio
async def test_a_pair_with_no_group_key_is_still_valid(db):
    """Optional, because a pair is a legitimate thing to store on its own.

    Every row written before this column existed is one, and requiring a key would make the editor
    unable to save anything at all until the whole table had been rewritten.
    """
    created = await create_matchmaking_slot_service(db, *COHORT_A, *COHORT_B)

    assert created["group_key"] is None
    assert (await get_all_slots_service(db))[0]["group_key"] is None


@pytest.mark.asyncio
async def test_a_malformed_group_key_is_refused_with_a_message_naming_it(db):
    """400, not a 500 and not a stored string that will not come back as itself.

    The alternative -- letting it through -- writes a value the editor cannot match on load, which is
    the failure this column exists to remove, so it is worth a round trip to catch.
    """
    with pytest.raises(Exception) as excinfo:
        await create_matchmaking_slot_service(db, *COHORT_A, *COHORT_B, group_key="not-a-uuid")

    assert getattr(excinfo.value, "status_code", None) == 400
    assert "group_key" in str(excinfo.value.detail)


@pytest.mark.asyncio
async def test_an_empty_group_key_means_absent_rather_than_invalid(db):
    """A form field the operator never touched submits "", and that means "no box".

    Refusing it would make it impossible to save from a client that includes the field
    unconditionally, which is the ordinary case for one that did not create it.
    """
    created = await create_matchmaking_slot_service(db, *COHORT_A, *COHORT_B, group_key="   ")

    assert created["group_key"] is None


@pytest.mark.asyncio
async def test_the_column_is_nullable_on_a_table_that_predates_it(db):
    """A deployed table has rows written before this column existed.

    ``apply_missing_columns`` will not invent a value for them, so the column has to accept NULL.
    Asserted against the real schema rather than the model, because the model is the thing being
    changed and would agree with itself either way.
    """
    from sqlalchemy import inspect

    columns = {column["name"]: column for column in inspect(db.engine).get_columns("matchmaking_slots")}

    assert "group_key" in columns
    assert columns["group_key"]["nullable"] is True


# --------------------------------------------------------------------------- overlapping boxes


@pytest.mark.asyncio
async def test_two_boxes_naming_the_same_pair_are_both_stored(db):
    """[A, B] and [A, B, C] overlap on A-B, and that is a configuration, not a mistake.

    The duplicate check is scoped to the box as well as the pair precisely so this can be saved.
    Refusing the second box would leave the editor unable to represent overlapping boxes at all,
    and an operator who wants A-B both on its own and inside a larger box has no other way to say so.
    """
    first, second = str(uuid.uuid4()), str(uuid.uuid4())

    await save_box(db, [COHORT_A, COHORT_B], first)
    await save_box(db, [COHORT_A, COHORT_B, COHORT_C], second)

    with db.sessions() as session:
        rows = session.query(MatchmakingSlotRecord).all()
    # Four rows: A-B twice, once per box, plus A-C and B-C.
    assert len(rows) == 4
    assert {row.group_key for row in rows} == {first, second}


@pytest.mark.asyncio
async def test_the_same_pair_twice_in_one_box_is_still_a_duplicate(db):
    """The 409 keeps its job: an operator who pressed save twice is told, rather than served silently.

    Scoping the check to the box must not have made it a no-op -- that is the failure mode of
    "relax the constraint until the error stops appearing".
    """
    group_key = str(uuid.uuid4())
    await save_box(db, [COHORT_A, COHORT_B], group_key)

    with pytest.raises(Exception) as excinfo:
        await create_matchmaking_slot_service(
            db, *COHORT_A, *COHORT_B, group_key=group_key
        )

    assert getattr(excinfo.value, "status_code", None) == 409


@pytest.mark.asyncio
async def test_a_pair_repeated_without_a_box_is_still_a_duplicate(db):
    """The legacy path keeps its own duplicate detection.

    Two NULL-group rows naming the same pair are the same rule written twice, which is what the 409
    has always meant, and nothing about the new column should have relaxed it.
    """
    await save_box(db, [COHORT_A, COHORT_B])

    with pytest.raises(Exception) as excinfo:
        await create_matchmaking_slot_service(db, *COHORT_B, *COHORT_A)

    assert getattr(excinfo.value, "status_code", None) == 409


@pytest.mark.asyncio
async def test_the_duplicate_check_is_order_independent_within_a_box(db):
    """A-B and B-A are one rule, and the operator who adds the second should be told it exists."""
    group_key = str(uuid.uuid4())
    await save_box(db, [COHORT_A, COHORT_B], group_key)

    with pytest.raises(Exception) as excinfo:
        await create_matchmaking_slot_service(
            db, *COHORT_B, *COHORT_A, group_key=group_key
        )

    assert getattr(excinfo.value, "status_code", None) == 409


@pytest.mark.asyncio
async def test_a_duplicate_in_one_of_two_boxes_is_found_whichever_row_is_read(db):
    """A pair stored in two boxes must still be a duplicate in *both*.

    The check fetches a row for the pair and compares its box; if it fetched whichever row the
    planner liked, "A-B already exists" would be answered by one of the two boxes and the other
    would silently accept a duplicate -- the same configuration reporting itself consistent or
    self-contradictory depending on the query plan. Asked here of the box written *second*, which is
    the one a first-row comparison would miss.
    """
    await save_box(db, [COHORT_A, COHORT_B], BOX_ONE)
    await save_box(db, [COHORT_A, COHORT_B], BOX_TWO)

    with pytest.raises(Exception) as excinfo:
        await create_matchmaking_slot_service(db, *COHORT_A, *COHORT_B, BOX_TWO)

    assert getattr(excinfo.value, "status_code", None) == 409


@pytest.mark.asyncio
async def test_a_pair_stored_in_two_boxes_may_join_a_third(db):
    """The complement: a third box naming an existing pair is stored, not refused.

    Together with the test above this pins the check to "same pair *and* same box", in both
    directions. Either half on its own would pass a suite that only ever used one box.
    """
    third = str(uuid.UUID(int=3))
    await save_box(db, [COHORT_A, COHORT_B], BOX_ONE)
    await save_box(db, [COHORT_A, COHORT_B], BOX_TWO)

    await save_box(db, [COHORT_A, COHORT_B], third)

    assert len(await get_all_slots_service(db)) == 3


@pytest.mark.asyncio
async def test_a_cohort_may_be_in_several_boxes(db):
    """[A, B, C] and [D, E, A]: A is in both, and both must survive.

    This is the configuration the whole feature exists to express, so it is written as one save
    rather than two so that a duplicate anywhere in the second box would fail the test.
    """
    await save_box(db, [COHORT_A, COHORT_B, COHORT_C], str(uuid.uuid4()))
    await save_box(db, [COHORT_D, COHORT_E, COHORT_A], str(uuid.uuid4()))

    assert cohort_is_configured(db, *COHORT_A)
    assert cohort_is_configured(db, *COHORT_B)
    assert cohort_is_configured(db, *COHORT_C)
    assert cohort_is_configured(db, *COHORT_D)
    assert cohort_is_configured(db, *COHORT_E)


# --------------------------------------------------------------------------- the rule ignores it


@pytest.mark.asyncio
async def test_pairing_does_not_depend_on_the_group_key(db):
    """The rule reads pairs. Grouping is how the editor *shows* a configuration, nothing more.

    Stated as two configurations that must be indistinguishable rather than as a comment, because
    the tempting optimisation is to group the rows once and match on the group: faster, and it would
    quietly make one box behave differently from several boxes naming the same pairs -- the exact
    thing this column must not leak into. A box of three and three separate boxes permitting the
    same pairs are the same rule, and the table is compared without the keys.
    """
    one_box = str(uuid.uuid4())
    await save_box(db, [COHORT_A, COHORT_B, COHORT_C], one_box)
    pairs_in_one_box = stored_pairs(db)

    with db.sessions.begin() as session:
        session.query(MatchmakingSlotRecord).delete()

    for pair_box in ([COHORT_A, COHORT_B], [COHORT_A, COHORT_C], [COHORT_B, COHORT_C]):
        await save_box(db, pair_box, str(uuid.uuid4()))
    pairs_in_three_boxes = stored_pairs(db)

    assert pairs_in_one_box == pairs_in_three_boxes


@pytest.mark.asyncio
async def test_a_cohort_named_only_by_another_boxes_cohort_is_still_configured(db):
    """Being in a slot means being named by a row, whatever box that row came from.

    A cohort in box [D, E, A] is named by that box's rows and is therefore configured, even though
    no row in *its own* box exists -- it has no box of its own to be in.
    """
    await save_box(db, [COHORT_D, COHORT_E, COHORT_A], str(uuid.uuid4()))

    assert cohort_is_configured(db, *COHORT_A)
    assert not cohort_is_configured(db, *COHORT_B)


@pytest.mark.asyncio
async def test_the_order_slots_are_listed_in_keeps_each_box_together(db):
    """The editor rebuilds a list of boxes from this ordering, so it cannot be left to the planner.

    An unordered SELECT reshuffles whenever PostgreSQL picks a different plan, and a drag-and-drop
    screen whose boxes move between saves is one an operator cannot tell apart from one that lost a
    box. Asserted as *contiguity* -- each box's rows arrive as one unbroken run -- rather than as an
    exact list, because which box sorts first is an implementation detail and "rows of a box are
    adjacent" is the property the screen actually depends on.
    """
    await save_box(db, [COHORT_A, COHORT_B, COHORT_C], BOX_ONE)
    await save_box(db, [COHORT_D, COHORT_E], BOX_TWO)

    keys = [slot["group_key"] for slot in await get_all_slots_service(db)]

    assert len(keys) == 4
    assert runs_of(keys) == [BOX_ONE, BOX_TWO]


@pytest.mark.asyncio
async def test_rows_with_no_box_sort_after_the_ones_that_have_one(db):
    """Legacy rows read in the order the pairs were created, and stay together.

    They share the NULL group, so the same contiguity argument applies to all of them at once. This
    is the order the pre-migration configuration is shown in until somebody saves it.
    """
    await save_box(db, [COHORT_A, COHORT_B])
    await save_box(db, [COHORT_C, COHORT_D])

    keys = [slot["group_key"] for slot in await get_all_slots_service(db)]

    assert keys == [None, None]


@pytest.mark.asyncio
async def test_a_keyed_box_and_a_legacy_box_are_both_reported(db):
    """The migration case: some rows upgraded, some not, and the screen still renders both.

    Asserted because it is the state every deployment is in between the two, and an ordering that
    only works when every row has a key would interleave them.
    """
    await save_box(db, [COHORT_A, COHORT_B])
    await save_box(db, [COHORT_C, COHORT_D], BOX_TWO)

    keys = [slot["group_key"] for slot in await get_all_slots_service(db)]

    assert None in keys and BOX_TWO in keys
    assert keys == [BOX_TWO, None]  # the keyed box first, the legacy row last
