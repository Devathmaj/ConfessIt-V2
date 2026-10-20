"""Tests for the stateful recipient suppression table.

The table client is replaced with an in-memory fake. No test reads
``local.settings.json`` or contacts Azure Storage.
"""

from datetime import datetime, timedelta, timezone

from azure.core.exceptions import ResourceNotFoundError
import pytest

import email_suppression


class _FakeTable:
    def __init__(self):
        self.entities: dict[tuple[str, str], dict] = {}
        self.created = False

    def create_table(self):
        self.created = True

    def get_entity(self, partition_key, row_key):
        try:
            return dict(self.entities[(partition_key, row_key)])
        except KeyError:
            raise ResourceNotFoundError("not found") from None

    def upsert_entity(self, entity):
        self.entities[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)

    def delete_entity(self, partition_key, row_key):
        try:
            del self.entities[(partition_key, row_key)]
        except KeyError:
            raise ResourceNotFoundError("not found") from None


@pytest.fixture
def table(monkeypatch):
    fake = _FakeTable()
    monkeypatch.setattr(email_suppression, "_client", lambda: fake)
    return fake


def test_record_and_find_an_address_suppression(table):
    email_suppression.record_suppression(
        "User@Example.com", reason="InvalidRecipientAddress", suppress_domain=False
    )

    found = email_suppression.find_suppression("user@example.com")

    assert found is not None
    assert found.scope == email_suppression.EMAIL_PARTITION
    assert found.target == "user@example.com"
    assert found.reason == "InvalidRecipientAddress"
    assert found.created_at
    assert email_suppression.find_suppression("other@example.com") is None


def test_domain_suppression_is_found_for_every_address_at_that_domain(table):
    email_suppression.record_suppression(
        "one@missing.example", reason="domain_has_no_mx", suppress_domain=True
    )

    found = email_suppression.find_suppression("two@missing.example")

    assert found is not None
    assert found.scope == email_suppression.DOMAIN_PARTITION
    assert found.target == "missing.example"
    assert found.reason == "domain_has_no_mx"


def test_domain_suppression_stores_only_a_domain_row(table):
    email_suppression.record_suppression(
        "one@missing.example", reason="domain_has_no_mx", suppress_domain=True
    )

    assert set(table.entities) == {
        (
            email_suppression.DOMAIN_PARTITION,
            email_suppression._row_key("missing.example"),
        )
    }


def test_stale_domain_suppression_is_deleted_for_a_fresh_check(table):
    email_suppression.record_suppression(
        "one@missing.example", reason="domain_has_no_mx", suppress_domain=True
    )
    key = (
        email_suppression.DOMAIN_PARTITION,
        email_suppression._row_key("missing.example"),
    )
    table.entities[key]["CreatedAt"] = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat()

    assert email_suppression.find_suppression("two@missing.example") is None
    assert key not in table.entities


def test_legacy_domain_row_without_timestamp_is_rechecked(table):
    email_suppression.record_suppression(
        "one@missing.example", reason="domain_has_no_mx", suppress_domain=True
    )
    key = (
        email_suppression.DOMAIN_PARTITION,
        email_suppression._row_key("missing.example"),
    )
    table.entities[key]["CreatedAt"] = ""

    assert email_suppression.find_suppression("one@missing.example") is None
    assert key not in table.entities


def test_address_suppression_does_not_expire(table):
    email_suppression.record_suppression(
        "person@example.com", reason="InvalidRecipientAddress", suppress_domain=False
    )
    key = (
        email_suppression.EMAIL_PARTITION,
        email_suppression._row_key("person@example.com"),
    )
    table.entities[key]["CreatedAt"] = (
        datetime.now(timezone.utc) - timedelta(days=365)
    ).isoformat()

    found = email_suppression.find_suppression("person@example.com")

    assert found is not None
    assert found.scope == email_suppression.EMAIL_PARTITION


def test_email_matching_is_case_insensitive_and_trimmed(table):
    email_suppression.record_suppression(
        "  Mixed.Case@Example.COM  ", reason="invalid", suppress_domain=False
    )

    assert email_suppression.find_suppression("mixed.case@example.com") is not None
    assert email_suppression.find_suppression("MIXED.CASE@EXAMPLE.COM") is not None


def test_reason_is_bounded_before_it_is_stored(table):
    email_suppression.record_suppression(
        "person@example.com", reason="x" * 500, suppress_domain=False
    )

    found = email_suppression.find_suppression("person@example.com")

    assert found is not None
    assert len(found.reason) == email_suppression.MAX_REASON_LENGTH
