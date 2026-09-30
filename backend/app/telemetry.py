# app/telemetry.py

"""Span hygiene applied before any span leaves the process.

The OpenTelemetry HTTP instrumentation puts the raw, percent-decoded query string
on the span: under the current conventions that is the discrete ``url.query``
attribute the backend emits (``OTEL_SEMCONV_STABILITY_OPT_IN=http``), and on an
outbound client span it is folded into ``url.full`` as well. Two of this
application's query parameters are therefore credentials in the trace store: the
scoped asset delivery token ``t`` (see the asset routes in ``app.main``, which
accept it as a query parameter because an ``<img src>`` cannot send an
Authorization header), and anything else a client chooses to put there, such as
``password``.

The whole query string is dropped rather than only the parameter names known to
be secret. That is deliberate on two counts. A query value is usually unique per
request, so retaining any of them risks turning a span attribute into a
high-cardinality field that is expensive to index and useless to aggregate
against. And a parameter nobody has classified yet is precisely the one that
leaks, so a name-based allowlist would have to be kept in step with every route
by hand. Dropping the query means the rule cannot be forgotten: a new endpoint
that starts accepting a secret in the query string is covered with no opt-in.

This runs as the instrumentation's ``server_request_hook`` rather than in the
endpoints, for two reasons. It is the extension point the instrumentation
provides for adjusting a server span, so it covers every route, including ones
added later, instead of relying on each handler to opt in. And it is the only
point at which the rewrite sticks: the middleware copies its collected
attributes onto the span *after* the span has been started, so a span processor's
``on_start`` would be overwritten, and ``Span.set_attributes`` refuses to write
once the span has ended, so ``on_end`` is too late. The hook runs in between, and
the redacted value is what gets serialised to the collector -- the secret is never
exported in the first place, rather than being hidden somewhere downstream.
"""

from opentelemetry.semconv.attributes.url_attributes import (
    URL_FULL,
    URL_PATH,
    URL_QUERY,
)
from opentelemetry.trace import Span

# The value left in place of a removed query string. Constant, so redacting a
# query cannot introduce cardinality.
REDACTED = "[REDACTED]"

# The pre-1.20 names, written out rather than imported.
#
# opentelemetry-semantic-conventions 0.66b1 does export HTTP_URL and HTTP_TARGET, but
# only from opentelemetry.semconv._incubating.attributes.http_attributes -- an
# underscore-prefixed private path whose contents are explicitly not covered by the
# package's compatibility guarantees. The current names come from the public
# opentelemetry.semconv.attributes.* modules, so importing the retired pair from _incubating
# would make this module -- and therefore the entire application, since main.py imports
# it at startup -- fail to import on any release that reorganises that tree. A security
# control is the last thing that should be able to take a service down over a package
# layout change.
#
# The strings themselves do not depend on the package at all. They are the attribute
# keys as they appear on the wire, fixed by the semantic conventions specification: a
# span emitted by any version of any instrumentation library carries these exact keys.
# Reading them as literals is therefore the more correct expression of what the code
# needs -- a name to match against, not a constant to dereference.
#
# test_trace_url_redaction.py cross-checks these two literals against the SDK constants
# when the _incubating module happens to be importable, so a genuine renumbering of the
# conventions is still caught. That check lives in the tests on purpose: it is worth
# breaking a test run over, and not worth breaking an import over.
_PRE_1_20_URL = "http.url"    # full URL on client spans
_PRE_1_20_TARGET = "http.target"   # request target, may carry the query

# Attributes whose value is a request target or full URL. Everything from the
# first "?" onwards is the query string.
#
# The backend emits only the current conventions, so URL_FULL, URL_PATH and
# URL_QUERY are what this application's own spans actually carry. The two
# pre-1.20 names are kept anyway, and this is deliberately not compatibility
# handling for our own emission: the hook is a security control that runs over
# every span it is handed, and the spans it is handed are not only this
# application's. Any HTTP instrumentation that has not opted in -- the SQLAlchemy
# or Redis instrumentors, a third-party library instrumenting its own client
# calls, or a future release reverting to the default mode -- produces spans on
# the same tracer under the old names, and a leaked Cloudinary signature on one of
# those is exactly the leak this module exists to prevent. Dropping two strings
# from a tuple costs nothing; the query rule is required to hold for whatever
# emitted the span, not for whatever we happen to configure today.
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
    ``scope`` argument is required by the hook signature and unused: the values
    to sanitise are already on the span.
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
    Cloudinary calls in ``app.services.storage_service`` -- that library issues its
    requests through a ``urllib3.PoolManager`` rather than httpx or requests. The
    instrumentor copies the URL it is given onto the span when the span is created
    and then calls this hook, so this is the last point at which the rewrite
    sticks, exactly as it is for the server spans above.

    The reason it is needed is the same shape as the server-side one. Cloudinary
    signs its delivery URLs, so a query string on an outbound request can carry a
    ``s=`` signature and a ``t=`` timestamp, and the upload and admin APIs accept
    parameters this application has no control over. Dropping the whole query
    string keeps a credential from being exported in the first place, and stops a
    per-request value from becoming a span attribute.
    """
    redact_query_strings(span, None)


# ---------------------------------------------------------------------------
# Database spans without opentelemetry-instrumentation-sqlalchemy.
# ---------------------------------------------------------------------------
#
# The contrib SQLAlchemy instrumentor (0.66b1, the latest release) refuses
# SQLAlchemy >= 2.1 -- it logs a version warning and instruments nothing, so
# every query ran unattributed and no span, metric or slow-query signal existed
# for the database. Downgrading SQLAlchemy to regain the spans would trade the
# database itself for its telemetry, so this module carries a small in-house
# Engine adapter built only on stable public APIs (sqlalchemy.event cursor
# hooks + the OpenTelemetry trace API, both unchanged since long before 2.0).
#
# Delete this section -- and use SQLAlchemyInstrumentor again -- once a
# contrib release supports the installed SQLAlchemy. The gate below routes by
# version so that day needs no other change, and test_sqlalchemy_tracing pins
# the invariant that matters (queries emit CLIENT spans, failures ERROR them)
# rather than which implementation produces them.

_MANUAL_SQLALCHEMY_MIN_VERSION = (2, 1)

# Attribute on the SQLAlchemy execution context holding the open span. A
# bespoke name so a future contrib version running alongside cannot mistake it
# for its own bookkeeping (or vice versa).
_SPAN_SLOT = "_confessit_otel_span"

# Marker on engines already carrying our listeners, so a repeated init cannot
# double-instrument (which would end each span twice and double-count every
# query in spanmetrics).
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
    keeps (the database signal is deliberately not opted into the new
    conventions; see docker-compose.yml).

    ``statement`` is the placeholder form -- bound values travel in
    ``parameters``, which is never recorded -- so no row content reaches the
    span. The codebase upholds that by binding everywhere; grepping for
    ``text(f"`` must come back empty.
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

    Anything else -- comments, CTEs starting mid-text, empty strings -- becomes
    ``"DB"`` rather than an unbounded or misleading name. Spanmetrics groups by
    this name, so it must stay low-cardinality: never the statement itself.
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
