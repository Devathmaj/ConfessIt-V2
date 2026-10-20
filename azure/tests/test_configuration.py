"""Static checks for the queue-trigger configuration and validation rules."""

import json
from pathlib import Path

import pytest

from email_validation import InvalidEmailRequest, validate_email_request


def test_host_json_minimizes_concurrency_and_retries_poisoned_messages():
    host = json.loads((Path(__file__).resolve().parents[1] / "host.json").read_text(encoding="utf-8"))
    queue_config = host["extensions"]["queues"]

    assert queue_config["batchSize"] == 1
    assert queue_config["maxDequeueCount"] == 5
    assert queue_config["maxPollingInterval"] == "00:00:05"
    # QueueClient.send_message sends plain UTF-8 text; the trigger must not
    # assume the Azure Functions default of base64-encoded message bodies.
    assert queue_config["messageEncoding"] == "none"
    assert host["extensionBundle"]["id"] == "Microsoft.Azure.Functions.ExtensionBundle"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"type": "login"},
        {
            "link": "https://app.example.com/login",
            "email": "a@b.co",
            "name": "Test Person",
            "regno": "x",
            "type": "notification",
        },
        {
            "link": "https://app.example.com/login",
            "email": "a@b.co",
            "regno": "x",
            "type": "login",
        },
        {
            "title": "A title",
            "body": "A body",
            "email": "a@b.co",
            "name": "Test Person",
            "regno": "x",
            "type": "unsupported",
        },
    ],
)
def test_validation_rejects_incomplete_or_unsupported_requests(payload):
    with pytest.raises(InvalidEmailRequest):
        validate_email_request(payload)


def test_validation_normalizes_whitespace_and_keeps_the_exact_link():
    request = validate_email_request(
        {
            "link": "  https://app.example.com/verify-login?token=abc  ",
            "email": " person@example.com ",
            "name": "  Test Person  ",
            "regno": " 22/CS0455 ",
            "type": "login",
        }
    )

    assert request.link == "https://app.example.com/verify-login?token=abc"
    assert request.email == "person@example.com"
    assert request.name == "Test Person"
    assert request.regno == "22/CS0455"


def test_validation_accepts_a_notification_title_and_body():
    request = validate_email_request(
        {
            "title": "  New confession report  ",
            "body": "  A participant reported a confession.  ",
            "email": "staff@example.com",
            "name": "Staff Person",
            "regno": "staff-1",
            "type": "notification",
        }
    )

    assert request.type == "notification"
    assert request.title == "New confession report"
    assert request.body == "A participant reported a confession."
    assert request.link is None
    assert request.email == "staff@example.com"


def test_validation_accepts_a_notification_link():
    request = validate_email_request(
        {
            "title": "New confession report",
            "body": "A participant reported a confession.",
            "link": "  https://app.example.com/admin/reports  ",
            "email": "staff@example.com",
            "name": "Staff Person",
            "regno": "staff-1",
            "type": "notification",
        }
    )

    assert request.link == "https://app.example.com/admin/reports"


@pytest.mark.parametrize(
    "link",
    ["not-a-url", "javascript:alert(1)", "https:///no-host", 12],
)
def test_validation_rejects_an_invalid_notification_link(link):
    with pytest.raises(InvalidEmailRequest):
        validate_email_request(
            {
                "title": "New confession report",
                "body": "A participant reported a confession.",
                "link": link,
                "email": "staff@example.com",
                "name": "Staff Person",
                "regno": "staff-1",
                "type": "notification",
            }
        )


@pytest.mark.parametrize("admin_email", [None, "  admin@example.com  "])
def test_validation_accepts_a_blocked_account_request(admin_email):
    payload = {
        "email": "blocked@example.com",
        "name": "Blocked Person",
        "regno": "blocked-1",
        "type": "account_blocked",
    }
    if admin_email is not None:
        payload["admin_email"] = admin_email

    request = validate_email_request(payload)

    assert request.type == "account_blocked"
    assert request.email == "blocked@example.com"
    assert request.admin_email == (
        "admin@example.com" if admin_email is not None else None
    )
    assert request.link is None


@pytest.mark.parametrize(
    "admin_email",
    ["not-an-email", "admin@example", "admin@\nexample.com", 12],
)
def test_validation_rejects_an_invalid_administrator_email(admin_email):
    with pytest.raises(InvalidEmailRequest):
        validate_email_request(
            {
                "email": "blocked@example.com",
                "name": "Blocked Person",
                "regno": "blocked-1",
                "admin_email": admin_email,
                "type": "account_blocked",
            }
        )
