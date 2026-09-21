"""One profile-picture change per participant every three days.

``users.last_profile_picture_at`` is the whole state, claimed by a conditional UPDATE the
same way ``test_confession_cooldown.py`` claims ``last_confession_at`` -- see
``profile_service``. What makes this rule different from the others is the *ordering*
requirement it carries: an upload that is refused must not consume the participant's one
change, because the refusals a person meets first are the ordinary ones (the file was the
wrong type, the window was already running) and a rule that charged for them would lock an
account out for three days over a typo.

So the tests below pin both halves: the claim, and the fact that everything which can fail
*before* the claim leaves the column exactly as it was.
"""
import asyncio
import base64
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import select, text

from app.accounts import AccountType
from app.db_models import UserRecord
from app.models import UserDetails
from app.postgres import get_postgres_client
from app.services.profile_service import (
    PROFILE_PICTURE_COOLDOWN_DAYS,
    upload_profile_picture_service,
)
from app.services.storage_service import storage_service

# A real 1x1 PNG, so these tests run through the real validator rather than around it. The
# geometry check reads the header, so anything that only *looks* like a PNG would be caught
# here rather than in production.
ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

WINDOW = timedelta(days=PROFILE_PICTURE_COOLDOWN_DAYS)


@pytest.fixture
def db():
    return get_postgres_client()


@pytest.fixture(autouse=True)
def live_storage(monkeypatch):
    """Stand in for Cloudinary: record what was stored and what was destroyed.

    Recording is what makes "the refused upload was cleaned up" and "the pre-check never
    uploaded at all" observable. The real validator is left alone on purpose -- it runs on
    bytes in memory and has nothing to do with the network -- so a rejected file is refused
    by the same code the API refuses it with.
    """
    uploads: list[str] = []
    destroyed: list[str] = []

    def _upload(*, content, owner_regno):
        public_id = (
            f"profile_pics/{owner_regno}/Stored{len(uploads) + 1:04d}IdUnique/profile"
        )
        uploads.append(public_id)
        return {"public_id": public_id, "format": "png"}

    def _delete(public_id, **kwargs):
        destroyed.append(public_id)
        return True

    monkeypatch.setattr(storage_service, "upload_profile_image", _upload, raising=True)
    monkeypatch.setattr(storage_service, "delete_profile_asset", _delete, raising=True)
    monkeypatch.setattr(
        storage_service, "profile_delivery_url", lambda *a, **k: "https://cdn.example/x.png",
        raising=True,
    )
    return SimpleNamespace(uploads=uploads, destroyed=destroyed)


def make_user(db, regno: str = "alice") -> str:
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno, name=f"User {regno}", email=f"{regno.lower()}@test.com",
            year=2, department="Test_Class", gender="other",
        )
        session.add(row)
        session.flush()
        return str(row.id)


def as_user(regno: str = "alice") -> UserDetails:
    return UserDetails(
        regno=regno, name="Test User", email=f"{regno.lower()}@test.com",
        year=2, department="Test_Class", gender="other",
        account_type=AccountType.USERS.value,
    )


def upload_file(content: bytes = ONE_PIXEL_PNG, filename: str = "me.png") -> UploadFile:
    return UploadFile(filename=filename, file=io.BytesIO(content))


async def change_picture(db, *, regno: str = "alice", content: bytes = ONE_PIXEL_PNG):
    return await upload_profile_picture_service(as_user(regno), db, upload_file(content))


def set_last_change_at(db, regno: str, when: datetime) -> None:
    with db.sessions.begin() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
        row.last_profile_picture_at = when


def last_change_at(db, regno: str):
    with db.sessions() as session:
        return session.scalar(
            select(UserRecord.last_profile_picture_at).where(UserRecord.regno == regno)
        )


def stored_picture(db, regno: str) -> str | None:
    with db.sessions() as session:
        return session.scalar(
            select(UserRecord.profile_picture_public_id).where(UserRecord.regno == regno)
        )


# --------------------------------------------------------------------------- the claim itself


@pytest.mark.asyncio
async def test_the_first_change_is_accepted(db):
    make_user(db)

    result = await change_picture(db)

    assert result["message"] == "Profile picture uploaded successfully"
    assert last_change_at(db, "alice") is not None
    assert stored_picture(db, "alice").endswith("/profile")


@pytest.mark.asyncio
async def test_a_second_change_inside_the_window_is_refused(db, live_storage):
    make_user(db)
    await change_picture(db)
    first = stored_picture(db, "alice")

    with pytest.raises(HTTPException) as excinfo:
        await change_picture(db)

    assert excinfo.value.status_code == 429, excinfo.value.detail
    assert stored_picture(db, "alice") == first, "the refused change must not be stored"
    assert live_storage.destroyed == [], (
        "the picture the row still names must not be retired by a refused attempt"
    )


