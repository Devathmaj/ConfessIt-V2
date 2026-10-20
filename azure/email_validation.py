"""Validation shared by the HTTP enqueue and queue-triggered processing functions.

Both functions accept the same request shapes, so one validator is the only place
that can drift. Login requests carry a magic link; notification requests carry
the already-composed title and body, plus an optional link to the page the
notification is about; blocked-account requests optionally carry an
administrator's email address. Invalid input is represented by
:class:`InvalidEmailRequest`; callers decide whether that becomes an HTTP 400 or
a rejected queue message.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
import re

LOGIN_REQUEST_TYPE = "login"
NOTIFICATION_REQUEST_TYPE = "notification"
ACCOUNT_BLOCKED_REQUEST_TYPE = "account_blocked"
SUPPORTED_REQUEST_TYPES = frozenset(
    {
        LOGIN_REQUEST_TYPE,
        NOTIFICATION_REQUEST_TYPE,
        ACCOUNT_BLOCKED_REQUEST_TYPE,
    }
)

# A queue message is capped at 64 KiB. These bounds keep a single request far
# below that while still accepting every real magic link and registration number.
MAX_LINK_LENGTH = 8192
MAX_EMAIL_LENGTH = 254
MAX_NAME_LENGTH = 160
MAX_REGNO_LENGTH = 100

# The notification columns in the backend are 120 and 500 characters wide. The
# Function accepts exactly the data those columns can hold, so an email can never
# contain a title or body the in-app inbox did not store.
MAX_TITLE_LENGTH = 120
MAX_NOTIFICATION_BODY_LENGTH = 500

# Deliberately permissive: the goal is to reject missing, malformed and header-
# injection-shaped values before ACS sees them, not to be the final authority on
# RFC 5322. ACS performs its own recipient validation.
_EMAIL_PATTERN = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")

# The backend's registration-number gate uses the same alphabet and bound. ``\w``
# is Unicode in a str pattern, matching the application's acceptance of letters
# and digits from any script.
_REGNO_PATTERN = re.compile(rf"^[\w./-]{{1,{MAX_REGNO_LENGTH}}}$")

# Names and titles are free text (any script, spaces, punctuation), but control
# characters would break the greeting/subject or allow plain-text/header
# injection. A notification body may contain line breaks and tabs, which are
# normal prose; every other control character is rejected.
_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
_BODY_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class InvalidEmailRequest(ValueError):
    """A request that must not be queued or delivered."""


@dataclass(frozen=True)
class EmailRequest:
    """The validated fields needed to build one login or notification email."""

    link: str | None
    email: str
    name: str
    regno: str
    type: str
    title: str | None = None
    body: str | None = None
    admin_email: str | None = None


def _required_string(
    payload: Mapping[str, Any], field: str, *, max_length: int
) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise InvalidEmailRequest(f"{field} is required and must be a string.")
    text = value.strip()
    if not text:
        raise InvalidEmailRequest(f"{field} must not be blank.")
    if len(text) > max_length:
        raise InvalidEmailRequest(
            f"{field} must not exceed {max_length} characters."
        )
    return text


def _valid_link(payload: Mapping[str, Any]) -> str:
    link = _required_string(payload, "link", max_length=MAX_LINK_LENGTH)
    try:
        parsed_link = urlsplit(link)
    except ValueError:
        raise InvalidEmailRequest("link must be an absolute http(s) URL.") from None
    if parsed_link.scheme.lower() not in ("http", "https") or not parsed_link.netloc:
        raise InvalidEmailRequest("link must be an absolute http(s) URL.")
    return link


def _optional_link(payload: Mapping[str, Any]) -> str | None:
    """A notification's link, when present, under the same rules as a login link."""
    value = payload.get("link")
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidEmailRequest("link must be an absolute http(s) URL.")
    if not value.strip():
        return None
    return _valid_link(payload)


def _optional_admin_email(payload: Mapping[str, Any]) -> str | None:
    """The administrator contact in a blocked-account email, when one was found."""
    value = payload.get("admin_email")
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidEmailRequest("admin_email must be a valid email address.")
    admin_email = value.strip()
    if not admin_email:
        return None
    if (
        len(admin_email) > MAX_EMAIL_LENGTH
        or _CONTROL_CHARACTER_PATTERN.search(admin_email)
        or not _EMAIL_PATTERN.fullmatch(admin_email)
    ):
        raise InvalidEmailRequest("admin_email must be a valid email address.")
    return admin_email


def validate_email_request(payload: Any) -> EmailRequest:
    """Validate one email request and return its normalised fields.

    ``type="login"`` requires an absolute http(s) ``link``. ``type="notification"``
    requires a ``title`` and ``body`` and accepts an optional absolute http(s)
    ``link``; the backend has already decided that this recipient should receive
    the notification. ``type="account_blocked"`` accepts an optional administrator
    email to include in the notice. No other type is accepted.
    """
    if not isinstance(payload, Mapping):
        raise InvalidEmailRequest("The request body must be a JSON object.")

    request_type = payload.get("type")
    if request_type not in SUPPORTED_REQUEST_TYPES:
        raise InvalidEmailRequest(
            "type must be 'login', 'notification' or 'account_blocked'."
        )

    name = _required_string(payload, "name", max_length=MAX_NAME_LENGTH)
    if _CONTROL_CHARACTER_PATTERN.search(name):
        raise InvalidEmailRequest("name must not contain control characters.")

    email = _required_string(payload, "email", max_length=MAX_EMAIL_LENGTH)
    if not _EMAIL_PATTERN.fullmatch(email):
        raise InvalidEmailRequest("email must be a valid email address.")

    regno = _required_string(payload, "regno", max_length=MAX_REGNO_LENGTH)
    if not _REGNO_PATTERN.fullmatch(regno):
        raise InvalidEmailRequest(
            "regno may contain only letters, numbers and the punctuation . / - _."
        )

    if request_type == LOGIN_REQUEST_TYPE:
        return EmailRequest(
            link=_valid_link(payload),
            email=email,
            name=name,
            regno=regno,
            type=LOGIN_REQUEST_TYPE,
        )

    if request_type == ACCOUNT_BLOCKED_REQUEST_TYPE:
        return EmailRequest(
            link=None,
            email=email,
            name=name,
            regno=regno,
            type=ACCOUNT_BLOCKED_REQUEST_TYPE,
            admin_email=_optional_admin_email(payload),
        )

    title = _required_string(payload, "title", max_length=MAX_TITLE_LENGTH)
    if _CONTROL_CHARACTER_PATTERN.search(title):
        raise InvalidEmailRequest("title must not contain control characters.")

    body = _required_string(
        payload, "body", max_length=MAX_NOTIFICATION_BODY_LENGTH
    )
    if _BODY_CONTROL_CHARACTER_PATTERN.search(body):
        raise InvalidEmailRequest("body must not contain control characters.")

    return EmailRequest(
        link=_optional_link(payload),
        email=email,
        name=name,
        regno=regno,
        type=NOTIFICATION_REQUEST_TYPE,
        title=title,
        body=body,
    )
