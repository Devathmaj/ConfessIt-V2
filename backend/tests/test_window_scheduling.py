"""Window-based scheduled content: eligibility by session window, silent cleanup.

A scheduled date no longer needs to equal a session's start date. Whenever a
session is created -- by the reset loop, by a cadence change, or by a manual
Update -- its window ``[period_start, period_end)`` decides which scheduled
entries are eligible (earliest date first, row id tie-break), and entries that
can never fire again are discarded rather than left to linger. Ordinary
rollovers preserve entries meant for later windows; forced re-anchoring
(cadence changes, manual resets) discards what the new window invalidates.

Dates here are relative to the real today wherever an admin endpoint is
involved (those read the live clock); only the scheduler entry points take
explicit instants, so no test depends on which calendar day it runs on.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db_models import (
    ConnectionsGameRecord,
    ConnectionsScheduledPuzzleRecord,
    ScheduledGameContentRecord,
    WordleGameRecord,
)
from app.main import app
from app.services import game_scheduler
from app.services.auth_service import get_current_user
from app.services.wordle_service import solution_words
from conftest import override_get_admin_user

UTC = timezone.utc

GROUPS = [
    ["apple", "banana", "cherry", "date"],
    ["eagle", "falcon", "goose", "hawk"],
    ["island", "jungle", "kayak", "lemon"],
    ["mountain", "nugget", "ocean", "piano"],
]


def at(day: date, hour: int = 0, minute: int = 1) -> datetime:
    """An aware UTC instant on ``day`` -- the scheduler's 00:01 by default."""
    return datetime.combine(day, time(hour, minute), tzinfo=UTC)


def today() -> date:
    return datetime.now(UTC).date()


def _set_admin() -> None:
    app.dependency_overrides[get_current_user] = override_get_admin_user


def make_admin_client() -> TestClient:
    _set_admin()
    return TestClient(app, follow_redirects=False)


