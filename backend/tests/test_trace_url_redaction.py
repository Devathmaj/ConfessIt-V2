# tests/test_trace_url_redaction.py

"""The application must never export a query string into the trace store.

`app.telemetry.redact_query_strings` exists because the OpenTelemetry ASGI
middleware appends the raw query string to `http.url`, and this application
authenticates asset requests with a scoped token in the `t` query parameter.

These tests drive the real instrumentation and the real hook, and assert against
an in-memory exporter, so what is checked is exactly what the OTLP exporter
serialises -- not what a UI happens to display. That distinction matters here:
a Grafana-side redaction would leave the value in Tempo, which is the leak being
closed.
"""

import pytest
from fastapi import FastAPI, Query
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.semconv.trace import SpanAttributes
from opentelemetry.trace import SpanKind

from app.telemetry import REDACTED, redact_query_strings

# The exact query string from the audit report. Both values are the secrets under
# test, so a regression that reintroduces either one is caught by name.
SECRET_QUERY = "t=SECRET_ASSET_TOKEN_ABC&password=hunter2"
SECRET_VALUES = ("SECRET_ASSET_TOKEN_ABC", "hunter2")


@pytest.fixture
def instrumented():
    """A throwaway app whose spans are captured in memory.

    Built on its own TracerProvider rather than the process-wide one, so the
    assertions read the spans this app produced and do not depend on whether
    `app.main` found an OTLP endpoint to configure.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    app = FastAPI()

    # Mirrors the asset delivery routes in app.main, which take the scoped token as
    # a query parameter because an <img src> cannot send an Authorization header.
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


def test_http_url_keeps_the_path_and_drops_the_query(instrumented):
    client, exporter = instrumented

    client.get(f"/api/profile/files/photo.png?{SECRET_QUERY}")

    url = _server_span(exporter).attributes[SpanAttributes.HTTP_URL]
    assert url.endswith(f"/api/profile/files/photo.png?{REDACTED}")
    assert "SECRET_ASSET_TOKEN_ABC" not in url
    assert "hunter2" not in url


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


def test_useful_http_attributes_survive_redaction(instrumented):
    """Redaction must not cost the attributes the trace is actually read for."""
    client, exporter = instrumented

    client.get(f"/api/profile/files/photo.png?{SECRET_QUERY}")

    attributes = _server_span(exporter).attributes
    assert attributes[SpanAttributes.HTTP_METHOD] == "GET"
    assert attributes[SpanAttributes.HTTP_STATUS_CODE] == 200
    # The templated route is what keeps span volume aggregatable; the concrete path
    # is what makes a single trace legible. Both are paths, and both must be intact.
    assert attributes[SpanAttributes.HTTP_ROUTE] == "/api/profile/files/{path:path}"
    assert attributes[SpanAttributes.HTTP_TARGET] == "/api/profile/files/photo.png"
    assert attributes[SpanAttributes.HTTP_SCHEME] == "http"


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


def test_request_without_a_query_string_is_left_alone(instrumented):
    client, exporter = instrumented

    client.get("/api/profile/files/photo.png")

    url = _server_span(exporter).attributes[SpanAttributes.HTTP_URL]
    assert REDACTED not in url
    assert url.endswith("/api/profile/files/photo.png")


def test_real_app_instruments_with_the_redacting_hook():
    """The hook has to be attached to the app that actually serves traffic.

    A hook that is correct but never registered would leave the leak in place, so
    this asserts the wiring on the real application rather than a stand-in.
    """
    from app.main import app as real_app

    middlewares = [m for m in real_app.user_middleware if isinstance(m.cls, type)
                   and issubclass(m.cls, OpenTelemetryMiddleware)]
    assert len(middlewares) == 1, "expected exactly one OTel middleware on the real app"
    assert middlewares[0].kwargs.get("server_request_hook") is redact_query_strings, (
        "the real app's OTel middleware has no redacting server_request_hook, so query "
        "strings would be exported unredacted"
    )
