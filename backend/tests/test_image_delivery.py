"""Tests for direct image delivery from Cloudinary.

Images are no longer served by this application. A profile picture is an ordinary public
Cloudinary object handed to the browser as a plain CDN URL; a love note image is an
``authenticated`` object handed out behind a signed delivery URL. There is no proxy route, no
HMAC of our own, and no path parser -- the address is built from a stored *identity* and a
fixed layout.

That moves the security question. It used to be "does this request's token match this path, and
does the database say this account may have it?", both answered on every request by this
application. It is now:

* does the stored identity have the layout the code expects, so an object cannot be named or
  destroyed by a value that was never ours;
* is a URL signed *before* it is handed out, and only for a viewer entitled to that note;
* does the delivery URL fail closed when the signature cannot be produced, rather than quietly
  becoming a public one.

Each of those has a test below.

**What this design gives up, stated plainly.** A signed URL does not expire. Expiry comes from
Cloudinary's authentication tokens, which need an account-wide Auth token key unavailable on
this plan, and a token ACL is not a stronger form of a signature -- it makes Cloudinary reject
the signature. So an issued love note URL is a durable bearer capability, and the property that
an already-issued URL cannot be withdrawn is *not* bounded by anything here. What remains is
that it cannot be forged without the API secret, cannot be lifted onto another note, is never
stored, and is minted only after the entitlement check. ``test_the_issued_url_does_not_expire``
pins that limitation so it stays a known, deliberate cost rather than a surprise.
"""

import re
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from app.config import settings, validate_cloudinary_settings
from app.main import app
from app.services import love_note_service
from app.services.storage_service import storage_service

NOTE_ID = "22222222-2222-2222-2222-222222333333"
OTHER_NOTE_ID = "33333333-3333-3333-3333-333333444444"
OWNER = "22CS045"


def _signature(url: str) -> str:
    """The ``s--<sig>--`` path segment, which is what authorises a private delivery."""
    match = re.search(r"/s--([A-Za-z0-9_-]+)--/", url)
    assert match, f"no signed-URL signature in {url!r}"
    return match.group(1)


# ---------------------------------------------------------------------------
# No proxy. Not a weakened one: none.
# ---------------------------------------------------------------------------


def test_the_application_serves_no_image_route():
    """The strongest statement available: there is no code path that returns image bytes.

    Every ``/files/`` route was a place where a missing entitlement check would have leaked
    somebody else's picture, so they are gone rather than locked. A new one added by copying a
    neighbour is exactly what this catches.
    """
    served = [
        getattr(route, "path", "")
        for route in app.routes
        if "/files/" in getattr(route, "path", "")
    ]
    assert served == []


@pytest.mark.parametrize(
    "path",
    [
        "/api/profile/files/anything/at/all.jpg",
        "/api/love-notes/files/anything/at/all.png",
        "/api/profile/files/love_notes/x/image?t=whatever",
    ],
)
def test_the_old_proxy_paths_are_gone(client, path):
    """A 404, not a 403. The route is not registered at all.

    Answering a request for an image with "not found" rather than "forbidden" is the honest
    result: there is nothing here to authorise, so there is nothing to refuse.
    """
    assert client.get(path).status_code == 404


# ---------------------------------------------------------------------------
# Identities and their layout
# ---------------------------------------------------------------------------


def test_profile_identity_is_pinned_to_the_layout():
    """Four segments, fixed names, the owner in the middle.

    The layout is what ``profile_owner_segment`` later re-derives the owner from, so it is not
    cosmetic: an id whose shape moved would make the delete-time ownership check fail closed,
    which is safe but leaves objects nobody can remove.
    """
    asset_id = storage_service.new_asset_id()
    public_id = storage_service.profile_public_id(OWNER, asset_id)

    assert public_id == f"profile_pics/{OWNER}/{asset_id}/profile"
    assert storage_service.profile_owner_segment(public_id) == OWNER


