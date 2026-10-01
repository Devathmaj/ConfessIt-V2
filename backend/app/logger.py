import logging
import os
import stat as stat_module
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

# SDK 1.45.x deprecates its own LoggingHandler in favour of the handler from
# opentelemetry-instrumentation-logging, and warns on import. The SDK handler is kept
# anyway: instrumentation-logging is deliberately not a dependency here, and the SDK
# handler is what puts trace_id and span_id on every exported LogRecord, which is the
# field M1's correlation depends on. Adding the package to silence a warning would
# trade a known, working log path for a new dependency and a new failure mode. The
# deprecation is currently a warning only; the handler still populates trace context.
#
# The OTLP handler ships WARNING and above only. The collector feeds Loki, which
# retains everything for its full retention period, so INFO and DEBUG stay
# console-only: anything below a warning -- including the DEBUG-only token hints
# and any identifier a future info line carries by accident -- never reaches
# retained storage. The level-by-level Grafana panel simply goes quiet at INFO;
# the WARN/ERROR panels it is actually watched through are unaffected.

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

# CA bundle cap: a PEM CA is ~1-2KB; 10KB admits chains while bounding what the
# telemetry path reads into memory before any parsing.
_OTLP_CERT_MAX_BYTES = 10 * 1024
# Symlink targets must resolve under one of these prefixes. Regular files are
# accepted anywhere (tests/dev mount certs from tmp), but a symlink escaping to
# an attacker-controlled path is refused -- this closes the check-then-open
# TOCTOU without breaking local runs that point at a tmp file.
_OTLP_CERT_SYMLINK_ALLOWED_PREFIXES = ("/certs", "/etc/otel", "/etc/ssl", "/run/secrets")


def _read_otlp_certificate(path: str) -> bytes:
    """Read the OTLP CA bundle with TOCTOU hardening.

    Realpath-confines symlink escapes to the collector cert directories, opens
    with O_NOFOLLOW where the platform offers it, fstats the open descriptor
    (not the path) and enforces a 10KB cap before reading. Raises RuntimeError
    without echoing the configured path.
    """
    try:
        realpath = os.path.realpath(path)
    except OSError as exc:
        raise RuntimeError("OTEL_EXPORTER_OTLP_CERTIFICATE could not be read") from exc
    try:
        is_link = os.path.islink(path)
    except OSError:
        is_link = False
    if is_link and not any(
        realpath == prefix or realpath.startswith(prefix.rstrip("/") + "/")
        for prefix in _OTLP_CERT_SYMLINK_ALLOWED_PREFIXES
    ):
        raise RuntimeError(
            "OTEL_EXPORTER_OTLP_CERTIFICATE must resolve under /certs "
            "(symlink escape refused)"
        )
    flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError("OTEL_EXPORTER_OTLP_CERTIFICATE could not be read") from exc
    try:
        st = os.fstat(fd)
        if stat_module.S_ISDIR(st.st_mode):
            raise RuntimeError("OTEL_EXPORTER_OTLP_CERTIFICATE is a directory")
        if st.st_size <= 0 or st.st_size > _OTLP_CERT_MAX_BYTES:
            raise RuntimeError(
                f"OTEL_EXPORTER_OTLP_CERTIFICATE size out of bounds "
                f"(max {_OTLP_CERT_MAX_BYTES} bytes)"
            )
        chunks: list[bytes] = []
        remaining = _OTLP_CERT_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > _OTLP_CERT_MAX_BYTES or not data:
            raise RuntimeError(
                "OTEL_EXPORTER_OTLP_CERTIFICATE is empty or exceeds size cap"
            )
        return data
    except OSError as exc:
        raise RuntimeError("OTEL_EXPORTER_OTLP_CERTIFICATE could not be read") from exc
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


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
    data = _read_otlp_certificate(certificate)
    return False, grpc.ssl_channel_credentials(data)

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

    _OTEL_LOG_HANDLER = LoggingHandler(level=logging.WARNING, logger_provider=provider)
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
