"""Two rules about how many accounts may exist and which of them may be silenced.

Both are the kind of rule that is worthless in the wrong place:

* **The per-table caps** (``MAX_USER_COUNT``, ``MAX_ADMIN_COUNT``, ``MAX_MODERATOR_COUNT``) are
  read from ``.env``. An administrator has to be told when the ceiling has been reached, which
  means the refusal has to be an HTTP status and not an exception from somewhere deep in a
  service -- and it has to name the numbers, because "limit reached" with no numbers is not
  actionable.

* **An admin cannot be blocked** is enforced by a CHECK constraint on the ``admin`` table, and
  that is the whole point of the way it is written. The route that blocks accounts resolves its
  target by id across all three tables, so a rule living in that route would be one forgotten
  ``if`` away from not existing. These tests therefore deliberately bypass the API with raw SQL
  to check that PostgreSQL is the thing refusing -- a test that only went through the endpoint
  would pass just as happily with the constraint deleted and the route check left in place.

The counting rule has a subtlety worth stating, because it is the part that is easy to get
backwards: **a blocked account still occupies a slot.** Blocking is reversible and is not
deletion, so if blocked rows were excluded from the count then blocking somebody would free a
slot, and the cap would silently become a limit on how many accounts are *currently allowed to
sign in* rather than on how many exist.
"""

import contextlib

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.accounts import AccountType, account_cap, account_cap_lock_key
from app.config import settings
from app.db_models import AdminRecord, ModeratorRecord, UserRecord
from app.main import app
from app.models import AccountDetails
from app.services.auth_service import get_current_user


ACCOUNT_MODELS_BY_TYPE = {
    AccountType.USERS.value: UserRecord,
    AccountType.MODERATOR.value: ModeratorRecord,
    AccountType.ADMIN.value: AdminRecord,
}

CAP_SETTING_BY_TYPE = {
    AccountType.USERS.value: "MAX_USER_COUNT",
    AccountType.ADMIN.value: "MAX_ADMIN_COUNT",
    AccountType.MODERATOR.value: "MAX_MODERATOR_COUNT",
}


@pytest.fixture
def restore_caps():
    """Put the three ceilings back after a test has moved one.

    ``settings`` is a process-wide singleton and these tests have to move the numbers to test
    them, so without this one test's ceiling would silently become every later test's ceiling --
    and because the suite shares a database, that would surface as an unrelated test failing
    with a 409.
    """
    saved = {
        field: getattr(settings, field) for field in CAP_SETTING_BY_TYPE.values()
    }
    yield
    for field, value in saved.items():
        setattr(settings, field, value)


def _payload(regno: str, account_type: str, **overrides):
    """A create payload that is valid for any account type, plus its participant fields."""
    payload = {
        "account_type": account_type,
        "regno": regno,
        "name": f"Person {regno}",
        "email": f"{regno.lower()}@example.com",
    }
    if account_type == AccountType.USERS.value:
        payload["year"] = 2
        payload["department"] = "Cap Class"
        # Gender is a ``users`` column like year and department, so a staff create that
        # carried one would be a field the router drops rather than a field it writes.
        payload["gender"] = "other"
    payload.update(overrides)
    return payload


def _count(db, account_type: str) -> int:
    model = ACCOUNT_MODELS_BY_TYPE[account_type]
    with db.sessions() as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def _seed_rows(db, account_type: str, how_many: int, blocked: bool = False) -> None:
    """Write rows straight into the table, to fill it to a known depth.

    Raw inserts rather than the create endpoint, because the endpoint is the thing under test:
    using it to arrange the preconditions would let a cap that is one too strict reject the
    setup and be reported as a passing test that asserted nothing.
    """
    model = ACCOUNT_MODELS_BY_TYPE[account_type]
    with db.sessions.begin() as session:
        for index in range(how_many):
            regno = f"FILL-{account_type}-{index}"
            participant_fields = (
                {"year": 2, "department": "Fill Class", "gender": "other"}
                if account_type == AccountType.USERS.value
                else {}
            )
            session.add(
                model(
                    regno=regno,
                    name=f"Filler {index}",
                    email=f"fill-{account_type}-{index}@example.com",
                    is_blocked=blocked,
                    **participant_fields,
                )
            )


# ----------------------------------------------------------------------- the caps


