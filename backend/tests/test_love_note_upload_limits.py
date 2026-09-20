"""Tests for the resource limits on love note image uploads.

A love note image arrives as base64 text inside a JSON body, so the whole encoded
string is already resident in memory before any application code runs, and
``base64.b64decode`` then allocates a second buffer of the same order. Nothing bounded
either allocation, so a single request could hand the process tens of megabytes and
then hand PIL a small file that describes an arbitrarily large bitmap.

These tests pin the three bounds that close that off -- encoded length (checked before
decoding), decoded length, and header dimensions -- plus the fact that the stored object
is the original, kept private by a Cloudinary token ACL rather than by resizing it.
"""

import base64
import io
import tracemalloc

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import CheckConstraint

from app.db_models import LoveNoteRecord
from app.limits import LOVE_NOTE_BODY_MAX
from app.services import storage_service as storage_service_module
from app.services.love_note_service import LoveNoteCreate
from app.services.storage_service import storage_service


# ----------------------------------------------------------------------
# Fixtures / helpers
# ----------------------------------------------------------------------


@pytest.fixture
def enabled(monkeypatch):
    """Force the upload path live so ``_ensure_enabled`` never short-circuits a test."""
    monkeypatch.setattr(storage_service, "enabled", True, raising=False)
    return storage_service


@pytest.fixture
def no_decode(monkeypatch):
    """Make any base64 decode an immediate, loud failure.

    Used to prove the encoded-length check runs *before* the allocation it exists to
    prevent, rather than merely rejecting the payload afterwards.
    """

    def _boom(*args, **kwargs):
        raise AssertionError("base64 payload was decoded before the size check rejected it")

    monkeypatch.setattr(storage_service_module.base64, "b64decode", _boom, raising=True)


def png_bytes(width: int, height: int) -> bytes:
    """A solid-colour PNG of the requested size.

    Solid colour compresses to a few KB, so these fixtures stay small on disk while
    still advertising a large pixel grid -- exactly the shape of a pixel bomb.
    """
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (120, 30, 60)).save(buf, format="PNG")
    return buf.getvalue()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def data_uri(data: bytes) -> str:
    return "data:image/png;base64," + b64(data)


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------


def test_small_image_decodes_with_its_format_and_dimensions():
    raw = png_bytes(64, 32)
    decoded, fmt, (width, height) = storage_service.decode_base64_image(b64(raw))

    assert decoded == raw
    assert fmt == "png"
    assert (width, height) == (64, 32)


def test_data_uri_prefix_and_embedded_whitespace_are_accepted():
    """The frontend posts canvas.toDataURL(), so the data URI form is the normal one."""
    raw = png_bytes(16, 16)
    payload = "data:image/png;base64," + "\n".join(b64(raw)[i:i + 76] for i in range(0, len(b64(raw)), 76))

    decoded, fmt, (width, height) = storage_service.decode_base64_image(payload)

    assert decoded == raw
    assert fmt == "png"
    assert (width, height) == (16, 16)


# ----------------------------------------------------------------------
# Bound 1: encoded length, before the decode
# ----------------------------------------------------------------------


def test_oversized_encoded_payload_is_rejected_without_decoding(no_decode):
    """The reported DoS: a huge body must not be decoded to find out it is too big."""
    payload = "A" * (storage_service.max_base64_chars + 1)

    with pytest.raises(HTTPException) as excinfo:
        storage_service.decode_base64_image(payload)

    assert excinfo.value.status_code == 413
    assert "too large" in excinfo.value.detail.lower()


