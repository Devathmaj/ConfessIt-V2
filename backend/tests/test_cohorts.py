"""
That ``year`` and ``department`` are two fields, and behave like two fields.

They used to be one free-text ``class_name`` column. Everything below follows from taking that
column apart, and each test exists because the old single column made the property untestable
rather than merely untrue:

* the database has to hold year to a whole number in a fixed range, because matchmaking groups by the pair and a
  row holding 0 or 13 would sit beside every real cohort without ever matching one;
* two participants in the same department but different years are two cohorts, not one -- the whole
  reason for the split, and the thing that quietly comes back if a comparison is written against a
  joined string;
* the cohort list has to report each pair once, not once per participant, or the recipient filter
  grows a duplicate entry for every student in a popular department.

The department half has no such rule and deliberately so: the list of departments belongs to the
institution, not to this codebase, so it is free text bounded only by length. The year half is the
mirror image: it holds no names at all, because an undergraduate programme and a school both need
a "which year is this person in" field and spell it differently, and storing one institution's
spelling would have needed a second column to serve the other.
"""

import pytest

from app.db_models import MatchmakingSlotRecord, UserRecord
from app.limits import USER_YEAR_MAX, USER_YEAR_MIN
from app.services.matchmaking_slot_service import (
    init_matchmaking_slot_service,
    list_participant_cohorts,
)


def add_user(regno: str, year: int, department: str) -> UserRecord:
    """A participant row with just enough on it to occupy a cohort."""
    user = UserRecord(
        regno=regno,
        name=f"Person {regno}",
        email=f"{regno}@example.com",
        year=year,
        department=department,
        gender="other",
    )
    return user


@pytest.mark.asyncio
async def test_the_cohort_list_reports_each_pair_once(db):
    """Two students in one cohort produce one entry, not two.

    The filter is built from this list, so a per-participant list would offer the operator a
    "CSE 2" option once for every student in it.
    """
    with db.sessions.begin() as session:
        session.add(add_user("dup-1", 2, "CSE"))
        session.add(add_user("dup-2", 2, "CSE"))

    cohorts = await list_participant_cohorts(db)
    assert {"year": 2, "department": "CSE"} in cohorts
    assert cohorts.count({"year": 2, "department": "CSE"}) == 1


@pytest.mark.asyncio
async def test_a_department_alone_does_not_make_one_cohort(db):
    """Same department, different years: two cohorts.

    This is the property the split was for. With one ``class_name`` column holding "CSE" twice,
    there was no way to ask for everybody in year 2 -- and with it holding "2 CSE", the
    department could not be filtered on its own. Either way the two halves moved together.
    """
    with db.sessions.begin() as session:
        session.add(add_user("year-2", 2, "CSE"))
        session.add(add_user("year-3", 3, "CSE"))

    cohorts = await list_participant_cohorts(db)
    assert {"year": 2, "department": "CSE"} in cohorts
    assert {"year": 3, "department": "CSE"} in cohorts


@pytest.mark.asyncio
async def test_the_same_year_in_two_departments_is_two_cohorts(db):
    """The mirror case, so neither half is treated as the primary one."""
    with db.sessions.begin() as session:
        session.add(add_user("dept-cse", 2, "CSE"))
        session.add(add_user("dept-ece", 2, "ECE"))

    cohorts = await list_participant_cohorts(db)
    assert {"year": 2, "department": "CSE"} in cohorts
    assert {"year": 2, "department": "ECE"} in cohorts


@pytest.mark.asyncio
async def test_a_cohort_with_no_participants_is_not_listed(db):
    """The list is derived from the people who exist, not from the four years times every slot.

    A cohort nobody occupies would still be a valid filter value, and selecting it would show an
    empty recipient list with no explanation of why.
    """
    with db.sessions.begin() as session:
        session.add(add_user("only", 1, "Mechanical"))

    cohorts = await list_participant_cohorts(db)
    assert {"year": 4, "department": "Mechanical"} not in cohorts


