"""Azure Communication Services Email delivery for ConfessIt emails.

This module owns the only ACS interaction in the Azure Functions app. It builds
either a login email or a notification email from the already-validated queue
payload and sends it through the official ``azure-communication-email`` SDK.
The backend decides who may receive a notification; this module only renders
the title, body and optional target link it was given.

A successful ``begin_send`` poller result is the only evidence of delivery. A
transient failure raises, which lets the Functions runtime retry the queue
message. A permanent ACS 400 is translated to :class:`PermanentEmailError` so
the trigger can acknowledge and suppress the recipient instead of poisoning the
queue. Delivery is at-least-once: ACS itself can accept a message and a later
poll can still fail, so a retried message may produce a duplicate email. That is
preferable to silently losing a login link or notification.
"""

from __future__ import annotations

from collections.abc import Mapping
import html
import os
import time
from typing import Any

from azure.communication.email import EmailClient
import dns.exception
import dns.resolver

from email_validation import (
    LOGIN_REQUEST_TYPE,
    NOTIFICATION_REQUEST_TYPE,
    EmailRequest,
)

# The outbound emails are intentionally simple: one recipient, one purpose, no
# unrelated email features.
LOGIN_EMAIL_SUBJECT = "Your ConfessIt sign-in link"
LOGIN_LINK_VALIDITY_MINUTES = 15


class EmailDeliveryError(RuntimeError):
    """ACS did not confirm that the email was sent."""


class PermanentEmailError(EmailDeliveryError):
    """A failure that retrying the same message cannot fix.

    ``recipient`` is true when the rejected value is the recipient address or
    domain. ``suppress_domain`` is true only when the error is domain-wide, so a
    bad mailbox at a valid provider does not silence every user there.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        recipient: bool = False,
        suppress_domain: bool = False,
    ) -> None:
        super().__init__(message)
        self.reason = (
            str(reason or "permanent_failure").strip()[:64]
            or "permanent_failure"
        )
        self.recipient = bool(recipient)
        self.suppress_domain = bool(suppress_domain)


# DNS is consulted once per domain per worker process. The rows in Table Storage
# are the durable answer; this cache just saves repeated lookups for a fan-out
# to several recipients at the same domain.
_DOMAIN_CHECK_TTL_SECONDS = 3600.0
_DOMAIN_CHECK_CACHE: dict[str, tuple[bool, float]] = {}

_RECIPIENT_ERROR_TOKENS = (
    "recipient",
    "mailbox",
    "email address",
    "emailaddress",
    "email domain",
    "emaildomain",
)
_INVALID_ERROR_TOKENS = (
    "invalid",
    "not valid",
    "not found",
    "notfound",
    "does not exist",
    "doesn't exist",
    "doesnotexist",
    "doesntexist",
    "not exist",
    "notexist",
    "nonexistent",
    "non-existent",
    "not allowed",
    "notallowed",
    "blocked",
    "rejected",
    "suppressed",
    "unable to route",
    "no such",
)


def _error_value(error: Any, primary: str, fallback: str) -> str:
    value = getattr(error, primary, None) if error is not None else None
    if not value:
        value = fallback
    return str(value or "")


def _permanent_acs_error(exc: BaseException) -> PermanentEmailError | None:
    """Translate a 400 ACS rejection into a no-retry error.

    All 400 responses are permanent for this particular message: the same body
    cannot become valid by being sent again. Only recipient/address/domain
    wording causes the address to be added to the suppression table.
    """
    if getattr(exc, "status_code", None) != 400:
        return None

    error = getattr(exc, "error", None)
    code = _error_value(error, "code", getattr(exc, "error_code", "") or "")
    message = _error_value(error, "message", getattr(exc, "reason", "") or str(exc))
    text = f"{code} {message}".lower()
    recipient = any(token in text for token in _RECIPIENT_ERROR_TOKENS) and any(
        token in text for token in _INVALID_ERROR_TOKENS
    )
    suppress_domain = recipient and "domain" in text
    return PermanentEmailError(
        "ACS rejected the email request permanently.",
        reason=code or "acs_400",
        recipient=recipient,
        suppress_domain=suppress_domain,
    )


def _permanent_result_error(error: Any) -> PermanentEmailError | None:
    if not isinstance(error, Mapping):
        return None
    code = str(error.get("code") or "")
    message = str(error.get("message") or "")
    text = f"{code} {message}".lower()
    recipient = any(token in text for token in _RECIPIENT_ERROR_TOKENS) and any(
        token in text for token in _INVALID_ERROR_TOKENS
    )
    if not recipient:
        return None
    return PermanentEmailError(
        "ACS reported a permanent recipient failure.",
        reason=code or "acs_recipient_failure",
        recipient=True,
        suppress_domain="domain" in text,
    )


def check_recipient_domain(email: str) -> None:
    """Reject a domain that cannot receive mail before calling ACS.

    A domain with no MX record is permanently undeliverable. DNS transport
    failures are treated as transient and allowed through to ACS; the queue's
    retry policy handles those, while the suppression table handles the former.
    """
    domain = (
        email.rsplit("@", 1)[-1].strip().lower().rstrip(".")
        if "@" in email
        else ""
    )
    if not domain:
        raise PermanentEmailError(
            "The recipient has no deliverable domain.",
            reason="invalid_recipient_domain",
            recipient=True,
            suppress_domain=False,
        )

    now = time.monotonic()
    cached = _DOMAIN_CHECK_CACHE.get(domain)
    if cached is not None and now - cached[1] < _DOMAIN_CHECK_TTL_SECONDS:
        if cached[0]:
            return
        raise PermanentEmailError(
            "The recipient domain has no MX record.",
            reason="domain_has_no_mx",
            recipient=True,
            suppress_domain=True,
        )

    try:
        answers = list(
            dns.resolver.resolve(domain, "MX", lifetime=3.0, search=False)
        )
        valid = any(str(answer.exchange).rstrip(".") != "" for answer in answers)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        valid = False
    except (dns.resolver.LifetimeTimeout, dns.exception.Timeout):
        return
    except dns.exception.DNSException:
        return

    _DOMAIN_CHECK_CACHE[domain] = (valid, now)
    if not valid:
        raise PermanentEmailError(
            "The recipient domain has no MX record.",
            reason="domain_has_no_mx",
            recipient=True,
            suppress_domain=True,
        )


def _required_setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise EmailDeliveryError(f"{name} is not configured.")
    return value


def _login_link(request: EmailRequest) -> str:
    if request.type != LOGIN_REQUEST_TYPE or request.link is None:
        raise EmailDeliveryError("A login email request must include a link.")
    return request.link


def _plain_text(request: EmailRequest) -> str:
    link = _login_link(request)
    return (
        f"Hello {request.name},\n\n"
        "You requested a sign-in link for ConfessIt.\n\n"
        f"Your login link is valid for {LOGIN_LINK_VALIDITY_MINUTES} minutes "
        "and can only be used once.\n\n"
        f"Open Link: {link}\n\n"
        "If you didn't request this sign-in link, you can safely ignore this email.\n\n"
        "Thanks,\n"
        "The ConfessIt Team"
    )


def _html(request: EmailRequest) -> str:
    name = html.escape(request.name)
    link = html.escape(_login_link(request), quote=True)
    return f"""<!doctype html>