@pytest.mark.asyncio
async def test_the_refusal_says_the_rule_and_when_to_try_again(db):
    """A bare 429 leaves the participant with neither the rule nor a wait they can act on."""
    make_user(db)
    await change_picture(db)

    with pytest.raises(HTTPException) as excinfo:
        await change_picture(db)

    detail = excinfo.value.detail
    assert f"once every {PROFILE_PICTURE_COOLDOWN_DAYS} days" in detail
    assert "2 days and 23 hours" in detail  # ~71h59m left
    retry_after = int(excinfo.value.headers["Retry-After"])
    assert 259100 < retry_after <= 259200


@pytest.mark.asyncio
async def test_the_window_is_three_days_not_merely_some_cooldown(db):
    """Pin the number, so nobody quietly shortens it and the suite still passes."""
    make_user(db)
    now = datetime.now(timezone.utc)

    set_last_change_at(db, "alice", now - WINDOW + timedelta(minutes=1))
    with pytest.raises(HTTPException) as excinfo:
        await change_picture(db)
    assert excinfo.value.status_code == 429

    set_last_change_at(db, "alice", now - WINDOW - timedelta(minutes=1))
    assert (await change_picture(db))["message"] == "Profile picture uploaded successfully"


@pytest.mark.asyncio
async def test_a_refused_attempt_does_not_extend_the_window(db):
    """The window ends three days after the accepted change, not after the refusal."""
    make_user(db)
    await change_picture(db)
    accepted_at = last_change_at(db, "alice")

    with pytest.raises(HTTPException):
        await change_picture(db)
    with pytest.raises(HTTPException):
        await change_picture(db)

    assert last_change_at(db, "alice") == accepted_at


@pytest.mark.asyncio
async def test_a_stale_caller_cannot_smuggle_a_second_change(db):
    """The caller's own model can still say "never changed".

    The claim is a fresh conditional UPDATE against the committed row, so a participant
    whose token was minted before the change is refused all the same.
    """
    make_user(db)
    await change_picture(db)

    stale = as_user("alice")
    assert stale.last_profile_picture_at is None

    with pytest.raises(HTTPException) as excinfo:
        await upload_profile_picture_service(stale, db, upload_file())
    assert excinfo.value.status_code == 429


@pytest.mark.asyncio
async def test_the_cooldown_is_per_participant(db):
    """One account throttled is not everybody throttled."""
    make_user(db, "alice")
    make_user(db, "bob")

    await change_picture(db, regno="alice")

    assert (await change_picture(db, regno="bob"))["message"] == (
        "Profile picture uploaded successfully"
    )


@pytest.mark.asyncio
async def test_a_naive_stored_timestamp_is_not_a_server_error(db):
    """TIMESTAMPTZ can still arrive naive from a script or a hand-edited fixture.

    Cooldown arithmetic across a naive and an aware datetime raises, and turning that into
    a 500 would make the cooldown itself the outage.
    """
    make_user(db)
    set_last_change_at(db, "alice", datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=6))

    with pytest.raises(HTTPException) as excinfo:
        await change_picture(db)
    assert excinfo.value.status_code == 429


# ------------------------------------------------- a refused attempt must cost the participant


@pytest.mark.asyncio
async def test_a_file_the_validator_rejects_does_not_consume_the_change(db, live_storage):
    """The rule this whole feature has to get right.

    Validation runs inside the upload, which runs before the claim, so a rejected file
    leaves the column untouched: the participant's one change is still theirs afterwards,
    and a typo cannot cost them three days.
    """
    make_user(db)
    uploads_before = len(live_storage.uploads)

    with pytest.raises(HTTPException) as excinfo:
        await change_picture(db, content=b"this is not an image")

    assert excinfo.value.status_code == 400, excinfo.value.detail
    assert len(live_storage.uploads) == uploads_before, (
        "nothing should have been stored for a file that failed validation"
    )
    assert last_change_at(db, "alice") is None, "the rejected file claimed the window"

    # The change itself is still available, and it is the successful one that starts the
    # window rather than the rejection that preceded it.
    await change_picture(db)
    accepted_at = last_change_at(db, "alice")
    assert accepted_at is not None

    # Inside the window the file is still checked first: the body rules run before the
    # window is even consulted, so a bad file is reported as a bad file rather than as a
    # wait, and either way nothing moves.
    with pytest.raises(HTTPException) as excinfo:
        await change_picture(db, content=b"this is not an image")
    assert excinfo.value.status_code == 400, excinfo.value.detail
    assert last_change_at(db, "alice") == accepted_at


@pytest.mark.asyncio
async def test_the_window_is_refused_before_anything_is_uploaded(db, live_storage):
    """The common case is a participant clicking again a minute later.

    Refusing before the upload keeps that from costing a store-and-delete round trip per
    click, and it means the refusal cannot itself be the thing that consumed the window.
    """
    make_user(db)
    await change_picture(db)
    uploads_before = len(live_storage.uploads)

    with pytest.raises(HTTPException) as excinfo:
        await change_picture(db)

    assert excinfo.value.status_code == 429
    assert len(live_storage.uploads) == uploads_before, (
        "a participant inside the window must not reach Cloudinary at all"
    )


