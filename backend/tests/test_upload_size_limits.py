"""Tests for the upload size limits on the request and handler layers.

``await file.read()`` buffers the whole body before any handler code runs, so a per-handler
size check happens only after the memory has already been spent, and a client that omits
or understates ``Content-Length`` never meets that check at all. These tests cover the
three bounds that close that off: the declared length at the request layer, the declared
part size, and a chunked read that stops as soon as the cap is passed.
"""

import io
import logging
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PIL import Image
from starlette.datastructures import UploadFile
from starlette.responses import PlainTextResponse

from app.config import settings
from app.main import (
    RequestBodyLimitMiddleware,
    RequestBodyTooLarge,
    app,
    request_body_limit,
)
from app.services import profile_service
from app.services.storage_service import storage_service


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def png_bytes(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def upload(data: bytes, *, declared=None, filename: str = "pic.png") -> UploadFile:
    """An ``UploadFile`` over ``data``, carrying the size the multipart parser recorded."""
    return UploadFile(
        filename=filename,
        file=io.BytesIO(data),
        size=len(data) if declared is None else declared,
    )


class UndeclaredSizeUpload:
    """An upload part with no ``size``, so only the chunked read can bound it.

    The multipart parser fills ``UploadFile.size`` from the part headers, but a chunked
    request has no part headers to read. The declared-size guard then has nothing to check,
    and the bounded read is the only thing standing between that client and the heap -- so
    it has to hold for an upload that never declared a length in the first place.
    """

    def __init__(self, stream, *, size=None):
        self.file = stream
        self.filename = "pic.png"
        self.content_type = "image/png"
        if size is not None:
            self.size = size

    async def read(self, size: int = -1):
        return self.file.read(size)


class EndlessStream:
    """A read() that yields zeros forever without materialising the whole thing.

    Lets a test claim a gigabyte upload for the cost of the few chunks actually read.
    """

    def __init__(self):
        self.total = 0

    def read(self, size: int = -1):
        count = size if size and size > 0 else 1 << 20
        self.total += count
        return b"\0" * count


# ----------------------------------------------------------------------
# Bound 1: the declared part size, checked before the body is touched
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_declared_size_over_the_cap_is_refused_without_reading():
    f = upload(b"", declared=storage_service.max_image_bytes + 1)
    reads = []

    async def _read(size=-1):
        reads.append(size)
        raise AssertionError("the oversize body was read before the declared size was checked")

    f.read = _read

    with pytest.raises(HTTPException) as excinfo:
        await storage_service.read_upload_capped(f)

    assert excinfo.value.status_code == 413
    assert reads == []


@pytest.mark.asyncio
async def test_declared_size_at_the_cap_is_accepted():
    data = png_bytes(8, 8)
    f = upload(data, declared=storage_service.max_image_bytes)
    assert await storage_service.read_upload_capped(f) == data


# ----------------------------------------------------------------------
# Bound 2: the chunked read, which is what a lying Content-Length cannot avoid
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unbounded_body_is_cut_off_partway_through():
    """A client that declares little but sends gigabytes must still be bounded.

    This is the case the request-layer check cannot see, and the reason the read itself
    has to enforce the limit rather than trusting a header.
    """
    stream = EndlessStream()
    f = UndeclaredSizeUpload(stream)

    with pytest.raises(HTTPException) as excinfo:
        await storage_service.read_upload_capped(f)

    assert excinfo.value.status_code == 413
    # One chunk of overshoot is the most the reader can hold past the cap, which is what
    # keeps the peak buffer tied to the limit instead of to the attacker's upload.
    assert stream.total <= storage_service.max_image_bytes + storage_service.UPLOAD_READ_CHUNK_BYTES


@pytest.mark.asyncio
async def test_chunked_read_understated_size_is_still_bounded():
    """A part that lies about its size (declared 1 byte, actually huge) is still cut off."""
    stream = EndlessStream()
    f = UndeclaredSizeUpload(stream, size=1)

    with pytest.raises(HTTPException) as excinfo:
        await storage_service.read_upload_capped(f)

    assert excinfo.value.status_code == 413
    assert stream.total <= storage_service.max_image_bytes + storage_service.UPLOAD_READ_CHUNK_BYTES


@pytest.mark.asyncio
async def test_chunked_body_within_the_cap_is_returned_whole():
    data = png_bytes(8, 8)
    f = UndeclaredSizeUpload(io.BytesIO(data))

    assert await storage_service.read_upload_capped(f) == data


@pytest.mark.asyncio
async def test_explicit_cap_is_honoured():
    f = UndeclaredSizeUpload(io.BytesIO(b"x" * 5000))

    with pytest.raises(HTTPException) as excinfo:
        await storage_service.read_upload_capped(f, max_bytes=1000)

    assert excinfo.value.status_code == 413


@pytest.mark.asyncio
async def test_empty_upload_is_not_an_error_at_the_size_layer():
    """An empty part is a validation problem, not a size one; it must reach the validator."""
    assert await storage_service.read_upload_capped(UndeclaredSizeUpload(io.BytesIO(b""))) == b""


# ----------------------------------------------------------------------
# The profile handler must go through the bounded read
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profile_upload_does_not_buffer_an_unbounded_body():
    """The handler path, not just the helper.

    ``db`` is None because the request is refused before the service reaches the
    database; a handler that fell back to buffering first would surface as a different
    error here.
    """
    stream = EndlessStream()
    # Only ``regno`` is needed now: the upload reads the body first and refuses it at 413
    # without ever naming the object. Without ``regno`` the test would be measuring an
    # AttributeError instead of the size limit.
    user = type(
        "U",
        (),
        {
            "id": "22CS045-0000-0000-0000-000000000001",
            "regno": "22CS045",
            "profile_picture_public_id": None,
            "profile_picture_format": None,
            "account_type": "users",
        },
    )()

    with pytest.raises(HTTPException) as excinfo:
        await profile_service.upload_profile_picture_service(user, None, UndeclaredSizeUpload(stream))

    assert excinfo.value.status_code == 413
    assert stream.total <= storage_service.max_image_bytes + storage_service.UPLOAD_READ_CHUNK_BYTES


# ----------------------------------------------------------------------
# The same geometry caps, on the multipart path
# ----------------------------------------------------------------------


def test_profile_upload_rejects_an_oversized_but_small_file():
    """A few KB of PNG can still describe a bitmap that costs gigabytes to decode."""
    f = upload(png_bytes(storage_service.max_image_dimension + 1, 1))

    with pytest.raises(HTTPException) as excinfo:
        storage_service.validate_image_file(f.file.getvalue(), max_size_mb=2)

    assert excinfo.value.status_code == 413
    assert "dimensions" in excinfo.value.detail.lower()


def test_profile_upload_accepts_a_normal_picture():
    data = png_bytes(320, 240)
    assert storage_service.validate_image_file(data, max_size_mb=2) == "png"


# ----------------------------------------------------------------------
# Bound 3: the request layer, before the framework buffers anything
# ----------------------------------------------------------------------


class _Downstream:
    def __init__(self):
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1


class _BodyReadingDownstream(_Downstream):
    """An app that actually drains the body, like a route that parses a form."""

    def __init__(self, total):
        super().__init__()
        self.total = total
        self.seen = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        while True:
            message = await receive()
            self.seen += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        await PlainTextResponse("ok")(scope, receive, send)


def http_scope(content_length, path="/api/profile/upload-picture"):
    headers = [] if content_length is None else [(b"content-length", str(content_length).encode())]
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": headers,
    }


