"""Tests for the queue-triggered processing function.

The ACS sender is faked at the function boundary so these tests exercise the
message validation, acknowledgement/retry decision, and safe logging without
sending real email.
"""

from datetime import datetime, timedelta, timezone
import json
import logging
import re
from types import SimpleNamespace

import pytest

import function_app
from email_delivery import PermanentEmailError
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


def _notification_payload(**overrides):
    payload = {
        "schema_version": 1,
        "request_id": "request-2",
        "title": "New confession report",
        "body": "A participant reported a confession.",
        "email": "staff@example.com",
        "name": "Staff Person",
        "regno": "staff-1",
        "type": "notification",
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


def test_valid_notification_message_uses_the_notification_sender(monkeypatch, caplog):
    sent = []
    login_sent = []
    monkeypatch.setattr(
        function_app, "send_notification_email", lambda request: sent.append(request)
    )
    monkeypatch.setattr(
        function_app, "send_login_email", lambda request: login_sent.append(request)
    )
    caplog.set_level(logging.INFO, logger=function_app.__name__)

    function_app.process_email(_FakeQueueMessage(_notification_payload()))

    assert login_sent == []
    assert len(sent) == 1
    request = sent[0]
    assert isinstance(request, EmailRequest)
    assert request.type == "notification"
    assert request.title == "New confession report"
    assert request.body == "A participant reported a confession."
    assert request.email == "staff@example.com"
    assert request.name == "Staff Person"
    assert request.regno == "staff-1"
    assert request.link is None

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "email_process_start" in logs
    assert "email_send_succeeded" in logs
    assert "email_type=notification" in logs
    assert "New confession report" not in logs
    assert "A participant reported a confession." not in logs
    assert "staff@example.com" not in logs


def test_notification_message_preserves_its_target_link(monkeypatch):
    sent = []
    monkeypatch.setattr(
        function_app, "send_notification_email", lambda request: sent.append(request)
    )
    link = "https://app.example.com/admin/games/manage/wordle"

    function_app.process_email(
        _FakeQueueMessage(_notification_payload(link=link))
    )

    assert len(sent) == 1
    assert sent[0].link == link


def test_suppressed_recipient_is_acknowledged_without_sending(monkeypatch, caplog):
    sent = []
    monkeypatch.setattr(function_app, "send_login_email", lambda request: sent.append(request))
    monkeypatch.setattr(
        function_app,
        "find_suppression",
        lambda email: SimpleNamespace(scope="domain", reason="domain_has_no_mx"),
    )
    caplog.set_level(logging.INFO, logger=function_app.__name__)

    function_app.process_email(_FakeQueueMessage(_payload()))

    assert sent == []
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "email_suppressed" in logs
    assert "scope=domain" in logs
    assert "email_send_succeeded" not in logs
    assert "person@example.com" not in logs


def test_domain_preflight_failure_is_recorded_and_not_sent(monkeypatch, caplog):
    sent = []
    recorded = []

    def _fail(email):
        raise PermanentEmailError(
            "The recipient domain has no MX record.",
            reason="domain_has_no_mx",
            recipient=True,
            suppress_domain=True,
        )

    monkeypatch.setattr(function_app, "send_login_email", lambda request: sent.append(request))
    monkeypatch.setattr(function_app, "check_recipient_domain", _fail)
    monkeypatch.setattr(
        function_app,
        "record_suppression",
        lambda email, **kwargs: recorded.append((email, kwargs)),
    )
    caplog.set_level(logging.INFO, logger=function_app.__name__)

    function_app.process_email(_FakeQueueMessage(_payload()))

    assert sent == []
    assert recorded == [
        (
            "person@example.com",
            {"reason": "domain_has_no_mx", "suppress_domain": True},
        )
    ]
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "email_send_suppressed" in logs
    assert "reason=domain_has_no_mx" in logs
    assert "suppression_scope=domain" in logs
    assert "person@example.com" not in logs


def test_permanent_acs_recipient_failure_is_recorded_and_acknowledged(
    monkeypatch, caplog
):
    recorded = []

    def _fail(request):
        raise PermanentEmailError(
            "ACS rejected the email request permanently.",
            reason="InvalidRecipientAddress",
            recipient=True,
            suppress_domain=False,
        )

    monkeypatch.setattr(function_app, "send_login_email", _fail)
    monkeypatch.setattr(
        function_app,
        "record_suppression",
        lambda email, **kwargs: recorded.append((email, kwargs)),
    )
    caplog.set_level(logging.INFO, logger=function_app.__name__)

    function_app.process_email(_FakeQueueMessage(_payload()))

    assert recorded == [
        (
            "person@example.com",
            {"reason": "InvalidRecipientAddress", "suppress_domain": False},
        )
    ]
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "email_send_permanent" in logs
    assert "recipient_error=yes" in logs
    assert "suppression_scope=address" in logs
    assert "person@example.com" not in logs


def test_permanent_non_recipient_failure_is_acknowledged_without_suppression(
    monkeypatch, caplog
):
    recorded = []

    def _fail(request):
        raise PermanentEmailError(
            "ACS rejected the email request permanently.",
            reason="BadRequest",
            recipient=False,
        )

    monkeypatch.setattr(function_app, "send_login_email", _fail)
    monkeypatch.setattr(
        function_app,
        "record_suppression",
        lambda email, **kwargs: recorded.append((email, kwargs)),
    )
    caplog.set_level(logging.INFO, logger=function_app.__name__)

    function_app.process_email(_FakeQueueMessage(_payload()))

    assert recorded == []
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "email_send_permanent" in logs
    assert "recipient_error=no" in logs
    assert "suppression_scope=none" in logs
    assert "email_send_succeeded" not in logs


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
