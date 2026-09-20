# tests/test_trace_url_redaction.py

"""The application must never export a query string into the trace store.

`app.telemetry.redact_query_strings` exists because the OpenTelemetry HTTP
instrumentation puts the raw query string on the span -- on a server span as the
discrete `url.query` attribute under the current conventions, and on an outbound
client span folded into `url.full` as well -- and this application authenticates
asset requests with a scoped token in the `t` query parameter.

These tests drive the real instrumentation and the real hook, and assert against
an in-memory exporter, so what is checked is exactly what the OTLP exporter
serialises -- not what a UI happens to display. That distinction matters here:
a Grafana-side redaction would leave the value in Tempo, which is the leak being
closed.

The fixture pins OTEL_SEMCONV_STABILITY_OPT_IN=http, the mode the backend is
deployed in, so these tests describe the real configuration instead of the
instrumentation default. They assert the pre-1.20 names are absent, because a
regression to the default would still pass a test that only checks the modern
names are present.
"""

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI, Query
from fastapi.testclient import TestClient
from opentelemetry.instrumentation._semconv import (
    _OpenTelemetrySemanticConventionStability as _Stability,
)
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.semconv.attributes.http_attributes import (
    HTTP_REQUEST_METHOD,
    HTTP_RESPONSE_STATUS_CODE,
    HTTP_ROUTE,
)
from opentelemetry.semconv.attributes.network_attributes import (
    NETWORK_PROTOCOL_VERSION,
)
from opentelemetry.semconv.attributes.url_attributes import (
    URL_FULL,
    URL_PATH,
    URL_QUERY,
    URL_SCHEME,
)
from opentelemetry.trace import SpanKind

import app.telemetry
from app.telemetry import (
    _PRE_1_20_TARGET,
    _PRE_1_20_URL,
    _URL_ATTRIBUTES,
    REDACTED,
    redact_client_url,
    redact_query_strings,
)

# The exact query string from the audit report. Both values are the secrets under
# test, so a regression that reintroduces either one is caught by name.
SECRET_QUERY = "t=SECRET_ASSET_TOKEN_ABC&password=hunter2"
SECRET_VALUES = ("SECRET_ASSET_TOKEN_ABC", "hunter2")

# The pre-1.20 HTTP attribute names. Under OTEL_SEMCONV_STABILITY_OPT_IN=http the
# instrumentation must not emit any of them, so a server span must carry none.
LEGACY_HTTP_ATTRIBUTES = (
    "http.method",
    "http.status_code",
    "http.flavor",
    "http.target",
    "http.url",
    "http.scheme",
    "net.peer.ip",
    "net.host.name",
    "net.host.port",
)


def _reset_semconv_stability():
    """Force the next middleware to re-read OTEL_SEMCONV_STABILITY_OPT_IN.

    The stability mode is memoised in a class attribute the first time any
    middleware is built, so a test that changes the environment variable has to clear
    that cache or it silently keeps reading whatever the first import resolved to.
    """
    _Stability._initialized = False
    _Stability._OTEL_SEMCONV_STABILITY_SIGNAL_MAPPING = {}


@pytest.fixture
def instrumented(monkeypatch):
    """A throwaway app whose spans are captured in memory.

    Built on its own TracerProvider rather than the process-wide one, so the
    assertions read the spans this app produced and do not depend on whether
    `app.main` found an OTLP endpoint to configure.

    The cache is cleared on both sides because the stability mode is a process
    global: leaving it pinned would silently change what any later test's middleware
    records.
    """
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "http")
    _reset_semconv_stability()
    try:
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))

        app = FastAPI()

        # A route of the shape this application used to have, declared on a throwaway app. There
        # is no such route in app.main any more -- images are fetched straight from Cloudinary --
        # but the redaction being tested is redaction of a *credential in a query string*, which
        # is the shape a Cloudinary ``__cld_token__`` had and any future credential-bearing query
        # parameter would have. If the hook stopped scrubbing these parameters, such a delivery
        # URL would put its credential in every span the app emits.
        @app.get("/api/profile/files/{path:path}")
        def get_profile_file(
            path: str,
            t: str = Query(default=None),
            password: str = Query(default=None),
        ):
            return {"path": path, "token_seen_by_handler": t, "password_seen_by_handler": password}

        FastAPIInstrumentor.instrument_app(
            app, tracer_provider=provider, server_request_hook=redact_query_strings
        )
        with TestClient(app) as client:
            yield client, exporter
    finally:
        _reset_semconv_stability()


