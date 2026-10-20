"""Tests for the HTTP enqueue function.

The queue is represented by a controlled fake; nothing here connects to Azure
Storage, ACS, or reads ``local.settings.json``.
"""

import json
import logging
from datetime import datetime

import pytest
from azure.core.exceptions import AzureError, ResourceExistsError

import function_app


class _FakeRequest:
    def __init__(self, payload=None, *, raises=None):
        self._payload = payload
        self._raises = raises

    def get_json(self):
        if self._raises is not None:
            raise self._raises
        return self._payload


class _FakeQueue:
    def __init__(self):
        self.messages = []
        self.error = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def send_message(self, body):
        if self.error is not None:
            raise self.error
        self.messages.append(body)
        return type("Sent", (), {"id": "queue-message-1"})()


@pytest.fixture(autouse=True)
def _reset_queue_initialization():
    function_app._queue_initialized = False
    yield
    function_app._queue_initialized = False


@pytest.fixture
def fake_queue(monkeypatch):
    queue = _FakeQueue()
    monkeypatch.setattr(function_app, "_get_queue_client", lambda: queue)
    return queue


def _valid_payload(**overrides):
    payload = {
        "link": "https://app.example.com/verify-login?token=secret-token",
        "email": "person@example.com",
        "name": "Test Person",
        "regno": "22/CS0455",
        "type": "login",
    }
    payload.update(overrides)
    return payload


def _valid_notification_payload(**overrides):
    payload = {
        "title": "New confession report",
        "body": "A participant reported a confession.",
        "email": "staff@example.com",
        "name": "Staff Person",
        "regno": "staff-1",
        "type": "notification",
    }
    payload.update(overrides)
    return payload


def _valid_blocked_payload(**overrides):
    payload = {
        "email": "blocked@example.com",
        "name": "Blocked Person",
        "regno": "blocked-1",
        "admin_email": "admin@example.com",
        "type": "account_blocked",
    }
    payload.update(overrides)
    return payload


def _body(response):
    return json.loads(response.get_body().decode("utf-8"))


def test_valid_login_request_is_accepted_and_queued(fake_queue):
    response = function_app.enqueue_email(_FakeRequest(_valid_payload()))

    assert response.status_code == 202
    assert _body(response)["status"] == "queued"
    assert len(fake_queue.messages) == 1

    queued = json.loads(fake_queue.messages[0])
    assert queued["type"] == "login"
    assert queued["link"] == "https://app.example.com/verify-login?token=secret-token"
    assert queued["email"] == "person@example.com"
    assert queued["name"] == "Test Person"
    assert queued["regno"] == "22/CS0455"
    assert queued["schema_version"] == 1
    assert queued["request_id"]
    datetime.fromisoformat(queued["enqueued_at"])  # parseable UTC timestamp


def test_valid_notification_request_is_accepted_and_queued(fake_queue):
    response = function_app.enqueue_email(
        _FakeRequest(_valid_notification_payload())
    )

    assert response.status_code == 202
    assert len(fake_queue.messages) == 1

    queued = json.loads(fake_queue.messages[0])
    assert queued["type"] == "notification"
    assert queued["title"] == "New confession report"
    assert queued["body"] == "A participant reported a confession."
    assert queued["email"] == "staff@example.com"
    assert queued["name"] == "Staff Person"
    assert queued["regno"] == "staff-1"
    assert "link" not in queued
    assert queued["schema_version"] == 1


def test_notification_link_is_accepted_and_queued(fake_queue):
    response = function_app.enqueue_email(
        _FakeRequest(
            _valid_notification_payload(
                link="https://app.example.com/admin/reports"
            )
        )
    )

    assert response.status_code == 202
    queued = json.loads(fake_queue.messages[0])
    assert queued["type"] == "notification"
    assert queued["link"] == "https://app.example.com/admin/reports"