def test_a_matchmaking_slot_keeps_the_four_halves_apart(db):
    """A slot is a pair of cohorts, so it needs both halves of each.

    Storing one ``class_a`` string meant the slot could only be compared as a joined value, which
    is the same defect this whole module is about, one level up.
    """
    with db.sessions.begin() as session:
        session.add(
            MatchmakingSlotRecord(
                year_a=2,
                department_a="CSE",
                year_b=2,
                department_b="ECE",
            )
        )

    with db.sessions() as session:
        stored = session.query(MatchmakingSlotRecord).one()
        assert (stored.year_a, stored.department_a) == (2, "CSE")
        assert (stored.year_b, stored.department_b) == (2, "ECE")


def test_the_year_range_spans_one_to_twelve():
    """Fixed, so it can be asserted rather than derived from whatever is in the table.

    Twelve rather than four because the same schema serves a school as well as an undergraduate
    programme, and this is where that decision is recorded: the bounds are the one number a
    deployment that needs a thirteenth year has to come back and change, in this file and in
    ``limits.py`` and in ``frontend/src/lib/accountFields.ts``. Asserting the exact values is what
    makes the change a deliberate edit rather than a drift nobody notices.
    """
    assert (USER_YEAR_MIN, USER_YEAR_MAX) == (1, 12)


def seed_cohorts(db):
    """Two participants, so two cohorts exist for the editor and the palette to have something in.

    Years 2 and 10 on purpose: past a single digit, string handling and numeric handling disagree,
    which is what makes "is the year a number all the way to the browser" an actual question rather
    than a formality.
    """
    with db.sessions.begin() as session:
        session.add(add_user("cfg-1", 2, "CSE"))
        session.add(add_user("cfg-2", 10, "CSE"))


def seed_one_slot(db):
    """A participant in one cohort and a slot pairing it with another.

    Both halves are populated because the failure these tests exist for happened on the join of a
    two-element sequence, which needs a populated half to reach.
    """
    seed_cohorts(db)
    with db.sessions.begin() as session:
        session.add(
            MatchmakingSlotRecord(
                year_a=2,
                department_a="CSE",
                year_b=10,
                department_b="CSE",
            )
        )


def test_the_config_screen_renders_a_cohort_label(db, admin_client):
    """``GET /admin/config`` must not 500 on a slot whose year half is a number.

    This is the exact regression: ``cohort_label`` built its output with ``" · ".join`` over
    ``(department, year)``, which raises ``TypeError: sequence item 1: expected str instance, int
    found`` the moment ``year`` became an int. It reached production because every test above
    reads the database directly -- none of them went through the endpoint where the label is
    actually built, so a whole suite of green cohort tests sat on top of a screen that would not
    load.
    """
    seed_one_slot(db)

    response = admin_client.get("/admin/config")

    assert response.status_code == 200
    slot = response.json()["slots"][0]
    # Order-independent by design (A-B and B-A are one rule), so assert on the set of labels.
    assert {slot["label_a"], slot["label_b"]} == {"CSE · 2", "CSE · 10"}


def test_the_config_screen_reports_the_year_as_a_number(db, admin_client):
    """The same response must carry a number, not a stringified one.

    ``label_a`` proves the label renders; this proves the machine-readable halves are still ints
    for the browser's cohort key, which compares them against ``users.year``.
    """
    seed_one_slot(db)

    body = admin_client.get("/admin/config").json()
    slot = body["slots"][0]

    assert {cohort["year"] for cohort in body["cohorts"]} == {2, 10}
    assert all(isinstance(cohort["year"], int) for cohort in body["cohorts"])
    assert {slot["year_a"], slot["year_b"]} == {2, 10}


