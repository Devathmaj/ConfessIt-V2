"""Tests for profile-asset ownership on the delete path.

A profile picture is stored as its Cloudinary *identity* -- ``profile_picture_public_id`` plus
``profile_picture_format`` -- and handed to the browser as an ordinary public URL derived from
that identity at read time. Nothing proxies or authorises the read any more, because nothing
serves the read any more; the object is public by design (see ``account_picture_service``).

That makes these tests the only thing standing between one account and another's avatar on the
*destroy* path: an account must not be able to wipe an object belonging to somebody else, even
when the row it is authenticated against names that object.

The stored identity is treated as untrusted throughout. It is not only ever written by the
owner: an admin creating or editing a user can set these columns to any string
(``routers/admin.py``), and a public id is just text.
"""

import io
import logging
from unittest.mock import Mock

import pytest
from fastapi import HTTPException, UploadFile

from app.services import account_picture_service, profile_service
from app.services.storage_service import storage_service

OWNER = "22CS045"
OTHER = "22CS099"

OWNED_ID = f"profile_pics/{OWNER}/AbCdEfGhIjKlMnOpQrStUv/profile"
FOREIGN_ID = f"profile_pics/{OTHER}/ZyXwVuTsRqPoNmLkJiHgFe/d/profile"


# ---------------------------------------------------------------------------
# Ownership of the destroy
# ---------------------------------------------------------------------------


@pytest.fixture
def destroyed(monkeypatch):
    """Record every Cloudinary destroy call instead of performing it."""
    calls = []

    def _destroy(public_id, resource_type="image", type="upload", invalidate=False, **kwargs):
        calls.append((public_id, type, invalidate))
        return {"result": "ok"}

    monkeypatch.setattr(
        "app.services.storage_service.cloudinary_destroy", _destroy, raising=True
    )
    return calls


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    """Force the delete path live so ``enabled`` never short-circuits the test."""
    monkeypatch.setattr(storage_service, "enabled", True, raising=False)
    return storage_service


def test_own_asset_is_deleted(destroyed):
    assert storage_service.delete_profile_asset(OWNED_ID, owner_regno=OWNER) is True
    assert destroyed == [(OWNED_ID, "upload", True)]


def test_foreign_asset_is_never_destroyed(destroyed):
    """The core rule: knowing the id is not permission to delete it."""
    assert storage_service.delete_profile_asset(FOREIGN_ID, owner_regno=OWNER) is False
    assert destroyed == []


def test_missing_owner_cannot_delete_anything(destroyed):
    """Without a known owner there is nothing to check against, so refuse."""
    assert storage_service.delete_profile_asset(OWNED_ID, owner_regno="") is False
    assert destroyed == []


def test_owner_match_uses_a_whole_segment_not_a_prefix(destroyed):
    """``22CS0455`` must not be treated as owning ``22CS045``'s folder."""
    assert storage_service.delete_profile_asset(OWNED_ID, owner_regno=OWNER + "5") is False
    assert destroyed == []


def test_owner_segments_are_compared_case_sensitively(destroyed):
    """``22cs045`` must not be treated as owning ``22CS045``'s folder.

    Folding case here would be the vulnerability, not the convenience: regnos are the account
    identifier in this application, so if two regnos differing only in case can both exist,
    case-folding would let either one destroy the other's avatar. The folder is sanitised on the
    way in without changing case, so an exact comparison is both the safe rule and the one
    that matches what was written.
    """
    assert storage_service.delete_profile_asset(OWNED_ID, owner_regno="22cs045") is False
    assert destroyed == []


def test_a_url_is_not_an_identity_and_is_refused(destroyed):
    """A stored *address* must not be torn apart and acted on.

    The column holds a public_id, so a value that looks like a URL can only be stale data from
    before the move or a hand-edited row. Accepting one would mean re-introducing the parser --
    and with it the host-spoofing question -- for a value that should never appear. Refusing it
    is the behaviour that keeps the rule simple: an id either sits in this account's folder or
    it is not deleted.
    """
    url = storage_service.profile_delivery_url(OWNED_ID, "png")
    assert url.startswith("https://res.cloudinary.com/")

    assert storage_service.delete_profile_asset(url, owner_regno=OWNER) is False
    assert destroyed == []


def test_a_full_url_on_a_foreign_host_does_not_grant_deletion(destroyed):
    """A lookalike host must not help an attacker, and must not crash anything either."""
    spoofed = f"https://evil.example.invalid/{FOREIGN_ID}"

    assert storage_service.delete_profile_asset(spoofed, owner_regno=OWNER) is False
    assert destroyed == []


