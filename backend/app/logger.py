import logging
import os
import sys

import grpc
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource

# Correctly define the formatter
FORMATTER = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

_OTEL_LOG_HANDLER = None

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

    Returns (insecure, credentials) for the gRPC exporters. Plaintext is the default
    because the local Compose collector presents no certificate; a collector on another
    host requires OTEL_EXPORTER_OTLP_INSECURE=false, and the CA named by
    OTEL_EXPORTER_OTLP_CERTIFICATE is loaded to verify it, so telemetry is never
    exported over TLS while trusting an unverified collector.
    """
    insecure = os.getenv("OTEL_EXPORTER_OTLP_INSECURE", "true").lower() in {"1", "true", "yes"}
    if insecure:
        return True, None

    certificate = os.getenv("OTEL_EXPORTER_OTLP_CERTIFICATE")
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

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return None

    service_name = os.getenv("OTEL_SERVICE_NAME", "confessit-backend")
    resource = Resource.create({"service.name": service_name})

    insecure, credentials = otlp_transport()

    provider = LoggerProvider(resource=resource)
    exporter = OTLPLogExporter(endpoint=endpoint, insecure=insecure, credentials=credentials)
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))

    _OTEL_LOG_HANDLER = LoggingHandler(level=logging.INFO, logger_provider=provider)
    return _OTEL_LOG_HANDLER

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