def test_encoded_budget_is_derived_from_the_configured_image_size():
    """The encoded cap must track MAX_IMAGE_SIZE_MB rather than being a fixed number.

    base64 spends 4 characters per 3 bytes, so the cap has to be 4/3 of the decoded
    budget (plus a little slack) for the decoded check to stay meaningful.
    """
    expected = (storage_service.max_image_bytes // 3) * 4
    assert storage_service.max_base64_chars >= expected
    assert storage_service.max_base64_chars <= expected + 4096


def test_oversized_payload_is_rejected_before_it_is_even_scanned():
    """The raw field length is what gets checked, so refusing is O(1) in the body size.

    Padding the payload with whitespace would shrink back under the cap once folded, so
    this also pins that the check runs on the raw string: otherwise an attacker could
    make the server walk an arbitrarily large body just to measure it.
    """
    filler = storage_service.max_base64_chars + 1
    payload = "A" * (filler // 2) + " " * (filler - filler // 2)

    tracemalloc.start()
    try:
        with pytest.raises(HTTPException) as excinfo:
            storage_service.decode_base64_image(payload)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert excinfo.value.status_code == 413
    assert peak < 64 * 1024, f"rejecting the payload allocated {peak} bytes; it was scanned first"


# ----------------------------------------------------------------------
# Bound 2: decoded length, before the image parser
# ----------------------------------------------------------------------


def test_oversized_decoded_payload_is_rejected(monkeypatch):
    """Encoded within budget but decoding past the byte cap is still refused.

    The encoded cap allows slack for whitespace and a data URI prefix, so this is the
    check that keeps that slack from becoming a way through.
    """
    monkeypatch.setattr(storage_service, "max_base64_chars", 10_000_000, raising=True)
    oversized = b"\x89PNG\r\n\x1a\n" + b"\x00" * (storage_service.max_image_bytes + 1)

    with pytest.raises(HTTPException) as excinfo:
        storage_service.decode_base64_image(b64(oversized))

    assert excinfo.value.status_code == 413


# ----------------------------------------------------------------------
# Bound 3: header dimensions
# ----------------------------------------------------------------------


def test_image_wider_than_the_per_side_limit_is_rejected():
    """A 1-pixel-tall image is tiny on the wire but over the dimension cap."""
    with pytest.raises(HTTPException) as excinfo:
        storage_service.decode_base64_image(b64(png_bytes(storage_service.max_image_dimension + 1, 1)))

    assert excinfo.value.status_code == 413
    assert "dimensions" in excinfo.value.detail.lower()


def test_image_taller_than_the_per_side_limit_is_rejected():
    with pytest.raises(HTTPException) as excinfo:
        storage_service.decode_base64_image(b64(png_bytes(1, storage_service.max_image_dimension + 1)))

    assert excinfo.value.status_code == 413
    assert "dimensions" in excinfo.value.detail.lower()


def test_image_over_the_total_pixel_limit_is_rejected():
    """Both sides fit under the per-side cap, but the grid is still too large.

    4000x4000 passes a 6000px per-side check and fails the pixel budget, so the pixel
    check is not redundant with the dimension check.
    """
    side = int(storage_service.max_image_pixels**0.5) + 1
    assert side <= storage_service.max_image_dimension, "test needs a square under the per-side cap"

    with pytest.raises(HTTPException) as excinfo:
        storage_service.decode_base64_image(b64(png_bytes(side, side)))

    assert excinfo.value.status_code == 413
    assert "pixels" in excinfo.value.detail.lower()


def test_oversized_dimensions_are_rejected_before_pixels_are_decoded(monkeypatch):
    """The rejection must be driven by the header, not by materialising the bitmap."""
    # Built up front: saving a PNG decodes pixels, which is fine for the fixture but
    # would trip the tripwire below.
    small = b64(png_bytes(4, 4))
    oversized = b64(png_bytes(storage_service.max_image_dimension + 1, 1))

    def _boom(self):
        raise AssertionError("PIL decompressed pixel data before the dimension check ran")

    monkeypatch.setattr(storage_service_module.Image.Image, "load", _boom, raising=True)

    # A normal note is accepted without any decode happening, and an oversized one is
    # refused for the same reason -- so a bomb costs no more to reject than a thumbnail.
    storage_service.decode_base64_image(small)

    with pytest.raises(HTTPException) as excinfo:
        storage_service.decode_base64_image(oversized)

    assert excinfo.value.status_code == 413


# ----------------------------------------------------------------------
# Malformed input
# ----------------------------------------------------------------------


@pytest.mark.parametrize("payload", ["", "   ", "data:image/png;base64,", "data:image/png;base64," + ","])
def test_empty_payloads_are_rejected(payload):
    with pytest.raises(HTTPException) as excinfo:
        storage_service.decode_base64_image(payload)

    assert excinfo.value.status_code == 400


def test_non_image_bytes_are_rejected_as_invalid_data():
    with pytest.raises(HTTPException) as excinfo:
        storage_service.decode_base64_image(b64(b"this is definitely not an image"))

    assert excinfo.value.status_code == 400


def test_undecodable_base64_is_rejected():
    with pytest.raises(HTTPException) as excinfo:
        storage_service.decode_base64_image("!!!not base64!!!")

    assert excinfo.value.status_code == 400


def test_unsupported_format_is_rejected():
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (0, 0, 0)).save(buf, format="BMP")

    with pytest.raises(HTTPException) as excinfo:
        storage_service.decode_base64_image(b64(buf.getvalue()))

    assert excinfo.value.status_code == 415


def test_non_string_payload_is_rejected():
    with pytest.raises(HTTPException) as excinfo:
        storage_service.decode_base64_image(None)

    assert excinfo.value.status_code == 400


# ----------------------------------------------------------------------
# Cloudinary side of the fix
# ----------------------------------------------------------------------


def test_upload_stores_the_original_untransformed_but_token_gated(monkeypatch, enabled):
    """Two things at once, because they are the same decision.

    Nothing is resized on the way in: the validator has already refused anything over the
    dimension and pixel ceilings, so a ``crop="limit"`` transformation would only ever fire
    on an image that was already refused. What the upload *must* carry is ``type="authenticated"``
    and nothing else -- that storage type is what makes the object unreadable from a plain CDN
    URL, and it is the only reason a signed delivery URL is needed at all.
    """
    recorded = {}

    def _upload(file, **kwargs):
        recorded.update(kwargs)
        return {
            "public_id": kwargs["public_id"],
            "secure_url": "https://res.example/x.png",
            "type": "authenticated",
        }

    monkeypatch.setattr(storage_service_module, "cloudinary_upload", _upload, raising=True)

    result = enabled.upload_love_note_image(
        image_base64=b64(png_bytes(48, 24)),
        love_note_id="22222222-2222-2222-2222-222222333333",
    )

    assert "transformation" not in recorded, "the original is stored as sent"
    assert recorded["type"] == "authenticated"
    assert "access_control" not in recorded, (
        "a token ACL makes Cloudinary reject the signed delivery URLs this plan relies on"
    )
    assert recorded["public_id"] == "love_notes/22222222-2222-2222-2222-222222333333/image"
    assert result == {
        "public_id": "love_notes/22222222-2222-2222-2222-222222333333/image",
        "format": "png",
    }


def test_upload_fails_when_the_account_stored_it_publicly(monkeypatch, enabled):
    """A private type the account ignored must not look like a successful private upload.

    If Cloudinary accepted the upload but stored it publicly, the note would be readable by
    anyone holding the URL and the signature in the inbox would be decoration. The object is
    destroyed and the request is refused instead, so the failure is a 503 at send time
    rather than a leak discovered later.
    """
    destroyed = []

    def _upload(file, **kwargs):
        return {
            "public_id": kwargs["public_id"],
            "secure_url": "https://res.example/x.png",
            "type": "upload",
            "access_mode": "public",
        }

    monkeypatch.setattr(storage_service_module, "cloudinary_upload", _upload, raising=True)
    monkeypatch.setattr(
        storage_service_module,
        "cloudinary_destroy",
        lambda public_id, **kwargs: destroyed.append(public_id) or {"result": "ok"},
        raising=True,
    )

    with pytest.raises(HTTPException) as excinfo:
        enabled.upload_love_note_image(
            image_base64=b64(png_bytes(8, 8)),
            love_note_id="22222222-2222-2222-2222-222222333333",
        )

    assert excinfo.value.status_code == 503
    assert destroyed == ["love_notes/22222222-2222-2222-2222-222222333333/image"]


def test_upload_rejects_an_oversized_image_before_contacting_cloudinary(monkeypatch, enabled):
    def _upload(*args, **kwargs):
        raise AssertionError("Cloudinary was contacted for an image that exceeds the limits")

    monkeypatch.setattr(storage_service_module, "cloudinary_upload", _upload, raising=True)

    with pytest.raises(HTTPException) as excinfo:
        enabled.upload_love_note_image(
            image_base64=b64(png_bytes(enabled.max_image_dimension + 1, 1)),
            love_note_id="22222222-2222-2222-2222-222222333333",
        )

    assert excinfo.value.status_code == 413


def test_upload_failure_does_not_leak_the_upstream_error(monkeypatch, enabled):
    monkeypatch.setattr(
        storage_service_module,
        "cloudinary_upload",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("cloud-name=secret bucket=love_notes id=abc")),
        raising=True,
    )

    with pytest.raises(HTTPException) as excinfo:
        enabled.upload_love_note_image(
            image_base64=b64(png_bytes(8, 8)),
            love_note_id="22222222-2222-2222-2222-222222333333",
        )

    assert excinfo.value.status_code == 500
    assert "cloud-name" not in excinfo.value.detail
    assert "love_notes" not in excinfo.value.detail