def test_love_note_object_is_not_deletable_through_the_profile_path(destroyed):
    """A profile delete must not reach an authenticated love note image.

    The two layouts live in the same account, so a bug in the segment check would let an
    ordinary avatar replacement delete a private note. The check is on the layout, and this
    is the test for it.
    """
    note_id = "love_notes/22222222-2222-2222-2222-222222333333/image"
    assert storage_service.delete_profile_asset(note_id, owner_regno=OWNER) is False
    assert destroyed == []


def test_refusal_is_logged_for_audit(caplog):
    # app.logger.get_logger() sets propagate=False so a module's records are not
    # re-delivered by the root logger, which also means caplog's root handler never
    # sees them. Attach the capture handler to the module logger itself so the audit
    # assertion still observes the real record.
    target = logging.getLogger("app.services.storage_service")
    target.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="app.services.storage_service"):
            storage_service.delete_profile_asset(FOREIGN_ID, owner_regno=OWNER)
    finally:
        target.removeHandler(caplog.handler)

    refusals = [r for r in caplog.records if "Refused profile asset delete" in r.getMessage()]
    assert len(refusals) == 1
    message = refusals[0].getMessage()
    # The audit line has to name both sides, or it is not actionable after the fact.
    assert OWNER in message
    assert OTHER in message


def test_an_upstream_delete_failure_never_reaches_the_caller(monkeypatch, caplog):
    """The raw Cloudinary error embeds the public_id, so it must not reach the client.

    The profile path swallows the failure entirely and reports ``False``: the replacement is
    already committed, so an orphaned object is a cleanup problem and a 500 would answer a
    successful upload with an error. Nothing about the upstream message may escape, which is a
    stronger guarantee than sanitising it.
    """

    def _destroy(public_id, **kwargs):
        raise RuntimeError("cloudinary 404 not found for public_id prof-secret-token")

    monkeypatch.setattr(
        "app.services.storage_service.cloudinary_destroy", _destroy, raising=True
    )
    target = logging.getLogger("app.services.storage_service")
    target.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.ERROR, logger="app.services.storage_service"):
            assert storage_service.delete_profile_asset(OWNED_ID, owner_regno=OWNER) is False
    finally:
        target.removeHandler(caplog.handler)

    # The detail is logged for operators, which is where it belongs, and is not returned.
    assert any("prof-secret-token" in r.getMessage() for r in caplog.records)


def test_love_note_delete_failure_raises_but_still_does_not_leak(monkeypatch):
    """The love note path does raise, because the row must not be deleted behind a live object.

    It raises the *fixed* message, not Cloudinary's: the SDK's error text names the object, and
    "the caller wrapped it" is a rule a future caller forgets. The real cause is in the log.
    """

    def _destroy(public_id, **kwargs):
        raise RuntimeError("cloudinary 500 for public_id note-secret-token")

    monkeypatch.setattr(
        "app.services.storage_service.cloudinary_destroy", _destroy, raising=True
    )

    note_id = "love_notes/22222222-2222-2222-2222-222222333333/image"
    with pytest.raises(HTTPException) as excinfo:
        storage_service.delete_love_note_asset(note_id)

    assert excinfo.value.status_code == 500
    assert "note-secret-token" not in str(excinfo.value.detail)
    assert excinfo.value.detail == "Failed to delete file"


def test_love_note_delete_refuses_a_foreign_layout(monkeypatch):
    """A corrupt stored id must not turn a note deletion into an unrelated destroy."""
    calls = []
    monkeypatch.setattr(
        "app.services.storage_service.cloudinary_destroy",
        lambda public_id, **k: calls.append(public_id) or {"result": "ok"},
        raising=True,
    )

    assert storage_service.delete_love_note_asset(OWNED_ID) is False
    assert storage_service.delete_love_note_asset("love_notes/../profile_pics/x/profile") is False
    assert calls == []


def test_owner_segment_helper():
    assert storage_service.profile_owner_segment(OWNED_ID) == OWNER
    assert storage_service.profile_owner_segment("") is None
    assert storage_service.profile_owner_segment(None) is None
    # A bare name is not a four-segment profile identity, so it cannot be authorised. The
    # layout check is stricter than "read the second segment", which is what keeps
    # ``love_notes/x/image`` from being read as an owner called ``x``.
    assert storage_service.profile_owner_segment("profile") is None
    assert storage_service.profile_owner_segment("love_notes/x/image") is None
    assert storage_service.profile_owner_segment(f"profile_pics/{OWNER}/x/notprofile") is None


