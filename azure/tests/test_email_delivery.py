"""Tests for the ACS Email SDK integration.

The official SDK calls used here are ``EmailClient.from_connection_string``,
``begin_send`` and ``poller.result()``. The client is replaced with a fake so no
real email is sent and no real connection string is needed.
"""

import pytest

import email_delivery
from email_validation import validate_email_request


class _FakePoller:
    def __init__(self, result):
        self._result = result

    def result(self):
        return self._result


class _FakeEmailClient:
    connection_strings: list[str] = []
    sent_messages: list[dict] = []
    result: dict = {"id": "acs-operation-1", "status": "Succeeded"}
    error: Exception | None = None

    def __init__(self):
        self.closed = False

    @classmethod
    def from_connection_string(cls, connection_string):
        cls.connection_strings.append(connection_string)
        return cls()

    def begin_send(self, message):
        if type(self).error is not None:
            raise type(self).error
        type(self).sent_messages.append(message)
        return _FakePoller(type(self).result)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.closed = True
        return False


@pytest.fixture(autouse=True)
def _fake_acs(monkeypatch):
    _FakeEmailClient.connection_strings.clear()
    _FakeEmailClient.sent_messages.clear()
    _FakeEmailClient.result = {"id": "acs-operation-1", "status": "Succeeded"}
    _FakeEmailClient.error = None
    monkeypatch.setattr(email_delivery, "EmailClient", _FakeEmailClient)
    return _FakeEmailClient


def _request(**overrides):
    payload = {
        "link": "https://app.example.com/verify-login?token=secret-token",
        "email": "person@example.com",
        "name": "Test Person",
        "regno": "22/CS0455",
        "type": "login",
    }
    payload.update(overrides)
    return validate_email_request(payload)


def test_send_uses_configured_connection_sender_and_recipient(monkeypatch):
    monkeypatch.setenv("ACS_EMAIL_CONNECTION_STRING", "endpoint=https://test.example.com/;accessKey=dGVzdA==")
    monkeypatch.setenv("ACS_SENDER_ADDRESS", "sender@example.com")

    result = email_delivery.send_login_email(_request())

    assert result["status"] == "Succeeded"
    assert _FakeEmailClient.connection_strings == [
        "endpoint=https://test.example.com/;accessKey=dGVzdA=="
    ]
    assert len(_FakeEmailClient.sent_messages) == 1
    message = _FakeEmailClient.sent_messages[0]
    assert message["senderAddress"] == "sender@example.com"
    assert message["recipients"] == {"to": [{"address": "person@example.com"}]}
    assert message["content"]["subject"] == email_delivery.LOGIN_EMAIL_SUBJECT

    link = "https://app.example.com/verify-login?token=secret-token"
    plain_text = message["content"]["plainText"]
    html_body = message["content"]["html"]

    assert "Hello Test Person," in plain_text
    assert "You requested a sign-in link for ConfessIt." in plain_text
    assert "valid for 15 minutes" in plain_text
    assert "can only be used once" in plain_text
    assert f"Open Link: {link}" in plain_text
    assert "Copy link" not in plain_text
    assert "22/CS0455" not in plain_text

    assert "Hello Test Person," in html_body
    assert "<strong>15 minutes</strong>" in html_body
    open_button_at = html_body.index(">Open Link</a>")
    raw_link_at = html_body.index(f">{link}</a>")
    assert open_button_at < raw_link_at
    assert f'href="{link}"' in html_body
    assert "Copy link" not in html_body
    assert "22/CS0455" not in html_body


def test_html_escapes_user_controlled_name_and_link(monkeypatch):
    monkeypatch.setenv("ACS_EMAIL_CONNECTION_STRING", "endpoint=https://test.example.com/;accessKey=dGVzdA==")
    monkeypatch.setenv("ACS_SENDER_ADDRESS", "sender@example.com")

    email_delivery.send_login_email(
        _request(
            name='A <B> & "C"',
            link="https://app.example.com/verify-login?token=abc&next=1",
        )
    )

    html_body = _FakeEmailClient.sent_messages[0]["content"]["html"]
    assert "A &lt;B&gt; &amp; &quot;C&quot;" in html_body
    assert "token=abc&amp;next=1" in html_body


def test_send_raises_when_acs_reports_failure():
    _FakeEmailClient.result = {"id": "acs-operation-1", "status": "Failed"}

    with pytest.raises(email_delivery.EmailDeliveryError):
        email_delivery.send_login_email(_request())


def test_send_raises_when_acs_result_cannot_be_confirmed():
    _FakeEmailClient.result = {"id": "acs-operation-1"}

    with pytest.raises(email_delivery.EmailDeliveryError):
        email_delivery.send_login_email(_request())


def test_send_propagates_sdk_errors_for_retry(monkeypatch):
    _FakeEmailClient.error = RuntimeError("temporary ACS outage")

    with pytest.raises(RuntimeError, match="temporary ACS outage"):
        email_delivery.send_login_email(_request())


def test_missing_sender_is_a_configuration_failure(monkeypatch):
    monkeypatch.delenv("ACS_SENDER_ADDRESS", raising=False)

    with pytest.raises(email_delivery.EmailDeliveryError, match="ACS_SENDER_ADDRESS"):
        email_delivery.send_login_email(_request())


def test_missing_connection_string_is_a_configuration_failure(monkeypatch):
    monkeypatch.delenv("ACS_EMAIL_CONNECTION_STRING", raising=False)

    with pytest.raises(email_delivery.EmailDeliveryError, match="ACS_EMAIL_CONNECTION_STRING"):
        email_delivery.send_login_email(_request())
