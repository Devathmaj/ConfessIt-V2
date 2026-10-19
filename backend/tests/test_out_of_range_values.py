"""A value the column cannot hold is refused, not answered with a 500.

The companion to ``test_malformed_id_sweep.py``: that file proves the *whole
surface* refuses a value it cannot hold, and this one pins the three rules that
surface relies on, so each of them says what it is for.

**The offset.** A page size is bounded on every paginated endpoint; the offset
was not, and an offset is an ``OFFSET`` clause -- a PostgreSQL bigint. A value
past that range is refused by the database as "bigint out of range", which is a
500 for a request that asked for a page that does not exist. Twenty-three
routes took one.

**The year.** A participant's year is an integer column. A year outside the
range is refused by the column as "integer out of range" -- again a 500 for a
value the client got wrong.

**The bound that was not there.** The matchmaking-slot route declared its years
as ``Year = Body(...)``, the bounded int the participant payload uses. That
spelling **drops the bound**: a parameter whose default is an explicit ``Body``
takes that ``Body`` as its field info, and the constraints on the annotation are
not carried over. The route's own comment claimed a 422 naming the field; the
truth was a year of 13 reaching the table and being refused by a CHECK
constraint as a 500. The bound is now written where it survives.

**The payload checked twice.** The generation routes publish one model and
validate against a stricter one. The second check raises a Pydantic
``ValidationError``, which is not an ``HTTPException`` -- and an exception nobody
handles is a 500 for a payload the client got wrong.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db_models import MatchmakingSlotRecord
from app.limits import PAGE_OFFSET_MAX, USER_YEAR_MAX, USER_YEAR_MIN


# ----------------------------------------------------------------- the offset


@pytest.mark.parametrize(
    "path",
    [
        "/confessions",
        "/notifications",
        "/love-notes/inbox",
        "/love-notes/users",
    ],
)
def test_a_pagination_offset_past_the_column_is_refused(db, client, path):
    """A request for a page past the end is a refusal, not a server fault."""
    response = client.get(path, params={"skip": 2 ** 70})

    assert response.status_code != 500, response.text
    assert response.status_code == 422, response.text


def test_an_offset_just_inside_the_bound_is_accepted(db, client):
    """The bound refuses a page that cannot exist, not a page that can.

    A client paging to the end of a long list must not be refused for it.
    """
    response = client.get("/confessions", params={"skip": PAGE_OFFSET_MAX})

    assert response.status_code == 200, response.text
    assert response.json()["confessions"] == []


# ------------------------------------------------------------------- the year


def test_a_year_outside_the_range_is_refused(db, client, admin_client):
    """The admin user list filters on a participant's year, an integer column."""
    response = admin_client.get("/admin/users", params={"year": 2 ** 70})

    assert response.status_code != 500, response.text
    assert response.status_code == 422, response.text


def test_a_year_beyond_the_range_is_refused(db, admin_client):
    """The range, not merely the column's width.

    A year of 13 fits the column and is refused by the range instead, so the
    answer is the same 422 either way rather than a 500 for the one that does
    not fit and a 404 for the one that does.
    """
    response = admin_client.get("/admin/users", params={"year": USER_YEAR_MAX + 1})

    assert response.status_code == 422, response.text


# --------------------------------------------- the bound that was not being applied


def test_the_matchmaking_slot_route_refuses_a_year_outside_the_range(
    db, admin_client
):
    """The bound has to be somewhere the framework will enforce.

    Declared as ``Year = Body(...)`` the bound was silently dropped, so the year
    reached the table and the CHECK constraint refused it -- a 500, which is the
    thing the bound exists to prevent.
    """
    response = admin_client.post(
        "/admin/config/matchmaking-slot",
        json={
            "year_a": USER_YEAR_MAX + 1,
            "department_a": "CSE",
            "year_b": USER_YEAR_MIN,
            "department_b": "ECE",
        },
    )

    assert response.status_code != 500, response.text
    assert response.status_code == 422, response.text


def test_the_refusal_names_the_field_that_was_wrong(db, admin_client):
    """A 422 that says "body" is a shape mismatch; one that says which field is
    what the caller needs to fix it.

    The delete route on this same path already names its field, and the test
    ``test_the_delete_endpoint_names_the_field_it_wanted`` records that as the
    convention.
    """
    response = admin_client.post(
        "/admin/config/matchmaking-slot",
        json={
            "year_a": USER_YEAR_MAX + 1,
            "department_a": "CSE",
            "year_b": USER_YEAR_MIN,
            "department_b": "ECE",
        },
    )

    assert response.status_code == 422
    assert any(
        "year_a" in str(detail.get("loc", "")) for detail in response.json()["detail"]
    )


def test_a_year_inside_the_range_is_still_accepted(db, admin_client):
    """The counterweight: the bound refuses a year outside the range, and
    nothing else. A bound that refused everything would pass the test above."""
    response = admin_client.post(
        "/admin/config/matchmaking-slot",
        json={
            "year_a": USER_YEAR_MIN,
            "department_a": "CSE",
            "year_b": USER_YEAR_MAX,
            "department_b": "ECE",
        },
    )

    assert response.status_code == 200, response.text
    with db.sessions() as session:
        row = session.scalar(select(MatchmakingSlotRecord))
    assert (row.year_a, row.year_b) == (USER_YEAR_MIN, USER_YEAR_MAX)


# --------------------------------------------- the payload that is checked twice


@pytest.mark.parametrize(
    "path, field",
    [
        ("/admin/plot-twist/blueprints", "max_scenes"),
        ("/admin/plot-twist/blueprints", "decision_count"),
        ("/admin/case-closed/blueprints", "suspect_count"),
    ],
)
def test_a_generation_config_outside_its_range_is_refused(db, admin_client, path, field):
    """The route publishes a loose model and validates against a strict one.

    The strict one is the authority, and its refusal has to reach the client as
    the 422 it is rather than as an unhandled exception.
    """
    response = admin_client.post(path, json={"difficulty": "medium", field: 2 ** 70})

    assert response.status_code != 500, response.text
    assert response.status_code == 422, response.text


def test_a_generation_config_inside_its_range_is_accepted(db, admin_client):
    """The counterweight again: a good payload still works."""
    response = admin_client.post(
        "/admin/plot-twist/blueprints", json={"difficulty": "medium", "max_scenes": 10}
    )

    assert response.status_code == 201, response.text