def set_config(admin: TestClient, game_id: str, payload: dict) -> dict:
    _set_admin()
    response = admin.put(f"/admin/games/config/{game_id}", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def schedule_wordle(admin: TestClient, target: date, word: str) -> dict:
    _set_admin()
    response = admin.post(
        "/admin/wordle/schedule",
        json={"target_date": target.isoformat(), "word": word},
    )
    assert response.status_code == 201, response.text
    return response.json()


def schedule_puzzle(admin: TestClient, target: date) -> dict:
    _set_admin()
    response = admin.post(
        "/admin/connections/schedule",
        json={"target_date": target.isoformat(), "groups": GROUPS},
    )
    assert response.status_code == 201, response.text
    return response.json()


def scheduled_words(db) -> list:
    with db.sessions() as session:
        return session.scalars(
            select(ScheduledGameContentRecord).order_by(
                ScheduledGameContentRecord.target_date.asc()
            )
        ).all()


def wordle_answers(db) -> list:
    with db.sessions() as session:
        return [
            row.answer
            for row in session.scalars(select(WordleGameRecord)).all()
            if row.answer
        ]


def active_wordle(db):
    with db.sessions() as session:
        return session.scalar(
            select(WordleGameRecord).where(WordleGameRecord.status == "active")
        )


# --- 1. a scheduled date equal to the session start date -----------------------


def test_a_start_date_schedule_fires_for_its_window(db):
    admin = make_admin_client()
    set_config(admin, "wordle", {"content_mode": "manual"})
    day = today()
    schedule_wordle(admin, day, solution_words()[0])

    info = game_scheduler.ensure_single_game(db, "wordle", at(day))

    assert info["action"] == "created"
    assert info["status"] == "active"
    assert active_wordle(db).answer == solution_words()[0]
    assert scheduled_words(db) == []


# --- 2. a scheduled date inside the window but past its start ------------------


def test_an_in_window_schedule_fires_without_matching_the_start(db):
    admin = make_admin_client()
    set_config(admin, "wordle", {"content_mode": "manual"})
    set_config(admin, "wordle", {"reset_cadence": "every_2_days"})
    day = today()
    schedule_wordle(admin, day + timedelta(days=1), solution_words()[1])

    info = game_scheduler.ensure_single_game(db, "wordle", at(day))

    assert info["action"] == "created"
    assert info["period_end"] == (day + timedelta(days=2)).isoformat()
    assert active_wordle(db).answer == solution_words()[1]
    assert scheduled_words(db) == []


# --- 3. a scheduled date equal to the exclusive end date -----------------------


def test_an_end_date_schedule_waits_for_the_next_window(db):
    admin = make_admin_client()
    set_config(admin, "wordle", {"content_mode": "manual"})
    set_config(admin, "wordle", {"reset_cadence": "every_3_days"})
    day = today()
    schedule_wordle(admin, day + timedelta(days=3), solution_words()[2])

    # The end date is excluded from the half-open window: pending, and kept.
    info = game_scheduler.ensure_single_game(db, "wordle", at(day))
    assert info["status"] == "pending"
    assert len(scheduled_words(db)) == 1

    rolled = game_scheduler.ensure_single_game(db, "wordle", at(day + timedelta(days=3)))
    assert rolled["action"] == "created"
    assert active_wordle(db).answer == solution_words()[2]
    assert scheduled_words(db) == []


# --- 4. cadence change: some fit, others fall outside --------------------------


def test_cadence_change_uses_the_window_fit_and_discards_the_rest(db):
    admin = make_admin_client()
    set_config(admin, "wordle", {"content_mode": "manual"})
    day = today()
    words = [solution_words()[3], solution_words()[4], solution_words()[5]]
    schedule_wordle(admin, day + timedelta(days=1), words[0])
    schedule_wordle(admin, day + timedelta(days=2), words[1])
    schedule_wordle(admin, day + timedelta(days=5), words[2])
    game_scheduler.ensure_single_game(db, "wordle", at(day))

    response = admin.put("/admin/games/config/wordle", json={"reset_cadence": "every_3_days"})
    assert response.status_code == 200, response.text

    current = active_wordle(db)
    assert current is not None
    assert current.period_start_date == day
    assert current.period_end_date == day + timedelta(days=3)
    # Earliest in-window entry wins; the in-window loser and the out-of-window
    # entry are silently discarded, never deferred.
    assert current.answer == words[0]
    assert scheduled_words(db) == []


# --- 5. deterministic selection among several in-window entries -----------------


def test_earliest_in_window_entry_wins_deterministically(db):
    admin = make_admin_client()
    set_config(admin, "wordle", {"content_mode": "manual"})
    set_config(admin, "wordle", {"reset_cadence": "every_3_days"})
    day = today()
    schedule_wordle(admin, day + timedelta(days=2), solution_words()[7])
    schedule_wordle(admin, day + timedelta(days=1), solution_words()[6])

    game_scheduler.ensure_single_game(db, "wordle", at(day))

    assert active_wordle(db).answer == solution_words()[6]
    assert scheduled_words(db) == []


# --- 6. manual update with a supplied word + cleanup ---------------------------


def test_manual_update_with_supplied_word_cleans_its_window(db):
    admin = make_admin_client()
    set_config(admin, "wordle", {"content_mode": "manual"})
    day = today()
    schedule_wordle(admin, day, solution_words()[8])
    schedule_wordle(admin, day + timedelta(days=1), solution_words()[9])

    _set_admin()
    updated = admin.post(
        "/admin/wordle", json={"mode": "custom", "word": solution_words()[10]}
    )
    assert updated.status_code == 201, updated.text
    assert updated.json()["answer"] == solution_words()[10]

    # Today's unused entry belonged to this window and is gone; tomorrow's
    # entry belongs to a future window and is preserved.
    remaining = scheduled_words(db)
    assert [row.target_date for row in remaining] == [day + timedelta(days=1)]


# --- 7. manual update without a supplied word ----------------------------------


def test_manual_update_without_a_word_uses_the_window_schedule(db):
    admin = make_admin_client()
    set_config(admin, "wordle", {"content_mode": "manual"})
    day = today()
    schedule_wordle(admin, day, solution_words()[11])

    _set_admin()
    updated = admin.post("/admin/wordle", json={"mode": "custom"})
    assert updated.status_code == 201, updated.text
    assert updated.json()["answer"] == solution_words()[11]
    assert updated.json()["status"] == "active"
    assert scheduled_words(db) == []


def test_manual_update_without_a_word_or_schedule_waits(db):
    admin = make_admin_client()
    set_config(admin, "wordle", {"content_mode": "manual"})
    game_scheduler.ensure_single_game(db, "wordle", at(today()))

    _set_admin()
    updated = admin.post("/admin/wordle", json={"mode": "custom"})
    assert updated.status_code == 201, updated.text
    assert updated.json()["status"] == "pending"
    assert updated.json()["answer"] is None


# --- 8. ordinary rollover preserves future windows -----------------------------


def test_rollover_preserves_entries_for_later_windows(db):
    admin = make_admin_client()
    set_config(admin, "wordle", {"content_mode": "manual"})
    day = today()
    schedule_wordle(admin, day + timedelta(days=2), solution_words()[12])

    first = game_scheduler.ensure_single_game(db, "wordle", at(day))
    assert first["status"] == "pending"
    assert len(scheduled_words(db)) == 1

    second = game_scheduler.ensure_single_game(db, "wordle", at(day + timedelta(days=1)))
    assert second["status"] == "pending"
    assert len(scheduled_words(db)) == 1

    third = game_scheduler.ensure_single_game(db, "wordle", at(day + timedelta(days=2)))
    assert third["status"] == "active"
    assert active_wordle(db).answer == solution_words()[12]
    assert scheduled_words(db) == []


# --- 9. scheduler/admin-update race --------------------------------------------


def test_race_consumes_a_schedule_exactly_once(db):
    """A scheduler tick racing a wordless manual Update over one scheduled row.

    Both racers open the same window and compete for the same entry; the
    per-game lock serializes them, so the row is consumed once, exactly one
    session stays live, and the word lands in exactly one session row.
    """
    from app.services.wordle_service import update_wordle_session

    admin = make_admin_client()
    set_config(admin, "wordle", {"content_mode": "manual"})
    set_config(admin, "wordle", {"reset_cadence": "every_3_days"})
    day = today()
    word = solution_words()[13]
    schedule_wordle(admin, day + timedelta(days=3), word)
    boundary = day + timedelta(days=3)

    barrier = threading.Barrier(2)

    def reset() -> dict:
        barrier.wait(timeout=30)
        return game_scheduler.ensure_single_game(db, "wordle", at(boundary))

    def update():
        barrier.wait(timeout=30)
        return update_wordle_session(db, mode="custom", word=None, token=None, now=at(boundary))

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_reset = pool.submit(reset)
        future_update = pool.submit(update)
        future_reset.result(timeout=60)
        future_update.result(timeout=60)

    with db.sessions() as session:
        live = session.scalars(
            select(WordleGameRecord).where(
                WordleGameRecord.status.in_(("active", "pending"))
            )
        ).all()
        assert len(live) == 1
        assert (
            session.scalar(
                select(func.count()).select_from(ScheduledGameContentRecord)
            )
            == 0
        )
        holders = [
            row.answer for row in session.scalars(select(WordleGameRecord)).all()
        ]
        assert holders.count(word) == 1


# --- 10. discarded schedules never activate later ------------------------------


def test_discarded_schedules_never_activate_later(db):
    admin = make_admin_client()
    set_config(admin, "wordle", {"content_mode": "manual"})
    day = today()
    doomed = solution_words()[14]
    schedule_wordle(admin, day + timedelta(days=1), solution_words()[15])
    schedule_wordle(admin, day + timedelta(days=4), doomed)
    game_scheduler.ensure_single_game(db, "wordle", at(day))

    _set_admin()
    response = admin.put("/admin/games/config/wordle", json={"reset_cadence": "every_3_days"})
    assert response.status_code == 200, response.text
    assert active_wordle(db).answer == solution_words()[15]
    assert scheduled_words(db) == []

    for offset in (3, 6, 9, 12):
        game_scheduler.ensure_single_game(db, "wordle", at(day + timedelta(days=offset)))
    assert doomed not in wordle_answers(db)
    assert scheduled_words(db) == []


# --- cadence-change guards ------------------------------------------------------


def test_same_value_cadence_save_changes_nothing(db):
    """Re-saving the current cadence is not a re-anchor: no new session, no cleanup."""
    admin = make_admin_client()
    set_config(admin, "wordle", {"content_mode": "manual"})
    set_config(admin, "wordle", {"reset_cadence": "every_3_days"})
    day = today()
    schedule_wordle(admin, day + timedelta(days=1), solution_words()[16])
    schedule_wordle(admin, day + timedelta(days=5), solution_words()[18])
    first = game_scheduler.ensure_single_game(db, "wordle", at(day))

    _set_admin()
    response = admin.put("/admin/games/config/wordle", json={"reset_cadence": "every_3_days"})
    assert response.status_code == 200, response.text

    with db.sessions() as session:
        rows = session.scalars(select(WordleGameRecord)).all()
        assert [str(row.id) for row in rows] == [first["session_id"]]
    remaining = scheduled_words(db)
    assert [row.target_date for row in remaining] == [day + timedelta(days=5)]


def test_daily_and_weekly_changes_do_not_reanchor(db):
    """Only 2/3/5-day switches force a new window; daily/weekly take effect
    at the next boundary with history and schedules untouched."""
    admin = make_admin_client()
    set_config(admin, "wordle", {"content_mode": "manual"})
    set_config(admin, "wordle", {"reset_cadence": "every_3_days"})
    day = today()
    schedule_wordle(admin, day + timedelta(days=1), solution_words()[17])
    schedule_wordle(admin, day + timedelta(days=5), solution_words()[19])
    first = game_scheduler.ensure_single_game(db, "wordle", at(day))

    _set_admin()
    response = admin.put("/admin/games/config/wordle", json={"reset_cadence": "daily"})
    assert response.status_code == 200, response.text

    with db.sessions() as session:
        rows = session.scalars(select(WordleGameRecord)).all()
        assert [str(row.id) for row in rows] == [first["session_id"]]
    remaining = scheduled_words(db)
    assert [row.target_date for row in remaining] == [day + timedelta(days=5)]


# --- connections puzzles follow the same window rules --------------------------


def active_connections(db):
    with db.sessions() as session:
        return session.scalar(
            select(ConnectionsGameRecord).where(
                ConnectionsGameRecord.status == "active"
            )
        )


def test_connections_consumes_an_in_window_puzzle(db):
    admin = make_admin_client()
    set_config(admin, "connections", {"reset_cadence": "every_3_days"})
    day = today()
    schedule_puzzle(admin, day + timedelta(days=1))

    info = game_scheduler.ensure_single_game(db, "connections", at(day))

    assert info["action"] == "created"
    assert info["status"] == "active"
    row = active_connections(db)
    assert row is not None and row.period_end_date == day + timedelta(days=3)
    with db.sessions() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(ConnectionsScheduledPuzzleRecord)
            )
            == 0
        )


def test_connections_update_without_groups_uses_the_window_schedule(db):
    admin = make_admin_client()
    day = today()
    schedule_puzzle(admin, day)

    _set_admin()
    updated = admin.post("/admin/connections", json={})
    assert updated.status_code == 201, updated.text
    assert updated.json()["status"] == "active"
    assert len(updated.json()["groups"]) == 4
    with db.sessions() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(ConnectionsScheduledPuzzleRecord)
            )
            == 0
        )
