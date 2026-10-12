"""Bulk participant onboarding: all-or-nothing, participants only, bounded and strict.

The endpoint is the first place a single request can create many accounts, so the tests are
arranged around the properties that make that safe rather than around the happy path alone:

* a file that would cross ``MAX_USER_COUNT`` writes *none* of its rows, and the check runs
  before the first insert;
* the 1000-row ceiling is independent of the deployment's account ceiling;
* one invalid or duplicate row cancels the whole file;
* there is no way to put a staff account in the file;
* the department ceiling is applied to the uploaded set as a whole;
* the parser refuses formulas, macros/external links and fake XLSX containers before any row
  reaches the database.
"""

import io
import zipfile

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select

from app import limits as app_limits
from app.config import settings
from app.db_models import AdminRecord, ModeratorRecord, UserRecord

HEADER = "registration number,email,full name,gender,year,department"

PASSWORDLESS_DEFAULTS = {
    "notifications_enabled": True,
    "matchmaking_enabled": True,
    "love_notes_receive_enabled": True,
    "love_notes_send_enabled": False,
    "is_blocked": False,
}


def csv_file(*rows: str) -> bytes:
    return ("\n".join((HEADER, *rows)) + "\n").encode("utf-8")


def row(
    regno: str,
    *,
    name: str | None = None,
    email: str | None = None,
    gender: str = "other",
    year: object = 2,
    department: str = "CSE",
) -> str:
    resolved_email = email if email is not None else f"{regno.lower()}@example.com"
    resolved_name = name if name is not None else f"Person {regno}"
    return f"{regno},{resolved_email},{resolved_name},{gender},{year},{department}"


def upload(admin_client, content: bytes, filename: str = "participants.csv"):
    content_type = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if filename.endswith(".xlsx")
        else "text/csv"
    )
    return admin_client.post(
        "/admin/users/bulk-upload",
        files={"file": (filename, content, content_type)},
    )


def xlsx_file(rows: list[list[object]], *, header: list[str] | None = None) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(header or ["registration number", "email", "full name", "gender", "year", "department"])
    for values in rows:
        worksheet.append(values)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def with_zip_member(content: bytes, member: str, payload: bytes = b"not a real part") -> bytes:
    """Rebuild an XLSX with one extra ZIP member, preserving the workbook parts."""
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as source:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                target.writestr(info, source.read(info.filename))
            target.writestr(member, payload)
    return output.getvalue()


def user_count(db) -> int:
    with db.sessions() as session:
        return session.scalar(select(func.count()).select_from(UserRecord)) or 0


def seed_user(db, regno: str, *, email: str | None = None, department: str = "CSE") -> None:
    with db.sessions.begin() as session:
        session.add(
            UserRecord(
                regno=regno,
                name=f"Existing {regno}",
                email=email or f"{regno.lower()}@example.com",
                gender="other",
                year=2,
                department=department,
            )
        )


# ------------------------------------------------------------------ the happy path