def test_a_regno_cannot_introduce_a_path_segment():
    """A regno is server-owned data but still a human-chosen string, and a public_id is a path.

    Without sanitising, a regno containing a slash would name a different object -- or one in
    somebody else's folder, since the segment after the owner is what the ownership check reads.
    The folder name below is what the sanitiser produces, and the point is that it is a single
    segment containing no slash at all, not that it is pretty.
    """
    public_id = storage_service.profile_public_id("../../events", storage_service.new_asset_id())
    owner = public_id.split("/")[1]

    assert "/" not in owner
    assert owner == "..-..-events"
    assert public_id.count("/") == 3
    # And it is still recognised as that (odd) owner's own folder, so the object remains
    # deletable by the account it belongs to rather than becoming unreachable.
    assert storage_service.profile_owner_segment(public_id) == owner


@pytest.mark.parametrize(
    "value",
    ["", None, "../../etc/passwd", "love_notes/x/image", "profile_pics/only/three"],
)
def test_foreign_or_malformed_identities_are_not_owned_by_anyone(value):
    """Anything that is not exactly a profile identity has no owner, so it cannot be deleted."""
    assert storage_service.profile_owner_segment(value) is None


@pytest.mark.parametrize(
    "value",
    ["", None, "profile_pics/x/y/profile", "love_notes/x/image/extra", "love_notes/x/../profile"],
)
def test_foreign_or_malformed_note_identities_are_not_note_images(value):
    """Symmetric with the profile layout: a note id is only recognised in its own shape.

    The shape check is what stops a hand-edited or corrupt row from turning a note deletion
    into a destroy of some unrelated object. A merely odd-looking but well-formed segment is
    fine -- the value comes from our own authoritative row, not from a request.
    """
    assert storage_service.love_note_id_segment(value) is None


def test_a_note_identity_is_derived_from_its_own_note_id():
    """One object, one note, permanently.

    The note id is minted before the upload and goes into the public_id, so an image cannot be
    shared between two notes, cannot outlive one, and cannot be re-pointed at another. This is
    what replaced a caller-chosen filename on the old upload path.
    """
    assert storage_service.love_note_public_id(NOTE_ID) == f"love_notes/{NOTE_ID}/image"
    assert storage_service.love_note_id_segment(
        storage_service.love_note_public_id(NOTE_ID)
    ) == NOTE_ID


# ---------------------------------------------------------------------------
# Profile delivery: public, plain, no credential
# ---------------------------------------------------------------------------


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(storage_service, "enabled", True, raising=False)
    return storage_service


def test_a_profile_url_is_a_plain_public_cdn_url(enabled):
    """No token, no query string, no signature.

    A profile picture appears in matchmaking lists and chat headers to people who are not the
    owner, so it is public by design -- and the *absence* of any credential here is the property
    worth pinning. It is what stops a future change from quietly reusing the love note signing
    machinery here and attaching a credential nobody asked for.
    """
    url = storage_service.profile_delivery_url(
        f"profile_pics/{OWNER}/AbCdEfGhIjKlMnOpQrStUv/profile", "png"
    )

    assert url.startswith("https://res.cloudinary.com/")
    assert "/image/upload/" in url
    assert "?" not in url
    assert "__cld_token__" not in url
    assert "/s--" not in url
    assert url.endswith(".png")


def test_a_profile_url_is_stable_for_the_same_identity(enabled):
    """No version or timestamp is baked in, so a stored identity always yields the same URL."""
    identity = (f"profile_pics/{OWNER}/AbCdEfGhIjKlMnOpQrStUv/profile", "png")
    assert storage_service.profile_delivery_url(*identity) == storage_service.profile_delivery_url(*identity)


def test_an_account_with_no_picture_has_no_url(enabled):
    """None in, None out -- never an empty string, which the frontend would try to load."""
    assert storage_service.profile_delivery_url(None, None) is None
    assert storage_service.profile_delivery_url("", "") is None


