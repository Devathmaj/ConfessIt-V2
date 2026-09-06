"""Matchmaking must consume exactly one attempt per request, even under concurrency.

A single matchmaking attempt is a consumable right enforced by ``users.last_matchmaking_at``.
Reading that value and later writing it in a separate transaction leaves a window where two
concurrent requests both observe "eligible" and both create a match.
"""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import func, or_, select

from app.db_models import ConversationRecord, MatchRecord, UserRecord
from app.models import UserDetails
from app.postgres import get_postgres_client
from app.services import matchmaking_service


def make_user(db, regno, gender, *, matchmaking_enabled=True, last_matchmaking_at=None, role="user"):
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno, name=f"User {regno}", email=f"{regno.lower()}@test.com",
            class_name="Test_Class", gender=gender, role=role,
            matchmaking_enabled=matchmaking_enabled,
            last_matchmaking_at=last_matchmaking_at,
        )
        session.add(row)
        session.flush()
        return str(row.id)


def as_user(db, regno) -> UserDetails:
    with db.sessions() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
    return UserDetails(
        id=str(row.id), regno=row.regno, name=row.name, email=row.email,
        class_name=row.class_name, gender=row.gender,
        matchmaking_enabled=row.matchmaking_enabled,
        notifications_enabled=row.notifications_enabled,
        role=row.role, last_matchmaking_at=row.last_matchmaking_at,
    )


def count_matches(db, *regnos) -> int:
    """Count match rows involving any of the given regnos, in either column."""
    with db.sessions() as session:
        return session.scalar(
            select(func.count()).select_from(MatchRecord).where(
                or_(MatchRecord.user_1_regno.in_(regnos), MatchRecord.user_2_regno.in_(regnos))
            )
        ) or 0


@pytest.fixture
def db():
    return get_postgres_client()


def test_single_request_creates_exactly_one_match(db):
    make_user(db, "alice", "female")
    for regno in ("bob", "carol", "dave"):
        make_user(db, regno, "male")

    result = matchmaking_service.find_match_service(as_user(db, "alice"), db)

    assert result["status"] == "matched"
    assert count_matches(db, "alice", result["matched_with"]["regno"]) == 1


def test_concurrent_requests_consume_only_one_attempt(db):
    """The core regression: N simultaneous requests must yield exactly one match."""
    make_user(db, "alice", "female")
    for regno in ("bob", "carol", "dave", "erin", "frank"):
        make_user(db, regno, "male")

    workers = 8
    barrier = threading.Barrier(workers)
    results = []
    lock = threading.Lock()

    def attempt():
        user = as_user(db, "alice")
        barrier.wait(timeout=30)  # maximise the overlap window
        try:
            outcome = matchmaking_service.find_match_service(user, db)
        except HTTPException as exc:
            outcome = ("rejected", exc.status_code)
        with lock:
            results.append(outcome)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(attempt) for _ in range(workers)]:
            future.result(timeout=60)

    matched = [r for r in results if isinstance(r, dict) and r.get("status") == "matched"]
    rejected = [r for r in results if isinstance(r, tuple)]

    assert len(matched) == 1, f"expected 1 match, got {len(matched)}: {results}"
    assert len(matched) + len(rejected) == workers
    assert all(status == 429 for _, status in rejected), rejected
    assert count_matches(db, "alice") == 1


