"""Azure Functions app for the magic-link email pipeline.

Two functions share this app:

* ``enqueue_email`` (``POST /api/email``) validates a backend request and writes
  it to Azure Storage Queue. It never sends email.
* ``process_email`` is a Storage Queue trigger that reads one message, validates
  it again, and sends the login email through Azure Communication Services.

Queue settings in ``host.json`` deliberately use ``batchSize: 1``. That is the
strongest sequential-processing configuration the Storage Queue trigger offers
in a local single-instance run, but it is not a FIFO guarantee: Storage Queue is
at-least-once, retries and visibility timeouts can reorder or duplicate
messages, and a scaled-out Function App can run more than one worker.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import re
import threading
import uuid
from collections.abc import Mapping
from typing import Any

import azure.functions as func
from azure.core.exceptions import AzureError, ResourceExistsError
from azure.storage.queue import QueueClient

from email_delivery import send_login_email
from email_validation import InvalidEmailRequest, validate_email_request

logger = logging.getLogger(__name__)

# The queue trigger binding needs the queue name at import time. The Functions
# host loads local.settings.json (or application settings) into the environment
# before importing this module; failing here is clearer than indexing a function
# against a queue nobody configured.
QUEUE_CONNECTION_SETTING = "EMAIL_QUEUE_CONNECTION_STRING"
QUEUE_NAME_SETTING = "EMAIL_QUEUE_NAME"
_QUEUE_NAME = (os.environ.get(QUEUE_NAME_SETTING) or "").strip()
if not _QUEUE_NAME:
    raise RuntimeError(f"{QUEUE_NAME_SETTING} must be configured for the queue trigger.")

# Storage Queue messages are capped at 64 KiB. The validated fields are much
# smaller, but checking the serialized body keeps a future field from producing
# an infrastructure error after validation has already succeeded.
_QUEUE_MESSAGE_LIMIT_BYTES = 64 * 1024

# create_queue is idempotent only by exception, so remember the one successful
# check rather than paying a round trip on every request.
_queue_initialized = False
_queue_initialization_lock = threading.Lock()

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


def _required_setting(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} must be configured.")
    return value


def _get_queue_client() -> QueueClient:
    """Build a queue client, creating the queue idempotently on first use."""
    global _queue_initialized
    queue_name = _required_setting(QUEUE_NAME_SETTING)
    connection_string = _required_setting(QUEUE_CONNECTION_SETTING)
    # The Python queue SDK sends UTF-8 text by default. host.json sets
    # messageEncoding=none so the trigger reads that text directly instead of
    # trying (and failing) to base64-decode it. Keep the two in sync.
    client = QueueClient.from_connection_string(
        conn_str=connection_string, queue_name=queue_name
    )

    if not _queue_initialized:
        with _queue_initialization_lock:
            if not _queue_initialized:
                try:
                    client.create_queue()
                except ResourceExistsError:
                    # Another request, a previous run, or the developer created
                    # it first. The queue is ready either way.
                    pass
                _queue_initialized = True
    return client


def _json_response(payload: dict[str, Any], *, status_code: int) -> func.HttpResponse:
    return func.HttpResponse(
        body=json.dumps(payload, separators=(",", ":")),
        status_code=status_code,
        mimetype="application/json",
        headers={"Cache-Control": "no-store"},
    )


def _error_response(status_code: int, error: str, message: str) -> func.HttpResponse:
    return _json_response(
        {"error": error, "message": message},
        status_code=status_code,
    )


def _error_code(exc: BaseException) -> str:
    """The safe Azure error code, never the exception text or request payload."""
    value = getattr(exc, "error_code", None)
    return str(value)[:64] if value else "unknown"


def _safe_log_value(value: Any, *, max_length: int = 64) -> str:
    """Bound untrusted identifiers and strip log-injection control characters."""
    text = str(value or "")
    text = "".join("?" if (char.isspace() or ord(char) < 32) else char for char in text)
    return text[:max_length]


def _parse_enqueued_at(payload: Mapping[str, Any]) -> datetime | None:
    value = payload.get("enqueued_at")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _safe_request_id(payload: Mapping[str, Any]) -> str:
    """Return the enqueue-generated request id, if it has the expected shape.

    The value is logged, so it is restricted to the 32-hex-character UUID form
    ``enqueue_email`` writes. A crafted queue message cannot smuggle a link or
    address into the log line through this field.
    """
    value = payload.get("request_id")
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value):
        return value
    return ""


def _queue_delay_seconds(
    payload: Mapping[str, Any], message: func.QueueMessage
) -> float | None:
    """Seconds between enqueue and processing, when a trustworthy timestamp exists."""
    # The host's insertion time is authoritative. The payload timestamp is only
    # a fallback for a host version that does not supply metadata.
    enqueued_at = getattr(message, "insertion_time", None)
    if isinstance(enqueued_at, datetime) and enqueued_at.tzinfo is None:
        enqueued_at = enqueued_at.replace(tzinfo=timezone.utc)
    if not isinstance(enqueued_at, datetime):
        enqueued_at = _parse_enqueued_at(payload)
    if not isinstance(enqueued_at, datetime):
        return None
    return round((datetime.now(timezone.utc) - enqueued_at).total_seconds(), 3)


@app.function_name(name="enqueue_email")
@app.route(route="email", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def enqueue_email(req: func.HttpRequest) -> func.HttpResponse:
    """Validate one login email request and queue it for asynchronous delivery."""
    try:
        payload = req.get_json()
    except ValueError:
        return _error_response(
            400,
            "invalid_json",
            "The request body must be valid JSON.",
        )

    try:
        request = validate_email_request(payload)
    except InvalidEmailRequest as exc:
        return _error_response(400, "invalid_request", str(exc))

    request_id = uuid.uuid4().hex
    queued_request = {
        "schema_version": 1,
        "request_id": request_id,
        "type": request.type,
        "link": request.link,
        "email": request.email,
        "name": request.name,
        "regno": request.regno,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    message_body = json.dumps(queued_request, separators=(",", ":"), ensure_ascii=False)

    if len(message_body.encode("utf-8")) > _QUEUE_MESSAGE_LIMIT_BYTES:
        return _error_response(
            413,
            "email_request_too_large",
            "The email request is too large to queue.",
        )

    try:
        with _get_queue_client() as queue:
            queued_message = queue.send_message(message_body)
    except AzureError as exc:
        logger.error(
            "email_enqueue_failed request_id=%s error_type=%s error_code=%s",
            request_id,
            type(exc).__name__,
            _error_code(exc),
        )
        return _error_response(
            503,
            "email_queue_unavailable",
            "The email request could not be queued. Please try again.",
        )
    except RuntimeError as exc:
        # Missing EMAIL_QUEUE_* configuration is an operator error, not a
        # transient service failure. Log the variable name only, never a value.
        logger.error(
            "email_enqueue_not_configured request_id=%s error_type=%s",
            request_id,
            type(exc).__name__,
        )
        return _error_response(
            500,
            "email_pipeline_not_configured",
            "The email pipeline is not configured.",
        )
    except Exception as exc:
        logger.error(
            "email_enqueue_failed request_id=%s error_type=%s error_code=%s",
            request_id,
            type(exc).__name__,
            _error_code(exc),
        )
        return _error_response(
            500,
            "email_enqueue_failed",
            "The email request could not be queued.",
        )

    logger.info(
        "email_request_queued request_id=%s queue_message_id=%s",
        request_id,
        _safe_log_value(getattr(queued_message, "id", None)),
    )
    return _json_response(
        {"status": "queued", "request_id": request_id},
        status_code=202,
    )


@app.function_name(name="process_email")
@app.queue_trigger(
    arg_name="message",
    queue_name=_QUEUE_NAME,
    connection=QUEUE_CONNECTION_SETTING,
)
def process_email(message: func.QueueMessage) -> None:
    """Process exactly one queued email request.

    Validation failures are acknowledged and dropped: retrying a permanently
    malformed message only repeats the same failure. Send failures are re-raised
    so the Functions runtime retries the message and eventually moves it to the
    queue's poison queue after ``maxDequeueCount``.
    """
    message_id = _safe_log_value(getattr(message, "id", None))
    dequeue_count = getattr(message, "dequeue_count", None)

    try:
        payload = message.get_json()
    except (TypeError, ValueError):
        logger.error(
            "email_message_rejected message_id=%s dequeue_count=%s reason=malformed_json",
            message_id,
            dequeue_count,
        )
        return

    try:
        request = validate_email_request(payload)
    except InvalidEmailRequest as exc:
        logger.error(
            "email_message_rejected message_id=%s dequeue_count=%s reason=%s",
            message_id,
            dequeue_count,
            _safe_log_value(exc, max_length=200),
        )
        return

    # From here on the payload is a login request. ``request_id`` is generated
    # by enqueue_email; fall back to the host's message id for older messages.
    request_id = _safe_request_id(payload) or message_id
    delay = _queue_delay_seconds(payload, message)
    logger.info(
        "email_process_start message_id=%s request_id=%s dequeue_count=%s "
        "queue_delay_seconds=%s",
        message_id,
        request_id,
        dequeue_count,
        delay,
    )

    try:
        send_login_email(request)
    except Exception as exc:
        logger.error(
            "email_send_failed message_id=%s request_id=%s dequeue_count=%s "
            "error_type=%s error_code=%s",
            message_id,
            request_id,
            dequeue_count,
            type(exc).__name__,
            _error_code(exc),
        )
        # Never acknowledge a failed send. The runtime owns retry/poison.
        raise

    logger.info(
        "email_send_succeeded message_id=%s request_id=%s dequeue_count=%s",
        message_id,
        request_id,
        dequeue_count,
    )