def test_no_transformation_is_requested(enabled):
    """The URL addresses the original upload, nothing else.

    A transformation in a delivery URL is a way of asking the CDN to re-encode the stored
    object, and the stored object is not always something we want re-encoded at every size and
    shape a caller can name. Sizing is the frontend's job, via CSS.
    """
    url = storage_service.profile_delivery_url(
        f"profile_pics/{OWNER}/AbCdEfGhIjKlMnOpQrStUv/profile", "jpg"
    )
    assert "/upload/" in url
    assert not re.search(r"/(w_|h_|c_|ar_|e_)", url)


# ---------------------------------------------------------------------------
# Love note delivery: private, signed, scoped
# ---------------------------------------------------------------------------


def test_a_love_note_url_is_authenticated_and_signed(enabled):
    """Both halves matter, and they are different halves.

    ``/image/authenticated/`` is what makes the CDN refuse the object without a signature at all.
    The ``s--...--`` segment is what makes it serve it. A URL with only the first is a broken
    image; a URL with only the second is a public object wearing a signature as decoration.
    """
    url = storage_service.love_note_delivery_url(
        storage_service.love_note_public_id(NOTE_ID), "png"
    )

    assert "/image/authenticated/" in url
    assert "/s--" in url
    assert url.startswith("https://")


def test_a_love_note_url_carries_no_expiring_token(enabled):
    """The ``__cld_token__`` mechanism must not creep back in.

    An authentication token needs the account's Auth token key, which this plan does not have, so
    a URL carrying one would 401 on every fetch while looking correct. Emitting a signature is
    the only delivery form that actually works here.
    """
    url = storage_service.love_note_delivery_url(
        storage_service.love_note_public_id(NOTE_ID), "png"
    )

    assert "__cld_token__" not in url
    assert "exp=" not in url
    assert "hmac=" not in url


def test_the_signature_is_scoped_to_that_one_note(enabled):
    """Two notes, two signatures.

    The signature covers the URL path, so it cannot be lifted from one note's URL onto another's.
    Without that, a single leaked URL would be a capability for the whole folder.
    """
    first = storage_service.love_note_delivery_url(
        storage_service.love_note_public_id(NOTE_ID), "png"
    )
    second = storage_service.love_note_delivery_url(
        storage_service.love_note_public_id(OTHER_NOTE_ID), "png"
    )

    assert _signature(first) != _signature(second)
    assert NOTE_ID in first and OTHER_NOTE_ID not in first


def test_the_issued_url_does_not_expire(enabled):
    """Pins the accepted cost of signing rather than using tokens.

    There is no expiry parameter, and re-deriving the same identity yields a byte-identical URL.
    This is the limitation that makes entitlement checks on every request load-bearing rather
    than merely good practice: once issued, a URL is good indefinitely. If this test ever starts
    failing because a TTL was reintroduced, that is an *improvement* -- but it has to be a
    deliberate change, because it also means the Auth token key is now required.
    """
    url = storage_service.love_note_delivery_url(
        storage_service.love_note_public_id(NOTE_ID), "png"
    )
    again = storage_service.love_note_delivery_url(
        storage_service.love_note_public_id(NOTE_ID), "png"
    )

    assert url == again
    assert "?" not in url, "an expiry would live in the query string"


# ---------------------------------------------------------------------------
# Failing closed
# ---------------------------------------------------------------------------


def test_an_unsigned_url_is_never_handed_out(monkeypatch):
    """The CDN would refuse it on every fetch, so it must never leave the process.

    Reached by monkeypatching the SDK rather than by unsetting a secret: the signing key is the
    API secret, which ``validate_cloudinary_settings`` already requires, so there is no
    environment in which this function can legitimately be asked for an unsigned URL. That it
    still refuses one is what makes the invariant hold rather than merely being currently true.
    """
    monkeypatch.setattr(storage_service, "enabled", True, raising=False)
    monkeypatch.setattr(storage_service, "_ensure_enabled", lambda: None)
    monkeypatch.setattr(
        "app.services.storage_service.cloudinary_url",
        lambda *a, **k: ("https://res.cloudinary.com/x/image/authenticated/v1/y.png", {}),
        raising=True,
    )

    with pytest.raises(HTTPException) as excinfo:
        storage_service.love_note_delivery_url(storage_service.love_note_public_id(NOTE_ID), "png")

    assert excinfo.value.status_code == 503
    assert "/image/authenticated/" not in str(excinfo.value.detail)