@pytest.mark.parametrize("account_type", sorted(CAP_SETTING_BY_TYPE))
def test_the_cap_is_read_from_settings_for_every_account_type(
    restore_caps, account_type
):
    """Each table's ceiling comes from its own variable, not from a shared default.

    ``account_cap`` is the single lookup every cap check goes through, so this is what pins the
    variable-to-table wiring. Getting it wrong is silent and nasty in the safe direction: a
    moderator cap that read ``MAX_USER_COUNT`` would refuse to add moderators in a deployment
    that had raised the participant ceiling for a new cohort.
    """
    setattr(settings, CAP_SETTING_BY_TYPE[account_type], 7)
    assert account_cap(account_type) == 7


@pytest.mark.parametrize("account_type", sorted(CAP_SETTING_BY_TYPE))
def test_every_account_table_locks_on_its_own_key(restore_caps, account_type):
    """The advisory-lock keys must differ per table.

    They serialise account creation so a count-then-insert cannot be raced past the cap. Sharing
    one key would still be *correct* -- it would just serialise creation of a participant against
    creation of a moderator, so an unrelated slow request could delay an admin action. Sharing a
    key with some other lock in the codebase would be worse than slow.
    """
    keys = {account_cap_lock_key(kind) for kind in CAP_SETTING_BY_TYPE}
    assert len(keys) == len(CAP_SETTING_BY_TYPE)


def test_creating_an_account_at_the_cap_is_refused(restore_caps, admin_client, db):
    """At the ceiling, the next create is a 409 naming the numbers -- not a 500."""
    _seed_rows(db, AccountType.USERS.value, 3)
    settings.MAX_USER_COUNT = 3

    response = admin_client.post(
        "/admin/users", json=_payload("over-the-line", AccountType.USERS.value)
    )

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    # The operator has to be able to act on this, so the ceiling and the current count are in
    # the message. "Limit reached" alone does not say whether anybody is left or it is simply
    # misconfigured.
    assert "3" in detail
    assert str(settings.MAX_USER_COUNT) in detail


def test_creating_the_last_slot_below_the_cap_succeeds(restore_caps, admin_client, db):
    """The boundary has to be 'at' the cap, not 'over' it -- or the last slot is unusable."""
    _seed_rows(db, AccountType.USERS.value, 3)
    settings.MAX_USER_COUNT = 4

    response = admin_client.post(
        "/admin/users", json=_payload("last-slot", AccountType.USERS.value)
    )

    assert response.status_code == 201, response.text
    assert _count(db, AccountType.USERS.value) == 4

    # And now that the slot is taken, it is refused.
    again = admin_client.post(
        "/admin/users", json=_payload("one-too-many", AccountType.USERS.value)
    )
    assert again.status_code == 409


def test_a_blocked_account_still_counts_towards_the_cap(restore_caps, admin_client, db):
    """The cap is on rows, not on accounts currently allowed to sign in.

    This is the requirement most easily implemented the other way round, and it is worth pinning
    explicitly: a blocked account must occupy a slot, so blocking somebody cannot be used to make
    room for a new one.
    """
    # Fill the table to the cap using rows that are all blocked. If blocked rows were excluded
    # from the count this table would look empty and the next create would wrongly succeed.
    _seed_rows(db, AccountType.USERS.value, 2, blocked=True)
    settings.MAX_USER_COUNT = 2

    with db.sessions() as session:
        blocked_total = session.scalar(
            select(func.count())
            .select_from(UserRecord)
            .where(UserRecord.is_blocked.is_(True))
        )
    assert blocked_total == 2

    response = admin_client.post(
        "/admin/users", json=_payload("blocked-are-full", AccountType.USERS.value)
    )

    assert response.status_code == 409, response.text


def test_blocking_an_account_does_not_free_a_slot(restore_caps, admin_client, db):
    """The end-to-end version of the rule: block somebody, then try to take their place."""
    created = admin_client.post(
        "/admin/users", json=_payload("occupier", AccountType.USERS.value)
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]

    settings.MAX_USER_COUNT = 1
    assert admin_client.put(f"/admin/users/{user_id}/block?blocked=true").status_code == 200

    response = admin_client.post(
        "/admin/users", json=_payload("newcomer", AccountType.USERS.value)
    )
    assert response.status_code == 409