def test_blocked_account_request_is_accepted_and_queued(fake_queue):
    response = function_app.enqueue_email(
        _FakeRequest(_valid_blocked_payload())
    )

    assert response.status_code == 202
    queued = json.loads(fake_queue.messages[0])
    assert queued["type"] == "account_blocked"
    assert queued["email"] == "blocked@example.com"
    assert queued["name"] == "Blocked Person"
    assert queued["regno"] == "blocked-1"
    assert queued["admin_email"] == "admin@example.com"
    assert "link" not in queued
    assert "title" not in queued
    assert "body" not in queued


def test_blocked_account_request_allows_no_admin_email(fake_queue):
    payload = _valid_blocked_payload()
    del payload["admin_email"]

    response = function_app.enqueue_email(_FakeRequest(payload))

    assert response.status_code == 202
    queued = json.loads(fake_queue.messages[0])
    assert "admin_email" not in queued


def test_queue_success_is_not_reported_as_email_delivery(fake_queue, monkeypatch):
    called = []
    monkeypatch.setattr(function_app, "send_login_email", lambda request: called.append(request))

    response = function_app.enqueue_email(_FakeRequest(_valid_payload()))

    assert response.status_code == 202
    assert called == []


def test_enqueue_logs_do_not_expose_sensitive_values(fake_queue, caplog):
    caplog.set_level(logging.INFO, logger=function_app.__name__)

    response = function_app.enqueue_email(_FakeRequest(_valid_payload()))

    assert response.status_code == 202
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "email_request_queued" in logs
    assert "secret-token" not in logs
    assert "person@example.com" not in logs
    assert "Test Person" not in logs
    assert "22/CS0455" not in logs


def test_notification_enqueue_logs_do_not_expose_content(fake_queue, caplog):
    caplog.set_level(logging.INFO, logger=function_app.__name__)

    response = function_app.enqueue_email(
        _FakeRequest(_valid_notification_payload())
    )

    assert response.status_code == 202
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "email_request_queued" in logs
    assert "New confession report" not in logs
    assert "A participant reported a confession." not in logs
    assert "staff@example.com" not in logs
    assert "Staff Person" not in logs
    assert "staff-1" not in logs


def test_malformed_json_is_rejected(fake_queue):
    response = function_app.enqueue_email(
        _FakeRequest(raises=ValueError("not json"))
    )

    assert response.status_code == 400
    assert _body(response)["error"] == "invalid_json"
    assert fake_queue.messages == []