def test_csv_upload_creates_participants_with_the_same_defaults_as_single_create(
    admin_client, db
):
    response = upload(
        admin_client,
        csv_file(
            row("bulk-1", name="Ada Lovelace", gender="Female", year="2"),
            row("bulk-2", name="Grace Hopper", email="GRACE@EXAMPLE.COM", year=3),
        ),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created"] == 2
    assert body["total_users"] == 2
    assert body["remaining_slots"] == settings.MAX_USER_COUNT - 2

    with db.sessions() as session:
        users = {
            user.regno: user
            for user in session.scalars(select(UserRecord)).all()
        }
    assert set(users) == {"bulk-1", "bulk-2"}
    first = users["bulk-1"]
    assert first.name == "Ada Lovelace"
    assert first.gender == "female"
    assert first.year == 2
    assert first.department == "CSE"
    assert users["bulk-2"].email == "grace@example.com"
    for user in users.values():
        for attribute, expected in PASSWORDLESS_DEFAULTS.items():
            assert getattr(user, attribute) is expected
        assert user.interests == []
        assert user.profile_picture_public_id is None
        assert user.profile_picture_format is None


def test_xlsx_upload_reads_the_first_worksheet(admin_client, db):
    content = xlsx_file(
        [
            ["xlsx-1", "xlsx1@example.com", "Xlsx One", "male", 1, "ECE"],
            ["xlsx-2", "xlsx2@example.com", "Xlsx Two", "other", 12, "MECH"],
        ]
    )

    response = upload(admin_client, content, filename="participants.xlsx")

    assert response.status_code == 201, response.text
    assert response.json()["created"] == 2
    assert user_count(db) == 2


def test_a_successful_upload_is_visible_to_the_next_account_list_read(admin_client, db):
    """The list's short count cache is cleared, so the screen does not contradict the result."""
    before = admin_client.get("/admin/users", params={"account_type": "users"}).json()
    assert before["total"] == 0

    response = upload(admin_client, csv_file(row("cache-1"), row("cache-2")))
    assert response.status_code == 201, response.text

    after = admin_client.get("/admin/users", params={"account_type": "users"}).json()
    assert after["total"] == 2


def test_uploaded_departments_join_existing_spellings_case_insensitively(admin_client, db):
    seed_user(db, "existing-dept", department="CSE")

    response = upload(admin_client, csv_file(row("dept-join", department="cse")))

    assert response.status_code == 201, response.text
    with db.sessions() as session:
        uploaded = session.scalar(select(UserRecord).where(UserRecord.regno == "dept-join"))
    assert uploaded.department == "CSE"


def test_two_spellings_of_one_new_department_become_one_value(admin_client, db):
    response = upload(
        admin_client,
        csv_file(
            row("dept-spelling-1", department="New Department"),
            row("dept-spelling-2", department="new department"),
        ),
    )

    assert response.status_code == 201, response.text
    with db.sessions() as session:
        departments = session.scalars(
            select(UserRecord.department).order_by(UserRecord.regno)
        ).all()
    assert departments == ["New Department", "New Department"]


# ------------------------------------------------------- participants only


def test_an_account_type_column_is_refused_rather_than_ignored(admin_client, db):
    """There is no path through this endpoint that mints a staff account."""
    header = "account_type,registration number,email,full name,gender,year,department"
    content = (header + "\nadmin,STAFF-1,staff@example.com,Staff,other,2,CSE\n").encode()

    response = upload(admin_client, content)

    assert response.status_code == 400, response.text
    assert "account_type" in response.text
    assert user_count(db) == 0
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(AdminRecord)) == 0
        assert session.scalar(select(func.count()).select_from(ModeratorRecord)) == 0


def test_a_moderator_cannot_bulk_upload(moderator_client, db):
    response = upload(moderator_client, csv_file(row("no-permission")))

    assert response.status_code == 403, response.text
    assert user_count(db) == 0


# ----------------------------------------------------- caps and all-or-nothing


def test_upload_is_refused_whole_when_it_would_cross_max_user_count(
    admin_client, db, monkeypatch
):
    """The exact shape of the requirement: check the projected total before writing."""
    for index in range(2):
        seed_user(db, f"existing-{index}")
    monkeypatch.setattr(settings, "MAX_USER_COUNT", 5)
    before = user_count(db)

    response = upload(
        admin_client,
        csv_file(*(row(f"over-cap-{index}") for index in range(4))),
    )

    assert response.status_code == 409, response.text
    assert "2" in response.text and "5" in response.text
    assert user_count(db) == before
    with db.sessions() as session:
        assert session.scalar(
            select(UserRecord).where(UserRecord.regno == "over-cap-0")
        ) is None


def test_upload_filling_the_last_slots_succeeds(admin_client, db, monkeypatch):
    for index in range(2):
        seed_user(db, f"fits-{index}")
    monkeypatch.setattr(settings, "MAX_USER_COUNT", 4)

    response = upload(
        admin_client,
        csv_file(row("last-slot-1"), row("last-slot-2")),
    )

    assert response.status_code == 201, response.text
    assert user_count(db) == 4


