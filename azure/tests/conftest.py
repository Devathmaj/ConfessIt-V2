"""Test setup for the Azure Functions app.

Dummy values are injected into the process environment before the Function App
module is imported. No test opens ``local.settings.json`` and no test needs a
real Storage account or ACS resource.
"""

import os
import sys
from pathlib import Path

import pytest

AZURE_ROOT = Path(__file__).resolve().parents[1]
if str(AZURE_ROOT) not in sys.path:
    sys.path.insert(0, str(AZURE_ROOT))

os.environ.setdefault("EMAIL_QUEUE_NAME", "email-requests-test")
os.environ.setdefault(
    "EMAIL_QUEUE_CONNECTION_STRING",
    "DefaultEndpointsProtocol=http;AccountName=test;AccountKey=dGVzdA==;"
    "BlobEndpoint=http://127.0.0.1:10000/test;"
    "QueueEndpoint=http://127.0.0.1:10001/test;"
    "TableEndpoint=http://127.0.0.1:10002/test;",
)
os.environ.setdefault(
    "ACS_EMAIL_CONNECTION_STRING",
    "endpoint=https://test.communication.azure.com/;accessKey=dGVzdA==",
)
os.environ.setdefault("ACS_SENDER_ADDRESS", "sender@example.com")


@pytest.fixture(autouse=True)
def _no_external_email_state(monkeypatch):
    """Keep process tests off DNS and Azure Table Storage.

    Tests that exercise either seam override the relevant attribute themselves;
    every other test gets an automatic pass-through so the suite never depends
    on network state.
    """
    import function_app

    monkeypatch.setattr(function_app, "find_suppression", lambda email: None)
    monkeypatch.setattr(
        function_app, "record_suppression", lambda email, **kwargs: None
    )
    monkeypatch.setattr(function_app, "check_recipient_domain", lambda email: None)