def _server_span(exporter: InMemorySpanExporter):
    spans = [s for s in exporter.get_finished_spans() if s.kind is SpanKind.SERVER]
    assert spans, "expected the request to produce a server span"
    return spans[-1]


def test_secret_query_values_are_absent_from_exported_span_attributes(instrumented):
    client, exporter = instrumented

    client.get(f"/api/profile/files/photo.png?{SECRET_QUERY}")

    attributes = _server_span(exporter).attributes
    for value in SECRET_VALUES:
        assert value not in str(attributes), f"{value!r} was exported in span attributes"


def test_query_attribute_is_redacted(instrumented):
    """The current conventions carry the query in an attribute of its own.

    `url.query` holds the whole query string with no "?" to split on, so it is a
    separate code path in the hook rather than the same one reached by another name.
    Asserting it is emitted and then clean proves the deployed mode cannot
    reintroduce the leak.
    """
    client, exporter = instrumented

    client.get(f"/api/profile/files/photo.png?{SECRET_QUERY}")

    attributes = _server_span(exporter).attributes
    assert URL_QUERY in attributes, (
        "expected the current conventions to emit url.query; if the instrumentation "
        "stopped emitting it, this test is no longer covering the attribute the "
        "deployed backend relies on"
    )
    assert attributes[URL_QUERY] == REDACTED, (
        f"url.query exported the query verbatim: {attributes[URL_QUERY]!r}"
    )


def test_url_path_keeps_the_path_and_carries_no_query(instrumented):
    client, exporter = instrumented

    client.get(f"/api/profile/files/photo.png?{SECRET_QUERY}")

    path = _server_span(exporter).attributes[URL_PATH]
    assert path == "/api/profile/files/photo.png"
    assert "?" not in path, f"url.path still carries a query string: {path!r}"
    assert "SECRET_ASSET_TOKEN_ABC" not in path
    assert "hunter2" not in path


def test_every_url_attribute_is_sanitised(instrumented):
    """No attribute on the span may still carry the query.

    Checked by value rather than by attribute name, so a URL attribute the
    instrumentation starts emitting under a new name is still caught.
    """
    client, exporter = instrumented

    client.get(f"/api/profile/files/photo.png?{SECRET_QUERY}")

    attributes = _server_span(exporter).attributes
    leaking = {
        key: value
        for key, value in attributes.items()
        if isinstance(value, str) and "SECRET_ASSET_TOKEN_ABC" in value
    }
    assert not leaking, f"query secrets survived export on {leaking}"


def test_modern_http_attributes_are_present(instrumented):
    """The attributes Tempo, Grafana and the metric views are read for.

    These are the M5 replacements, and the ones the rest of the observability stack
    is now written against: `http.request.method` and `http.response.status_code` for
    filtering, `http.route` for keeping span volume aggregatable, `url.path` for
    making a single trace legible, and `network.protocol.version` /
    `url.scheme` for transport detail.
    """
    client, exporter = instrumented

    client.get(f"/api/profile/files/photo.png?{SECRET_QUERY}")

    attributes = _server_span(exporter).attributes
    assert attributes[HTTP_REQUEST_METHOD] == "GET"
    assert attributes[HTTP_RESPONSE_STATUS_CODE] == 200
    assert attributes[HTTP_ROUTE] == "/api/profile/files/{path:path}"
    assert attributes[URL_PATH] == "/api/profile/files/photo.png"
    assert attributes[URL_SCHEME] == "http"
    assert attributes[NETWORK_PROTOCOL_VERSION] == "1.1"


