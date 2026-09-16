"""Tests for sign-in being scoped to one account table.

A regno is unique only *within* an account table, so the same registration number can exist
as a participant and as staff at the same time. These tests pin the three things that make
that a supported state rather than a coin flip:

* each sign-in page reads only its own table, so a duplicate regno resolves to the account
  the person came in for;
* the session that follows is pinned to that exact row by primary key, so a shared regno
  cannot move a live session from one account to another between two requests;
* an account created in one table may reuse a regno held in another, because the ambiguity
  the old cross-table check existed to prevent no longer exists.

The end-to-end tests drive the real HTTP endpoints, because the category lives in the URL the
browser calls. A service-level test could not catch a route that searched the wrong table.
"""

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.accounts import AccountType
from app.config import settings
from app.db_models import (AdminRecord, LoginTokenRecord, ModeratorRecord,
                           UserRecord, find_account_in_table)
from app.main import app
from app.services.auth_service import create_access_token, get_current_user

# Deliberately the same regno in every table seeded below. That is the state the old design
# had to refuse and this one has to resolve correctly.
SHARED = "shared-reg"

STAFF_ONLY = "staff-only"


class _FromLoopback:
    """Presents every request as arriving from 127.0.0.1.

    A magic-link token is recorded against the address it was minted from, and
    ``login_tokens.request_ip`` is an ``inet`` column, so the default ``testclient`` hostname
    is not storable at all. Starlette's ``TestClient`` sets that address in a private
    transport, so it is set here instead -- as a two-line ASGI wrapper rather than by
    importing a private symbol.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": ("127.0.0.1", 51234)}
        await self.app(scope, receive, send)


@pytest.fixture
def client_with_no_auth_override():
    """A client whose authentication is the real thing, not the conftest override.

    The sign-in endpoints are public, but proving that a category-scoped *session* stays
    bound to its own account means letting a real token through ``get_current_user``, which
    the autouse fixture replaces with a fixed participant.
    """
    saved = app.dependency_overrides.pop(get_current_user, None)
    try:
        with TestClient(_FromLoopback(app)) as c:
            yield c
    finally:
        if saved is not None:
            app.dependency_overrides[get_current_user] = saved


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _seed_shared(db):
    """One regno in all three tables. Returns {account_type: id}."""
    ids = {}
    with db.sessions.begin() as session:
        participant = UserRecord(
            regno=SHARED, name="Participant", email="shared@example.com",
            class_name="Test_Class", gender="other",
        )
        moderator = ModeratorRecord(regno=SHARED, name="Moderator", gender="other")
        admin = AdminRecord(regno=SHARED, name="Admin", gender="other")
        session.add_all([participant, moderator, admin])
        session.flush()
        ids[AccountType.USERS.value] = str(participant.id)
        ids[AccountType.MODERATOR.value] = str(moderator.id)
        ids[AccountType.ADMIN.value] = str(admin.id)
    return ids


def _minted_tokens(db):
    with db.sessions() as session:
        return session.scalars(select(LoginTokenRecord)).all()


def _mint_for(db, account_type, regno=SHARED):
    """Mint a usable magic-link token for one account, as the service does.

    Calls the same function the sign-in endpoint calls. The endpoint's own table lookup is
    covered by the tests above; what is left to arrange here is a token the browser could
    actually have received, which only the caller of this function ever sees in plaintext.
    """
    from app.create_token import create_and_store_magic_token

    with db.sessions() as session:
        found = find_account_in_table(session, account_type, regno)
    assert found is not None, f"no {account_type} account with regno {regno!r}"
    resolved_type, row = found

    class _Request:
        # Matches the address the client fixture presents, because a token is bound to the
        # address it was minted from and `request_ip` is an `inet` column.
        client = type("C", (), {"host": "127.0.0.1"})()
        headers = {"user-agent": "Test Agent"}

    return create_and_store_magic_token(db, str(row.id), resolved_type, _Request())  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# Each page reads only its own table
# ----------------------------------------------------------------------


def test_each_sign_in_page_mints_a_link_for_its_own_account(client_with_no_auth_override, db):
    """The point of the feature: one regno, two accounts, two correct magic links."""
    ids = _seed_shared(db)

    for account_type in (AccountType.USERS.value, AccountType.MODERATOR.value):
        response = client_with_no_auth_override.post(
            f"/auth/login/{account_type}/magic", json={"regno": SHARED}
        )
        assert response.status_code == 200, response.text

    # Each table's row got its own token, and neither token points at the other table.
    minted = {(str(t.account_type), str(t.account_id)) for t in _minted_tokens(db)}
    assert minted == {
        (AccountType.USERS.value, ids[AccountType.USERS.value]),
        (AccountType.MODERATOR.value, ids[AccountType.MODERATOR.value]),
    }


def test_a_page_refuses_a_regno_that_only_exists_in_another_table(client_with_no_auth_override, db):
    """A moderator's regno typed on the participant page is not a participant."""
    with db.sessions.begin() as session:
        session.add(ModeratorRecord(regno=STAFF_ONLY, name="Mod", gender="other"))

    response = client_with_no_auth_override.post(
        f"/auth/login/{AccountType.USERS.value}/magic", json={"regno": STAFF_ONLY}
    )

    assert response.status_code == 404
    # The message names the page the person is on, so they know which door to use. It does
    # not confirm the regno exists anywhere, or in which other table.
    detail = response.json()["detail"]
    assert "participant" in detail
    assert STAFF_ONLY not in detail
    assert "moderator" not in detail


