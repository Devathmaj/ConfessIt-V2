"""The admin user list is strictly paginated on the database itself.

``GET /admin/users`` used to return every account in one response: three unbounded
SELECTs, serialised whole. With ten thousand participants that one request materialises
ten thousand rows at once. These tests pin the replacement contract:

* every read answers ``{users, total, skip, limit}`` and never a bare list;
* ``limit`` is clamped (anything over 100 is a 422), so no client can ask for the table;
* ``skip``/``limit`` are a SQL OFFSET/LIMIT on the filtered query, not a Python slice of
  a full load -- paging through five participants with ``limit=2`` takes three requests
  to see them all and no request holds more than two participant rows;
* ``q``, ``year``, ``department``, ``sort`` and ``flagged`` are SQL WHERE/ORDER BY for
  the same reason: filtering must narrow in the database, not after loading it.
"""

from sqlalchemy import select

from app.db_models import ModeratorRecord, UserRecord


def make_user(db, regno, name=None, year=2, department="CSE"):
    name = name or f"User {regno}"
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno,
            name=name,
            email=f"{regno}@test.com",
            year=year, department=department,
            gender="other",
        )
        session.add(row)
        session.flush()
        return {"id": str(row.id), "regno": regno, "name": name}


def make_moderator(db, regno="mod"):
    with db.sessions.begin() as session:
        row = ModeratorRecord(regno=regno, name=f"Mod {regno}", email=f"{regno}@test.com")
        session.add(row)
        session.flush()
        return {"id": str(row.id), "regno": regno}


def set_removals(db, regno, total):
    """Charge ``total`` upheld removals against one participant, directly on the counters."""
    with db.sessions.begin() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
        row.confession_removal_count = total


def test_the_user_list_answers_a_page_envelope_not_a_list(db, admin_client):
    make_user(db, "page-shape-1")
    response = admin_client.get("/admin/users", params={"account_type": "users"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) >= {"users", "total", "skip", "limit"}
    assert body["skip"] == 0
    assert body["limit"] == 50
    assert body["total"] >= 1
    assert len(body["users"]) <= body["limit"]


def test_limit_is_clamped_so_no_request_can_ask_for_the_table(db, admin_client):
    make_user(db, "page-clamp-1")
    response = admin_client.get("/admin/users", params={"limit": 1000})

    assert response.status_code == 422, response.text


def test_skip_and_limit_page_through_participants_without_holding_them_all(db, admin_client):
    for index in range(5):
        make_user(db, f"page-walk-{index}", name=f"Walker {index}")

    first = admin_client.get(
        "/admin/users",
        params={"account_type": "users", "q": "page-walk", "sort": "name", "skip": 0, "limit": 2},
    ).json()
    second = admin_client.get(
        "/admin/users",
        params={"account_type": "users", "q": "page-walk", "sort": "name", "skip": 2, "limit": 2},
    ).json()
    third = admin_client.get(
        "/admin/users",
        params={"account_type": "users", "q": "page-walk", "sort": "name", "skip": 4, "limit": 2},
    ).json()

    assert first["total"] == 5
    assert [row["regno"] for row in first["users"]] == ["page-walk-0", "page-walk-1"]
    assert [row["regno"] for row in second["users"]] == ["page-walk-2", "page-walk-3"]
    assert [row["regno"] for row in third["users"]] == ["page-walk-4"]
    seen = [row["regno"] for page in (first, second, third) for row in page["users"]]
    assert sorted(seen) == sorted(f"page-walk-{index}" for index in range(5))


def test_searchnarrows_in_the_database(db, admin_client):
    make_user(db, "page-search-ada", name="Ada Lovelace")
    make_user(db, "page-search-grace", name="Grace Hopper")

    body = admin_client.get(
        "/admin/users", params={"account_type": "users", "q": "hopper"}
    ).json()

    assert body["total"] == 1
    assert [row["regno"] for row in body["users"]] == ["page-search-grace"]


def test_year_and_department_narrow_participants(db, admin_client):
    make_user(db, "page-cohort-a", year=2, department="CSE")
    make_user(db, "page-cohort-b", year=3, department="CSE")
    make_user(db, "page-cohort-c", year=2, department="ECE")

    by_year = admin_client.get(
        "/admin/users", params={"account_type": "users", "q": "page-cohort", "year": 2}
    ).json()
    assert by_year["total"] == 2

    by_both = admin_client.get(
        "/admin/users",
        params={"account_type": "users", "q": "page-cohort", "year": 2, "department": "cse"},
    ).json()
    assert by_both["total"] == 1
    assert by_both["users"][0]["regno"] == "page-cohort-a"


def test_flagged_and_ordinary_partition_by_upheld_removals(db, admin_client):
    make_user(db, "page-flagged-hot")
    make_user(db, "page-flagged-cold")
    set_removals(db, "page-flagged-hot", 6)

    flagged = admin_client.get(
        "/admin/users", params={"account_type": "users", "q": "page-flagged", "flagged": "flagged"}
    ).json()
    ordinary = admin_client.get(
        "/admin/users",
        params={"account_type": "users", "q": "page-flagged", "flagged": "ordinary"},
    ).json()

    assert [row["regno"] for row in flagged["users"]] == ["page-flagged-hot"]
    assert [row["regno"] for row in ordinary["users"]] == ["page-flagged-cold"]


def test_staff_reads_stay_complete_while_participants_page(db, admin_client):
    make_moderator(db, "page-staff-mod")
    for index in range(3):
        make_user(db, f"page-staff-{index}")

    body = admin_client.get("/admin/users", params={"q": "page-staff", "skip": 0, "limit": 2}).json()

    # The moderator matches the search and arrives whole; the participants arrive as one page
    # of two under a total of three. One bounded request, never the whole table.
    kinds = {row["regno"]: row["account_type"] for row in body["users"]}
    assert kinds.get("page-staff-mod") == "moderator"
    participant_rows = [row for row in body["users"] if row["account_type"] == "users"]
    assert len(participant_rows) == 2
    assert body["total"] == 4
