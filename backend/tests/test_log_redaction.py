"""Credential query values must not survive into Uvicorn's handshake logs.

F-03 regression coverage for the logging boundary. The WebSocket transport now
uses a one-use ticket rather than the access token, but a ticket is still a
credential and the handshake line is still logged. The filter is deliberately
tested on the record Uvicorn actually builds, including its ``%s`` arguments,
rather than on a pre-rendered string.
"""

import logging

from app.logger import (
    QueryCredentialRedactionFilter,
    install_query_credential_redaction,
)


def _uvicorn_handshake_record(path: str):
    return logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "WebSocket %s" [accepted]',
        args=("127.0.0.1:54321", path),
        exc_info=None,
    )


def test_an_accepted_handshake_line_redacts_a_ticket_value():
    record = _uvicorn_handshake_record(
        "/messages/ws/abc?ticket=live-ticket-value"
    )

    assert QueryCredentialRedactionFilter().filter(record) is True

    rendered = record.getMessage()
    assert "live-ticket-value" not in rendered
    assert "ticket=[REDACTED]" in rendered


def test_redaction_covers_both_socket_paths_and_both_credential_names():
    cases = [
        ("/messages/ws/abc?token=access-token-value&x=1", "access-token-value"),
        ("/games/case-closed/ws?ticket=case-closed-ticket-value", "case-closed-ticket-value"),
        ("/anything?ACCESS_TOKEN=uppercase-secret", "uppercase-secret"),
        ("/anything?refresh_token=refresh-secret", "refresh-secret"),
    ]
    for path, credential in cases:
        rendered = _uvicorn_handshake_record(path)
        QueryCredentialRedactionFilter().filter(rendered)
        message = rendered.getMessage()
        assert credential not in message
        assert "[REDACTED]" in message


def test_the_filter_is_installed_on_uvicorns_own_loggers():
    install_query_credential_redaction()

    for name in ("uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        assert any(
            isinstance(existing, QueryCredentialRedactionFilter)
            for existing in logger.filters
        )