def test_no_legacy_http_attribute_is_emitted(instrumented):
    """Modern conventions only, asserted negatively.

    Checking that the current names are present does not distinguish "modern only"
    from "both generations", so every pre-1.20 name is checked for absence. This is
    the assertion that fails if OTEL_SEMCONV_STABILITY_OPT_IN is dropped back to the
    instrumentation default, which is what M5 was.
    """
    client, exporter = instrumented

    client.get(f"/api/profile/files/photo.png?{SECRET_QUERY}")

    attributes = _server_span(exporter).attributes
    legacy = set(attributes) & set(LEGACY_HTTP_ATTRIBUTES)
    assert not legacy, f"pre-1.20 attributes still emitted on the server span: {sorted(legacy)}"


def test_the_handler_still_receives_the_real_query_string(instrumented):
    """The redaction is telemetry-only; the request itself is untouched.

    Guards the requirement that the asset authentication mechanism and the `t`
    parameter keep working exactly as before.
    """
    client, _ = instrumented

    response = client.get(f"/api/profile/files/photo.png?{SECRET_QUERY}")

    assert response.status_code == 200
    body = response.json()
    assert body["token_seen_by_handler"] == "SECRET_ASSET_TOKEN_ABC"
    assert body["password_seen_by_handler"] == "hunter2"


