"""Database queries must emit spans on the installed SQLAlchemy.

The contrib SQLAlchemy instrumentor refuses versions >= 2.1, which used to mean
every query ran unattributed with only a boot warning to show for it. These
tests pin the replacement contract: whichever implementation the version gate
selects, a query produces exactly one CLIENT span (ERRORed on failure) carrying
the db.* attributes the dashboards aggregate by.
"""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from app.telemetry import (
    _operation_name,
    instrument_database_engine,
    sqlalchemy_instrumentation,
)


@pytest.fixture
def traced():
    """A throwaway engine wired to an in-memory span exporter.

    SQLite keeps the test hermetic (no database, no pool); the cursor hooks
    under test are dialect-agnostic. The provider is passed explicitly so the
    global tracer provider -- shared by the whole suite -- is never touched.
    """
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    engine = create_engine("sqlite://", poolclass=NullPool)
    assert instrument_database_engine(engine, tracer_provider=provider) == "manual"
    try:
        yield engine, exporter, provider
    finally:
        engine.dispose()


def test_gate_routes_the_installed_sqlalchemy_to_manual():
    import sqlalchemy

    major, minor = (int(part) for part in sqlalchemy.__version__.split(".")[:2])
    assert (major, minor) >= (2, 1)
    assert sqlalchemy_instrumentation() == "manual"


def test_successful_query_emits_one_client_span(traced):
    engine, exporter, provider = traced
    parent_tracer = provider.get_tracer("test-parent")
    with parent_tracer.start_as_current_span("request"):
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    queries = [span for span in exporter.get_finished_spans() if span.name == "SELECT"]
    assert len(queries) == 1
    (span,) = queries
    assert span.kind == SpanKind.CLIENT
    assert span.status.status_code == StatusCode.UNSET
    assert span.attributes["db.system"] == "sqlite"
    assert "SELECT 1" in span.attributes["db.statement"]


def test_failing_query_ends_an_error_span(traced):
    engine, exporter, _provider = traced
    with engine.connect() as connection:
        with pytest.raises(Exception):
            connection.execute(text("SELECT * FROM no_such_table"))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    (span,) = spans
    assert span.name == "SELECT"
    assert span.status.status_code == StatusCode.ERROR
    assert span.events, "the database error must be recorded on the span"


def test_query_span_nests_under_the_ambient_span(traced):
    # Trace correlation depends on this: a db span that does not share the
    # request span's trace id cannot be joined to it in Tempo.
    engine, exporter, provider = traced
    parent_tracer = provider.get_tracer("test-parent")
    with parent_tracer.start_as_current_span("request") as parent:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    (span,) = [span for span in exporter.get_finished_spans() if span.name == "SELECT"]
    assert span.parent is not None
    assert span.parent.trace_id == parent.get_span_context().trace_id


def test_double_instrumentation_still_emits_one_span(traced):
    engine, exporter, _provider = traced
    assert instrument_database_engine(engine) == "manual"
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))

    assert len(exporter.get_finished_spans()) == 1


def test_operation_name_never_carries_statement_text():
    assert _operation_name("select * from users") == "SELECT"
    assert _operation_name("  INSERT INTO users (a) VALUES (1)") == "INSERT"
    assert _operation_name("/* comment */ SELECT 1") == "DB"
    assert _operation_name("") == "DB"
