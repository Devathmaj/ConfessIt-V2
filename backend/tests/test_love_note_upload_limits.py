"""Tests for the resource limits on love note image uploads.

A love note image arrives as base64 text inside a JSON body, so the whole encoded
string is already resident in memory before any application code runs, and
``base64.b64decode`` then allocates a second buffer of the same order. Nothing bounded
either allocation, so a single request could hand the process tens of megabytes and
then hand PIL a small file that describes an arbitrarily large bitmap.

These tests pin the three bounds that close that off -- encoded length (checked before
decoding), decoded length, and header dimensions -- plus the ``crop="limit"``
transformation that bounds anything which still reaches Cloudinary.
"""

import base64
import io
import tracemalloc

import pytest
from fastapi import HTTPException
from PIL import Image

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


def test_upload_bounds_the_stored_asset_via_a_limit_crop(monkeypatch, enabled):
    """The upload must carry w/h so nothing unbounded can be stored or delivered.

    "limit" scales to fit inside the box and never upscales, so a legitimate note is
    stored untouched while an oversized one is still bounded at the CDN.
    """
    recorded = {}

    def _upload(file, **kwargs):
        recorded.update(kwargs)
        return {"public_id": kwargs["public_id"], "secure_url": "https://res.example/x.png", "version": 1}

    monkeypatch.setattr(storage_service_module, "cloudinary_upload", _upload, raising=True)

    result = enabled.upload_love_note_image(image_base64=b64(png_bytes(48, 24)), sender_name="Alice")

    transformation = recorded["transformation"]
    assert transformation["width"] == enabled.max_image_dimension
    assert transformation["height"] == enabled.max_image_dimension
    assert transformation["crop"] == "limit"
    assert (result["width"], result["height"]) == (48, 24)


def test_upload_rejects_an_oversized_image_before_contacting_cloudinary(monkeypatch, enabled):
    def _upload(*args, **kwargs):
        raise AssertionError("Cloudinary was contacted for an image that exceeds the limits")

    monkeypatch.setattr(storage_service_module, "cloudinary_upload", _upload, raising=True)

    with pytest.raises(HTTPException) as excinfo:
        enabled.upload_love_note_image(
            image_base64=b64(png_bytes(enabled.max_image_dimension + 1, 1)),
            sender_name="Alice",
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
        enabled.upload_love_note_image(image_base64=b64(png_bytes(8, 8)), sender_name="Alice")

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
    """love_notes.body is String(2000); without this the message is the unbounded field."""
    with pytest.raises(ValueError):
        LoveNoteCreate(
            recipient_id="22222222-2222-2222-2222-222222333333",
            image_base64="AAAA",
            body="x" * 2001,
            anonymous=False,
        )