def test_a_token_style_url_is_rejected_even_though_it_looks_credentialed(monkeypatch):
    """A URL is not trusted because it mentions a credential.

    The signature check looks for ``s--...--`` specifically. A ``__cld_token__`` URL from a
    misconfigured or future caller carries no signature, so the CDN would refuse it; accepting it
    on the strength of the token-shaped string would be exactly the confusion that produced this
    design's original bug.
    """
    monkeypatch.setattr(storage_service, "enabled", True, raising=False)
    monkeypatch.setattr(storage_service, "_ensure_enabled", lambda: None)
    monkeypatch.setattr(
        "app.services.storage_service.cloudinary_url",
        lambda *a, **k: (
            "https://res.cloudinary.com/x/image/authenticated/v1/y.png?__cld_token__=exp=1~hmac=z",
            {},
        ),
        raising=True,
    )

    with pytest.raises(HTTPException) as excinfo:
        storage_service.love_note_delivery_url(storage_service.love_note_public_id(NOTE_ID), "png")

    assert excinfo.value.status_code == 503


def test_startup_does_not_require_an_auth_token_key(monkeypatch):
    """Regression guard for the bug that made love notes undeliverable.

    Cloudinary accepted a token ACL on this account, reported it back so the upload-time check
    passed, and then rejected the signature-based URLs actually issued -- every love note image
    401'd. The fix removed the token ACL, and with it the startup requirement for a key that
    does not exist on this plan. If a key is ever required again, the delivery path has to be
    re-verified live first, because a green test suite does not prove the CDN accepts the URL.
    """
    monkeypatch.delattr(settings, "CLOUDINARY_AUTH_TOKEN_KEY", raising=False)
    monkeypatch.delattr(settings, "CLOUDINARY_LOVE_NOTE_URL_TTL_SECONDS", raising=False)

    validate_cloudinary_settings()


def test_startup_refuses_a_deployment_with_no_cloudinary_credentials(monkeypatch):
    """The API secret is load-bearing here, not just for uploads.

    It is what signs every love note delivery URL, so a deployment missing it can store images
    but can never serve a private one. Caught at boot rather than as a 503 in an inbox.
    """
    monkeypatch.setattr(settings, "CLOUDINARY_CLOUD_NAME", None)

    with pytest.raises(RuntimeError) as excinfo:
        validate_cloudinary_settings()

    assert "CLOUDINARY_CLOUD_NAME" in str(excinfo.value)


def test_a_valid_configuration_passes():
    """The check is not vacuous: a good deployment must get through it."""
    validate_cloudinary_settings()


# ---------------------------------------------------------------------------
# Entitlement is decided before a URL exists
# ---------------------------------------------------------------------------


def _note(**overrides):
    values = dict(
        id=NOTE_ID,
        sender_id="11111111-1111-1111-1111-111111111111",
        recipient_id="33333333-3333-3333-3333-333333444444",
        body="hello",
        status="approved",
        image_public_id=f"love_notes/{NOTE_ID}/image",
        image_format="png",
        removed_at=None,
    )
    values.update(overrides)
    return Mock(**values)


def test_an_unentitled_viewer_is_given_no_url():
    """``None``, not a URL without a signature.

    The entitlement check and the signing live in one function precisely so this cannot come
    apart: a caller that forgot the check would otherwise still get a working capability.
    """
    note = _note(status="pending_review")

    assert (
        love_note_service.love_note_image_url(
            note, viewer_id=note.recipient_id, viewer_account_type="users"
        )
        is None
    )


def test_an_entitled_viewer_is_given_a_signed_url():
    url = love_note_service.love_note_image_url(
        _note(), viewer_id="33333333-3333-3333-3333-333333444444", viewer_account_type="users"
    )
    assert "/s--" in url


