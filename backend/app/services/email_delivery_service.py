"""Queue login, notification and blocked-account emails through the Functions endpoint.

The backend deliberately knows nothing about Azure Storage Queue or Azure
Communication Services: it posts the composed request (login link, or
notification title/body/link, or the blocked-account administrator contact,
plus recipient name and type) to ``EMAIL_FUNCTION_URL`` (``POST /api/email``
by default) and trusts the Function App to enqueue it. A successful response
therefore means "accepted for asynchronous delivery", never "the recipient
has the email".
"""

from __future__ import annotations

import httpx

from ..config import settings
from ..logger import get_logger, log_identity_hash

logger = get_logger(__name__)

# The enqueue call is part of a login or notification-producing request, so it
# must not hold the caller open for long; five seconds is ample for a local
# Functions endpoint and lets the host fail fast.
EMAIL_REQUEST_TIMEOUT_SECONDS = 5.0


def _configured_url() -> str | None:
    """The configured enqueue URL, or None when the pipeline is not set up."""
    value = (settings.EMAIL_FUNCTION_URL or "").strip()
    return value or None


async def _enqueue_async_email(
    payload: dict[str, str],
    *,
    kind: str,
    regno: str,
    success_message: str,
) -> bool:
    """Post one already-composed request and keep every failure non-fatal.

    ``kind`` is the safe log label for this message; it never contains the
    recipient address, link, name or registration number.
    """
    url = _configured_url()
    regno_hash = log_identity_hash(regno)
    if url is None:
        logger.error(
            "%s was not queued: EMAIL_FUNCTION_URL is not configured "
            "(regno_hash=%s)",
            kind,
            regno_hash,
        )
        return False

    try:
        timeout = httpx.Timeout(EMAIL_REQUEST_TIMEOUT_SECONDS)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)
    except httpx.TimeoutException:
        logger.error(
            "%s was not queued: request timed out after %ss (regno_hash=%s)",
            kind,
            EMAIL_REQUEST_TIMEOUT_SECONDS,
            regno_hash,
        )
        return False
    except httpx.RequestError as exc:
        logger.error(
            "%s was not queued: request failed (error_type=%s, regno_hash=%s)",
            kind,
            type(exc).__name__,
            regno_hash,
        )
        return False
    except Exception as exc:  # never let an email failure break the caller
        logger.error(
            "%s was not queued: unexpected client error "
            "(error_type=%s, regno_hash=%s)",
            kind,
            type(exc).__name__,
            regno_hash,
        )
        return False

    if not 200 <= response.status_code < 300:
        logger.warning(
            "%s was not queued: endpoint rejected the request "
            "(status_code=%s, regno_hash=%s)",
            kind,
            response.status_code,
            regno_hash,
        )
        return False

    logger.info(
        "%s (status_code=%s, regno_hash=%s)",
        success_message,
        response.status_code,
        regno_hash,
    )
    return True


async def enqueue_login_email(
    *, link: str, email: str, name: str, regno: str
) -> bool:
    """Submit one login email request to the Functions queue endpoint.

    Returns True only when the endpoint answered with a 2xx. Any failure
    returns False and is logged with safe diagnostics -- the raw magic link,
    recipient address and registration number are never written to logs. The
    caller keeps its existing uniform login response either way, so this is
    not a "was the account real?" signal.
    """
    return await _enqueue_async_email(
        {
            "link": link,
            "email": email,
            "name": name,
            "regno": regno,
            "type": "login",
        },
        kind="Login email",
        regno=regno,
        success_message="Login magic-link email accepted for queueing",
    )


async def enqueue_account_blocked_email(
    *,
    email: str,
    name: str,
    regno: str,
    admin_email: str | None = None,
) -> bool:
    """Notify a blocked account that it cannot currently sign in.

    This is a sign-in outcome, not a notification: the caller must send it
    regardless of ``notifications_enabled``. ``admin_email`` is one
    administrator's contact address, selected by the backend at random, so the
    blocked person has a route to appeal. Same best-effort contract as the
    login hand-off, and never logs the addresses or message.
    """
    payload = {
        "email": email,
        "name": name,
        "regno": regno,
        "type": "account_blocked",
    }
    if admin_email:
        payload["admin_email"] = admin_email
    return await _enqueue_async_email(
        payload,
        kind="Blocked-account email",
        regno=regno,
        success_message="Blocked-account email accepted for queueing",
    )


def enqueue_notification_email(
    *,
    title: str,
    body: str,
    email: str,
    name: str,
    regno: str,
    link: str | None = None,
) -> bool:
    """Submit one notification email request to the Functions queue endpoint.

    Synchronous counterpart to :func:`enqueue_login_email`. The caller must
    apply the account's delivery preferences before calling: the Function App
    only renders the title, body and optional link and has no access to the
    application database.

    Returns True only when the endpoint answered with a 2xx. Every failure is
    swallowed and logged with safe diagnostics so a missing email can never
    undo the in-app notification that was already committed. The title, body,
    link, recipient address, name and registration number are never logged.
    """
    url = _configured_url()
    regno_hash = log_identity_hash(regno)
    if url is None:
        logger.debug(
            "Notification email was not queued: EMAIL_FUNCTION_URL is not configured "
            "(regno_hash=%s)",
            regno_hash,
        )
        return False

    payload = {
        "title": title,
        "body": body,
        "email": email,
        "name": name,
        "regno": regno,
        "type": "notification",
    }
    if link:
        payload["link"] = link

    try:
        timeout = httpx.Timeout(EMAIL_REQUEST_TIMEOUT_SECONDS)
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, json=payload)
    except httpx.TimeoutException:
        logger.error(
            "Notification email was not queued: request timed out after %ss "
            "(regno_hash=%s)",
            EMAIL_REQUEST_TIMEOUT_SECONDS,
            regno_hash,
        )
        return False
    except httpx.RequestError as exc:
        logger.error(
            "Notification email was not queued: request failed "
            "(error_type=%s, regno_hash=%s)",
            type(exc).__name__,
            regno_hash,
        )
        return False
    except Exception as exc:  # never let an email failure break the notification
        logger.error(
            "Notification email was not queued: unexpected client error "
            "(error_type=%s, regno_hash=%s)",
            type(exc).__name__,
            regno_hash,
        )
        return False

    if not 200 <= response.status_code < 300:
        logger.warning(
            "Notification email was not queued: endpoint rejected the request "
            "(status_code=%s, regno_hash=%s)",
            response.status_code,
            regno_hash,
        )
        return False

    logger.info(
        "Notification email accepted for queueing "
        "(status_code=%s, regno_hash=%s)",
        response.status_code,
        regno_hash,
    )
    return True