def _feed(total, chunk=65536):
    """A receive channel that yields ``total`` bytes and records how much was pulled."""
    state = {"left": total, "read": 0}

    async def receive():
        if state["left"] <= 0:
            return {"type": "http.request", "body": b"", "more_body": False}
        size = min(chunk, state["left"])
        state["left"] -= size
        state["read"] += size
        return {"type": "http.request", "body": b"\0" * size, "more_body": True}

    receive.state = state
    return receive


async def drive_middleware(scope, *, max_bytes=1024, total=0, drain=False):
    """Run the middleware and report whether the body was read or the app reached."""
    downstream = _BodyReadingDownstream(total) if drain else _Downstream()
    receive = _feed(total)
    sent = []

    async def send(message):
        sent.append(message)

    # The refusal is logged; the log file handler is not what these tests are about and
    # its rollover is unreliable under the test runner.
    app_logger = logging.getLogger("app.main")
    was_disabled = app_logger.disabled
    app_logger.disabled = True
    raised = None
    try:
        await RequestBodyLimitMiddleware(downstream, max_bytes=max_bytes)(scope, receive, send)
    except RequestBodyTooLarge as exc:
        raised = exc
    finally:
        app_logger.disabled = was_disabled

    return SimpleNamespace(
        reached=downstream.calls,
        bytes_pulled=receive.state["read"],
        sent=sent,
        raised=raised,
    )


@pytest.mark.asyncio
async def test_oversize_content_length_is_refused_without_reading_the_body():
    result = await drive_middleware(http_scope(5_000_000), total=5_000_000)

    assert result.reached == 0, "the request reached the app despite declaring an oversize body"
    assert result.bytes_pulled == 0, "the body was read despite declaring an oversize length"
    assert result.sent[0]["type"] == "http.response.start"
    assert result.sent[0]["status"] == 413
    # The body arrives in its own message, after the response head.
    assert b"".join(m.get("body", b"") for m in result.sent) == b"Request body too large"


