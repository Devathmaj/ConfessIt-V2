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
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

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