def test_blocked_accounts_count_towards_the_upload_cap(admin_client, db, monkeypatch):
    with db.sessions.begin() as session:
        session.add(
            UserRecord(
                regno="blocked-slot",
                name="Blocked",
                email="blocked@example.com",
                gender="other",
                year=2,
                department="CSE",
                is_blocked=True,
            )
        )
    monkeypatch.setattr(settings, "MAX_USER_COUNT", 1)

    response = upload(admin_client, csv_file(row("would-be-second")))

    assert response.status_code == 409, response.text
    assert user_count(db) == 1


def test_more_than_1000_rows_is_refused_even_when_the_account_cap_is_much_larger(
    admin_client, db, monkeypatch
):
    monkeypatch.setattr(settings, "MAX_USER_COUNT", 10_000)
    rows = [row(f"row-limit-{index}") for index in range(app_limits.BULK_UPLOAD_MAX_ROWS + 1)]

    response = upload(admin_client, csv_file(*rows))

    assert response.status_code == 400, response.text
    assert str(app_limits.BULK_UPLOAD_MAX_ROWS) in response.text
    assert user_count(db) == 0


def test_exactly_1000_rows_is_accepted(admin_client, db, monkeypatch):
    monkeypatch.setattr(settings, "MAX_USER_COUNT", 1000)
    rows = [row(f"bulk-boundary-{index}") for index in range(app_limits.BULK_UPLOAD_MAX_ROWS)]

    response = upload(admin_client, csv_file(*rows))

    assert response.status_code == 201, response.text
    assert response.json()["created"] == app_limits.BULK_UPLOAD_MAX_ROWS
    assert user_count(db) == app_limits.BULK_UPLOAD_MAX_ROWS


def test_one_invalid_row_cancels_every_valid_row_beside_it(admin_client, db):
    response = upload(
        admin_client,
        csv_file(
            row("all-or-nothing-1"),
            row("all-or-nothing-2"),
            row("all-or-nothing-3", year=13),
        ),
    )

    assert response.status_code == 400, response.text
    assert "row 4" in response.text.lower()
    assert user_count(db) == 0


def test_a_duplicate_inside_the_file_cancels_the_upload(admin_client, db):
    response = upload(
        admin_client,
        csv_file(row("dupe-1"), row("dupe-2"), row("DUPE-1")),
    )

    assert response.status_code == 400, response.text
    assert "duplicate" in response.text.lower()
    assert user_count(db) == 0


def test_an_existing_regno_is_matched_case_insensitively(admin_client, db):
    seed_user(db, "Existing-Regno")

    response = upload(admin_client, csv_file(row("existing-regno")))

    assert response.status_code == 409, response.text
    assert user_count(db) == 1


def test_an_existing_registration_number_or_email_cancels_the_upload(admin_client, db):
    seed_user(db, "already-there", email="taken@example.com")

    response = upload(
        admin_client,
        csv_file(
            row("brand-new"),
            row("another-new", email="TAKEN@example.com"),
        ),
    )

    assert response.status_code == 409, response.text
    assert user_count(db) == 1
    with db.sessions() as session:
        assert session.scalar(
            select(UserRecord).where(UserRecord.regno == "brand-new")
        ) is None


def test_the_department_ceiling_applies_to_the_uploaded_set_as_a_whole(
    admin_client, db, monkeypatch
):
    seed_user(db, "department-holder", department="CSE")
    monkeypatch.setattr(app_limits, "MAX_DEPARTMENTS", 2)
    before = user_count(db)

    over = upload(
        admin_client,
        csv_file(
            row("dept-over-1", department="CSE"),
            row("dept-over-2", department="ECE"),
            row("dept-over-3", department="MECH"),
        ),
    )

    assert over.status_code == 409, over.text
    assert user_count(db) == before

    fits = upload(
        admin_client,
        csv_file(
            row("dept-fits-1", department="CSE"),
            row("dept-fits-2", department="ECE"),
        ),
    )
    assert fits.status_code == 201, fits.text
    assert user_count(db) == before + 2


