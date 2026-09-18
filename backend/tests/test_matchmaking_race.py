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
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError

from app.db_models import ConversationRecord, MatchRecord, UserRecord
from app.models import UserDetails
from app.postgres import get_postgres_client
from app.services import matchmaking_service


def make_user(db, regno, gender, *, matchmaking_enabled=True, last_matchmaking_at=None):
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno, name=f"User {regno}", email=f"{regno.lower()}@test.com",
            year=2, department="Test_Class", gender=gender,
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
        year=row.year, department=row.department, gender=row.gender,
        matchmaking_enabled=row.matchmaking_enabled,
        notifications_enabled=row.notifications_enabled,
        account_type="users", last_matchmaking_at=row.last_matchmaking_at,
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


@pytest.fixture(autouse=True)
def deterministic_pairing(monkeypatch):
    """Switch off the rare same-gender roll for every test in this file.

    ``SAME_GENDER_MATCH_CHANCE`` is a real feature: on a 0.1% roll the candidate filter
    flips from "different gender" to "same gender". Most tests here seed exactly one
    opposite-gender partner, so on that rare roll there is no candidate at all and the
    request 404s instead of matching -- which is correct behaviour, but it made these
    tests fail roughly once every few hundred runs for no real reason. The same-gender
    path is covered deliberately by ``test_rare_same_gender_matching_can_apply``.
    """
    monkeypatch.setattr(matchmaking_service, "SAME_GENDER_MATCH_CHANCE", 0)


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


def test_opposite_gender_pairing_is_the_default(db):
    """Without the rare roll, the candidate filter is strictly opposite-gender.

    Kept separate from the same-gender test on purpose: each test starts from a truncated
    table, so "the only eligible partner" is unambiguous in both directions.
    """
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")

    result = matchmaking_service.find_match_service(as_user(db, "alice"), db)
    assert result["status"] == "matched"
    assert result["matched_with"]["regno"] == "bob"
    assert result["is_rare_match"] is False


def test_rare_same_gender_matching_can_apply(db, monkeypatch):
    """The rare same-gender roll is a real feature, so pin its behaviour explicitly.

    The suite-wide fixture disables the roll; this test re-enables it to prove the
    filter genuinely switches to same-gender candidates rather than being dead code.
    """
    make_user(db, "erin", "female")
    make_user(db, "frank", "female")

    monkeypatch.setattr(matchmaking_service, "SAME_GENDER_MATCH_CHANCE", 100)
    result = matchmaking_service.find_match_service(as_user(db, "erin"), db)

    assert result["status"] == "matched"
    assert result["matched_with"]["regno"] == "frank"
    assert result["is_rare_match"] is True


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


# ------------------------------------------------------------------ the pair_key guarantee


def make_match(db, first, second, pair_key):
    with db.sessions.begin() as session:
        row = MatchRecord(
            user_1_regno=first, user_2_regno=second,
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=4),
            pair_key=pair_key,
        )
        session.add(row)
        session.flush()
        return str(row.id)


def test_pair_key_is_unique_in_the_live_schema(db):
    """The unique index on ``pair_key`` is the entire race guard, so check the real schema.

    ``create_all`` builds it from the model's ``unique=True``. Asserting against the live
    schema rather than the model declaration is the point: the service can only read a
    duplicate as "already taken" if the database actually refuses to store it.
    """
    with db.engine.begin() as connection:
        indexes = connection.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = 'matches' "
                "AND indexdef LIKE '%UNIQUE%' AND indexdef LIKE '%(pair_key)%'"
            )
        ).scalars().all()

    assert indexes, "no unique index on matches.pair_key in the database"


def test_the_database_refuses_two_live_matches_for_one_pair(db):
    """Writing the same pair twice must fail at the database, not merely look wrong later."""
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    make_match(db, "alice", "bob", "alice|bob")

    with db.sessions.begin() as session:
        session.add(
            MatchRecord(
                user_1_regno="bob", user_2_regno="alice",
                created_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=4),
                pair_key=matchmaking_service._pair_key("alice", "bob"),
            )
        )
        with pytest.raises(IntegrityError) as excinfo:
            session.flush()

    # Either spelling is acceptable; what matters is that the index is what refused it.
    assert "pair_key" in str(excinfo.value) or "ix_matches_pair_key" in str(excinfo.value)


def test_a_pair_the_database_refuses_is_treated_as_taken(db):
    """A collision the candidate filter cannot see must be absorbed, not surfaced as a 500.

    ``_create_match_attempt`` filters out partners who are already matched to this user,
    but that filter is only an optimisation -- it reads a candidate list and then writes,
    and the unique index is what actually settles a race between two requests that read
    the same list. The blocking row here is filed under a different pair's regnos while
    holding alice|bob's key, which is precisely the state the filter cannot see and the
    index can. Every retry then draws the same taken partner, so the service must exhaust
    its retries and report a conflict rather than propagate the IntegrityError.
    """
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    # Same gender as alice, so they are never candidates for her, but they can carry the
    # blocking row: the constraint is on the key, not on who the participants are.
    make_user(db, "carol", "female")
    make_user(db, "dave", "female")
    make_match(db, "carol", "dave", matchmaking_service._pair_key("alice", "bob"))

    with pytest.raises(HTTPException) as excinfo:
        matchmaking_service.find_match_service(as_user(db, "alice"), db)

    assert excinfo.value.status_code == 409, excinfo.value.detail
    assert count_matches(db, "alice", "bob") == 0
