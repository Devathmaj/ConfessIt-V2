# tests/test_metrics_endpoint.py

"""The /metrics endpoint must be scrapeable, and must not shadow the OTLP path.

Prometheus cannot observe a pushed pipeline. The backend's application metrics reach
Prometheus by way of the collector, so nothing in that path says whether the backend
process is alive -- the collector is healthy whether or not anything is feeding it. This
endpoint exists to close that gap, and it is what makes up{job="confessit-backend"} a
statement about the application rather than about its collector.

Two things therefore have to hold, and they pull in opposite directions:

  * the body must parse as a Prometheus exposition, or Prometheus marks the scrape
    failed and reports up=0 for a process that is serving perfectly. A 200 alone is
    not enough, which is why these tests parse the body rather than only checking the
    status code;
  * the body must not contain the OTel instruments. The application metrics already
    reach Prometheus over OTLP; serving a second copy here would put every http_server_*
    series in the database twice under different job labels, and any RED query that
    did not happen to pin exported_job would count each request twice. That is a worse
    defect than the one the endpoint is solving, so it is asserted against rather than
    left to a comment.

The exposition is hand-written, so the format assertions here are what keep it honest.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import _METRICS_CONTENT_TYPE, _metrics_exposition

# Series the OTLP path already delivers. If any of these appear in the scrape body the
# endpoint has started duplicating the push pipeline.
OTLP_ONLY_SERIES = (
    "http_server_request_duration_seconds",
    "http_server_active_requests",
    "http_server_request_body_size_bytes",
    "http_server_response_body_size_bytes",
)


def _parse_exposition(body: str):
    """Parse the exposition the way Prometheus does, and return the sample values.

    Deliberately strict about the two things a scrape depends on: every non-comment
    line must be ``name value`` or ``name{labels} value``, and the float must parse.
    A looser check would pass on a body Prometheus rejects.
    """
    samples = {}
    types = {}
    helps = set()
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# HELP "):
            helps.add(line[len("# HELP "):].split(" ", 1)[0])
            continue
        if line.startswith("# TYPE "):
            name, _, kind = line[len("# TYPE "):].partition(" ")
            types[name] = kind
            continue
        if line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        assert name, "sample line has no name: %r" % raw
        samples[name] = float(value)
    return samples, types, helps


def test_metrics_endpoint_responds(client: TestClient):
    response = client.get("/metrics")
    assert response.status_code == 200


def test_metrics_declares_the_exposition_content_type(client: TestClient):
    # Prometheus dispatches on this media type. A JSON or default-text response would
    # either fail to parse or be read as plain log text, and the scrape would be marked
    # failed, so up=0 for a healthy process: the exact failure this endpoint is for.
    response = client.get("/metrics")
    content_type = response.headers["content-type"]
    assert content_type == _METRICS_CONTENT_TYPE
    assert "version=0.0.4" in content_type
    # Starlette appends a charset to text/* responses; stating it in the media type
    # already means there must be exactly one.
    assert content_type.count("charset=") == 1


def test_metrics_body_parses_as_a_prometheus_exposition(client: TestClient):
    body = client.get("/metrics").text
    samples, types, helps = _parse_exposition(body)
    assert samples, "no samples in the exposition"
    # Every sample needs a declared type, or Prometheus rejects the scrape.
    for name in samples:
        assert name in types, "%s has no # TYPE" % name
        assert name in helps, "%s has no # HELP" % name


def test_metrics_reports_liveness(client: TestClient):
    samples, types, _ = _parse_exposition(client.get("/metrics").text)
    assert samples["confessit_backend_up"] == 1
    assert types["confessit_backend_up"] == "gauge"


def test_metrics_start_time_is_a_plausible_epoch(client: TestClient):
    # Not NaN, not zero, not in the future: a start time that silently resets is
    # exactly the thing this series exists to make visible.
    import time

    samples, types, _ = _parse_exposition(client.get("/metrics").text)
    start = samples["confessit_backend_start_time_seconds"]
    assert types["confessit_backend_start_time_seconds"] == "gauge"
    assert start > 1_600_000_000, "start time predates 2020: %r" % start
    assert start <= time.time() + 1, "start time is in the future: %r" % start


def test_metrics_does_not_duplicate_the_otlp_pipeline(client: TestClient):
    # The one assertion that protects the OTLP path from this endpoint. If a
    # PrometheusMetricReader is ever attached to the same MeterProvider, or a series is
    # added here by hand, this fails rather than silently doubling every RED query.
    body = client.get("/metrics").text
    for name in OTLP_ONLY_SERIES:
        assert name not in body, (
            "%s is served here and already arrives over OTLP; the same series under two "
            "job labels double-counts" % name
        )


def test_metrics_carries_no_high_cardinality_labels():
    # A label on a liveness gauge is a way to mint a stream per value. The exposition is
    # bare name/value on purpose, so this holds structurally: there is no label syntax
    # to abuse. Asserted so that adding one later is a deliberate act.
    body = _metrics_exposition()
    assert "{" not in body, "the liveness exposition must stay unlabelled"


def test_metrics_is_absent_from_the_openapi_schema(client: TestClient):
    # Scrape endpoints are not part of the public API surface and should not show up in
    # the published schema alongside the application's real routes.
    paths = client.get("/openapi.json").json()["paths"]
    assert "/metrics" not in paths


# ---------------------------------------------------------------------------
# The scrape must not instrument itself
# ---------------------------------------------------------------------------
#
# Prometheus scrapes /metrics every 15s. If that request is instrumented like any other,
# the monitoring system becomes the busiest thing in the application: measured live at
# 92% of all request rate and ~3,800 spans/day, with Tempo's search results filled
# entirely with `GET /metrics`. Every "top routes" and "request rate" view on the
# operator dashboard then describes the scrape rather than the traffic it exists to
# observe. So the exclusion is asserted here, because a pattern that stops matching
# leaves the noise behind with nothing to indicate it.
#
# These are the tests that would have caught the first attempt, which used the
# intuitive `^/metrics/?$`. 0.66b0 does not match that argument against the path: it
# splits on commas, joins the parts into one alternation, and `re.search`es the result
# against the full `scheme://host:port/path?query` string. Anchored at the start of
# that string, the pattern can never match anything, and the endpoint looked excluded
# while still being fully instrumented.


def test_excluded_urls_pattern_excludes_the_liveness_path():
    # Asserted against the SDK's own matcher rather than a local reimplementation, so
    # this keeps meaning whatever `excluded_urls` does in the installed version.
    from opentelemetry.instrumentation.asgi import parse_excluded_urls

    from app.main import EXCLUDED_INSTRUMENTED_URLS

    excluded = parse_excluded_urls(EXCLUDED_INSTRUMENTED_URLS)
    assert excluded.url_disabled("http://backend:8001/metrics")
    assert excluded.url_disabled("http://backend:8001/metrics/")
    assert excluded.url_disabled("http://127.0.0.1:8001/metrics")
    assert excluded.url_disabled("http://backend/metrics")
    assert excluded.url_disabled("http://backend:8001/metrics?probe=1")


def test_excluded_urls_pattern_spares_everything_else():
    # The over-matching failure is as bad as the under-matching one: swallowing real
    # application routes would silently delete them from the metrics. These are the
    # near-misses a looser pattern gets wrong.
    from opentelemetry.instrumentation.asgi import parse_excluded_urls

    from app.main import EXCLUDED_INSTRUMENTED_URLS

    excluded = parse_excluded_urls(EXCLUDED_INSTRUMENTED_URLS)
    for url in (
        "http://backend:8001/api/metrics",
        "http://backend:8001/metricsomething",
        "http://backend:8001/confessions",
        "http://backend:8001/confessions/total_count",
        "http://backend:8001/api/profile/files/photo.png",
        "http://backend:8001/",
    ):
        assert not excluded.url_disabled(url), "%s must stay instrumented" % url


@pytest.fixture
def scrape_and_traffic_spans():
    """A throwaway app instrumented exactly as app.main is, with spans kept in memory.

    Built on its own TracerProvider rather than the process-wide one so the assertions
    read only the spans this app produced. It carries both a /metrics route and an
    ordinary route, because the two questions are only meaningful together: does the
    scrape get excluded, and does everything else still get instrumented?

    Reuses app.main's real EXCLUDED_INSTRUMENTED_URLS rather than a copy, so a change to
    the constant in the application is what these tests actually exercise.
    """
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from starlette.responses import PlainTextResponse

    from app.main import EXCLUDED_INSTRUMENTED_URLS, _metrics_exposition

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    app = FastAPI()

    @app.get("/metrics", include_in_schema=False)
    def read_metrics():
        return PlainTextResponse(
            _metrics_exposition(), media_type="text/plain; version=0.0.4"
        )

    @app.get("/confessions")
    def list_confessions():
        return {"confessions": []}

    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=provider, excluded_urls=EXCLUDED_INSTRUMENTED_URLS
    )
    with TestClient(app) as test_client:
        yield test_client, exporter


def test_scraping_metrics_emits_no_span(scrape_and_traffic_spans):
    # The behavioural version of the two pattern tests above: make the request the way
    # Prometheus does and assert the exporter saw nothing.
    client, exporter = scrape_and_traffic_spans
    assert client.get("/metrics").status_code == 200
    assert exporter.get_finished_spans() == (), (
        "scraping /metrics produced spans; it is excluded from instrumentation so the "
        "scrape does not dominate the traffic metrics"
    )


def test_a_real_request_is_still_instrumented(scrape_and_traffic_spans):
    # The control for the test above. Without it, a pattern that excluded the entire
    # application would pass both.
    client, exporter = scrape_and_traffic_spans
    assert client.get("/confessions").status_code == 200
    assert exporter.get_finished_spans(), "a normal request must still produce a span"
