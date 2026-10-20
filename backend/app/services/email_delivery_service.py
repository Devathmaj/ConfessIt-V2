"""Queue magic-link login emails through the Azure Functions enqueue endpoint.

The backend deliberately knows nothing about Azure Storage Queue or Azure
Communication Services. It posts the generated link, recipient, name and type
to the configured ``EMAIL_FUNCTION_URL`` (``POST /api/email`` by default) and
trusts the Function App to enqueue the request. A successful response therefore
means "accepted for asynchronous delivery", never "the recipient has the email".
"""

from __future__ import annotations

import httpx

from ..config import settings
from ..logger import get_logger, log_identity_hash

logger = get_logger(__name__)

# The enqueue call is part of the login request, so it must not hold the sign-in
# response open for long. Five seconds is ample for a local Functions endpoint
# and leaves room for the Functions host to fail fast.
EMAIL_REQUEST_TIMEOUT_SECONDS = 5.0


def _configured_url() -> str | None:
    """The configured enqueue URL, or None when the pipeline is not set up."""
    value = (settings.EMAIL_FUNCTION_URL or "").strip()
    return value or None


async def enqueue_login_email(
    *, link: str, email: str, name: str, regno: str
) -> bool:
    """Submit one login email request to the Functions queue endpoint.

    Returns True only when the endpoint answered with a 2xx. Any timeout,
    connection failure, non-2xx response, or unexpected client error returns
    False and is logged with safe diagnostics -- the raw magic link, recipient
    address and registration number are never written to logs. The caller keeps
    its existing uniform login response either way, so this is not a
    "was the account real?" signal.
    """
    url = _configured_url()
    regno_hash = log_identity_hash(regno)
    if url is None:
        logger.error(
            "Login email was not queued: EMAIL_FUNCTION_URL is not configured "
            "(regno_hash=%s)",
            regno_hash,
        )
        return False

    payload = {
        "link": link,
        "email": email,
        "name": name,
        "regno": regno,
        "type": "login",
    }

    try:
        timeout = httpx.Timeout(EMAIL_REQUEST_TIMEOUT_SECONDS)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)
    except httpx.TimeoutException:
        logger.error(
            "Login email was not queued: request timed out after %ss (regno_hash=%s)",
            EMAIL_REQUEST_TIMEOUT_SECONDS,
            regno_hash,
        )
        return False
    except httpx.RequestError as exc:
        logger.error(
            "Login email was not queued: request failed (error_type=%s, regno_hash=%s)",
            type(exc).__name__,
            regno_hash,
        )
        return False
    except Exception as exc:  # never let an email failure break sign-in
        logger.error(
            "Login email was not queued: unexpected client error "
            "(error_type=%s, regno_hash=%s)",
            type(exc).__name__,
            regno_hash,
        )
        return False

    if not 200 <= response.status_code < 300:
        logger.warning(
            "Login email was not queued: endpoint rejected the request "
            "(status_code=%s, regno_hash=%s)",
            response.status_code,
            regno_hash,
        )
        return False

    logger.info(
        "Login magic-link email accepted for queueing "
        "(status_code=%s, regno_hash=%s)",
        response.status_code,
        regno_hash,
    )
    return True