# ---------------------------------------------------------------------------
# Ordering: the previous object must survive a failed replacement.
# ---------------------------------------------------------------------------


def _upload_stub(monkeypatch, *, fail: bool):
    def _upload(*, file, content, owner_regno):
        if fail:
            raise HTTPException(status_code=500, detail="Failed to upload profile image: upstream down")
        return {
            "public_id": f"profile_pics/{owner_regno}/NewOneTwoThreeFourFiveSix/profile",
            "format": "png",
        }

    monkeypatch.setattr(storage_service, "upload_profile_image", _upload, raising=True)
    monkeypatch.setattr(storage_service, "profile_delivery_url", lambda *a, **k: "https://cdn/x.png", raising=True)


def _user_details(regno):
    return Mock(regno=regno, account_type="users")


def _fake_db(previous_public_id=None, *, last_profile_picture_at=None, claim_wins=True):
    """A database whose read paths answer the two questions this service now asks.

    The cooldown is why there are two sessions here rather than one: the wait is read
    *before* anything is uploaded (a session of its own, standing in for the committed row)
    and the window is claimed *after* it (the transactional one, whose conditional UPDATE
    answers ``claim_wins``). Both default to "eligible", which is the state every ordering
    test below is written in.
    """
    db = Mock()

    advisory_session = Mock()
    advisory_session.scalar.return_value = last_profile_picture_at
    advisory_context = Mock()
    advisory_context.__enter__ = Mock(return_value=advisory_session)
    advisory_context.__exit__ = Mock(return_value=False)
    db.sessions.return_value = advisory_context

    session = Mock()
    session.scalar.return_value = Mock(
        profile_picture_public_id=previous_public_id,
        last_profile_picture_at=last_profile_picture_at,
    )
    session.execute.return_value = Mock(rowcount=1 if claim_wins else 0)
    ctx = Mock()
    ctx.__enter__ = Mock(return_value=session)
    ctx.__exit__ = Mock(return_value=False)
    db.sessions.begin.return_value = ctx
    return db


def _upload_file():
    return UploadFile(filename="me.png", file=io.BytesIO(b"not-a-real-png"))


def _patch_validation(monkeypatch):
    monkeypatch.setattr(
        account_picture_service.storage_service,
        "validate_image_file",
        lambda *a, **k: "png",
        raising=True,
    )


async def _run_upload(monkeypatch, *, fail: bool, db, current):
    _upload_stub(monkeypatch, fail=fail)
    _patch_validation(monkeypatch)
    return await profile_service.upload_profile_picture_service(current, db, _upload_file())


@pytest.mark.asyncio
async def test_failed_upload_leaves_the_previous_picture_intact(monkeypatch, destroyed):
    """Regression: the old asset used to be deleted before the new one was confirmed.

    A failed upload therefore left the account pointing at an object that had already
    been destroyed, and no retry could recover it.
    """
    with pytest.raises(HTTPException):
        await _run_upload(
            monkeypatch, fail=True, db=_fake_db(OWNED_ID), current=_user_details(OWNER)
        )

    assert destroyed == [], "previous asset was destroyed even though the upload failed"


@pytest.mark.asyncio
async def test_successful_upload_deletes_the_previous_asset(monkeypatch, destroyed):
    db = _fake_db(OWNED_ID)

    result = await _run_upload(monkeypatch, fail=False, db=db, current=_user_details(OWNER))

    assert result["message"] == "Profile picture uploaded successfully"
    assert result["profile_picture_url"] == "https://cdn/x.png"
    assert destroyed == [(OWNED_ID, "upload", True)]


@pytest.mark.asyncio
async def test_the_new_identity_is_what_gets_persisted(monkeypatch, destroyed):
    """The row must be handed the object identity, never the URL that was shown to the browser.

    Storing the URL would put an address in the database that has to be parsed back into an
    identity before it can be deleted or re-derived -- and the address contains a version and a
    format that Cloudinary may change underneath us.
    """
    db = _fake_db(OWNED_ID)

    await _run_upload(monkeypatch, fail=False, db=db, current=_user_details(OWNER))

    written = db.sessions.begin.return_value.__enter__.return_value.scalar.return_value
    assert written.profile_picture_public_id == f"profile_pics/{OWNER}/NewOneTwoThreeFourFiveSix/profile"
    assert written.profile_picture_format == "png"


@pytest.mark.asyncio
async def test_first_upload_with_no_previous_object_destroys_nothing(monkeypatch, destroyed):
    """An account that has never had a picture must not reach the delete path at all."""
    await _run_upload(monkeypatch, fail=False, db=_fake_db(None), current=_user_details(OWNER))
    assert destroyed == []


