"""HTTP cache policy for the API.

The SPA's static assets are cached by Nginx and the service worker. This module
covers the API itself:

* sensitive reads (admin, staff, auth, notifications, chat, conversations,
  reports, profiles and the love-note inbox) are ``no-store``;
* dynamic GETs are ``private`` and must revalidate, with a weak ETag so a
  conditional request costs a 304 rather than a full JSON body;
* versioned static paths, if a deployment ever serves them from the API, are
  ``public, immutable`` for a year.

A blanket ``/api/*`` cache is deliberately absent. Most answers here are
per-viewer (reactions, reports, unread counts), and a shared cache would leak
one account's view to another. The per-path classification is the boundary.

The middleware buffers only API JSON GET responses, and only up to a bounded
size, so it cannot turn a large streaming download into a memory problem.
"""

from __future__ import annotations

import hashlib
from typing import Optional

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Paths (after any proxy prefix is stripped) that must never be stored by a
# browser, proxy or service worker. `love-notes/inbox` is called out separately
# from the public love-note surface because its rows are private messages.
SENSITIVE_PREFIXES = (
    "/admin",
    "/staff",
    "/auth",
    "/notifications",
    "/messages",
    "/conversations",
    "/reports",
    "/profile",
    "/love-notes/inbox",
)

# A deployment may leave the API under /api/ (the reverse-proxy layout). Strip
# that prefix before classifying, without rewriting the path the app sees.
API_PREFIXES = ("/api",)

DYNAMIC_PREFIXES = (
    "/confessions",
    "/leaderboards",
    "/games",
    "/matchmaking",
    "/love-notes",
    "/api/time",
)

IMMUTABLE_PREFIXES = ("/assets/", "/static/", "/floral_svgs/")

MAX_ETAG_BYTES = 1_000_000

SENSITIVE_CACHE = "no-store"
DYNAMIC_CACHE = "private, max-age=0, must-revalidate"
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"


def _normalised_path(path: str) -> str:
    for prefix in API_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            stripped = path[len(prefix):]
            return stripped or "/"
    return path


def _matches(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(
        path == prefix
        or path.startswith(prefix if prefix.endswith("/") else prefix + "/")
        for prefix in prefixes
    )


def cache_policy(path: str) -> Optional[str]:
    """The Cache-Control value for a path, or None when it is not classified."""
    normalised = _normalised_path(path)
    if _matches(normalised, SENSITIVE_PREFIXES):
        return SENSITIVE_CACHE
    if normalised in ("/", "/healthz", "/metrics"):
        return SENSITIVE_CACHE
    if _matches(normalised, IMMUTABLE_PREFIXES):
        return IMMUTABLE_CACHE
    if _matches(normalised, DYNAMIC_PREFIXES):
        return DYNAMIC_CACHE
    return None


def _etag_for(body: bytes) -> str:
    return 'W/"' + hashlib.sha256(body).hexdigest()[:32] + '"'


class HttpCacheMiddleware:
    """Stamp cache policy and add conditional-GET support to API reads."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        policy = cache_policy(scope.get("path", ""))

        # Writes are never cacheable; the endpoint may still set its own header.
        if method not in ("GET", "HEAD") or policy is None:
            if method not in ("GET", "HEAD"):
                async def send_no_store(message: Message) -> None:
                    if message["type"] == "http.response.start":
                        headers = MutableHeaders(raw=message.setdefault("headers", []))
                        headers.setdefault("Cache-Control", "no-store")
                    await send(message)

                await self.app(scope, receive, send_no_store)
                return
            await self.app(scope, receive, send)
            return

        if method == "HEAD" or policy == SENSITIVE_CACHE:
            async def send_policy(message: Message) -> None:
                if message["type"] == "http.response.start":
                    headers = MutableHeaders(raw=message.setdefault("headers", []))
                    if policy == SENSITIVE_CACHE:
                        headers["Cache-Control"] = policy
                    else:
                        headers.setdefault("Cache-Control", policy)
                await send(message)

            await self.app(scope, receive, send_policy)
            return

        start: Optional[Message] = None
        chunks: list[bytes] = []
        too_large = False

        async def capture(message: Message) -> None:
            nonlocal start, too_large
            if message["type"] == "http.response.start":
                start = message
                headers = MutableHeaders(raw=start.setdefault("headers", []))
                headers.setdefault("Cache-Control", policy)
                length = headers.get("content-length")
                if length is not None:
                    try:
                        if int(length) > MAX_ETAG_BYTES:
                            # Known to be too large before the body arrives: send
                            # the headers and pass the rest through unbuffered.
                            too_large = True
                            await send(start)
                            start = None
                    except ValueError:
                        pass
                return
            if message["type"] != "http.response.body":
                return
            body = message.get("body", b"")
            more_body = message.get("more_body", False)

            if too_large:
                await send(
                    {"type": "http.response.body", "body": body, "more_body": more_body}
                )
                return

            chunks.append(body)
            if sum(len(chunk) for chunk in chunks) > MAX_ETAG_BYTES:
                # Crossed the buffering ceiling mid-stream: hand over what was
                # buffered and stream the remainder, without an ETag.
                too_large = True
                await send(start)
                start = None
                await send(
                    {
                        "type": "http.response.body",
                        "body": b"".join(chunks),
                        "more_body": more_body,
                    }
                )
                chunks.clear()
                return

            if not more_body:
                await flush()

        async def flush() -> None:
            nonlocal start
            if start is None:
                return
            headers = MutableHeaders(raw=start.setdefault("headers", []))
            headers["Cache-Control"] = policy
            body = b"".join(chunks)
            content_type = headers.get("content-type", "")
            status = int(start.get("status", 200))

            cache_control = headers.get("cache-control", "").lower()
            if (
                status == 200
                and not too_large
                and "application/json" in content_type
                and "no-store" not in cache_control
            ):
                etag = _etag_for(body)
                headers["ETag"] = etag
                incoming = Headers(scope=scope).get("if-none-match", "")
                if incoming and (incoming.strip() == "*" or etag in incoming):
                    for header in ("content-length", "content-type"):
                        if header in headers:
                            del headers[header]
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 304,
                            "headers": headers.raw,
                        }
                    )
                    await send({"type": "http.response.body", "body": b""})
                    start = None
                    return

            await send(start)
            await send({"type": "http.response.body", "body": body, "more_body": False})
            start = None

        await self.app(scope, receive, capture)
