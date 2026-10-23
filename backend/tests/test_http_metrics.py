# tests/test_http_metrics.py

"""The backend must export HTTP server metrics that are bounded and free of secrets.

The ASGI middleware in this OpenTelemetry version already builds the standard
http.server.* instruments and takes a meter from FastAPIInstrumentor, so
registering a MeterProvider in `app.main` is the whole of the instrumentation. What
is not automatic is the attribute set, so `app.main` installs a View that allows a
fixed set of dimensions through.

The backend runs with OTEL_SEMCONV_STABILITY_OPT_IN=http, so the only HTTP server
histogram is `http.server.request.duration` and the dimensions are the current
convention names. The fixture pins that mode rather than inheriting it, so these
tests describe the deployed configuration instead of the instrumentation default.
They also assert the pre-1.20 names are *absent*, because a mode regression to the
default would otherwise still satisfy a test that only checks the modern names are
present.

These tests drive the real middleware and the real View, and read an in-memory
metric reader, so what is asserted is the data the OTLP exporter serialises -- not
what a Grafana panel happens to display. A bound applied in a dashboard would leave
the unbounded series in Prometheus, which is the growth being prevented here.
"""

import pytest
from fastapi import FastAPI, HTTPException, Query
from fastapi.testclient import TestClient
from opentelemetry.instrumentation._semconv import (
    _OpenTelemetrySemanticConventionStability as _Stability,
)
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    InMemoryMetricReader,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.metrics.view import View
from opentelemetry.semconv.attributes.http_attributes import (
    HTTP_REQUEST_METHOD,
    HTTP_RESPONSE_STATUS_CODE,
    HTTP_ROUTE,
)

from app.main import BOUNDED_HTTP_METRIC_ATTRIBUTES
from app.config import settings

# The exact query string from the audit report. The same two values the trace
# redaction test uses, so a regression that leaks either into metrics is caught by
# name here as well as on the span.
SECRET_QUERY = "t=SECRET_ASSET_TOKEN_ABC&password=hunter2"
SECRET_VALUES = ("SECRET_ASSET_TOKEN_ABC", "hunter2")

# A concrete path segment. If the concrete value ever becomes a dimension, every
# confession id becomes its own time series.
CONCRETE_ID = "12345"

# The one instrument this file is about. The pre-1.20 name was
# http.server.duration; the current name carries the same measurement in seconds.
DURATION = "http.server.request.duration"

# Dimensions that are client-controlled and therefore unbounded. Kept as names
# rather than importing the constants, so the test also fails if the semantics of
# the attribute are ever redefined underneath us.
UNBOUNDED = ("http.server_name", "http.host", "net.host.name", "net.host.port")

# The pre-1.20 HTTP attribute names. Under OTEL_SEMCONV_STABILITY_OPT_IN=http the
# instrumentation must not record any of them, and the View does not allow them, so
# these must not reach a data point.
LEGACY_HTTP_ATTRIBUTES = (
    "http.method",
    "http.status_code",
    "http.flavor",
    "http.target",
    "http.url",
    "http.scheme",
    "net.peer.ip",
)

# The View exactly as app.main builds it, from the app's own constant, so this
# cannot pass against a stale copy of the policy.
BOUNDED_VIEW = View(
    instrument_name="http.server.*",
    attribute_keys=BOUNDED_HTTP_METRIC_ATTRIBUTES,
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
    """A throwaway app whose measurements are collected in memory.

    The provider is passed to the instrumentor explicitly rather than installed
    globally: a global MeterProvider can only be set once per process, and
    `app.main` may already have set one, so an explicit provider is the only way to
    read back exactly what this app recorded.

    OTEL_SEMCONV_STABILITY_OPT_IN is pinned to the deployed value so the recorded
    instrument and attribute names do not depend on the environment the suite
    happens to run in. The cache is cleared on both sides because it is a process
    global: leaving "http" cached would silently change what any later test's
    middleware records.
    """
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "http")
    _reset_semconv_stability()
    try:
        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader], views=[BOUNDED_VIEW])

        app = FastAPI()

        @app.get("/confessions")
        def list_confessions():
            return []

        @app.get("/confessions/{confession_id}")
        def get_confession(
            confession_id: str,
            t: str = Query(default=None),
            password: str = Query(default=None),
        ):
            return {"id": confession_id, "token_seen_by_handler": t}

        @app.get("/auth/me")
        def me():
            raise HTTPException(status_code=401, detail="not authenticated")

        FastAPIInstrumentor.instrument_app(app, meter_provider=provider)
        with TestClient(app) as client:
            yield client, reader
    finally:
        _reset_semconv_stability()


