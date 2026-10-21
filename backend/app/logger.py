import hashlib
import logging
import os
import re
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

# Redact credential query values from Uvicorn WebSocket logs while keeping keys
# visible for diagnosis, including credentials added by future routes.
_QUERY_CREDENTIAL_RE = re.compile(
    r"([?&](?:token|ticket|access_token|refresh_token)=)[^&\s\"']+",
    re.IGNORECASE,
)


def _redact_query_credentials(value):
    if isinstance(value, str):
        return _QUERY_CREDENTIAL_RE.sub(r"\1[REDACTED]", value)
    return value


class QueryCredentialRedactionFilter(logging.Filter):
    """Redact credential values before any logging handler receives the record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact_query_credentials(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_query_credentials(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                key: _redact_query_credentials(value)
                for key, value in record.args.items()
            }
        return True


def install_query_credential_redaction() -> None:
    """Install the idempotent query-credential filter on Uvicorn loggers."""
    for name in ("uvicorn.error", "uvicorn.access"):
        target = logging.getLogger(name)
        if not any(
            isinstance(existing, QueryCredentialRedactionFilter)
            for existing in target.filters
        ):
            target.addFilter(QueryCredentialRedactionFilter())


install_query_credential_redaction()


def log_identity_hash(value: object) -> str:
    """Return a stable, peppered 8-hex tag for correlating log identifiers."""
    return hashlib.sha256(
        f"{value}:{settings.SECRET_KEY}".encode("utf-8")
    ).hexdigest()[:8]


def redact_log_identifiers(message: object, *identifiers: object) -> str:
    """Replace supplied identifiers with tags, longest first to avoid partial matches."""
    text = str(message)
    for identifier in sorted(
        {str(value) for value in identifiers if value},
        key=len,
        reverse=True,
    ):
        text = text.replace(identifier, f"[id:{log_identity_hash(identifier)}]")
    return text


_OTEL_LOG_HANDLER = None

# Keep the SDK handler for trace/span IDs without adding another dependency.
# Export WARNING+ to Loki; lower levels stay console-only to limit retained data.

def get_console_handler():
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(FORMATTER)
    return console_handler

# No file handler: OTLP is the sole Loki path, while stdout serves container logs.

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

    For emitting a freshly minted magic-link token to the developer's terminal
    during local debugging: get_logger() attaches an OTLP handler alongside the
    console one, which would export and retain a live bearer credential in Loki
    for the full retention period. This logger has no OTLP handler and never
    acquires one, so the token exists only for as long as the terminal does.
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