@pytest.mark.parametrize(
    "payload",
    [
        {
            "email": "person@example.com",
            "name": "Test Person",
            "regno": "22/CS0455",
            "type": "login",
        },
        {
            "link": "https://app.example.com/verify-login?token=x",
            "name": "Test Person",
            "regno": "22/CS0455",
            "type": "login",
        },
        {
            "link": "https://app.example.com/verify-login?token=x",
            "email": "person@example.com",
            "regno": "22/CS0455",
            "type": "login",
        },
        {
            "link": "https://app.example.com/verify-login?token=x",
            "email": "person@example.com",
            "name": "Test Person",
            "regno": "22/CS0455",
        },
        {
            "link": "not-a-url",
            "email": "person@example.com",
            "name": "Test Person",
            "regno": "22/CS0455",
            "type": "login",
        },
        {
            "link": "http://[::1",
            "email": "person@example.com",
            "name": "Test Person",
            "regno": "22/CS0455",
            "type": "login",
        },
        {
            "link": "https://app.example.com/verify-login?token=x",
            "email": "not-an-email",
            "name": "Test Person",
            "regno": "22/CS0455",
            "type": "login",
        },
        {
            "link": "https://app.example.com/verify-login?token=x",
            "email": "person@example.com",
            "name": "Test Person",
            "regno": "bad regno",
            "type": "login",
        },
        {
            "link": "https://app.example.com/verify-login?token=x",
            "email": "person@example.com",
            "name": "Test\nPerson",
            "regno": "22/CS0455",
            "type": "login",
        },
        {
            "title": "A title",
            "email": "staff@example.com",
            "name": "Staff Person",
            "regno": "staff-1",
            "type": "notification",
        },
        {
            "body": "A body",
            "email": "staff@example.com",
            "name": "Staff Person",
            "regno": "staff-1",
            "type": "notification",
        },
        {
            "title": "A\nbroken title",
            "body": "A body",
            "email": "staff@example.com",
            "name": "Staff Person",
            "regno": "staff-1",
            "type": "notification",
        },
        {
            "title": "A title",
            "body": "A broken\x00body",
            "email": "staff@example.com",
            "name": "Staff Person",
            "regno": "staff-1",
            "type": "notification",
        },
        {
            "title": "x" * 121,
            "body": "A body",
            "email": "staff@example.com",
            "name": "Staff Person",
            "regno": "staff-1",
            "type": "notification",
        },
        {
            "title": "A title",
            "body": "A body",
            "link": "not-a-url",
            "email": "staff@example.com",
            "name": "Staff Person",
            "regno": "staff-1",
            "type": "notification",
        },
        {
            "title": "A title",
            "body": "A body",
            "link": "javascript:alert(1)",
            "email": "staff@example.com",
            "name": "Staff Person",
            "regno": "staff-1",
            "type": "notification",
        },
        {
            "email": "blocked@example.com",
            "name": "Blocked Person",
            "regno": "blocked-1",
            "admin_email": "not-an-email",
            "type": "account_blocked",
        },
        {
            "email": "blocked@example.com",
            "regno": "blocked-1",
            "type": "account_blocked",
        },
        ["not", "an", "object"],
    ],
)
def test_missing_or_invalid_fields_are_rejected(fake_queue, payload):
    response = function_app.enqueue_email(_FakeRequest(payload))

    assert response.status_code == 400
    assert _body(response)["error"] == "invalid_request"
    assert fake_queue.messages == []


@pytest.mark.parametrize("request_type", ["LOGIN", "", None, 1, "bogus"])
def test_unsupported_types_are_rejected(fake_queue, request_type):
    response = function_app.enqueue_email(
        _FakeRequest(_valid_payload(type=request_type))
    )

    assert response.status_code == 400
    assert _body(response)["error"] == "invalid_request"
    assert "login" in _body(response)["message"]
    assert fake_queue.messages == []


def test_queue_failure_returns_service_unavailable(fake_queue):
    fake_queue.error = AzureError("storage unavailable")

    response = function_app.enqueue_email(_FakeRequest(_valid_payload()))

    assert response.status_code == 503
    assert _body(response)["error"] == "email_queue_unavailable"
    assert fake_queue.messages == []


def test_queue_creation_is_idempotent(monkeypatch):
    created = []

    class _QueueClient:
        def __init__(self):
            self.sent = []

        @classmethod
        def from_connection_string(cls, *, conn_str, queue_name):
            return cls()

        def create_queue(self):
            created.append(True)
            if len(created) == 1:
                raise ResourceExistsError("already exists")

        def send_message(self, body):
            self.sent.append(body)
            return type("Sent", (), {"id": "queue-message-2"})()

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(function_app, "QueueClient", _QueueClient)
    monkeypatch.setenv("EMAIL_QUEUE_NAME", "email-requests-test")
    monkeypatch.setenv("EMAIL_QUEUE_CONNECTION_STRING", "UseDevelopmentStorage=true")

    first = function_app._get_queue_client()
    second = function_app._get_queue_client()

    assert first is not second
    # The first call observes "already exists"; the second must not create again.
    assert len(created) == 1


def test_missing_queue_configuration_returns_server_error(monkeypatch):
    def _missing():
        raise RuntimeError("EMAIL_QUEUE_CONNECTION_STRING must be configured.")

    monkeypatch.setattr(function_app, "_get_queue_client", _missing)

    response = function_app.enqueue_email(_FakeRequest(_valid_payload()))

    assert response.status_code == 500
    assert _body(response)["error"] == "email_pipeline_not_configured"