def test_staff_review_gets_a_signed_url_for_a_removed_note(enabled):
    """The moderator has to be able to see what they are ruling on.

    Removal closes the note to participants, not to the person deciding whether its removal was
    correct -- so this is the one case where the removed check does not end the story.
    """
    url = love_note_service.love_note_image_url(
        _note(status="rejected", removed_at=object()),
        viewer_id="44444444-4444-4444-4444-444444444444",
        viewer_account_type="moderator",
    )
    assert url is not None and "/s--" in url



# ---------------------------------------------------------------------------
# No byte-serving path exists anywhere
# ---------------------------------------------------------------------------
#
# The strongest property of this design is negative: Cloudinary serves the bytes, so there is no
# code here that could serve them instead. A grep is the right instrument for a claim that spans
# the whole package rather than one function, because the failure mode is a *new* module
# somewhere, not a regression in a line that already exists.

_EGRESS_PATTERNS = (
    r"\burlopen\b",
    r"\burllib\.request\b",
    r"\brequests\.(get|post|put)\b",
    r"\bhttpx\.(get|post|Client|AsyncClient)\b",
    r"\baiohttp\b",
    r"\.content\b",  # streamed response bytes
)


@pytest.mark.parametrize(
    "module",
    [
        "app.services.storage_service",
        "app.services.love_note_service",
        "app.services.account_picture_service",
        "app.services.profile_service",
        "app.services.auth_service",
        "app.services.admin_service",
        "app.routers.love_notes",
        "app.routers.admin_love_notes",
        "app.main",
    ],
)
def test_no_module_downloads_cloudinary_bytes(module):
    """Nothing in the delivery path may fetch image bytes over HTTP.

    Asserted against the imported module's own source rather than a hardcoded copy of it, so it
    keeps holding wherever the code moves to.
    """
    import importlib
    import inspect

    source = inspect.getsource(importlib.import_module(module))
    for pattern in _EGRESS_PATTERNS:
        found = re.search(pattern, source)
        assert not found, f"{module} reaches for image bytes via {found.group(0)!r}"


def test_the_cloudinary_sdk_is_only_used_to_upload_build_and_destroy():
    """One abstraction, four operations, no HTTP client of our own.

    ``cloudinary.uploader``/``cloudinary.utils`` are the SDK's own HTTP path and are how an
    upload and a destroy are legitimately performed; what must not exist is a *fetch*. This
    pins the set of SDK entry points the storage layer is allowed to reach for.
    """
    import inspect

    from app.services import storage_service as storage_module

    allowed = {
        "cloudinary_upload",
        "cloudinary_destroy",
        "cloudinary_url",
        "Cloudinary",
    }
    source = inspect.getsource(storage_module)
    used = set(re.findall(r"\b(cloudinary_\w+)\b", source))
    assert used <= allowed, f"storage_service reached for {used - allowed}"


# ---------------------------------------------------------------------------
# Deleting an asset that is already gone
# ---------------------------------------------------------------------------


def test_deleting_a_missing_love_note_image_is_still_a_success(enabled, monkeypatch):
    """A note whose image was removed by hand must still be deletable.

    The row is going away either way. Treating "Cloudinary has never heard of it" as an error
    would leave a note that can never be deleted, and would report a failure for an outcome the
    caller wanted.
    """
    calls = []

    def _destroy(public_id, **kwargs):
        calls.append((public_id, kwargs.get("type"), kwargs.get("invalidate")))
        return {"result": "not found"}

    monkeypatch.setattr(
        "app.services.storage_service.cloudinary_destroy", _destroy, raising=True
    )
    assert storage_service.delete_love_note_asset(f"love_notes/{NOTE_ID}/image") is True
    assert calls == [(f"love_notes/{NOTE_ID}/image", "authenticated", True)]


def test_deleting_a_missing_profile_image_is_still_a_success(enabled, monkeypatch):
    """Same tolerance on the profile side, for the same reason."""
    monkeypatch.setattr(
        "app.services.storage_service.cloudinary_destroy",
        lambda public_id, **kwargs: {"result": "not found"},
        raising=True,
    )
    assert (
        storage_service.delete_profile_asset(
            f"profile_pics/{OWNER}/11111111-1111-1111-1111-111111111111/profile", owner_regno=OWNER
        )
        is True
    )