@pytest.mark.parametrize(
    "account_type", [AccountType.MODERATOR.value, AccountType.ADMIN.value]
)
def test_each_tables_cap_is_independent_of_the_others(
    restore_caps, admin_client, db, account_type
):
    """One table reaching its ceiling must not affect the other two.

    Otherwise raising the participant ceiling for a new intake would also mint staff, which is
    the opposite of what an operator changing that one number intends.
    """
    _seed_rows(db, AccountType.USERS.value, 2)
    settings.MAX_USER_COUNT = 0
    settings.MAX_ADMIN_COUNT = 10
    settings.MAX_MODERATOR_COUNT = 10

    response = admin_client.post(
        "/admin/users", json=_payload(f"still-ok-{account_type}", account_type)
    )
    assert response.status_code == 201, response.text


def test_a_zero_cap_refuses_all_creates_of_that_kind(restore_caps, admin_client, db):
    """Zero means "this kind cannot be created", not "unset" and not "unlimited".

    A deployment that provisions staff out of band sets its staff ceilings to 0 so the app
    cannot mint one by accident. Reading 0 as falsy and skipping the check would quietly undo
    that, so this is the one case where an ``if cap:`` instead of ``if count >= cap:`` matters.
    """
    settings.MAX_MODERATOR_COUNT = 0

    response = admin_client.post(
        "/admin/users", json=_payload("no-mods", AccountType.MODERATOR.value)
    )
    assert response.status_code == 409, response.text


def test_the_cap_refusal_does_not_write_a_row(restore_caps, admin_client, db):
    """A refused create must leave the table exactly as it was."""
    _seed_rows(db, AccountType.USERS.value, 2)
    settings.MAX_USER_COUNT = 2
    before = _count(db, AccountType.USERS.value)

    admin_client.post("/admin/users", json=_payload("ghost", AccountType.USERS.value))

    assert _count(db, AccountType.USERS.value) == before
    with db.sessions() as session:
        assert session.scalar(
            select(UserRecord).where(UserRecord.regno == "ghost")
        ) is None


# ---------------------------------------------------- an admin cannot be blocked


def test_the_database_refuses_to_block_an_admin(restore_caps, db):
    """The enforcement is a CHECK constraint, so raw SQL cannot get around it.

    This is the test that justifies the design. It never touches the application: it is
    ``UPDATE admin SET is_blocked = true``, which is exactly what an operator at a psql prompt, a
    migration or a future endpoint would do. If it were refused only by a route, this would pass
    nothing -- and the route would be the sole thing standing between the last admin and an
    unrecoverable deployment.
    """
    _seed_rows(db, AccountType.ADMIN.value, 1)
    with db.sessions() as session:
        admin_id = session.scalar(select(AdminRecord.id))

    with pytest.raises(DBAPIError) as caught:
        with db.engine.begin() as connection:
            connection.execute(
                text("UPDATE admin SET is_blocked = true WHERE id = :id"),
                {"id": admin_id},
            )
    assert "ck_admin_not_blockable" in str(caught.value)

    with db.sessions() as session:
        assert session.get(AdminRecord, admin_id).is_blocked is False


def test_the_database_refuses_to_create_an_already_blocked_admin(restore_caps, db):
    """Blocking at creation is the other way in, so it is refused too."""
    with pytest.raises(DBAPIError) as caught:
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO admin (id, regno, name, email, is_blocked) "
                    "VALUES (gen_random_uuid(), 'born-blocked', 'Born', "
                    "'born-blocked@example.com', true)"
                )
            )
    assert "ck_admin_not_blockable" in str(caught.value)


def test_the_constraint_is_declared_by_the_model_and_installed_in_the_database(db):
    """Both halves, because they can fail independently.

    The raw-SQL tests above would keep passing with this constraint deleted from the model,
    because ``apply_check_constraints`` only ever *adds* a missing constraint -- it never drops
    one that has disappeared from the models. On a database that already has it, the rule would
    therefore survive its removal from the code, and every behavioural test would go on passing
    while a freshly created database silently came up without it. Asserting the declaration
    directly is what stops that: this is the test that fails when someone deletes the rule
    instead of changing it.
    """
    declared = {c.name: c for c in AdminRecord.__table__.constraints}
    assert "ck_admin_not_blockable" in declared, (
        "the admin table no longer declares the constraint, so a database created from these "
        "models would have no rule preventing an admin from being blocked"
    )

    # And the installed constraint has to be the same rule, not a same-named leftover.
    with db.engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'admin'::regclass AND conname = 'ck_admin_not_blockable'"
            )
        ).fetchone()
    assert row is not None, "the constraint is declared but was never installed"
    assert "is_blocked" in row[0]