# ----------------------------------------------------------------------
# Request schema
# ----------------------------------------------------------------------


def test_model_rejects_an_oversized_image_base64():
    """Oversized payloads are refused during validation, before the service allocates."""
    with pytest.raises(ValueError):
        LoveNoteCreate(
            recipient_id="22222222-2222-2222-2222-222222333333",
            image_base64="A" * (storage_service.max_base64_chars + 1),
            body="hi",
            anonymous=False,
        )


def test_model_accepts_a_payload_at_the_cap():
    payload = "A" * storage_service.max_base64_chars
    note = LoveNoteCreate(
        recipient_id="22222222-2222-2222-2222-222222333333",
        image_base64=payload,
        body="hi",
        anonymous=False,
    )
    assert len(note.image_base64) == storage_service.max_base64_chars


def test_model_caps_the_note_body_to_the_column_width():
    """love_notes.body is bounded by a CHECK constraint; without this the message is unbounded.

    The cap is read from the model rather than restated as 1000, so that a limit moved in
    backend/app/limits.py cannot leave this test asserting a number the code no longer uses --
    a test that keeps passing at the wrong limit is worse than no test at all.

    The database rule is the CHECK and not the column width. ``varchar(n)`` refuses an over-long
    write during input, before any CHECK is evaluated, so ``ck_love_notes_body_length_1000``
    could never reject anything and ``love_notes.body`` is ``Text`` like every other body
    column. What matters is that the limit is declared once and enforced below the API, so the
    payload model and the constraint are asserted together.
    """
    lengths = {
        constraint.name: str(constraint.sqltext)
        for constraint in LoveNoteRecord.__table__.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name
    }
    assert lengths == {
        f"ck_love_notes_body_length_{LOVE_NOTE_BODY_MAX}": f"length(body) <= {LOVE_NOTE_BODY_MAX}"
    }

    with pytest.raises(ValueError):
        LoveNoteCreate(
            recipient_id="22222222-2222-2222-2222-222222333333",
            image_base64="AAAA",
            body="x" * (LOVE_NOTE_BODY_MAX + 1),
            anonymous=False,
        )