def test_a_cloudinary_failure_does_not_leak_the_cloud_name_or_the_public_id(enabled, monkeypatch):
    """An unexpected SDK error is logged, not forwarded.

    Cloudinary's error text carries the cloud name and the public_id. Anything that propagates
    ``exc.detail`` to a client would publish both, so the message is fixed here and the real
    cause goes to the log where it is still diagnosable.
    """
    def _boom(public_id, **kwargs):
        raise RuntimeError(
            f"cloudinary: request failed for public_id love_notes/{NOTE_ID}/image "
            f"on cloud demo-account"
        )

    monkeypatch.setattr(
        "app.services.storage_service.cloudinary_destroy", _boom, raising=True
    )
    with pytest.raises(HTTPException) as excinfo:
        storage_service.delete_love_note_asset(f"love_notes/{NOTE_ID}/image")

    assert excinfo.value.status_code == 500
    assert "demo-account" not in str(excinfo.value.detail)
    assert NOTE_ID not in str(excinfo.value.detail)


# ---------------------------------------------------------------------------
# Grouping in the Media Library, without touching delivery
# ---------------------------------------------------------------------------
#
# Cloudinary Collections cannot be driven from code -- there is no REST endpoint and no SDK
# method for them anywhere in Cloudinary's APIs. Tags can, and a dynamic collection with a rule
# on a tag fills itself. What matters here is the negative property: none of this may leak into
# a delivery URL, or into anything the authorisation path looks at.


def _upload_kwargs(monkeypatch, enabled, *, result_overrides=None):
    """Run one upload with Cloudinary stubbed out and return the kwargs it was called with."""
    recorded = {}

    def _upload(file, **kwargs):
        recorded.update(kwargs)
        base = {
            "public_id": kwargs["public_id"],
            "format": "png",
            "type": "authenticated" if kwargs["type"] == "authenticated" else "upload",
        }
        if kwargs.get("access_control"):
            base["access_control"] = kwargs["access_control"]
        base.update(result_overrides or {})
        return base

    monkeypatch.setattr(
        "app.services.storage_service.cloudinary_upload", _upload, raising=True
    )
    return recorded


def test_a_profile_upload_is_tagged_for_grouping(monkeypatch, enabled):
    recorded = _upload_kwargs(monkeypatch, enabled)

    class _Upload:
        filename = "photo.png"
        content_type = "image/png"

    enabled.upload_profile_image(
        file=_Upload(), content=_png_bytes(), owner_regno=OWNER
    )

    assert recorded["tags"] == ["profile_picture"]
    # A tag is metadata. It must not become part of where the object lives or how it is reached.
    assert recorded["public_id"].startswith(f"profile_pics/{OWNER}/")
    assert "profile_picture" not in recorded["public_id"]
    assert "folder" not in recorded, "the legacy folder parameter would rewrite the public_id"


def test_a_love_note_upload_is_tagged_for_grouping(monkeypatch, enabled):
    recorded = _upload_kwargs(monkeypatch, enabled)

    enabled.upload_love_note_image(
        image_base64=_b64(_png_bytes()), love_note_id=NOTE_ID
    )

    assert recorded["tags"] == ["love_note"]
    assert "love_note" not in recorded["public_id"].rsplit("/", 1)[-1]
    assert "folder" not in recorded, "the legacy folder parameter would rewrite the public_id"


