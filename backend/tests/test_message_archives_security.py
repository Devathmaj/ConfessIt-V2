"""Archive reads must not escape the archive directory or the process's memory.

The `date` path parameter used to be interpolated straight into a filename: `..`
survives os.path.join, the `.json.gz` suffix never stopped it, and the whole file
was json.load'ed unbounded and filtered in Python. These tests pin the three
properties the fix restores: only calendar dates resolve, resolution stays inside
the directory (even through symlinks), and reads are byte-capped and paginated.
"""

import gzip
import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.db_models import ConversationRecord, MatchRecord, MessageRecord, UserRecord
from app.services import message_archival_service as archival_module
from app.services.message_archival_service import MessageArchivalService


def _service(tmp_path):
    return MessageArchivalService(archive_dir=str(tmp_path))


def _write_day(service, date_key, count, regno="user-1", conversation_id="conv-1"):
    """One archive day through the real writer, so reads meet realistic files."""
    messages = [
        {
            "id": f"id-{i}",
            "conversation_id": conversation_id,
            "sender_regno": regno if i % 2 == 0 else "other",
            "receiver_regno": "other" if i % 2 == 0 else regno,
            "body": f"hello {i}",
            "created_at": datetime(2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc),
            "is_read": False,
        }
        for i in range(count)
    ]
    filename = service._archive_messages_to_file(date_key, messages)
    assert filename == f"messages_{date_key}.json.gz"
    return messages


@pytest.mark.parametrize(
    "evil",
    [
        "..",
        "../secret",
        "../../etc/passwd",
        "..\\..\\windows\\win.ini",
        "/abs/path",
        "2026-01-01/../secret",
        "messages_2026-01-05",
        "2026-01-05.json.gz",
        "2026-01-05%2e%2e%2fsecret",
        "",
        "2026-1-5",
        "01-05-2026",
    ],
)
def test_traversal_and_misshapen_dates_are_rejected(tmp_path, evil):
    # Rejected before any file is touched: even a missing file must not turn a
    # traversal probe into a quiet empty page.
    service = _service(tmp_path)
    with pytest.raises(HTTPException) as exc_info:
        service.get_archived_messages(date=evil)
    assert exc_info.value.status_code == 400


@pytest.mark.parametrize("impossible", ["2026-13-01", "2026-00-10", "2026-02-30", "abcd-ef-gh"])
def test_impossible_calendar_dates_are_rejected(tmp_path, impossible):
    # The shape gate alone accepts month 13; the calendar check must not.
    service = _service(tmp_path)
    with pytest.raises(HTTPException) as exc_info:
        service.get_archived_messages(date=impossible)
    assert exc_info.value.status_code == 400


def test_symlink_pointing_outside_is_rejected(tmp_path):
    # A symlink planted inside the directory that resolves outside it must not be
    # followed: realpath containment is the check the regex cannot do.
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    outside = tmp_path / "outside.json.gz"
    with gzip.open(outside, "wt", encoding="utf-8") as f:
        json.dump([], f)
    link = archive_dir / "messages_2026-01-05.json.gz"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("platform does not allow symlink creation here")
    service = MessageArchivalService(archive_dir=str(archive_dir))
    with pytest.raises(HTTPException) as exc_info:
        service.get_archived_messages(date="2026-01-05")
    assert exc_info.value.status_code == 400


def test_missing_day_is_an_empty_page_not_an_error(tmp_path):
    service = _service(tmp_path)
    page, total = service.get_archived_messages(date="2026-01-05")
    assert page == []
    assert total == 0


def test_round_trip_filters_and_pagination(tmp_path):
    service = _service(tmp_path)
    _write_day(service, "2026-01-05", 7)

    page, total = service.get_archived_messages(date="2026-01-05")
    assert total == 7
    assert len(page) == 7

    page, total = service.get_archived_messages(date="2026-01-05", skip=5, limit=5)
    assert total == 7
    assert len(page) == 2

    page, total = service.get_archived_messages(date="2026-01-05", user_regno="user-1")
    assert total == 7
    assert len(page) == 7

    page, total = service.get_archived_messages(date="2026-01-05", user_regno="nobody")
    assert total == 0
    assert page == []

    page, total = service.get_archived_messages(date="2026-01-05", conversation_id="conv-1")
    assert total == 7
    page, total = service.get_archived_messages(date="2026-01-05", conversation_id="other-conv")
    assert total == 0
    assert page == []


