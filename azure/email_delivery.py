"""Azure Communication Services Email delivery for login magic links.

This module owns the only ACS interaction in the Azure Functions app. It builds
a login email from the already-validated queue payload and sends it through the
official ``azure-communication-email`` SDK.

A successful ``begin_send`` poller result is the only evidence of delivery. A
failed or unconfirmable operation raises, which lets the Functions runtime retry
the queue message. Delivery is at-least-once: ACS itself can accept a message
and a later poll can still fail, so a retried message may produce a duplicate
email. That is preferable to silently losing a login link.
"""

from __future__ import annotations

from collections.abc import Mapping
import html
import os
from typing import Any

from azure.communication.email import EmailClient

from email_validation import EmailRequest

# The outbound email is intentionally simple: one recipient, one link, no
# notification templates or unrelated email features.
LOGIN_EMAIL_SUBJECT = "Your ConfessIt sign-in link"
LOGIN_LINK_VALIDITY_MINUTES = 15


class EmailDeliveryError(RuntimeError):
    """ACS did not confirm that the login email was sent."""


def _required_setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise EmailDeliveryError(f"{name} is not configured.")
    return value


def _plain_text(request: EmailRequest) -> str:
    return (
        f"Hello {request.name},\n\n"
        "You requested a sign-in link for ConfessIt.\n\n"
        f"Your login link is valid for {LOGIN_LINK_VALIDITY_MINUTES} minutes "
        "and can only be used once.\n\n"
        f"Open Link: {request.link}\n\n"
        "If you didn't request this sign-in link, you can safely ignore this email.\n\n"
        "Thanks,\n"
        "The ConfessIt Team"
    )


def _html(request: EmailRequest) -> str:
    name = html.escape(request.name)
    link = html.escape(request.link, quote=True)
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


def _result_status(result: Any) -> str:
    if not isinstance(result, Mapping):
        return ""
    status = result.get("status")
    return status.strip().lower() if isinstance(status, str) else ""


def send_login_email(request: EmailRequest) -> Mapping[str, Any]:
    """Send one login email and return the confirmed ACS result.

    Raises :class:`EmailDeliveryError` when configuration is missing or ACS does
    not return a terminal ``Succeeded`` status. SDK/HTTP errors propagate
    unchanged so the queue trigger leaves the message for a retry.
    """
    connection_string = _required_setting("ACS_EMAIL_CONNECTION_STRING")
    message = build_login_message(request)

    with EmailClient.from_connection_string(connection_string) as client:
        poller = client.begin_send(message)
        result = poller.result()

    if _result_status(result) != "succeeded":
        raise EmailDeliveryError("ACS Email did not report a successful send.")

    return result
