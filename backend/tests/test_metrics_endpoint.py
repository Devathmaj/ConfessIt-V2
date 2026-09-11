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
