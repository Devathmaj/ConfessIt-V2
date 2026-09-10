# app/telemetry.py

"""Span hygiene applied before any span leaves the process.

The OpenTelemetry ASGI middleware builds ``http.url`` by appending the raw,
percent-decoded query string to the request path. Two of this application's query
parameters are therefore credentials in the trace store: the scoped asset
delivery token ``t`` (see the asset routes in ``app.main``, which accept it as a
query parameter because an ``<img src>`` cannot send an Authorization header),
and anything else a client chooses to put there, such as ``password``.

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

from opentelemetry.semconv.trace import SpanAttributes
from opentelemetry.trace import Span

# The value left in place of a removed query string. Constant, so redacting a
# query cannot introduce cardinality.
REDACTED = "[REDACTED]"

# Attributes whose value is a request target or full URL. Everything from the
# first "?" onwards is the query string.
#
# http.url and http.target are the pre-1.20 semantic conventions emitted by the
# installed instrumentation; url.full is their current replacement. The current
# conventions are listed alongside them so the rule keeps holding if the
# instrumentation is upgraded and starts emitting the new names.
_URL_ATTRIBUTES = (
    SpanAttributes.HTTP_URL,
    SpanAttributes.HTTP_TARGET,
    SpanAttributes.URL_FULL,
)

# The current conventions give the query string its own attribute, so there is no
# "?" to split on: the entire value is the query and is replaced outright.
_QUERY_ATTRIBUTES = (SpanAttributes.URL_QUERY,)


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