<html lang="en">
  <body style="margin:0;padding:0;background:#f5f5f5;">
    <div style="max-width:560px;margin:0 auto;padding:32px 24px;
                font-family:Arial,Helvetica,sans-serif;font-size:16px;
                line-height:1.6;color:#222222;">
      <p style="margin:0 0 16px;">Hello {name},</p>
      <p style="margin:0 0 16px;">
        You requested a sign-in link for ConfessIt.
      </p>
      <p style="margin:0 0 20px;">
        Your login link is valid for <strong>{LOGIN_LINK_VALIDITY_MINUTES} minutes</strong>
        and can only be used once.
      </p>
      <p style="margin:0 0 16px;">
        <a href="{link}" target="_blank" rel="noopener noreferrer"
           style="display:inline-block;padding:12px 20px;background:#1f2937;
                  color:#ffffff;text-decoration:none;border-radius:6px;
                  font-weight:600;">Open Link</a>
      </p>
      <p style="margin:0 0 24px;font-size:14px;word-break:break-all;">
        <a href="{link}" target="_blank" rel="noopener noreferrer"
           style="color:#1d4ed8;">{link}</a>
      </p>
      <p style="margin:0 0 16px;">
        If you didn't request this sign-in link, you can safely ignore this email.
      </p>
      <p style="margin:0;">Thanks,<br>The ConfessIt Team</p>
    </div>
  </body>