def test_model_accepts_a_body_at_the_cap():
    """A note of exactly the cap is legal. The boundary is inclusive on both sides."""
    note = LoveNoteCreate(
        recipient_id="22222222-2222-2222-2222-222222333333",
        image_base64="AAAA",
        body="x" * LOVE_NOTE_BODY_MAX,
        anonymous=False,
    )
    assert len(note.body) == LOVE_NOTE_BODY_MAX


def test_model_counts_a_character_as_a_code_point_not_a_utf16_unit():
    """The cap is in characters, so an emoji spends one unit of it, not two.

    Pydantic counts `str` length, which is code points. This is asserted rather than assumed
    because it is what makes the limit safe to state as "1000 characters" to someone typing in
    an emoji-heavy field: if the count were UTF-16 units, a note of 1000 emoji would measure
    2000 and be refused for being half as long as the sender's allowance.
    """
    emoji = "\N{HEAVY BLACK HEART}"
    # One character, but two UTF-16 code units -- which is the unit the browser's textarea and
    # `.length` count, and the reason the frontend counts code points explicitly instead.
    assert len(emoji) == 1
    assert len(emoji.encode("utf-16-le")) == 2

    note = LoveNoteCreate(
        recipient_id="22222222-2222-2222-2222-222222333333",
        image_base64="AAAA",
        body=emoji * LOVE_NOTE_BODY_MAX,
        anonymous=False,
    )
    assert len(note.body) == LOVE_NOTE_BODY_MAX

    over = emoji * (LOVE_NOTE_BODY_MAX + 1)
    assert len(over) == LOVE_NOTE_BODY_MAX + 1
    with pytest.raises(ValueError):
        LoveNoteCreate(
            recipient_id="22222222-2222-2222-2222-222222333333",
            image_base64="AAAA",
            body=over,
            anonymous=False,
        )


def test_model_rejects_an_over_the_cap_body_of_east_asian_characters():
    """A CJK ideograph is one UTF-16 unit and one character, so it must not be counted twice.

    The mirror of the emoji case, and the one a `len()`-based limit would get right by accident.
    Asserted so that a future switch to a byte or code-unit count fails here rather than on a
    sender's device.
    """
    with pytest.raises(ValueError):
        LoveNoteCreate(
            recipient_id="22222222-2222-2222-2222-222222333333",
            image_base64="AAAA",
            body="\u3053" * (LOVE_NOTE_BODY_MAX + 1),
            anonymous=False,
        )