def test_a_moderator_can_still_be_blocked(restore_caps, db):
    """The rule is admin-only.

    Without this, "admins cannot be blocked" could be satisfied by a constraint on ``moderator``
    instead, or by one that stopped both tables being blockable at all. A moderator is a staff
    account an admin is entitled to suspend, and the existing block route says so.
    """
    _seed_rows(db, AccountType.MODERATOR.value, 1)
    with db.sessions() as session:
        moderator_id = session.scalar(select(ModeratorRecord.id))

    with db.engine.begin() as connection:
        connection.execute(
            text("UPDATE moderator SET is_blocked = true WHERE id = :id"),
            {"id": moderator_id},
        )

    with db.sessions() as session:
        assert session.get(ModeratorRecord, moderator_id).is_blocked is True


def test_unblocking_an_admin_is_permitted(restore_caps, db):
    """The constraint permits any value that is not true, so a repair stays possible.

    Worth pinning because "not true" and "false" are different rules. If a deployment somehow
    ended up with an admin flagged blocked -- an older release, a hand-edited row -- the
    constraint must not make that row permanently unwritable. Being unable to un-block an admin
    would be a worse failure than the one the constraint exists to prevent.
    """
    _seed_rows(db, AccountType.ADMIN.value, 1)
    with db.sessions() as session:
        admin_id = session.scalar(select(AdminRecord.id))

    with db.engine.begin() as connection:
        connection.execute(
            text("UPDATE admin SET is_blocked = false WHERE id = :id"), {"id": admin_id}
        )

    with db.sessions() as session:
        assert session.get(AdminRecord, admin_id).is_blocked is False


def test_a_null_is_blocked_admin_is_refused_by_the_not_null_column(restore_caps, db):
    """``is_blocked`` is NOT NULL in the database, so the "any value but true" reading is moot.

    The models declare the column nullable but the deployed schema tightened it, and this pins
    the fact that actually matters for the constraint: there is no third state to reason about,
    so ``ck_admin_not_blockable`` reduces to "false" on every row that can exist. ``IS NOT
    TRUE`` is still the spelling used, because it stays correct if that column is ever relaxed,
    but nothing should be written on the assumption that a NULL can be stored.
    """
    with pytest.raises(DBAPIError) as caught:
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO admin (id, regno, name, email, is_blocked) "
                    "VALUES (gen_random_uuid(), 'null-blocked', 'Null', "
                    "'null-blocked@example.com', NULL)"
                )
            )
    assert "is_blocked" in str(caught.value)


def test_the_block_route_refuses_an_admin_with_a_readable_error(
    restore_caps, admin_client, db
):
    """The API answers 400 and says why, instead of surfacing a constraint as a 500.

    The database is the enforcement either way; this only decides what the caller is told. A 500
    here would be indistinguishable, to whoever had to debug it, from the application breaking.
    """
    created = admin_client.post("/admin/users", json=_payload("an-admin", AccountType.ADMIN.value))
    assert created.status_code == 201, created.text
    admin_id = created.json()["id"]

    response = admin_client.put(f"/admin/users/{admin_id}/block?blocked=true")

    assert response.status_code == 400, response.text
    assert "admin" in response.json()["detail"].lower()

    with db.sessions() as session:
        assert session.get(AdminRecord, admin_id).is_blocked is False


def test_the_edit_route_refuses_to_block_an_admin_either(restore_caps, admin_client, db):
    """``is_blocked`` is no longer a field on the update payload at all.

    This used to be a second door to the same column, answered with a 400 from the router's
    own guard. That guard is gone because the field is: with ``extra="forbid"`` the request is
    refused by Pydantic, by name, before the route runs -- which is a stronger position, since
    it no longer depends on every write path remembering to check.
    """
    created = admin_client.post("/admin/users", json=_payload("edited-admin", AccountType.ADMIN.value))
    assert created.status_code == 201, created.text
    admin_id = created.json()["id"]

    response = admin_client.put(f"/admin/users/{admin_id}", json={"is_blocked": True})

    assert response.status_code == 422, response.text
    assert "is_blocked" in response.text
    with db.sessions() as session:
        assert session.get(AdminRecord, admin_id).is_blocked is False