def test_limit_is_clamped_to_the_ceiling(tmp_path):
    service = _service(tmp_path)
    _write_day(service, "2026-01-05", 5)
    page, total = service.get_archived_messages(date="2026-01-05", limit=10**9)
    assert total == 5
    assert len(page) == 5


def test_oversized_archive_answers_413_not_oom(tmp_path, monkeypatch):
    # Shrink the ceiling rather than writing a 32 MB bomb: crossing it must raise
    # 413 instead of materialising the file.
    service = _service(tmp_path)
    _write_day(service, "2026-01-05", 10)
    monkeypatch.setattr(archival_module, "_MAX_ARCHIVE_READ_BYTES", 64)
    with pytest.raises(HTTPException) as exc_info:
        service.get_archived_messages(date="2026-01-05")
    assert exc_info.value.status_code == 413


def test_corrupt_archive_is_a_500_not_an_empty_page(tmp_path):
    # A truncated bomb or tampered file must fail loudly: the old code swallowed
    # every error into `[]`, which is also what a quiet day looks like.
    target = tmp_path / "messages_2026-01-05.json.gz"
    target.write_bytes(b"not gzip at all")
    service = _service(tmp_path)
    with pytest.raises(HTTPException) as exc_info:
        service.get_archived_messages(date="2026-01-05")
    assert exc_info.value.status_code == 500

    with gzip.open(target, "wt", encoding="utf-8") as f:
        f.write("{not json")
    with pytest.raises(HTTPException) as exc_info:
        service.get_archived_messages(date="2026-01-05")
    assert exc_info.value.status_code == 500


def test_non_list_payload_is_rejected(tmp_path):
    # A planted dict (or scalar) would fail halfway through filtering; refuse it.
    target = tmp_path / "messages_2026-01-05.json.gz"
    with gzip.open(target, "wt", encoding="utf-8") as f:
        json.dump({"messages": []}, f)
    service = _service(tmp_path)
    with pytest.raises(HTTPException) as exc_info:
        service.get_archived_messages(date="2026-01-05")
    assert exc_info.value.status_code == 500


def test_router_rejects_bad_date_with_400(admin_client):
    response = admin_client.get("/admin/messages/archives/not-a-date")
    assert response.status_code == 400


def test_router_returns_paginated_envelope(admin_client):
    # No archive for this day in the real directory: an empty page with the full
    # envelope (total/skip/limit/count), proving the router and service agree on
    # the shape without needing fixture files on disk.
    response = admin_client.get("/admin/messages/archives/2026-01-05?skip=10&limit=5")
    assert response.status_code == 200
    body = response.json()
    assert body["date"] == "2026-01-05"
    assert body["total"] == 0
    assert body["skip"] == 10
    assert body["limit"] == 5
    assert body["count"] == 0
    assert body["messages"] == []


def test_archive_files_and_directory_are_owner_only(tmp_path):
    # Archives hold chat bodies: the directory must be 0o700 and each file
    # 0o600, never the old makedirs default that left them world-readable.
    service = _service(tmp_path)
    _write_day(service, "2026-01-05", 3)

    assert stat.S_IMODE(os.stat(str(tmp_path)).st_mode) == 0o700
    target = tmp_path / "messages_2026-01-05.json.gz"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


def test_repeated_write_for_one_day_merges_instead_of_overwriting(tmp_path):
    # A retried run after an aborted delete must reproduce the bytes, not orphan
    # the first batch under an overwrite: the second write for the same date
    # keeps both batches, deduped by id.
    service = _service(tmp_path)
    _write_day(service, "2026-01-05", 3)
    extra = [
        {
            "id": "id-extra",
            "conversation_id": "conv-1",
            "sender_regno": "user-1",
            "receiver_regno": "other",
            "body": "late arrival",
            "created_at": datetime(2026, 1, 5, 13, 0, 0, tzinfo=timezone.utc),
            "is_read": False,
        }
    ]
    assert service._archive_messages_to_file("2026-01-05", extra) == "messages_2026-01-05.json.gz"

    page, total = service.get_archived_messages(date="2026-01-05")
    assert total == 4
    assert {msg["id"] for msg in page} == {"id-0", "id-1", "id-2", "id-extra"}

    # Re-archiving the same rows is a no-op on the content.
    assert service._archive_messages_to_file("2026-01-05", extra) == "messages_2026-01-05.json.gz"
    _, total = service.get_archived_messages(date="2026-01-05")
    assert total == 4