def test_the_cohort_filter_endpoint_returns_numbers(db, _confession_author_client):
    """``GET /love-notes/cohorts`` under a ``List[Dict[str, str]]`` response model was a 500.

    Pydantic v2 does not coerce ``int`` into ``str``, so the heterogeneous pair was refused by
    response validation rather than quietly stringified. The annotation was the only thing wrong
    here, which is exactly why the type had to be checked at the boundary and not inferred.
    """
    seed_one_slot(db)

    response = _confession_author_client.get("/love-notes/cohorts")

    assert response.status_code == 200
    cohorts = response.json()
    assert {cohort["year"] for cohort in cohorts} == {2, 10}
    assert all(isinstance(cohort["year"], int) for cohort in cohorts)


def test_the_recipient_list_returns_numbers(db, _confession_author_client):
    """``GET /love-notes/users`` declares ``year: str`` on ``UserRecipient`` and would 500 too.

    Same cause as the cohort list, on the screen that matters most: this is the recipient picker
    the love note is addressed to, and it carries a year on every row.
    """
    seed_one_slot(db)

    response = _confession_author_client.get("/love-notes/users")

    assert response.status_code == 200
    recipients = response.json()
    assert recipients
    assert all(isinstance(recipient["year"], int) for recipient in recipients)
    assert {recipient["year"] for recipient in recipients} >= {2, 10}


@pytest.mark.asyncio
async def test_a_boot_does_not_invent_a_placeholder_slot(db):
    """The empty table is the initialised state; a boot must leave it empty.

    ``init_matchmaking_slot_service`` used to write one representative pair "to give the editor
    something to show", expecting the editor to expand it into the full all-to-all set on save. It
    does not: the editor reconstructs slots from the rows it is given, so an unconfigured
    deployment opened on a slot holding two of its cohorts, and saving from there replaced the
    intended all-to-all with that single pair.

    The row was also not harmless. It reads as a rule permitting one pair and nothing else, which is
    the opposite of the "everything may match everything" it was standing in for.
    """
    seed_cohorts(db)

    result = await init_matchmaking_slot_service(db)

    assert result["slots_count"] == 0
    assert result["configured"] is False
    with db.sessions() as session:
        assert session.query(MatchmakingSlotRecord).count() == 0


@pytest.mark.asyncio
async def test_a_boot_leaves_a_real_configuration_alone(db):
    """The no-op seeder must still not delete what an operator configured."""
    seed_one_slot(db)

    result = await init_matchmaking_slot_service(db)

    assert result["slots_count"] == 1
    assert result["configured"] is True
    with db.sessions() as session:
        assert session.query(MatchmakingSlotRecord).count() == 1


def test_the_delete_endpoint_takes_the_id_in_an_object(db, admin_client):
    """``DELETE`` reads ``{"slot_id": ...}``.

    The endpoint declared ``slot_id: str = Body(...)``, which makes FastAPI read the *entire body*
    as that one scalar. A client sending the object every other endpoint in this router takes was
    answered with a 422 naming ``body`` rather than ``slot_id`` -- a message that reads as a server
    fault rather than a shape mismatch, and which the save flow did not check, so the failed delete
    was reported to the operator as a successful save.
    """
    seed_one_slot(db)
    slot_id = admin_client.get("/admin/config").json()["slots"][0]["id"]

    response = admin_client.request(
        "DELETE", "/admin/config/matchmaking-slot", json={"slot_id": slot_id}
    )

    assert response.status_code == 200
    assert admin_client.get("/admin/config").json()["slots"] == []


def test_the_delete_endpoint_names_the_field_it_wanted(db, admin_client):
    """A wrong shape has to say which field was wrong.

    This is the assertion that would have made the original 422 diagnosable from the log alone: it
    must name ``slot_id``, so the caller can tell "I sent the wrong key" from "the server is
    broken".
    """
    seed_one_slot(db)

    response = admin_client.request(
        "DELETE", "/admin/config/matchmaking-slot", json={"slotId": "nope"}
    )

    assert response.status_code == 422
    assert any("slot_id" in str(detail.get("loc", "")) for detail in response.json()["detail"])