def _data_points(reader, instrument_name):
    """Every data point collected for one instrument, across all resources."""
    points = []
    for resource_metrics in reader.get_metrics_data().resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == instrument_name:
                    points.extend(metric.data.data_points)
    return points


def _attribute_sets(reader, instrument_name):
    return [dict(point.attributes) for point in _data_points(reader, instrument_name)]


def test_http_server_metrics_are_produced(instrumented):
    """Registering a provider is what turns the middleware's instruments on.

    Without it the SDK hands out a no-op meter and these are all empty, which is the
    state that left Prometheus with nothing but its own internal metrics.
    """
    client, reader = instrumented

    client.get("/confessions")

    assert _data_points(reader, DURATION), f"no {DURATION} recorded"
    assert _data_points(reader, "http.server.active_requests"), (
        "no http.server.active_requests recorded"
    )


def test_the_pre_1_20_duration_instrument_is_not_produced(instrumented):
    """The old instrument must be gone, not merely out of favour.

    Under OTEL_SEMCONV_STABILITY_OPT_IN=http the middleware does not create
    http.server.duration at all. If it did, the series would sit in Prometheus
    alongside the current one holding millisecond values under http_status_code, and
    every alert rule would be reading the current name while a second, differently
    shaped histogram of the same request went unnoticed.
    """
    client, reader = instrumented

    client.get("/confessions")
    client.get("/auth/me")

    assert not _data_points(reader, "http.server.duration"), (
        "the pre-1.20 http.server.duration instrument is still being recorded"
    )


def test_duration_carries_the_dimensions_the_derivations_need(instrumented):
    """Request rate, count, error rate and latency must all be derivable.

    rate and count come from the histogram's _count, error rate from splitting on
    http.response.status_code, latency from the buckets -- so the dimensions that
    make those possible have to be on the data point rather than merely on the span.
    These are the dimensions README.md and observability/alerting-rules.yml are
    written against, so they are the contract.
    """
    client, reader = instrumented

    client.get("/confessions")
    client.get("/auth/me")

    duration = _data_points(reader, DURATION)
    assert duration, f"expected {DURATION} data points"
    # A histogram point carries the count and the bucket layout the queries need.
    assert any(point.count for point in duration)

    statuses = {s[HTTP_RESPONSE_STATUS_CODE] for s in _attribute_sets(reader, DURATION)}
    assert 200 in statuses and 401 in statuses, (
        f"status code is not a dimension, so error rate is not derivable: {statuses}"
    )

    methods = {s[HTTP_REQUEST_METHOD] for s in _attribute_sets(reader, DURATION)}
    assert methods == {"GET"}


def test_no_legacy_http_attribute_is_recorded(instrumented):
    """Modern conventions only, asserted negatively.

    Checking that the current names are present does not distinguish "modern only"
    from "both generations", so the pre-1.20 names are checked for absence
    explicitly. This is the assertion that would fail if the stability setting were
    dropped back to the instrumentation default.
    """
    client, reader = instrumented

    client.get("/confessions")
    client.get(f"/confessions/{CONCRETE_ID}?{SECRET_QUERY}")
    client.get("/auth/me")

    present = {
        key
        for attributes in _attribute_sets(reader, DURATION)
        for key in attributes
    }
    legacy = present & set(LEGACY_HTTP_ATTRIBUTES)
    assert not legacy, f"pre-1.20 dimensions still on the data point: {sorted(legacy)}"


def test_route_is_the_template_not_the_concrete_path(instrumented):
    client, reader = instrumented

    client.get(f"/confessions/{CONCRETE_ID}")

    routes = {s[HTTP_ROUTE] for s in _attribute_sets(reader, DURATION)}
    assert routes == {"/confessions/{confession_id}"}, (
        f"expected the normalised route, got {routes}"
    )
    assert all(CONCRETE_ID not in str(s) for s in _attribute_sets(reader, DURATION)), (
        "the concrete path segment became a dimension, so every id is its own series"
    )


def test_query_secrets_never_reach_metric_attributes(instrumented):
    client, reader = instrumented

    client.get(f"/confessions/{CONCRETE_ID}?{SECRET_QUERY}")

    for attributes in _attribute_sets(reader, DURATION):
        rendered = str(attributes)
        for secret in SECRET_VALUES:
            assert secret not in rendered, f"{secret!r} was exported as a metric dimension"
        assert "?" not in rendered, "a query string was exported as a metric dimension"


