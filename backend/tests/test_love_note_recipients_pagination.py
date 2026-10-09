"""The love-note recipient list is paginated, filtered and searched on the database.

``GET /love-notes/users`` used to answer every eligible participant in one response and let
the picker search them in the browser. That is a full read of the participant table on every
open of the send dialog, and the client-side filters could only ever work on the rows it
already had. These tests pin the replacement contract:

* every read answers ``{users, total, skip, limit}`` and never a bare list;
* ``limit`` is clamped (anything over 100 is a 422), so no client can ask for the table;
* ``skip``/``limit`` walk a stable name order, so a filtered page still pages downwards
  without repeating or skipping a row;
* the viewer's own gender is excluded unless ``include_same_gender=true``;
* the name search is a SQL match counted under the same filter, not a scan of a loaded list;
* the cohort filter (year + department) narrows in SQL, and self and opted-out accounts
  never appear in a page or in the total.
"""

from contextlib import contextmanager

import pytest
from sqlalchemy import select

from app.accounts import AccountType
from app.db_models import UserRecord
from app.main import app
from app.models import UserDetails
from app.services.auth_service import get_current_user


def make_user(db, regno, gender, *, year=2, department="CSE", name=None,
              love_notes_receive_enabled=True):
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno,
            name=name or f"User {regno}",
            email=f"{regno}@test.com",
            year=year, department=department, gender=gender,
            love_notes_receive_enabled=love_notes_receive_enabled,
        )
        session.add(row)
        session.flush()
        return str(row.id)


def as_user(db, regno) -> UserDetails:
    with db.sessions() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
    return UserDetails(
        id=str(row.id), regno=row.regno, name=row.name, email=row.email,
        year=row.year, department=row.department, gender=row.gender,
        matchmaking_enabled=row.matchmaking_enabled,
        notifications_enabled=row.notifications_enabled, is_blocked=row.is_blocked,
        last_matchmaking_at=row.last_matchmaking_at,
        account_type=AccountType.USERS.value,
    )


@pytest.fixture
def login_as(db):
    """Run the calls inside the block as a named participant."""

    @contextmanager
    def _login(regno):
        previous = app.dependency_overrides.get(get_current_user)
        app.dependency_overrides[get_current_user] = lambda: as_user(db, regno)
        try:
            yield
        finally:
            if previous is not None:
                app.dependency_overrides[get_current_user] = previous
            else:
                app.dependency_overrides.pop(get_current_user, None)

    return _login


def test_the_recipient_list_answers_a_page_envelope_not_a_list(db, client, login_as):
    make_user(db, "viewer", "female")
    make_user(db, "candidate", "male")

    with login_as("viewer"):
        response = client.get("/love-notes/users")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) >= {"users", "total", "skip", "limit"}
    assert body["skip"] == 0
    assert body["limit"] == 50
    assert body["total"] == 1
    assert [user["name"] for user in body["users"]] == ["User candidate"]


def test_limit_is_clamped(db, client, login_as):
    make_user(db, "viewer", "female")
    make_user(db, "candidate", "male")

    with login_as("viewer"):
        assert client.get("/love-notes/users", params={"limit": 1000}).status_code == 422


def test_skip_and_limit_walk_a_stable_name_order(db, client, login_as):
    make_user(db, "viewer", "female")
    for index in range(5):
        make_user(db, f"candidate-{index}", "male", name=f"Person {index}")

    with login_as("viewer"):
        first = client.get("/love-notes/users", params={"skip": 0, "limit": 2}).json()
        second = client.get("/love-notes/users", params={"skip": 2, "limit": 2}).json()
        third = client.get("/love-notes/users", params={"skip": 4, "limit": 2}).json()

    assert first["total"] == 5
    assert [len(page["users"]) for page in (first, second, third)] == [2, 2, 1]
    names = [user["name"] for page in (first, second, third) for user in page["users"]]
    assert names == [f"Person {index}" for index in range(5)]
    ids = [user["id"] for page in (first, second, third) for user in page["users"]]
    assert len(set(ids)) == 5


def test_the_viewers_own_gender_is_hidden_until_the_switch_is_used(db, client, login_as):
    make_user(db, "viewer", "female")
    make_user(db, "she", "female")
    make_user(db, "he", "male")
    make_user(db, "they", "other")

    with login_as("viewer"):
        default_page = client.get("/love-notes/users").json()
        widened_page = client.get(
            "/love-notes/users", params={"include_same_gender": "true"}
        ).json()

    assert {user["name"] for user in default_page["users"]} == {"User he", "User they"}
    assert default_page["total"] == 2
    assert {user["name"] for user in widened_page["users"]} == {
        "User she", "User he", "User they",
    }
    assert widened_page["total"] == 3


def test_the_name_search_is_run_in_sql_and_counted_under_it(db, client, login_as):
    make_user(db, "viewer", "female")
    make_user(db, "alice", "male", name="Alice Anderson")
    make_user(db, "alina", "male", name="Alina Avery")
    make_user(db, "bob", "male", name="Bob Brown")

    with login_as("viewer"):
        page = client.get("/love-notes/users", params={"q": "ali"}).json()
        empty = client.get("/love-notes/users", params={"q": "nobody"}).json()

    assert page["total"] == 2
    assert [user["name"] for user in page["users"]] == ["Alice Anderson", "Alina Avery"]
    assert empty["total"] == 0
    assert empty["users"] == []


def test_the_cohort_filter_narrows_in_sql_case_insensitively(db, client, login_as):
    make_user(db, "viewer", "female")
    make_user(db, "cse-2", "male", year=2, department="CSE")
    make_user(db, "cse-3", "male", year=3, department="CSE")
    make_user(db, "ece-2", "male", year=2, department="ECE")

    with login_as("viewer"):
        year_two = client.get("/love-notes/users", params={"year": 2}).json()
        cse_two = client.get(
            "/love-notes/users", params={"year": 2, "department": "cse"}
        ).json()

    assert {user["name"] for user in year_two["users"]} == {"User cse-2", "User ece-2"}
    assert year_two["total"] == 2
    assert [user["name"] for user in cse_two["users"]] == ["User cse-2"]
    assert cse_two["total"] == 1


def test_a_filtered_page_still_paginates_with_the_filtered_total(db, client, login_as):
    """The filter limits what the pages walk; ``total`` counts the filtered set, not the table."""
    make_user(db, "viewer", "female")
    for index in range(5):
        make_user(db, f"cse-{index}", "male", name=f"CSE person {index}", department="CSE")
        make_user(db, f"ece-{index}", "male", name=f"ECE person {index}", department="ECE")

    with login_as("viewer"):
        first = client.get(
            "/love-notes/users", params={"department": "CSE", "skip": 0, "limit": 2}
        ).json()
        last = client.get(
            "/love-notes/users", params={"department": "CSE", "skip": 4, "limit": 2}
        ).json()

    assert first["total"] == 5
    assert [user["name"] for user in first["users"]] == ["CSE person 0", "CSE person 1"]
    assert [user["name"] for user in last["users"]] == ["CSE person 4"]


def test_the_viewer_and_opted_out_accounts_are_never_listed(db, client, login_as):
    make_user(db, "viewer", "female")
    make_user(db, "optedout", "male", love_notes_receive_enabled=False)

    with login_as("viewer"):
        page = client.get(
            "/love-notes/users", params={"include_same_gender": "true"}
        ).json()

    assert page["total"] == 0
    assert page["users"] == []
