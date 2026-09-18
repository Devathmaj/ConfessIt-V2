"""One confession per participant every two hours, enforced on the database.

``users.last_confession_at`` is the whole state. The rule is claimed with a conditional
UPDATE, the same mechanism ``test_matchmaking_race.py`` pins for the matchmaking attempt,
because reading that column and writing it in a later transaction leaves a window in which
two simultaneous posts both observe "eligible" and both land.

The advisory ``GET /confessions/cooldown`` is deliberately not the gate. It exists so the
compose screen can show the wait before anything is typed; the write re-derives the answer
itself, so the tests below check both paths agree without treating either as an
authorization check.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.db_models import ConfessionRecord, UserRecord
from app.models import CONFESSION_BODY_MAX, ConfessionCreate
from app.postgres import get_postgres_client
from app.services import confession_service
from app.services.confession_service import CONFESSION_COOLDOWN_HOURS, ConfessionService


VALID_BODY = {"body": "a confession", "anonymous": True, "comments_enabled": True}


@pytest.fixture
def db():
    return get_postgres_client()


@pytest.fixture
def service(db):
    return ConfessionService(db)


def make_user(db, regno: str) -> str:
    """A participant whose row exists, since the cooldown claims against ``users.id``."""
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno, name=f"User {regno}", email=f"{regno.lower()}@test.com",
            year=2, department="Test_Class", gender="other",
        )
        session.add(row)
        session.flush()
        return str(row.id)


def post(service, user_id: str, body: str = "a confession"):
    return service.create_confession(
        ConfessionCreate(body=body, anonymous=True, comments_enabled=True), user_id
    )


def set_last_confession_at(db, user_id: str, when: datetime) -> None:
    with db.sessions.begin() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.id == user_id))
        row.last_confession_at = when


def last_confession_at(db, user_id: str):
    with db.sessions() as session:
        return session.scalar(
            select(UserRecord.last_confession_at).where(UserRecord.id == user_id)
        )


def count_confessions(db, user_id: str) -> int:
    with db.sessions() as session:
        return session.scalar(
            select(func.count()).select_from(ConfessionRecord).where(
                ConfessionRecord.author_id == user_id
            )
        ) or 0


def test_the_first_confession_is_accepted(db, service):
    user_id = make_user(db, "alice")

    created = post(service, user_id)

    assert created.body == "a confession"
    assert count_confessions(db, user_id) == 1
    assert last_confession_at(db, user_id) is not None


def test_a_second_confession_inside_the_window_is_refused(db, service):
    user_id = make_user(db, "alice")
    post(service, user_id)

    with pytest.raises(HTTPException) as excinfo:
        post(service, user_id, "a second confession")

    assert excinfo.value.status_code == 429, excinfo.value.detail
    assert count_confessions(db, user_id) == 1, "the refused post must not be stored"


def test_the_refusal_says_when_to_try_again_and_by_when(db, service):
    """A bare 429 leaves the user with nothing to act on."""
    user_id = make_user(db, "alice")
    post(service, user_id)

    with pytest.raises(HTTPException) as excinfo:
        post(service, user_id)

    detail = excinfo.value.detail
    assert f"every {CONFESSION_COOLDOWN_HOURS} hours" in detail
    assert "1 hour" in detail and "5" in detail  # ~1h59m left
    retry_after = int(excinfo.value.headers["Retry-After"])
    assert 7100 < retry_after <= 7200


def test_the_window_is_two_hours_not_merely_some_cooldown(db, service):
    """Pin the number, so nobody quietly shortens it to 60s and the suite still passes."""
    user_id = make_user(db, "alice")

    set_last_confession_at(db, user_id, datetime.now(timezone.utc) - timedelta(hours=1, minutes=59))
    with pytest.raises(HTTPException) as excinfo:
        post(service, user_id)
    assert excinfo.value.status_code == 429

    set_last_confession_at(db, user_id, datetime.now(timezone.utc) - timedelta(hours=2, minutes=1))
    assert post(service, user_id).body == "a confession"
    assert count_confessions(db, user_id) == 1


def test_a_stale_caller_snapshot_cannot_smuggle_a_second_post(db, service):
    """The token snapshot says the user has never posted.

    A handler that trusted its own ``UserDetails`` would see ``last_confession_at`` as None
    and let this through. The claim is a fresh conditional UPDATE, so the committed row wins.
    """
    user_id = make_user(db, "alice")
    post(service, user_id)

    assert confession_service._seconds_until_confession_eligible(
        None, user_id, datetime.now(timezone.utc)
    ) > 0

    with pytest.raises(HTTPException) as excinfo:
        post(service, user_id)
    assert excinfo.value.status_code == 429


def test_concurrent_posts_create_exactly_one_confession(db, service):
    """The core regression: N simultaneous posts must yield exactly one confession."""
    user_id = make_user(db, "alice")

    workers = 8
    barrier = threading.Barrier(workers)
    results = []
    lock = threading.Lock()

    def attempt(index):
        barrier.wait(timeout=30)  # maximise the overlap window
        try:
            post(service, user_id, f"confession {index}")
            outcome = "accepted"
        except HTTPException as exc:
            outcome = f"rejected:{exc.status_code}"
        with lock:
            results.append(outcome)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(attempt, i) for i in range(workers)]:
            future.result(timeout=60)

    assert results.count("accepted") == 1, results
    assert set(results) <= {"accepted", "rejected:429"}, results
    assert count_confessions(db, user_id) == 1


def test_a_rejected_post_does_not_consume_the_window(db, service):
    """A refusal has to be free, or an error message would lock a user out for two hours."""
    user_id = make_user(db, "alice")
    post(service, user_id)

    with pytest.raises(HTTPException):
        post(service, user_id)

    assert count_confessions(db, user_id) == 1
    # The window still ends two hours after the accepted post, not two hours after the
    # attempt that was refused.
    set_last_confession_at(db, user_id, datetime.now(timezone.utc) - timedelta(hours=2, minutes=1))
    assert post(service, user_id).body == "a confession"


def test_the_advisory_check_agrees_with_the_write(db, service):
    user_id = make_user(db, "alice")

    assert service.get_confession_cooldown(user_id)["status"] == "eligible"

    post(service, user_id)

    after = service.get_confession_cooldown(user_id)
    assert after["status"] == "cooldown"
    assert after["cooldown_hours"] == CONFESSION_COOLDOWN_HOURS
    assert 7100 < after["remaining_seconds"] <= 7200
    assert f"every {CONFESSION_COOLDOWN_HOURS} hours" in after["message"]


def test_the_advisory_check_is_read_from_the_database(db, service):
    """It answers for a user the caller has never asked about before."""
    user_id = make_user(db, "alice")
    set_last_confession_at(db, user_id, datetime.now(timezone.utc) - timedelta(minutes=30))

    after = service.get_confession_cooldown(user_id)

    assert after["status"] == "cooldown"
    # 2h - 30m, allowing for the milliseconds between writing the column and reading it back
    # and for the answer being truncated to whole seconds.
    assert 5395 <= after["remaining_seconds"] <= 5400


def test_a_naive_stored_timestamp_is_not_a_server_error(db, service):
    """TIMESTAMPTZ can still arrive naive from a script or a fixture.

    Cooldown arithmetic across a naive and an aware datetime raises, and turning that into a
    500 would make the cooldown itself the outage.
    """
    user_id = make_user(db, "alice")
    set_last_confession_at(db, user_id, datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10))

    assert service.get_confession_cooldown(user_id)["status"] == "cooldown"
    with pytest.raises(HTTPException) as excinfo:
        post(service, user_id)
    assert excinfo.value.status_code == 429


def test_the_cooldown_is_per_participant(db, service):
    """One account throttled is not the feed throttled."""
    alice = make_user(db, "alice")
    bob = make_user(db, "bob")

    post(service, alice)

    assert post(service, bob).body == "a confession"
    assert count_confessions(db, bob) == 1


# ----------------------------------------------------------------- over HTTP, as the UI sees it


def test_the_cooldown_endpoint_is_participant_only(admin_client):
    """It reports on ``users.last_confession_at``, which a staff row does not have."""
    assert admin_client.get("/confessions/cooldown").status_code == 403


def test_the_cooldown_endpoint_reports_the_wait_over_http(_confession_author_client, db):
    client = _confession_author_client

    assert client.get("/confessions/cooldown").json()["status"] == "eligible"

    assert client.post("/confessions", json=VALID_BODY).status_code == 200

    after = client.get("/confessions/cooldown").json()
    assert after["status"] == "cooldown"
    assert 7100 < after["remaining_seconds"] <= 7200


def test_posting_twice_over_http_refuses_the_second(_confession_author_client, db):
    """The path the browser actually takes, including the Retry-After the notice uses."""
    client = _confession_author_client

    assert client.post("/confessions", json=VALID_BODY).status_code == 200
    response = client.post("/confessions", json={**VALID_BODY, "body": "a second one"})

    assert response.status_code == 429, response.text
    assert "every 2 hours" in response.json()["detail"]
    assert 7100 < int(response.headers["retry-after"]) <= 7200

    author_id = "33333333-3333-3333-3333-333333333333"
    assert count_confessions(db, author_id) == 1


def test_a_rejected_body_over_http_does_not_consume_the_window(_confession_author_client, db):
    """Validation runs before the service, so a refused post costs the user nothing.

    If the claim ran first and the body check second, an over-length post would lock someone
    out for two hours for a typo.
    """
    client = _confession_author_client

    too_long = client.post(
        "/confessions", json={**VALID_BODY, "body": "x" * (CONFESSION_BODY_MAX + 1)}
    )
    assert too_long.status_code == 422, too_long.text

    assert client.post("/confessions", json=VALID_BODY).status_code == 200