def test_creating_an_admin_with_is_blocked_is_refused(restore_caps, admin_client, db):
    """Same on the way in: ``is_blocked`` is not a creatable field, so it cannot be asked for."""
    response = admin_client.post(
        "/admin/users",
        json=_payload("blocked-admin", AccountType.ADMIN.value, is_blocked=True),
    )

    assert response.status_code == 422, response.text
    assert "is_blocked" in response.text
    # Nothing was written. A refusal that left a row behind would mean the constraint had been
    # relied on to reject the insert and merely reported afterwards.
    with db.sessions() as session:
        assert session.scalar(
            select(AdminRecord).where(AdminRecord.regno == "blocked-admin")
        ) is None


# ----------------------------------------------------------- editing an admin's profile


@contextlib.contextmanager
def acting_as_admin(account_id: str, *, regno: str = "acting-admin"):
    """Name one specific admin row as the caller for the block.

    ``admin_client`` resolves to a fresh ``AccountDetails`` whose id is generated per call, so
    it is never equal to any stored row's -- which is exactly the "somebody else" case. The
    self-edit case needs a caller whose id *is* the row's, so the dependency override is
    replaced for the block and put back afterwards.
    """
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: AccountDetails(
        id=account_id,
        regno=regno,
        name="Acting Admin",
        email=f"{regno}@example.com",
        account_type=AccountType.ADMIN.value,
    )
    try:
        yield
    finally:
        if previous is not None:
            app.dependency_overrides[get_current_user] = previous
        else:
            app.dependency_overrides.pop(get_current_user, None)


def test_an_admin_edits_only_their_own_admin_profile(admin_client, db):
    """Another admin's row answers 403 and does not move; the caller's own row is editable.

    Two admins, one caller: the distinction is the target's identity, not the capability --
    the caller holds ``users:update`` either way. The refused request is checked against the
    *row* and not only the response, because a 403 that had already written the new name
    would otherwise pass.
    """
    first = admin_client.post(
        "/admin/users", json=_payload("first-admin", AccountType.ADMIN.value)
    )
    second = admin_client.post(
        "/admin/users", json=_payload("second-admin", AccountType.ADMIN.value)
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    first_id = first.json()["id"]
    second_id = second.json()["id"]

    with acting_as_admin(second_id, regno="second-admin"):
        own = admin_client.put(f"/admin/users/{second_id}", json={"name": "Second Renamed"})
        another = admin_client.put(f"/admin/users/{first_id}", json={"name": "Taken Over"})

    assert own.status_code == 200, own.text
    assert own.json()["name"] == "Second Renamed"
    assert another.status_code == 403, another.text
    assert "own profile" in another.json()["detail"]

    with db.sessions() as session:
        assert session.get(AdminRecord, second_id).name == "Second Renamed"
        assert session.get(AdminRecord, first_id).name == "Person first-admin"


def test_editing_moderators_and_participants_is_untouched(admin_client, db):
    """The restriction is about the *target*, so the two other account kinds still edit."""
    moderator = admin_client.post(
        "/admin/users", json=_payload("still-mod", AccountType.MODERATOR.value)
    )
    participant = admin_client.post(
        "/admin/users", json=_payload("still-user", AccountType.USERS.value)
    )
    assert moderator.status_code == 201, moderator.text
    assert participant.status_code == 201, participant.text

    renamed_mod = admin_client.put(
        f"/admin/users/{moderator.json()['id']}", json={"name": "Mod Renamed"}
    )
    renamed_user = admin_client.put(
        f"/admin/users/{participant.json()['id']}",
        json={"name": "User Renamed", "year": 3},
    )

    assert renamed_mod.status_code == 200, renamed_mod.text
    assert renamed_mod.json()["name"] == "Mod Renamed"
    assert renamed_user.status_code == 200, renamed_user.text
    assert renamed_user.json()["name"] == "User Renamed"