def test_a_refused_page_mints_nothing(client_with_no_auth_override, db):
    with db.sessions.begin() as session:
        session.add(ModeratorRecord(regno=STAFF_ONLY, name="Mod", gender="other"))

    client_with_no_auth_override.post(
        f"/auth/login/{AccountType.USERS.value}/magic", json={"regno": STAFF_ONLY}
    )

    assert _minted_tokens(db) == []


def test_an_unknown_account_table_in_the_path_is_rejected_not_defaulted(client_with_no_auth_override, db):
    """`/auth/login/wizard/magic` must not be answered out of the participant table.

    The account table is a path parameter typed as the ``AccountType`` enum, so an
    unrecognised one is refused by the router before the service is reached -- a 422 that
    lists the valid tables, rather than a 404 that would be indistinguishable from a wrong
    URL. What matters either way is that no table is substituted and no token is minted.
    """
    _seed_shared(db)

    response = client_with_no_auth_override.post("/auth/login/wizard/magic", json={"regno": SHARED})

    assert response.status_code == 422
    assert _minted_tokens(db) == []


def test_find_account_in_table_refuses_an_undeclared_table(db):
    """Strict here, where ``normalize_account_type`` is deliberately permissive.

    Reading a *stored* discriminator with an unrecognised value should fall back to the least
    privileged table. Reading a *request* that named no real table must not: that would let a
    malformed caller have its regno resolved out of ``users`` by accident.
    """
    _seed_shared(db)

    with db.sessions() as session:
        assert find_account_in_table(session, "wizard", SHARED) is None
        assert find_account_in_table(session, AccountType.USERS.value, SHARED) is not None


# ----------------------------------------------------------------------
# The session is pinned to the row, not to the regno
# ----------------------------------------------------------------------


