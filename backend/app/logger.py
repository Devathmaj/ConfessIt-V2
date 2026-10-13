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

# Uvicorn's accepted-WebSocket handshake line includes the full path *and query
# string* at INFO. The application no longer sends an access token there -- the
# socket uses a one-use ticket -- but a ticket is still a credential, and a
# dependency or future route could put a token back in a query. Redacting the
# value at the logging boundary keeps every such line safe regardless of which
# caller built the URL. The name is kept so operators can still see which kind of
# credential was present.
_QUERY_CREDENTIAL_RE = re.compile(
    r"([?&](?:token|ticket|access_token|refresh_token)=)[^&\s\"']+",
    re.IGNORECASE,
)


def _redact_query_credentials(value):
    if isinstance(value, str):
        return _QUERY_CREDENTIAL_RE.sub(r"\1[REDACTED]", value)
    return value


class QueryCredentialRedactionFilter(logging.Filter):
    """Remove credential values from URL-shaped log messages.

    Filters are applied by the logger before handlers, so this covers both the
    console handler and any handler Uvicorn installs. It mutates the record's
    message arguments rather than formatter output so every handler sees the same
    sanitized line.
    """

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
    """Install the filter on the loggers Uvicorn uses for handshake lines.

    Called at import and again on startup: Uvicorn configures its loggers before
    importing the ASGI app, but a test harness or a future launcher may configure
    logging later, and adding the harmless filter twice is a no-op.
    """
    for name in ("uvicorn.error", "uvicorn.access"):
        target = logging.getLogger(name)
        if not any(
            isinstance(existing, QueryCredentialRedactionFilter)
            for existing in target.filters
        ):
            target.addFilter(QueryCredentialRedactionFilter())


install_query_credential_redaction()


def log_identity_hash(value: object) -> str:
    """A stable, non-reversible 8-hex-char tag for a regno or other log identifier.

    Keyed with ``SECRET_KEY`` (the pepper): identical inputs hash identically, so an
    operator can still correlate one actor's lines, but the tag reverses to nothing
    without the deployment secret. Eight hex chars (32 bits) is enough to join on and
    too short to brute-force a high-entropy client id; regnos are lower-entropy, which
    is why the pepper -- not just the hash -- carries the protection against dictionary
    enumeration.
    """
    return hashlib.sha256(
        f"{value}:{settings.SECRET_KEY}".encode("utf-8")
    ).hexdigest()[:8]


def redact_log_identifiers(message: object, *identifiers: object) -> str:
    """Replace named identifiers embedded in an exception or log message with tags.

    Upstream errors are the hard case: Cloudinary's text can repeat the public_id it
    failed on, and a public_id contains the owner's regno. Passing both the message and
    the values that must not survive produces a line that is still diagnosable by hash
    while containing no raw identifier. Longer values go first so a public_id is tagged
    as a whole rather than being partially rewritten through the regno inside it.
    """
    text = str(message)
    for identifier in sorted(
        {str(value) for value in identifiers if value},
        key=len,
        reverse=True,
    ):
        text = text.replace(identifier, f"[id:{log_identity_hash(identifier)}]")
    return text


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