def _seed_thread(db, sender_regno="old-timer", receiver_regno="listener"):
    """One match + conversation + users, returning ids for message seeding."""
    now = datetime.now(timezone.utc)
    with db.sessions.begin() as session:
        for regno in (sender_regno, receiver_regno):
            session.add(UserRecord(
                regno=regno, name=f"User {regno}", email=f"{regno}@test.com",
                year=2, department="Test_Class", gender="other",
            ))
        session.flush()
        match = MatchRecord(
            user_1_regno=sender_regno, user_2_regno=receiver_regno,
            created_at=now, expires_at=now + timedelta(days=30),
        )
        session.add(match)
        session.flush()
        conversation = ConversationRecord(
            match_id=str(match.id),
            initiator_regno=sender_regno, receiver_regno=receiver_regno,
            status="accepted", created_at=now,
        )
        session.add(conversation)
        session.flush()
        return str(conversation.id)


def _seed_message(db, conversation_id, sender, receiver, *, age_hours=None,
                  created_at=None, body="old hello"):
    stamp = created_at or (datetime.now(timezone.utc) - timedelta(hours=age_hours or 0))
    with db.sessions.begin() as session:
        session.add(MessageRecord(
            conversation_id=conversation_id,
            sender_regno=sender, receiver_regno=receiver,
            body=body, created_at=stamp,
            is_read=False,
        ))


def _message_count(db):
    with db.sessions() as session:
        return session.scalar(select(func.count()).select_from(MessageRecord)) or 0


def test_archive_run_moves_only_old_rows_and_counts_match(db, tmp_path, monkeypatch):
    # End to end through the real writer into an isolated directory: old rows
    # are archived and deleted by id, fresh rows survive, and the deleted total
    # is verified against the archived total.
    service = MessageArchivalService(archive_dir=str(tmp_path / "archives"))
    conversation_id = _seed_thread(db)
    # One shared instant for both old rows: two separate "5h/6h ago" stamps could
    # straddle UTC midnight into two date files and flake the file count.
    dusty_at = datetime.now(timezone.utc) - timedelta(hours=5)
    _seed_message(db, conversation_id, "old-timer", "listener",
                  created_at=dusty_at, body="dusty one")
    _seed_message(db, conversation_id, "old-timer", "listener",
                  created_at=dusty_at, body="dusty two")
    _seed_message(db, conversation_id, "old-timer", "listener", age_hours=0, body="fresh hello")
    monkeypatch.setattr(
        archival_module, "message_archival_service", service, raising=False
    )

    result = service.archive_old_messages(db, hours_old=4)

    assert result["archived_count"] == 2
    assert result["deleted_count"] == 2
    assert len(result["archive_files"]) == 1
    assert _message_count(db) == 1
    with db.sessions() as session:
        survivor = session.scalar(select(MessageRecord))
        assert survivor.body == "fresh hello"

    # A backdated row inserted after the run is picked up by the next one, and
    # merges into the same day file rather than orphaning the first batch.
    _seed_message(db, conversation_id, "old-timer", "listener", age_hours=5, body="late dust")
    result = service.archive_old_messages(db, hours_old=4)
    assert result["archived_count"] == 1
    assert result["deleted_count"] == 1
    assert _message_count(db) == 1


def test_failed_write_aborts_the_delete(db, tmp_path, monkeypatch):
    # Files before deletes: when the write fails the rows must stay where a
    # retry will find them, or the delete silently drops unarchived history.
    service = MessageArchivalService(archive_dir=str(tmp_path / "archives"))
    conversation_id = _seed_thread(db)
    _seed_message(db, conversation_id, "old-timer", "listener", age_hours=5)
    monkeypatch.setattr(service, "_archive_messages_to_file", lambda *args: "")

    result = service.archive_old_messages(db, hours_old=4)

    assert "error" in result
    assert result.get("deleted_count", 0) == 0
    assert _message_count(db) == 1