@pytest.mark.parametrize("account_type", [AccountType.USERS.value, AccountType.MODERATOR.value])
def test_a_redeemed_link_becomes_a_session_for_that_exact_account(
    client_with_no_auth_override, db, account_type
):
    """Redeem through the real endpoint and ask /auth/me who we are."""
    ids = _seed_shared(db)
    plaintext = _mint_for(db, account_type)

    verified = client_with_no_auth_override.get(
        f"/auth/login/magic/verify?token={plaintext}"
    )
    assert verified.status_code == 200, verified.text
    access_token = verified.json()["access_token"]

    claims = jwt.decode(
        access_token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
    )
    assert claims["account_id"] == ids[account_type]
    assert claims["account_type"] == account_type
    # Kept alongside for logs and for anything that wants the human identity.
    assert claims["sub"] == SHARED

    me = client_with_no_auth_override.get(
        "/auth/me", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert me.status_code == 200, me.text
    assert me.json()["id"] == ids[account_type]
    assert me.json()["account_type"] == account_type


def test_each_page_sends_its_account_to_its_own_landing_path(
    client_with_no_auth_override, db
):
    """A moderator's link must not offer the participant dashboard, or the admin console."""
    _seed_shared(db)

    destinations = {}
    for account_type in (AccountType.USERS.value, AccountType.MODERATOR.value, AccountType.ADMIN.value):
        verified = client_with_no_auth_override.get(
            f"/auth/login/magic/verify?token={_mint_for(db, account_type)}"
        )
        assert verified.status_code == 200, verified.text
        destinations[account_type] = verified.json()["redirect_url"]

    assert destinations[AccountType.USERS.value] == "/dashboard"
    # Staff are separated from each other, not only from participants. A moderator is sent to
    # the moderation hub, which lists the queue actions without the platform-wide figures the
    # admin console puts on screen -- so signing in never presents an account with numbers it
    # has no power to act on. An admin holds `stats:read` and lands on the console.
    assert destinations[AccountType.MODERATOR.value] == "/moderation"
    assert destinations[AccountType.ADMIN.value] == "/admin"


def test_a_token_with_only_a_regno_is_refused_rather_than_resolved(client_with_no_auth_override, db):
    """A token minted before the table was recorded must not be resolved by searching.

    Falling back to a regno search is the exact ambiguity this design removes. Refusing means
    those sessions end and their users sign in again, which is the acceptable cost of not
    guessing.
    """
    _seed_shared(db)
    legacy = create_access_token({"sub": SHARED})

    response = client_with_no_auth_override.get(
        "/auth/me", headers={"Authorization": f"Bearer {legacy}"}
    )

    assert response.status_code == 401


def test_a_token_naming_the_wrong_table_for_its_id_resolves_to_nothing(
    client_with_no_auth_override, db
):
    """A moderator's id presented as a participant must not fall back to the moderator."""
    ids = _seed_shared(db)
    forged = create_access_token(
        {
            "sub": SHARED,
            "account_id": ids[AccountType.MODERATOR.value],
            "account_type": AccountType.USERS.value,
        }
    )

    response = client_with_no_auth_override.get(
        "/auth/me", headers={"Authorization": f"Bearer {forged}"}
    )

    assert response.status_code == 401


# ----------------------------------------------------------------------
# A regno may now be reused across tables
# ----------------------------------------------------------------------


def test_the_create_endpoint_allows_a_regno_reused_from_another_table(admin_client, db):
    """The old cross-table 409 existed only because login used to search all three tables."""
    with db.sessions.begin() as session:
        session.add(UserRecord(
            regno=SHARED, name="Participant", email="shared@example.com",
            class_name="Test_Class", gender="other",
        ))

    response = admin_client.post(
        "/admin/users",
        json={
            "account_type": AccountType.MODERATOR.value,
            "regno": SHARED,
            "name": "Moderator",
            "gender": "other",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["account_type"] == AccountType.MODERATOR.value
    with db.sessions() as session:
        assert find_account_in_table(session, AccountType.MODERATOR.value, SHARED) is not None


def test_the_create_endpoint_still_refuses_a_duplicate_in_the_same_table(admin_client, db):
    _seed_shared(db)

    response = admin_client.post(
        "/admin/users",
        json={
            "account_type": AccountType.MODERATOR.value,
            "regno": SHARED,
            "name": "Second Moderator",
            "gender": "other",
        },
    )

    assert response.status_code == 409


def test_postgresql_still_refuses_a_duplicate_regno_within_one_table(db):
    """Where the real per-table guarantee lives, independent of any application check."""
    _seed_shared(db)

    with db.sessions.begin() as session:
        with pytest.raises(IntegrityError):
            session.add(ModeratorRecord(regno=SHARED, name="Clash", gender="other"))
            session.flush()


# ----------------------------------------------------------------------
# The active-session list reads accounts across all three tables
# ----------------------------------------------------------------------


def _consumed_token(db, account_id, account_type, *, regno_suffix=""):
    """A consumed, unexpired login token, as redemption leaves it."""
    from app.create_token import hash_token

    with db.sessions.begin() as session:
        now = datetime.now(timezone.utc)
        token = LoginTokenRecord(
            token_hash=hash_token(f"used-{account_type}-{regno_suffix}"),
            account_id=account_id,
            account_type=account_type,
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            used=True,
            revoked=False,
            consumed_at=now,
            session_status="active",
            consume_ip="127.0.0.1",
            request_ip="127.0.0.1",
            consume_user_agent="pytest",
            request_user_agent="pytest",
            device="pytest",
        )
        session.add(token)
        session.flush()
        return str(token.id)


def test_the_session_list_reads_an_account_out_of_each_table(admin_client, db):
    """Regression: this list is per-token and cross-table, and it must not blow up.

    It resolves each token's account with the table recorded on that token, so it exercises
    all three tables in one call. A participant-only version of this query would pass a test
    that only seeded participants, and a wrong arity on the resolver -- which is what
    actually happened -- is invisible to every test that never reaches this code.
    """
    ids = _seed_shared(db)
    with db.sessions.begin() as session:
        admin = AdminRecord(regno="staff-reg", name="Admin", gender="other")
        session.add(admin)
        session.flush()
        ids[AccountType.ADMIN.value] = str(admin.id)

    for account_type in (AccountType.USERS.value, AccountType.MODERATOR.value, AccountType.ADMIN.value):
        _consumed_token(db, ids[account_type], account_type, regno_suffix=account_type)

    response = admin_client.get("/admin/stats/active-sessions")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] >= 3
    # A staff session showing up here is the point: a ``users``-only query would return the
    # participant and quietly drop the other two.
    users = [entry["user"] for entry in body["sessions"]]
    assert {u["regno"] for u in users} >= {SHARED, "staff-reg"}
    assert {u["account_type"] for u in users} >= {
        AccountType.USERS.value,
        AccountType.MODERATOR.value,
        AccountType.ADMIN.value,
    }