@pytest.mark.asyncio
async def test_content_length_exactly_at_the_cap_is_allowed():
    result = await drive_middleware(http_scope(1024), total=1024, drain=True)
    assert result.reached == 1
    assert result.raised is None
    assert result.bytes_pulled == 1024


@pytest.mark.asyncio
async def test_content_length_under_the_cap_is_allowed():
    result = await drive_middleware(http_scope(10), total=10, drain=True)
    assert result.reached == 1
    assert result.raised is None


# ----------------------------------------------------------------------
# Undeclared bodies: a chunked request carries no length to check up front
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undeclared_body_under_the_cap_still_reaches_the_app():
    result = await drive_middleware(http_scope(None), max_bytes=1024, total=1000, drain=True)

    assert result.reached == 1
    assert result.raised is None
    assert result.bytes_pulled == 1000


@pytest.mark.asyncio
async def test_undeclared_body_past_the_cap_is_cut_off():
    """The bound that a Content-Length check cannot provide.

    Starlette spools a multipart body to a temp file before any handler runs, so a client
    that declares nothing would otherwise fill the disk rather than the heap. Counting
    what actually arrives is what stops it.
    """
    result = await drive_middleware(
        http_scope(None), max_bytes=1024, total=5_000_000, drain=True
    )

    assert result.raised is not None
    # Stopped within one chunk of the cap, not after the whole body.
    assert result.bytes_pulled <= 1024 + 65536


@pytest.mark.asyncio
async def test_undeclared_body_at_exactly_the_cap_is_allowed():
    result = await drive_middleware(http_scope(None), max_bytes=1024, total=1024, drain=True)
    assert result.raised is None
    assert result.bytes_pulled == 1024


@pytest.mark.asyncio
async def test_understated_content_length_is_still_bounded_by_the_count():
    """A client that declares a small length and then sends more gets caught mid-stream."""
    result = await drive_middleware(
        http_scope(10), max_bytes=1024, total=5_000_000, drain=True
    )
    assert result.raised is not None
    assert result.bytes_pulled <= 1024 + 65536


@pytest.mark.asyncio
async def test_the_raised_overflow_becomes_a_413_response():
    """The exception is a plain class, so it needs a handler to be a client error."""
    handler = app.exception_handlers[RequestBodyTooLarge]

    response = await handler(None, RequestBodyTooLarge())
    assert response.status_code == 413
    assert response.body == b"Request body too large"


@pytest.mark.asyncio
async def test_malformed_content_length_is_left_to_the_server_parser():
    result = await drive_middleware(
        {"type": "http", "method": "POST", "path": "/x", "headers": [(b"content-length", b"abc")]},
        total=10, drain=True,
    )
    assert result.reached == 1
    assert result.raised is None


@pytest.mark.asyncio
async def test_websocket_scopes_pass_through_untouched():
    """Websocket routes carry no request body; the limit must not apply to them."""
    result = await drive_middleware({"type": "websocket", "path": "/ws"})
    assert result.reached == 1
    assert result.raised is None
    assert result.sent == []


@pytest.mark.asyncio
async def test_get_requests_are_measured_too():
    """The limit is on the body, not the verb, so a GET cannot smuggle one past it."""
    scope = http_scope(5_000_000, path="/api/love-notes/users")
    scope["method"] = "GET"
    result = await drive_middleware(scope, total=5_000_000)
    assert result.reached == 0
    assert result.sent[0]["status"] == 413


# ----------------------------------------------------------------------
# Sizing of the request-layer default
# ----------------------------------------------------------------------


def test_body_limit_covers_the_largest_legitimate_request():
    """The default has to admit a love note at its full encoded cap, with room to spare."""
    limit = request_body_limit()
    assert limit > storage_service.max_base64_chars
    assert limit > storage_service.max_image_bytes


def test_body_limit_tracks_the_configured_image_size():
    """Raising MAX_IMAGE_SIZE_MB must raise the ceiling, or a legal upload gets refused."""
    service = storage_service
    original = service.max_image_bytes
    try:
        service.max_image_bytes = original * 4
        assert request_body_limit() > original * 4
    finally:
        service.max_image_bytes = original


def test_body_limit_honours_an_explicit_override(monkeypatch):
    monkeypatch.setattr(settings, "MAX_REQUEST_BODY_BYTES", 1234, raising=True)
    assert request_body_limit() == 1234


def test_cors_stays_outside_the_body_limit():
    """A 413 must still carry CORS headers, or the browser reports an opaque failure."""
    from app.main import app

    names = [mw.cls.__name__ for mw in app.user_middleware]
    assert names.index("CORSMiddleware") < names.index("RequestBodyLimitMiddleware")