@pytest.mark.asyncio
async def test_the_losing_side_of_a_race_cleans_up_its_own_upload(db, live_storage, monkeypatch):
    """The pre-check cannot see a rival that starts at the same moment.

    Both callers pass it, both store an object, and only one conditional UPDATE wins -- so
    the loser's object has to be deleted, because no row will ever name it.

    Two threads and a barrier inside the store, rather than ``asyncio.gather``: nothing
    between the pre-check and the store awaits, so on one event loop the second caller
    would not start until the first had already committed and would be refused by the
    pre-check instead of by the claim -- which is the path this test is not about.
    """
    make_user(db)

    barrier = threading.Barrier(2, timeout=30)
    real_store = storage_service.upload_profile_image

    def store_together(*, content, owner_regno):
        # Neither caller can commit until both have stored, so both are past the pre-check
        # and both reach the claim. That is the race, made to happen rather than waited for.
        barrier.wait()
        return real_store(content=content, owner_regno=owner_regno)

    monkeypatch.setattr(
        storage_service, "upload_profile_image", store_together, raising=True
    )

    outcomes = []

    def attempt():
        try:
            asyncio.run(change_picture(db))
            outcomes.append("accepted")
        except HTTPException as exc:
            outcomes.append(f"rejected:{exc.status_code}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(attempt) for _ in range(2)]:
            future.result(timeout=60)

    assert sorted(outcomes) == ["accepted", "rejected:429"], outcomes

    # Both objects were stored and only one is named by the row; the other was discarded.
    winner = stored_picture(db, "alice")
    assert len(live_storage.uploads) == 2
    assert live_storage.destroyed == [
        public_id for public_id in live_storage.uploads if public_id != winner
    ]


@pytest.mark.asyncio
async def test_concurrent_changes_produce_exactly_one_new_picture(db, live_storage):
    """The core regression: N simultaneous uploads must yield exactly one change."""
    make_user(db)
    workers = 6
    barrier = threading.Barrier(workers)
    results = []
    lock = threading.Lock()

    def attempt(index):
        barrier.wait(timeout=30)
        try:
            asyncio.run(change_picture(db))
            outcome = "accepted"
        except HTTPException as exc:
            outcome = f"rejected:{exc.status_code}"
        except Exception as exc:  # pragma: no cover - a pool timeout would be a suite problem
            outcome = f"error:{type(exc).__name__}"
        with lock:
            results.append(outcome)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(attempt, i) for i in range(workers)]:
            future.result(timeout=60)

    assert results.count("accepted") == 1, results
    assert set(results) <= {"accepted", "rejected:429"}, results
    assert last_change_at(db, "alice") is not None
    # Every object that did not win was cleaned up.
    assert len(live_storage.destroyed) == len(live_storage.uploads) - 1


# ------------------------------------------------------------------- over HTTP, as the UI sees it


@pytest.fixture
def picture_owner_client(db):
    """A client whose participant resolves to a row that actually exists.

    The shared ``client`` fixture resolves ``get_current_user`` to a fresh ``UserDetails``
    on every call with a new uuid, so the row this route writes would have nothing to
    claim against. Named id, real row -- the same reason ``_confession_author_client``
    exists.
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.auth_service import get_current_user
    from conftest import override_get_current_user

    owner_id = "44444444-4444-4444-4444-444444444444"
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, regno, name, email, year, department, gender) "
                "VALUES (:id, 'pictureuser', 'Picture User', 'picture@user.com', 2, 'Class', 'other') "
                "ON CONFLICT (regno) DO NOTHING"
            ),
            {"id": owner_id},
        )
    owner = UserDetails(
        id=owner_id, regno="pictureuser", name="Picture User", email="picture@user.com",
        year=2, department="Class", gender="other",
        account_type=AccountType.USERS.value,
    )
    app.dependency_overrides[get_current_user] = lambda: owner
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides[get_current_user] = override_get_current_user


def post_picture(client, content: bytes = ONE_PIXEL_PNG):
    return client.post(
        "/profile/upload-picture",
        files={"file": ("me.png", content, "image/png")},
    )


def test_posting_twice_over_http_refuses_the_second(picture_owner_client):
    """The path the browser actually takes, including the Retry-After the notice uses."""
    client = picture_owner_client

    first = post_picture(client)
    assert first.status_code == 200, first.text

    second = post_picture(client)

    assert second.status_code == 429, second.text
    assert f"once every {PROFILE_PICTURE_COOLDOWN_DAYS} days" in second.json()["detail"]
    assert 259100 < int(second.headers["retry-after"]) <= 259200


def test_a_rejected_file_over_http_does_not_consume_the_change(picture_owner_client):
    """Validation runs before the claim, so a refused file costs the participant nothing."""
    client = picture_owner_client

    rejected = client.post(
        "/profile/upload-picture",
        files={"file": ("me.txt", b"definitely not an image", "text/plain")},
    )
    assert rejected.status_code == 400, rejected.text

    # Eligible still, because nothing claimed the window.
    assert post_picture(client).status_code == 200
    assert post_picture(client).status_code == 429