def test_cooldown_blocks_a_second_sequential_attempt(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")

    assert matchmaking_service.find_match_service(as_user(db, "alice"), db)["status"] == "matched"

    with pytest.raises(HTTPException) as excinfo:
        matchmaking_service.find_match_service(as_user(db, "alice"), db)

    assert excinfo.value.status_code == 429
    assert count_matches(db, "alice", "bob") == 1


def test_cooldown_is_read_from_the_database_not_a_token_snapshot(db):
    """A user row mutated after the caller's snapshot was taken must still be seen."""

    make_user(db, "alice", "female")
    make_user(db, "bob", "male")

    stale = as_user(db, "alice")
    assert stale.last_matchmaking_at is None

    matchmaking_service.find_match_service(stale, db)

    # A fresh read (what every request performs) must show the consumed attempt even
    # though `stale` still says None.
    refreshed = as_user(db, "alice")
    assert refreshed.last_matchmaking_at is not None
    assert matchmaking_service.check_matchmaking_cooldown_service(refreshed, db)["status"] == "cooldown"


def test_no_candidates_consumes_the_attempt(db):
    make_user(db, "alice", "female")
    make_user(db, "onlyother", "female")  # same-gender pool is the only other user

    with pytest.raises(HTTPException) as excinfo:
        matchmaking_service.find_match_service(as_user(db, "alice"), db)

    assert excinfo.value.status_code == 404
    with db.sessions() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.regno == "alice"))
    assert row.last_matchmaking_at is not None


def test_match_and_pending_conversation_are_written_together(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")

    result = matchmaking_service.find_match_service(as_user(db, "alice"), db)

    with db.sessions() as session:
        match_row = session.scalar(
            select(MatchRecord).where(MatchRecord.id == uuid.UUID(result["match_id"]))
        )
        conversation = session.scalar(
            select(ConversationRecord).where(ConversationRecord.match_id == match_row.id)
        )
    assert conversation is not None, "match committed without its pending conversation"
    assert conversation.status == "pending"
    assert {conversation.initiator_regno, conversation.receiver_regno} == {"alice", "bob"}


def test_dead_cache_globals_are_gone():
    """The obfuscated-regno cache was never written, so the whole branch was dead."""
    for name in ("_cache_invalidation_key", "_cache_reallocation_key"):
        assert not hasattr(matchmaking_service, name), f"{name} is still dead code"


def test_concurrent_requests_never_double_match_a_pair(db):
    """Two users firing at once must not end up matched to each other twice."""
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")

    barrier = threading.Barrier(2)
    errors = []

    def attempt(regno):
        user = as_user(db, regno)
        barrier.wait(timeout=30)
        try:
            matchmaking_service.find_match_service(user, db)
        except HTTPException:
            pass
        except Exception as exc:  # noqa: BLE001 - surfaced via the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=attempt, args=(r,)) for r in ("alice", "bob")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, errors
    assert count_matches(db, "alice", "bob") == 1


def test_a_lapsed_pair_can_match_again(db):
    """Uniqueness applies to live matches only, so an expired match frees the pair."""

    make_user(db, "alice", "female")
    make_user(db, "bob", "male")

    first = matchmaking_service.find_match_service(as_user(db, "alice"), db)
    assert first["status"] == "matched"
    assert first["matched_with"]["regno"] == "bob"

    # Age the match and alice's cooldown past both the 4h cooldown and the 4h expiry.
    past = datetime.now(timezone.utc) - timedelta(hours=5)
    with db.sessions.begin() as session:
        match_row = session.scalar(
            select(MatchRecord).where(MatchRecord.id == uuid.UUID(first["match_id"]))
        )
        match_row.expires_at = past
        user_row = session.scalar(select(UserRecord).where(UserRecord.regno == "alice"))
        user_row.last_matchmaking_at = past

    second = matchmaking_service.find_match_service(as_user(db, "alice"), db)
    assert second["status"] == "matched"
    assert count_matches(db, "alice", "bob") == 2


def test_live_partner_is_excluded_from_the_candidate_pool(db):
    """While a match is live, that partner must not be drawn again."""
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    make_user(db, "carol", "male")

    first = matchmaking_service.find_match_service(as_user(db, "alice"), db)
    assert first["matched_with"]["regno"] in {"bob", "carol"}

    # Clear only the cooldown; the match is still live, so the partner stays excluded.
    with db.sessions.begin() as session:
        user_row = session.scalar(select(UserRecord).where(UserRecord.regno == "alice"))
        user_row.last_matchmaking_at = datetime.now(timezone.utc) - timedelta(hours=5)

    second = matchmaking_service.find_match_service(as_user(db, "alice"), db)
    assert second["matched_with"]["regno"] != first["matched_with"]["regno"]
