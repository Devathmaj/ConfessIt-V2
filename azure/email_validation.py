"""Validation shared by the HTTP enqueue and queue-triggered processing functions.

Both functions accept the same five fields, so one validator is the only place
that can drift. Invalid input is represented by :class:`InvalidEmailRequest`;
callers decide whether that becomes an HTTP 400 or a rejected queue message.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
import re

LOGIN_REQUEST_TYPE = "login"

# A queue message is capped at 64 KiB. These bounds keep a single request far
# below that while still accepting every real magic link and registration number.
MAX_LINK_LENGTH = 8192
MAX_EMAIL_LENGTH = 254
MAX_NAME_LENGTH = 160
MAX_REGNO_LENGTH = 100

# Deliberately permissive: the goal is to reject missing, malformed and header-
# injection-shaped values before ACS sees them, not to be the final authority on
# RFC 5322. ACS performs its own recipient validation.
_EMAIL_PATTERN = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")

# The backend's registration-number gate uses the same alphabet and bound. ``\w``
# is Unicode in a str pattern, matching the application's acceptance of letters
# and digits from any script.
_REGNO_PATTERN = re.compile(rf"^[\w./-]{{1,{MAX_REGNO_LENGTH}}}$")

# Names are free text (any script, spaces, punctuation), but control characters
# would break the greeting or allow plain-text/header injection.
_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")


class InvalidEmailRequest(ValueError):
    """A request that must not be queued or delivered."""


@dataclass(frozen=True)
class EmailRequest:
    """The validated fields needed to build a login email."""

    link: str
    email: str
    name: str
    regno: str
    type: str


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


def validate_email_request(payload: Any) -> EmailRequest:
    """Validate one login email request and return its normalised fields.

    Only ``type="login"`` is supported. ``notification`` is rejected explicitly
    rather than silently queued: the pipeline has no notification template and
    accepting the request would promise delivery that cannot happen.
    """
    if not isinstance(payload, Mapping):
        raise InvalidEmailRequest("The request body must be a JSON object.")

    link = _required_string(payload, "link", max_length=MAX_LINK_LENGTH)
    try:
        parsed_link = urlsplit(link)
    except ValueError:
        raise InvalidEmailRequest("link must be an absolute http(s) URL.") from None
    if parsed_link.scheme.lower() not in ("http", "https") or not parsed_link.netloc:
        raise InvalidEmailRequest("link must be an absolute http(s) URL.")

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

    request_type = payload.get("type")
    if request_type != LOGIN_REQUEST_TYPE:
        raise InvalidEmailRequest(
            "type must be 'login'; only login email requests are supported."
        )

    return EmailRequest(
        link=link,
        email=email,
        name=name,
        regno=regno,
        type=LOGIN_REQUEST_TYPE,
    )
