"""Persistent suppression state for recipients that cannot receive email.

The queue trigger is stateless, so "do not keep trying this bad recipient"
needs a durable place to live. Azure Table Storage is used because lookup is a
single point read and the Azure Storage account is already required by the
queue.

Two kinds of rows are stored:

* partition ``email`` -- one specific address ACS rejected permanently. These
  rows stay until an operator deletes them.
* partition ``domain`` -- a domain with no MX record, or one ACS rejected at
  the domain level. Domain rows are rechecked after a configurable interval
  (one hour by default), so a domain that is registered or gets MX records
  later can recover without manual intervention.

A new address at a different domain is checked independently. A new address at
a suppressed domain is skipped until that domain is rechecked; if the domain is
still invalid, the row is refreshed. The target itself is stored for
diagnostics, while the row key is a SHA-256 digest so no address is used as a
key.

The store is an optimisation, not a delivery dependency. If Table Storage is
unavailable the caller logs the failure and continues with the normal send
path; an unavailable suppression table must not stop every email.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableClient

EMAIL_PARTITION = "email"
DOMAIN_PARTITION = "domain"
DEFAULT_TABLE_NAME = "EmailSuppression"
DEFAULT_DOMAIN_RECHECK_SECONDS = 3600.0
MAX_REASON_LENGTH = 120

# Table creation is idempotent, but it is one extra request per process, not per
# message. A new worker process starts with False and converges the table once.
_table_ready = False


@dataclass(frozen=True)
class SuppressionRecord:
    """One active address/domain suppression row."""

    target: str
    scope: str
    reason: str
    created_at: str


def normalize_email(value: str) -> str:
    return value.strip().lower()


def email_domain(value: str) -> str:
    normalized = normalize_email(value)
    local_part, separator, domain = normalized.rpartition("@")
    if not separator or not local_part or not domain:
        return ""
    return domain.rstrip(".")


def _row_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _connection_string() -> str:
    for setting in (
        "EMAIL_SUPPRESSION_CONNECTION_STRING",
        "EMAIL_QUEUE_CONNECTION_STRING",
        "AzureWebJobsStorage",
    ):
        value = os.environ.get(setting, "").strip()
        if value:
            return value
    raise RuntimeError("No Storage connection string is configured for suppression.")


def _table_name() -> str:
    return (
        os.environ.get("EMAIL_SUPPRESSION_TABLE", DEFAULT_TABLE_NAME).strip()
        or DEFAULT_TABLE_NAME
    )


def _domain_recheck_seconds() -> float:
    raw = os.environ.get("EMAIL_DOMAIN_SUPPRESSION_TTL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_DOMAIN_RECHECK_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_DOMAIN_RECHECK_SECONDS
    return max(0.0, value)


def _parse_created_at(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _domain_row_is_stale(record: SuppressionRecord) -> bool:
    if record.scope != DOMAIN_PARTITION:
        return False
    created_at = _parse_created_at(record.created_at)
    if created_at is None:
        # A legacy row with no usable timestamp must be rechecked rather than
        # blocking a domain forever.
        return True
    age_seconds = (datetime.now(timezone.utc) - created_at).total_seconds()
    return age_seconds >= _domain_recheck_seconds()


def _client() -> TableClient:
    global _table_ready

    client = TableClient.from_connection_string(
        _connection_string(), table_name=_table_name()
    )
    if not _table_ready:
        try:
            client.create_table()
        except ResourceExistsError:
            pass
        _table_ready = True
    return client


def _as_record(entity: dict, *, scope: str, fallback_target: str) -> SuppressionRecord:
    return SuppressionRecord(
        target=str(entity.get("Target", fallback_target)),
        scope=scope,
        reason=str(entity.get("Reason", "permanently_undeliverable")),
        created_at=str(entity.get("CreatedAt", "")),
    )


def find_suppression(email: str) -> SuppressionRecord | None:
    """Return the active address or domain suppression row, if one exists.

    A stale domain row is deleted instead of returned. The caller then performs
    the normal MX preflight, which either sends the message or records the
    domain again with a fresh timestamp. This is what lets a newly registered
    domain or a newly added MX record recover automatically.
    """
    normalized = normalize_email(email)
    domain = email_domain(normalized)
    client = _client()

    if domain:
        domain_key = _row_key(domain)
        try:
            entity = client.get_entity(DOMAIN_PARTITION, domain_key)
        except ResourceNotFoundError:
            pass
        else:
            record = _as_record(
                entity, scope=DOMAIN_PARTITION, fallback_target=domain
            )
            if not _domain_row_is_stale(record):
                return record
            try:
                client.delete_entity(DOMAIN_PARTITION, domain_key)
            except ResourceNotFoundError:
                pass

    try:
        entity = client.get_entity(EMAIL_PARTITION, _row_key(normalized))
    except ResourceNotFoundError:
        return None
    return _as_record(entity, scope=EMAIL_PARTITION, fallback_target=normalized)


def record_suppression(
    email: str, *, reason: str, suppress_domain: bool
) -> None:
    """Persist an address failure, or a domain-wide failure.

    Domain failures are stored only at the domain level. That keeps a recovered
    domain from being blocked forever by an address row written at the same
    time. Address failures are stored only for that exact normalized address.
    """
    normalized = normalize_email(email)
    domain = email_domain(normalized)
    if not normalized:
        return

    created_at = datetime.now(timezone.utc).isoformat()
    safe_reason = str(reason).strip()[:MAX_REASON_LENGTH] or "permanently_undeliverable"
    if suppress_domain and domain:
        rows = [(DOMAIN_PARTITION, domain)]
    else:
        rows = [(EMAIL_PARTITION, normalized)]

    client = _client()
    for partition, target in rows:
        client.upsert_entity(
            {
                "PartitionKey": partition,
                "RowKey": _row_key(target),
                "Target": target,
                "Reason": safe_reason,
                "CreatedAt": created_at,
            }
        )