def test_a_varied_host_header_does_not_create_a_new_series(instrumented):
    """The reason the View exists.

    http.server_name is the raw Host header, so without the bound each distinct
    value is a separate set of time series. Asserted by counting distinct attribute
    sets: the two requests must aggregate into the same one.

    The current conventions do not put a Host-derived dimension on this instrument at
    all, so this also guards the case where a future release reintroduces one and the
    View has to be what stops it.
    """
    client, reader = instrumented

    client.get("/confessions", headers={"Host": "first.example"})
    client.get("/confessions", headers={"Host": "second.example"})

    assert _data_points(reader, DURATION)
    distinct = {
        tuple(sorted(attributes.items()))
        for attributes in _attribute_sets(reader, DURATION)
    }
    assert len(distinct) == 1, (
        f"two requests differing only in Host produced {len(distinct)} series, so the "
        "Host header is still a dimension"
    )


def test_no_unbounded_dimension_is_present(instrumented):
    client, reader = instrumented

    client.get(f"/confessions/{CONCRETE_ID}?{SECRET_QUERY}")
    client.get("/confessions", headers={"Host": "third.example"})

    present = {
        key
        for attributes in _attribute_sets(reader, DURATION)
        for key in attributes
    }
    offending = present & set(UNBOUNDED)
    assert not offending, f"unbounded dimensions present on the data point: {sorted(offending)}"


def test_the_bounded_set_is_explicit_and_excludes_the_unbounded_names():
    """Guards the policy itself, so widening it has to be a deliberate edit."""
    assert not BOUNDED_HTTP_METRIC_ATTRIBUTES & set(UNBOUNDED)
    assert {
        HTTP_REQUEST_METHOD,
        HTTP_RESPONSE_STATUS_CODE,
        HTTP_ROUTE,
    } <= BOUNDED_HTTP_METRIC_ATTRIBUTES


def test_the_bounded_set_contains_no_legacy_http_attribute():
    """The allowlist is modern-only.

    It is dead configuration while the backend runs in http mode, and dead
    configuration in a security-adjacent allowlist is worse than none: it reads as
    support for attributes the stack no longer emits, and it would silently start
    mattering again if the stability mode were ever reverted.
    """
    legacy = BOUNDED_HTTP_METRIC_ATTRIBUTES & set(LEGACY_HTTP_ATTRIBUTES)
    assert not legacy, f"pre-1.20 names still allowlisted: {sorted(legacy)}"


def test_init_metrics_registers_a_provider_and_the_bounded_view(monkeypatch):
    """The provider has to be registered globally, with the View attached.

    Two ways this can silently regress: not calling _init_metrics at all, or
    registering a provider without the View, which exports correctly-shaped but
    unbounded series. Both are checked here.
    """
    import app.main as main_module

    registered = {}
    monkeypatch.setattr(
        main_module.metrics, "set_meter_provider", lambda provider: registered.setdefault("provider", provider)
    )
    monkeypatch.setattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "otel-collector:4317")
    monkeypatch.setattr(settings, "OTEL_EXPORTER_OTLP_INSECURE", True)

    main_module._init_metrics()

    provider = registered.get("provider")
    assert isinstance(provider, MeterProvider), "no MeterProvider was registered globally"
    # SDK 1.45.0 keeps the readers on the provider; 1.25.0 exposed them as
    # _sdk_config.metric_readers, which no longer exists.
    readers = provider._metric_readers
    assert any(isinstance(r, PeriodicExportingMetricReader) for r in readers), (
        "the registered provider has no exporting reader, so nothing reaches the collector"
    )
    views = provider._sdk_config.views
    assert any(
        v._instrument_name == "http.server.*"
        and v._attribute_keys == BOUNDED_HTTP_METRIC_ATTRIBUTES
        for v in views
    ), "the bounded View is not attached to the registered provider"


def test_init_metrics_exports_to_the_configured_otlp_endpoint(monkeypatch):
    """Metrics must reuse the trace pipeline's endpoint, not a second one."""
    import app.main as main_module

    registered = {}
    monkeypatch.setattr(
        main_module.metrics, "set_meter_provider", lambda provider: registered.setdefault("provider", provider)
    )
    monkeypatch.setattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "otel-collector:4317")
    monkeypatch.setattr(settings, "OTEL_EXPORTER_OTLP_INSECURE", True)

    main_module._init_metrics()

    reader = registered["provider"]._metric_readers[0]
    exporter = reader._exporter
    assert "otel-collector:4317" in exporter._endpoint, (
        f"metric exporter points at {exporter._endpoint!r}, not the configured OTLP endpoint"
    )


def test_init_metrics_does_nothing_without_an_endpoint(monkeypatch):
    """Matches _init_tracing: with no collector configured, register nothing.

    Installing a provider pointed nowhere would leave the SDK exporting into a
    failed connection on every interval instead of staying inert.
    """
    import app.main as main_module

    registered = []
    monkeypatch.setattr(main_module.metrics, "set_meter_provider", registered.append)
    monkeypatch.setattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", None)

    main_module._init_metrics()

    assert registered == []