def test_the_grouping_tag_never_reaches_a_delivery_url(enabled):
    """The strongest form of 'this is only organisational'.

    A tag that appeared in the URL would be a public, permanent, guessable identifier for every
    note image in the account, which is the exact leakage the private delivery type exists to
    prevent. ``love_notes`` already contains the substring ``love_note``, so the check is on whole
    path segments and on the query string rather than on substrings.
    """
    note_url = storage_service.love_note_delivery_url(
        f"love_notes/{NOTE_ID}/image", "png"
    )
    profile_url = storage_service.profile_delivery_url(
        f"profile_pics/{OWNER}/11111111-1111-1111-1111-111111111111/profile", "png"
    )

    for url in (note_url, profile_url):
        segments = url.split("?", 1)[0].split("/")
        assert "love_note" not in segments, f"the tag became a path segment: {url}"
        assert "profile_picture" not in segments, f"the tag became a path segment: {url}"
        assert "profile_pictures" not in segments
        query = url.partition("?")[2]
        for tag in ("love_note", "profile_picture"):
            assert tag not in query, f"the tag leaked into the query string: {url}"


def test_a_love_note_upload_asks_for_no_access_control_list(monkeypatch, enabled):
    """The token ACL is the bug this delivery path is built to avoid.

    Cloudinary accepted ``access_control=[{"access_type": "token"}]`` on a free account, echoed
    the ACL back in the upload response so the post-upload verification passed, and then refused
    the signature-based URLs the application actually issues. Every love note image was
    undeliverable while the logs said it was protected. The parameter must not come back.
    """
    recorded = _upload_kwargs(monkeypatch, enabled)

    enabled.upload_love_note_image(
        image_base64=_b64(_png_bytes()), love_note_id=NOTE_ID
    )

    assert "access_control" not in recorded


def test_a_love_note_upload_is_still_stored_private(monkeypatch, enabled):
    """No ACL does not mean public.

    ``type="authenticated"`` is what makes the CDN demand a signature, and it is set on the
    upload request itself rather than relying on any account default.
    """
    recorded = _upload_kwargs(monkeypatch, enabled)

    enabled.upload_love_note_image(
        image_base64=_b64(_png_bytes()), love_note_id=NOTE_ID
    )

    assert recorded["type"] == "authenticated"


def test_a_stored_token_acl_is_rejected_rather_than_trusted(monkeypatch, enabled):
    """If the account ever starts applying a token ACL by default, fail loudly.

    Cloudinary silently accepting an upload it cannot later serve is the worst outcome: the note
    appears to have been sent, and the image is broken forever with nothing to explain why. The
    upload is destroyed and the send fails instead, while there is still an operator watching.
    """
    recorded = _upload_kwargs(
        monkeypatch,
        enabled,
        result_overrides={"access_control": [{"access_type": "token"}]},
    )
    destroyed = []
    monkeypatch.setattr(
        "app.services.storage_service.cloudinary_destroy",
        lambda *a, **k: destroyed.append(k) or {},
        raising=True,
    )

    with pytest.raises(HTTPException) as excinfo:
        enabled.upload_love_note_image(
            image_base64=_b64(_png_bytes()), love_note_id=NOTE_ID
        )

    assert excinfo.value.status_code == 503
    assert destroyed, "an undeliverable object was left behind in the account"



def test_an_unusable_tag_is_dropped_instead_of_failing_the_upload():
    """Cloudinary rejects the whole upload on a malformed tag.

    An operator typing ``Profile Pictures`` would therefore break every picture upload in the
    deployment. The characters are stripped instead, and the label degrades to nothing rather
    than taking the image down with it.
    """
    assert storage_service._asset_tags("Profile Pictures") == ["ProfilePictures"]
    assert storage_service._asset_tags("love note!") == ["lovenote"]
    assert storage_service._asset_tags("") == []
    assert storage_service._asset_tags("...///") == []
    assert storage_service._asset_tags(None) == []
    assert storage_service._asset_tags("ok_tag", "ok_tag") == ["ok_tag"]
    assert storage_service._asset_tags("a", "b") == ["a", "b"]


def _png_bytes() -> bytes:
    import io as _io

    from PIL import Image

    buf = _io.BytesIO()
    Image.new("RGB", (48, 24), (200, 40, 60)).save(buf, format="PNG")
    return buf.getvalue()


def _b64(raw: bytes) -> str:
    import base64 as _b64mod

    return "data:image/png;base64," + _b64mod.b64encode(raw).decode("ascii")