</html>"""


def _notification_fields(request: EmailRequest) -> tuple[str, str]:
    if (
        request.type != NOTIFICATION_REQUEST_TYPE
        or request.title is None
        or request.body is None
    ):
        raise EmailDeliveryError(
            "A notification email request must include a title and body."
        )
    return request.title, request.body


def _notification_plain_text(request: EmailRequest) -> str:
    title, body = _notification_fields(request)
    link_line = (
        f"\n\nOpen in ConfessIt: {request.link}" if request.link else ""
    )
    return (
        f"Hello {request.name},\n\n"
        f"{title}\n\n"
        f"{body}"
        f"{link_line}\n\n"
        "Thanks,\n"
        "The ConfessIt Team"
    )


def _notification_html(request: EmailRequest) -> str:
    title, body = _notification_fields(request)
    name = html.escape(request.name)
    safe_title = html.escape(title)
    safe_body = html.escape(
        body.replace("\r\n", "\n").replace("\r", "\n")
    ).replace("\n", "<br>")
    link_block = ""
    if request.link:
        safe_link = html.escape(request.link, quote=True)
        link_block = f"""      <p style="margin:0 0 16px;">
        <a href="{safe_link}" target="_blank" rel="noopener noreferrer"
           style="display:inline-block;padding:12px 20px;background:#1f2937;
                  color:#ffffff;text-decoration:none;border-radius:6px;
                  font-weight:600;">Open in ConfessIt</a>
      </p>
      <p style="margin:0 0 24px;font-size:14px;word-break:break-all;">
        <a href="{safe_link}" target="_blank" rel="noopener noreferrer"
           style="color:#1d4ed8;">{safe_link}</a>
      </p>
"""
    return f"""<!doctype html>
<html lang="en">
  <body style="margin:0;padding:0;background:#f5f5f5;">
    <div style="max-width:560px;margin:0 auto;padding:32px 24px;
                font-family:Arial,Helvetica,sans-serif;font-size:16px;
                line-height:1.6;color:#222222;">
      <p style="margin:0 0 16px;">Hello {name},</p>
      <h1 style="margin:0 0 16px;font-size:22px;line-height:1.3;">{safe_title}</h1>
      <div style="margin:0 0 24px;">{safe_body}</div>
{link_block}      <p style="margin:0;">Thanks,<br>The ConfessIt Team</p>
    </div>
  </body>
</html>"""


def build_login_message(request: EmailRequest) -> dict[str, Any]:
    sender = _required_setting("ACS_SENDER_ADDRESS")
    return {
        "content": {
            "subject": LOGIN_EMAIL_SUBJECT,
            "plainText": _plain_text(request),
            "html": _html(request),
        },
        "recipients": {
            "to": [{"address": request.email}],
        },
        "senderAddress": sender,
    }


def build_notification_message(request: EmailRequest) -> dict[str, Any]:
    sender = _required_setting("ACS_SENDER_ADDRESS")
    title, _ = _notification_fields(request)
    return {
        "content": {
            "subject": title,
            "plainText": _notification_plain_text(request),
            "html": _notification_html(request),
        },
        "recipients": {
            "to": [{"address": request.email}],
        },
        "senderAddress": sender,
    }


def _result_status(result: Any) -> str:
    if not isinstance(result, Mapping):
        return ""
    status = result.get("status")
    return status.strip().lower() if isinstance(status, str) else ""


def _send_message(message: dict[str, Any]) -> Mapping[str, Any]:
    connection_string = _required_setting("ACS_EMAIL_CONNECTION_STRING")

    try:
        with EmailClient.from_connection_string(connection_string) as client:
            poller = client.begin_send(message)
            result = poller.result()
    except Exception as exc:
        permanent = _permanent_acs_error(exc)
        if permanent is not None:
            raise permanent from exc
        raise

    if _result_status(result) != "succeeded":
        error = result.get("error") if isinstance(result, Mapping) else None
        permanent = _permanent_result_error(error)
        if permanent is not None:
            raise permanent
        raise EmailDeliveryError("ACS Email did not report a successful send.")

    return result


def send_login_email(request: EmailRequest) -> Mapping[str, Any]:
    """Send one login email and return the confirmed ACS result.

    Raises :class:`PermanentEmailError` for a permanent ACS 400 and
    :class:`EmailDeliveryError` for missing configuration or an unconfirmed
    result. Other SDK/HTTP errors propagate unchanged so the queue trigger
    leaves the message for a retry.
    """
    return _send_message(build_login_message(request))


def send_notification_email(request: EmailRequest) -> Mapping[str, Any]:
    """Send one notification email and return the confirmed ACS result.

    The title and body were composed by the backend. This function only renders
    and transmits them, with the same permanent/transient contract as the login
    sender.
    """
    return _send_message(build_notification_message(request))
