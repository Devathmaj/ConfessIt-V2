import logging
from .config import settings
import sys

import grpc
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource

# Correctly define the formatter
FORMATTER = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

_OTEL_LOG_HANDLER = None

# SDK 1.45.0 deprecates its own LoggingHandler in favour of the handler from
# opentelemetry-instrumentation-logging, and warns on import. The SDK handler is kept
# anyway: instrumentation-logging is deliberately not a dependency here, and the SDK
# handler is what puts trace_id and span_id on every exported LogRecord, which is the
# field M1's correlation depends on. Adding the package to silence a warning would
# trade a known, working log path for a new dependency and a new failure mode. The
# deprecation is currently a warning only; the handler still populates trace context.

def get_console_handler():
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(FORMATTER)
    return console_handler

# There is deliberately no file handler. This application used to write every record
# to my_app.log as well as to stdout and to OTLP, and promtail scraped both the file
# and the Docker host, so one log line reached Loki three times with differing
# content. The OTLP handler below is now the only path into Loki, and the console
# handler exists for `docker compose logs` rather than as an ingestion route --
# nothing scrapes stdout any more. Adding a file handler back would reintroduce the
# duplicate, so a log-on-disk requirement should be met by the collector's own
# export rather than by a second copy of the application log.

def otlp_transport():
    """Resolve OTLP transport security from the environment.

    Returns (insecure, credentials) for the gRPC exporters. Secure is the default:
    telemetry carries registration numbers, IPs and session identifiers, so a remote
    collector must present TLS and the CA named by OTEL_EXPORTER_OTLP_CERTIFICATE is
    loaded to verify it -- telemetry is never exported over TLS while trusting an
    unverified collector, and never plaintext unless OTEL_EXPORTER_OTLP_INSECURE is
    set back to true explicitly (which is what the local Compose collector, serving
    plaintext on its own network, does).
    """
    insecure = str(settings.OTEL_EXPORTER_OTLP_INSECURE).lower() in {"1", "true", "yes"}
    if insecure:
        return True, None

    certificate = settings.OTEL_EXPORTER_OTLP_CERTIFICATE
    if not certificate:
        raise RuntimeError(
            "OTEL_EXPORTER_OTLP_INSECURE is false but OTEL_EXPORTER_OTLP_CERTIFICATE is unset; "
            "refusing to send telemetry with no way to verify the collector"
        )
    try:
        with open(certificate, "rb") as handle:
            return False, grpc.ssl_channel_credentials(handle.read())
    except OSError as exc:
        raise RuntimeError(f"OTEL_EXPORTER_OTLP_CERTIFICATE could not be read: {certificate}") from exc

def _get_otel_log_handler():
    global _OTEL_LOG_HANDLER
    if _OTEL_LOG_HANDLER is not None:
        return _OTEL_LOG_HANDLER

    endpoint = settings.OTEL_EXPORTER_OTLP_ENDPOINT
    if not endpoint:
        return None

    service_name = settings.OTEL_SERVICE_NAME
    resource = Resource.create({"service.name": service_name})

    insecure, credentials = otlp_transport()

    provider = LoggerProvider(resource=resource)
    exporter = OTLPLogExporter(endpoint=endpoint, insecure=insecure, credentials=credentials)
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))

    _OTEL_LOG_HANDLER = LoggingHandler(level=logging.INFO, logger_provider=provider)
    return _OTEL_LOG_HANDLER

def get_console_only_logger(logger_name):
    """Return a logger that writes to stdout and to nothing else.

    The same logger is used for one purpose: emitting a freshly minted magic-link
    token to the developer's terminal during local debugging. get_logger() attaches
    an OTLP handler alongside the console one, so anything it records is exported to
    the collector and retained in Loki for the full retention period, which is the
    wrong place for a live bearer credential. This logger has no OTLP handler and
    never acquires one, so the token exists only for as long as the terminal does.
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        logger.addHandler(get_console_handler())
    logger.propagate = False
    return logger

def get_logger(logger_name):
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)  # Set the lowest logging level

    # Add handlers only if they haven't been added already
    if not logger.handlers:
        logger.addHandler(get_console_handler())
        otel_handler = _get_otel_log_handler()
        if otel_handler:
            logger.addHandler(otel_handler)
    
    # Prevent log messages from being propagated to the root logger
    logger.propagate = False

    return logger