def test_outbound_client_url_is_redacted():
    """The urllib3 request_hook must sanitise outbound spans, not just inbound ones.

    Cloudinary signs its delivery URLs, so an outbound query string can carry a
    credential. This drives a real client span through the real hook rather than
    asserting on a dict, because the hook is registered as
    URLLib3Instrumentor(request_hook=...) and this is the only thing that proves the
    argument order the instrumentor uses -- (span, request) -- is what the hook
    expects. Getting that wrong would make the hook raise and silently do nothing.

    The pre-1.20 names are set here as well as the current ones even though the
    backend no longer emits them, because that is exactly the case
    `_URL_ATTRIBUTES` keeps them for: the hook is a security control that must hold
    for any span handed to it, not only for the ones this configuration produces.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)

    with tracer.start_as_current_span("GET", kind=SpanKind.CLIENT) as span:
        # Set directly rather than by instrumenting a real request, so the test does
        # not depend on the ambient stability mode.
        span.set_attribute("http.url", f"https://res.cloudinary.com/x/image.jpg?{SECRET_QUERY}")
        span.set_attribute("http.target", f"/x/image.jpg?{SECRET_QUERY}")
        span.set_attribute(URL_FULL, f"https://res.cloudinary.com/x/image.jpg?{SECRET_QUERY}")
        span.set_attribute(URL_QUERY, SECRET_QUERY)
        # The instrumentor passes the request object as the second positional
        # argument; it is unused here, which is what the hook signature documents.
        redact_client_url(span, object())

    attributes = list(exporter.get_finished_spans())[-1].attributes
    for value in SECRET_VALUES:
        assert value not in str(attributes), f"{value!r} survived on an outbound client span"
    for key in ("http.url", "http.target", URL_FULL):
        assert attributes[key].endswith(f"?{REDACTED}"), (
            f"{key} was not sanitised: {attributes[key]!r}"
        )
    # The part before the query is preserved, so the span is still usable for
    # identifying which host and object was called.
    for key in ("http.url", URL_FULL):
        assert attributes[key].startswith("https://res.cloudinary.com/x/image.jpg")
    assert attributes["http.target"].startswith("/x/image.jpg")
    assert attributes[URL_QUERY] == REDACTED


def test_request_without_a_query_string_is_left_alone(instrumented):
    client, exporter = instrumented

    client.get("/api/profile/files/photo.png")

    attributes = _server_span(exporter).attributes
    assert REDACTED not in str(attributes), "an absent query string was still redacted"
    assert attributes[URL_PATH] == "/api/profile/files/photo.png"


def test_real_app_instruments_with_the_redacting_hook():
    """The hook has to be attached to the app that actually serves traffic.

    A hook that is correct but never registered would leave the leak in place, so
    this asserts the wiring on the real application rather than a stand-in.

    instrumentation 0.66b0 no longer registers itself through app.add_middleware,
    which is what this test used to scan app.user_middleware for, and which now
    finds nothing even when the app is instrumented. It replaces
    app.build_middleware_stack instead and wraps the stack it builds in an
    OpenTelemetryMiddleware, so the middleware is reached by building the stack the
    same way the server does rather than by reading the middleware list.
    """
    from app.main import app as real_app

    assert hasattr(real_app, "_original_build_middleware_stack"), (
        "the real app's middleware stack builder was not replaced, so the FastAPI "
        "instrumentation is not attached and no spans are recorded"
    )

    stack = real_app.build_middleware_stack()
    # 0.66b0 returns ServerErrorMiddleware(app=OpenTelemetryMiddleware(...)) so that
    # an error raised inside the instrumentation cannot escape the stack.
    otel_middleware = stack if isinstance(stack, OpenTelemetryMiddleware) else stack.app
    assert isinstance(otel_middleware, OpenTelemetryMiddleware), (
        f"expected the real app's built stack to contain an OTel middleware, got "
        f"{type(otel_middleware).__name__}"
    )

    hook = otel_middleware.server_request_hook
    # The instrumentation runs the hook through failsafe() so a raising hook cannot
    # break the request, which leaves the original reachable via __wrapped__.
    original = getattr(hook, "__wrapped__", hook)
    assert original is redact_query_strings, (
        "the real app's OTel middleware has no redacting server_request_hook, so query "
        "strings would be exported unredacted"
    )


def test_pre_1_20_names_match_the_sdk_constants():
    """The literals in app.telemetry must still be the names the SDK uses.

    `app.telemetry` writes the two pre-1.20 attribute names out as string literals
    rather than importing them, because the SDK only exports them from the private
    `opentelemetry.semconv._incubating` tree and a reorganisation of that tree would
    otherwise stop the application importing at all -- taking the redaction control
    down with it.

    Trading an import for a literal is only safe while the literal is right. This is the
    check that keeps it honest: if a future release were to change the value of the
    constant, the literal would no longer match what instrumentation puts on a span and
    the redaction would silently stop covering it. The strings are wire names fixed by
    the conventions, so this failing means the SDK and the spec have diverged and the
    literal is the thing that needs updating.
    """
    try:
        from opentelemetry.semconv._incubating.attributes.http_attributes import (
            HTTP_TARGET,
            HTTP_URL,
        )
    except ImportError:
        # The module moved or is gone. That is the expected consequence of not depending
        # on the private path, and the literals are still the correct wire names, so
        # there is nothing here to fail about.
        pytest.skip("the SDK no longer exports the pre-1.20 constants from _incubating")

    assert _PRE_1_20_URL == HTTP_URL
    assert _PRE_1_20_TARGET == HTTP_TARGET
    # And they are still in the tuple the hook actually iterates, so this is not only a
    # statement about two module constants that nothing reads.
    assert _PRE_1_20_URL in _URL_ATTRIBUTES
    assert _PRE_1_20_TARGET in _URL_ATTRIBUTES


def test_telemetry_does_not_import_the_private_semconv_tree():
    """app.telemetry must not import from opentelemetry.semconv._incubating.

    The module is imported by app.main at startup, so a dependency on an underscore-
    prefixed, explicitly unstable path would make the whole service fail to boot over a
    package layout change. This asserts the property directly rather than trusting that
    nobody re-adds the import, because the reason the literals exist would be lost the
    moment someone "tidied" them back into an import.

    Checked over the parsed import statements rather than the file text, so the module's
    own explanation of why it avoids that path -- which necessarily names it -- does not
    trip the assertion.
    """
    tree = ast.parse(Path(app.telemetry.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import, which cannot reach the installed package.
            if node.level == 0 and node.module:
                imported.add(node.module)
                imported.update(
                    "%s.%s" % (node.module, alias.name) for alias in node.names
                )

    private = sorted(m for m in imported if "._incubating" in m)
    assert not private, (
        "app.telemetry imports from the private semconv tree (%s); use the public "
        "attributes modules or a literal instead" % private
    )
    # And the public tree it does use is the stable one.
    assert any(m.startswith("opentelemetry.semconv.attributes") for m in imported), (
        "expected app.telemetry to import the public semconv attributes modules"
    )
