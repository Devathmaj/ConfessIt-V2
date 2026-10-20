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
