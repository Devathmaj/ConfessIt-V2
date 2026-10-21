# app/telemetry.py

"""Remove URL query strings from spans before export to protect credentials."""

from opentelemetry.semconv.attributes.url_attributes import (
    URL_FULL,
    URL_PATH,
    URL_QUERY,
)
from opentelemetry.trace import Span

# The value left in place of a removed query string. Constant, so redacting a
# query cannot introduce cardinality.
REDACTED = "[REDACTED]"

# Keep legacy keys as literals so SDK reorganization cannot disable redaction
# at startup; tests compare them with SDK values when available.
_PRE_1_20_URL = "http.url"    # full URL on client spans
_PRE_1_20_TARGET = "http.target"   # request target, may carry the query

# URL attributes may contain credentials after ``?``; keep legacy keys because
# third-party and non-opted-in instrumentors may still emit them.
_URL_ATTRIBUTES = (
    URL_FULL,          # current, client spans; absent on server spans upstream
    URL_PATH,          # current, the path only, but split on "?" defensively
    _PRE_1_20_URL,     # pre-1.20, full URL on client spans
    _PRE_1_20_TARGET,  # pre-1.20, request target, may carry the query
)

# The query string as a discrete attribute. Replaced outright.
_QUERY_ATTRIBUTES = (URL_QUERY,)


def _without_query(url: str) -> str:
    """Return ``url`` with its query string replaced by :data:`REDACTED`."""
    head, separator, _ = url.partition("?")
    if not separator:
        return url
    return f"{head}?{REDACTED}"


def redact_query_strings(span: Span, scope) -> None:
    """Strip query strings from a server span's URL attributes.

    Wired in as the instrumentation's ``server_request_hook``; see the module
    docstring for why this is the only point where the rewrite survives. The
    ``scope`` argument is required by the hook signature and unused.
    """
    redacted = {}

    for key in _URL_ATTRIBUTES:
        value = span.attributes.get(key)
        if isinstance(value, str) and "?" in value:
            redacted[key] = _without_query(value)

    for key in _QUERY_ATTRIBUTES:
        value = span.attributes.get(key)
        if isinstance(value, str) and value:
            redacted[key] = REDACTED

    if redacted:
        span.set_attributes(redacted)


def redact_client_url(span: Span, instance=None, headers=None, body=None) -> None:
    """Strip the query string from an outbound client span's URL attributes.

    Wired in as the urllib3 instrumentation's ``request_hook``, which covers the
    Cloudinary calls in ``app.services.storage_service`` (issued through a
    ``urllib3.PoolManager``, not httpx or requests). The instrumentor copies the
    URL onto the span at creation and then calls this hook, so this is the last
    point where the rewrite sticks. Cloudinary delivery URLs carry a ``s=``
    signature and ``t=`` timestamp, so dropping the whole query keeps a
    credential from being exported -- same shape as the server-side redaction.
    """
    redact_query_strings(span, None)


# Database spans use stable SQLAlchemy event hooks because the installed contrib
# instrumentor does not support this SQLAlchemy version. Replace this adapter when it does;
# tests pin the span behavior independently of the implementation.

_MANUAL_SQLALCHEMY_MIN_VERSION = (2, 1)

# Slot on the SQLAlchemy execution context holding the open span; bespoke name
# so a future contrib version running alongside cannot mistake it for its own
# bookkeeping (or vice versa).
_SPAN_SLOT = "_confessit_otel_span"

# Marker on engines already carrying our listeners, so a repeated init cannot
# double-instrument: each span would end twice and double-count in spanmetrics.
_INSTRUMENTED_MARKER = "_confessit_traced"


def _sqlalchemy_version():
    import sqlalchemy

    return tuple(int(part) for part in sqlalchemy.__version__.split(".")[:2])


def sqlalchemy_instrumentation() -> str:
    """Which database tracing to use for the installed SQLAlchemy: ``"contrib"``
    while the contrib instrumentor accepts it, ``"manual"`` once it refuses."""
    if _sqlalchemy_version() >= _MANUAL_SQLALCHEMY_MIN_VERSION:
        return "manual"
    return "contrib"


