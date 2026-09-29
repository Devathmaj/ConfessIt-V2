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