@pytest.mark.asyncio
async def test_replaced_foreign_asset_is_left_alone(monkeypatch, destroyed):
    """Even the normal replacement path must not touch another account's object.

    This is the scenario where an admin has pointed this account's columns at somebody
    else's picture: the victim uploading a new avatar must not delete it.
    """
    await _run_upload(
        monkeypatch, fail=False, db=_fake_db(FOREIGN_ID), current=_user_details(OWNER)
    )
    assert destroyed == []


@pytest.mark.asyncio
async def test_delete_failure_does_not_fail_the_upload(monkeypatch):
    """The new picture is already saved, so a cleanup error must not surface as a 500."""

    def _boom(*args, **kwargs):
        raise HTTPException(status_code=500, detail="Failed to delete file")

    monkeypatch.setattr(
        account_picture_service.storage_service, "delete_profile_asset", _boom, raising=True
    )

    result = await _run_upload(
        monkeypatch, fail=False, db=_fake_db(OWNED_ID), current=_user_details(OWNER)
    )
    assert result["message"] == "Profile picture uploaded successfully"


@pytest.mark.asyncio
async def test_the_old_object_survives_a_failed_database_write(monkeypatch, destroyed):
    """The ordering rule, tested at the step that can actually break it.

    The upload helper used to delete the previous object itself, on the strength of a comment
    claiming the replacement had already been stored. It had not: the caller persists the new
    identity *after* the helper returns. So a database failure between the two left the row
    still naming the old object, which the helper had already destroyed -- a broken image with
    no retry able to recover it, because the bytes were gone.

    Making the database write fail is the only way to observe the difference. Both previous tests
    pass either way, which is why this one exists.
    """

    class _FailingSession:
        """Stands in for a commit that fails after the upload has already succeeded."""

        def scalar(self, *args, **kwargs):
            return Mock(profile_picture_public_id=OWNED_ID)

        def execute(self, *args, **kwargs):
            # The cooldown claim is a conditional UPDATE, and it has to succeed here or the
            # service would refuse with a 429 instead of reaching the write that fails.
            return Mock(rowcount=1)

    class _FailingContext:
        def __enter__(self):
            return _FailingSession()

        def __exit__(self, exc_type, exc, tb):
            if exc_type is not None:
                return False
            raise RuntimeError("commit failed")

    db = _fake_db(OWNED_ID)
    db.sessions.begin.return_value = _FailingContext()

    _upload_stub(monkeypatch, fail=False)
    _patch_validation(monkeypatch)

    with pytest.raises(RuntimeError):
        await profile_service.upload_profile_picture_service(_user_details(OWNER), db, _upload_file())

    assert destroyed == [], (
        "the previous asset was destroyed even though the write that should have replaced it "
        "never landed"
    )


@pytest.mark.asyncio
async def test_the_old_object_goes_only_after_the_write(monkeypatch):
    """Not just "deleted eventually" -- deleted after the stored value changed."""
    order = []

    _upload_stub(monkeypatch, fail=False)
    _patch_validation(monkeypatch)
    monkeypatch.setattr(
        account_picture_service.storage_service,
        "delete_profile_asset",
        lambda *a, **k: order.append("delete"),
        raising=True,
    )

    db = _fake_db(OWNED_ID)
    # The write is recorded where it actually happens -- the assignment of the new identity --
    # rather than by wrapping the context manager, which is fiddly to do on a Mock and would be
    # testing the wrapper rather than the ordering.
    session = db.sessions.begin.return_value.__enter__.return_value
    original_scalar = session.scalar

    def _scalar(*args, **kwargs):
        row = original_scalar(*args, **kwargs)
        order.append("write")
        return row

    session.scalar = _scalar

    await profile_service.upload_profile_picture_service(_user_details(OWNER), db, _upload_file())

    assert order == ["write", "delete"]


def test_retiring_the_same_object_twice_is_a_no_op(monkeypatch):
    """Re-uploading an identical object must not delete the picture the row still points at."""
    calls = []
    monkeypatch.setattr(
        account_picture_service.storage_service,
        "delete_profile_asset",
        lambda public_id, **k: calls.append(public_id),
        raising=True,
    )

    account_picture_service.retire_previous_picture(
        previous_public_id=OWNED_ID,
        new_public_id=OWNED_ID,
        owner_regno=OWNER,
        actor_regno=OWNER,
    )

    assert calls == []