def _db_attributes(engine, statement: str) -> dict:
    """Span attributes for one query, in the pre-1.20 ``db.*`` names this stack
    keeps (the database signal is not opted into the new conventions; see
    docker-compose.yml).

    ``statement`` is the placeholder form -- bound values travel in
    ``parameters``, which is never recorded -- so no row content reaches the
    span; the codebase binds everywhere (grepping for ``text(f"`` must come
    back empty).
    """
    url = engine.url
    attributes = {
        "db.system": url.get_backend_name(),
        "db.statement": statement,
    }
    if url.database:
        attributes["db.name"] = url.database
    if url.username:
        attributes["db.user"] = url.username
    if url.host:
        attributes["net.peer.name"] = url.host
    if url.port:
        attributes["net.peer.port"] = url.port
    return attributes


def _operation_name(statement: str) -> str:
    """The leading SQL keyword (``SELECT``/``INSERT``/...) as the span name.

    Anything else -- comments, CTEs mid-text, empty strings -- becomes ``"DB"``.
    Spanmetrics groups by this name, so it must stay low-cardinality: never the
    statement itself.
    """
    first = (statement or "").split(None, 1)
    if first and len(first[0]) <= 12 and first[0].isalpha():
        return first[0].upper()
    return "DB"


def instrument_database_engine(engine, tracer_provider=None) -> str:
    """Attach per-query CLIENT spans to a SQLAlchemy engine; see the section
    header for why this exists.

    ``tracer_provider`` is for tests only (an in-memory exporter); production
    passes nothing and uses the global provider. Returns ``"manual"``. Safe to
    call twice on one engine (the second call is a no-op); listeners never
    raise into the query path -- a tracing failure skips the span, never the
    query.
    """
    from sqlalchemy import event as sa_event

    from opentelemetry import context as otel_context
    from opentelemetry import trace as trace_api
    from opentelemetry.trace import SpanKind, StatusCode

    if getattr(engine, _INSTRUMENTED_MARKER, False):
        return "manual"
    tracer = trace_api.get_tracer(
        __name__, tracer_provider=tracer_provider,
    )

    def _open_span(execution_context, statement: str):
        try:
            span = tracer.start_span(
                _operation_name(statement),
                kind=SpanKind.CLIENT,
                attributes=_db_attributes(engine, statement),
            )
            # Attach so the query nests under the request's server span (and
            # shares its trace id); the token restores the previous context
            # when the span closes.
            token = otel_context.attach(trace_api.set_span_in_context(span))
        except Exception:
            return
        try:
            setattr(execution_context, _SPAN_SLOT, (span, token))
        except Exception:
            otel_context.detach(token)
            span.end()

    def _close_span(execution_context, error=None):
        state = getattr(execution_context, _SPAN_SLOT, None)
        if not state:
            return
        span, token = state
        try:
            delattr(execution_context, _SPAN_SLOT)
        except Exception:
            pass
        try:
            if error is not None:
                span.record_exception(error)
                span.set_status(StatusCode.ERROR)
            span.end()
        finally:
            try:
                otel_context.detach(token)
            except Exception:
                pass

    def _before_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ):
        _open_span(context, statement if isinstance(statement, str) else "")

    def _after_cursor_execute(
        conn, cursor, statement, parameters, context, executemany
    ):
        _close_span(context)

    def _handle_error(context):
        # This hook receives an ExceptionContext wrapper, not the execution
        # context the span was stored on: unwrap to the execution that failed.
        # (Reading the slot off the wrapper would miss every time, leaking the
        # span and losing exactly the failing queries this hook exists for.)
        execution_context = getattr(context, "execution_context", context)
        _close_span(
            execution_context,
            error=getattr(context, "sqlalchemy_exception", None),
        )

    listeners = (
        ("before_cursor_execute", _before_cursor_execute),
        ("after_cursor_execute", _after_cursor_execute),
        ("handle_error", _handle_error),
    )
    try:
        for name, fn in listeners:
            sa_event.listen(engine, name, fn)
    except Exception:
        for name, fn in listeners:
            try:
                sa_event.remove(engine, name, fn)
            except Exception:
                pass
        raise
    try:
        setattr(engine, _INSTRUMENTED_MARKER, True)
    except Exception:
        pass
    return "manual"
