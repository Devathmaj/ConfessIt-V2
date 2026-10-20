"""Tests for the queue-triggered processing function.

The ACS sender is faked at the function boundary so these tests exercise the
message validation, acknowledgement/retry decision, and safe logging without
sending real email.
"""

from datetime import datetime, timedelta, timezone
import json
import logging
import re

import pytest

import function_app
from email_validation import EmailRequest


class _FakeQueueMessage:
    def __init__(self, payload=None, *, body=None, message_id="message-1", dequeue_count=1, insertion_time=None):
        self.id = message_id
        self.dequeue_count = dequeue_count
        self.insertion_time = insertion_time
        self._body = body if body is not None else json.dumps(payload)

    def get_json(self):
        return json.loads(self._body)


def _payload(**overrides):
    payload = {
        "schema_version": 1,
        "request_id": "request-1",
        "link": "https://app.example.com/verify-login?token=secret-token",
        "email": "person@example.com",
        "name": "Test Person",
        "regno": "22/CS0455",
        "type": "login",
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(overrides)
    return payload


def test_valid_login_message_is_sent_with_the_original_link(monkeypatch, caplog):
    sent = []
    monkeypatch.setattr(function_app, "send_login_email", lambda request: sent.append(request))
    caplog.set_level(logging.INFO, logger=function_app.__name__)

    function_app.process_email(_FakeQueueMessage(_payload()))

    assert len(sent) == 1
    request = sent[0]
    assert isinstance(request, EmailRequest)
    assert request.link == "https://app.example.com/verify-login?token=secret-token"
    assert request.email == "person@example.com"
    assert request.name == "Test Person"
    assert request.regno == "22/CS0455"
    assert request.type == "login"

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "email_process_start" in logs
    assert "email_send_succeeded" in logs
    # The link, recipient address, name and regno are never logged.
    assert "secret-token" not in logs
    assert "person@example.com" not in logs
    assert "Test Person" not in logs
    assert "22/CS0455" not in logs


def test_processing_delay_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(function_app, "send_login_email", lambda request: None)
    caplog.set_level(logging.INFO, logger=function_app.__name__)
    payload = _payload()
    payload.pop("enqueued_at")

    function_app.process_email(
        _FakeQueueMessage(
            payload,
            insertion_time=datetime.now(timezone.utc) - timedelta(seconds=12),
        )
    )

    logs = "\n".join(record.getMessage() for record in caplog.records)
    match = re.search(r"queue_delay_seconds=([0-9.]+)", logs)
    assert match is not None
    assert 11 <= float(match.group(1)) <= 14


@pytest.mark.parametrize(
    "message",
    [
        _FakeQueueMessage(body="{not-json"),
        _FakeQueueMessage(["not", "an", "object"]),
        _FakeQueueMessage(_payload(type="notification")),
        _FakeQueueMessage(_payload(type="LOGIN")),
        _FakeQueueMessage(_payload(link="javascript:alert(1)")),
        _FakeQueueMessage(_payload(email="not-an-email")),
        _FakeQueueMessage(_payload(regno="bad regno")),
        _FakeQueueMessage(_payload(name="Test\nPerson")),
        _FakeQueueMessage({key: value for key, value in _payload().items() if key != "link"}),
        _FakeQueueMessage({key: value for key, value in _payload().items() if key != "name"}),
    ],
)
def test_invalid_messages_are_acknowledged_without_sending(monkeypatch, caplog, message):
    sent = []
    monkeypatch.setattr(function_app, "send_login_email", lambda request: sent.append(request))
    caplog.set_level(logging.INFO, logger=function_app.__name__)

    function_app.process_email(message)

    assert sent == []
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "email_message_rejected" in logs
    assert "email_send_succeeded" not in logs


def test_crafted_request_id_cannot_smuggle_sensitive_values_into_logs(monkeypatch, caplog):
    monkeypatch.setattr(function_app, "send_login_email", lambda request: None)
    caplog.set_level(logging.INFO, logger=function_app.__name__)
    payload = _payload(request_id="https://app.example.com/verify-login?token=secret-token")

    function_app.process_email(_FakeQueueMessage(payload, message_id="message-1"))

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "email_send_succeeded" in logs
    assert "secret-token" not in logs
    assert "request_id=message-1" in logs


def test_transient_send_failure_is_propagated_to_the_runtime(monkeypatch, caplog):
    def _fail(request):
        raise RuntimeError("ACS unavailable")

    monkeypatch.setattr(function_app, "send_login_email", _fail)
    caplog.set_level(logging.INFO, logger=function_app.__name__)

    with pytest.raises(RuntimeError, match="ACS unavailable"):
        function_app.process_email(_FakeQueueMessage(_payload()))

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "email_send_failed" in logs
    assert "error_type=RuntimeError" in logs
    assert "email_send_succeeded" not in logs
    assert "secret-token" not in logs
    assert "person@example.com" not in logs


def test_missing_configuration_is_a_failure_not_a_delivery(monkeypatch, caplog):
    from email_delivery import EmailDeliveryError

    def _unconfigured(request):
        raise EmailDeliveryError("ACS_SENDER_ADDRESS is not configured.")

    monkeypatch.setattr(function_app, "send_login_email", _unconfigured)
    caplog.set_level(logging.INFO, logger=function_app.__name__)

    with pytest.raises(EmailDeliveryError):
        function_app.process_email(_FakeQueueMessage(_payload()))

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "email_send_failed" in logs
    assert "email_send_succeeded" not in logs