# ----------------------------------------------------------- strict field rules


@pytest.mark.parametrize(
    "bad_row",
    [
        row("bad-year-low", year=0),
        row("bad-year-high", year=app_limits.USER_YEAR_MAX + 1),
        row("bad-year-text", year="second"),
        row("bad-gender", gender="nonbinary"),
        row("bad-email", email="not-an-email"),
        row("bad-name", name=""),
        row(regno=""),
        row("bad-department", department=""),
        row("too-long-name", name="N" * (app_limits.USER_NAME_MAX + 1)),
        row(regno="R" * (app_limits.USER_REGNO_MAX + 1)),
        row("too-long-dept", department="D" * (app_limits.USER_DEPARTMENT_MAX + 1)),
    ],
)
def test_each_invalid_field_refuses_the_whole_file(admin_client, db, bad_row):
    response = upload(admin_client, csv_file(row("valid-row"), bad_row))

    assert response.status_code == 400, response.text
    assert user_count(db) == 0


def test_missing_or_unknown_columns_are_refused(admin_client, db):
    missing = ("registration number,email,full name,gender,department\n"
               "missing-year,missing@example.com,Missing,other,CSE\n").encode()
    unknown = (HEADER + ",favourite_colour\n"
               "unknown-col,unknown@example.com,Unknown,other,2,CSE,blue\n").encode()

    for content in (missing, unknown):
        response = upload(admin_client, content)
        assert response.status_code == 400, response.text
    assert user_count(db) == 0


def test_a_header_only_file_is_refused(admin_client, db):
    response = upload(admin_client, (HEADER + "\n").encode())

    assert response.status_code == 400, response.text
    assert user_count(db) == 0


def test_a_csv_formula_prefix_is_refused(admin_client, db):
    response = upload(
        admin_client,
        csv_file(row("formula-name", name="=HYPERLINK(\"https://example.test\")")),
    )

    assert response.status_code == 400, response.text
    assert "formula" in response.text.lower()
    assert user_count(db) == 0


# ---------------------------------------------------------------- file security


def test_a_fake_xlsx_is_refused(admin_client, db):
    response = upload(admin_client, csv_file(row("fake-xlsx")), filename="participants.xlsx")

    assert response.status_code == 400, response.text
    assert user_count(db) == 0


def test_an_xlsx_formula_is_refused(admin_client, db):
    response = upload(
        admin_client,
        xlsx_file([["formula-1", "formula@example.com", "=1+1", "other", 2, "CSE"]]),
        filename="participants.xlsx",
    )

    assert response.status_code == 400, response.text
    assert "formula" in response.text.lower()
    assert user_count(db) == 0


@pytest.mark.parametrize("member", ["xl/vbaProject.bin", "xl/externalLinks/externalLink1.xml"])
def test_macro_and_external_link_parts_are_refused(admin_client, db, member):
    content = with_zip_member(
        xlsx_file([["unsafe-1", "unsafe@example.com", "Unsafe", "other", 2, "CSE"]]),
        member,
    )

    response = upload(admin_client, content, filename="participants.xlsx")

    assert response.status_code == 400, response.text
    assert user_count(db) == 0


def test_an_unsupported_extension_is_refused(admin_client, db):
    response = upload(admin_client, csv_file(row("wrong-extension")), filename="participants.txt")

    assert response.status_code == 400, response.text
    assert user_count(db) == 0


def test_a_file_over_the_byte_limit_is_refused(admin_client, db, monkeypatch):
    monkeypatch.setattr(app_limits, "BULK_UPLOAD_MAX_BYTES", 200)

    response = upload(admin_client, csv_file(row("too-large", name="L" * 300)))

    assert response.status_code == 413, response.text
    assert user_count(db) == 0
