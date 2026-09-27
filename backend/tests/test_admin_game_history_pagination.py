"""Session-history lists are strictly paginated on the database itself.

The four history reads behind the games manage screens -- Wordle, Anagram and
Connections sessions plus Trivia Drop questions -- used to return every row in one
response. A season of daily sessions is hundreds of rows, each carrying its puzzle,
options or words, so one unbounded read grows heavier the longer the game runs. These
tests pin the replacement contract on each of the four:

* every read answers an envelope (``games`` or ``questions`` beside ``total``, ``skip``
  and ``limit``) and never a bare list;
* ``limit`` is clamped (anything over 100 is a 422), so no client can ask for the table;
* ``skip``/``limit`` are a SQL OFFSET/LIMIT on the newest-first order, not a Python
  slice of a full load;
* Trivia Drop additionally narrows by lifecycle ``status`` so the screen reads its small
  working set separately from its unbounded history instead of loading the bank to
  split it by hand.
"""

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from app.db_models import (
    AnagramGameRecord,
    ConnectionsGameRecord,
    TriviaDropQuestionRecord,
    WordleGameRecord,
)


def seed_sessions(db, model, count, **fields):
    """Straight rows with staggered stamps, newest last created -- no cadence rules."""
    base = datetime.now(timezone.utc)
    ids = []
    with db.sessions.begin() as session:
        for index in range(count):
            row = model(
                id=str(uuid4()),
                created_at=base - timedelta(minutes=count - index),
                **fields,
            )
            session.add(row)
            session.flush()
            ids.append(str(row.id))
    return ids


def seed_wordle(db, count):
    return seed_sessions(
        db, WordleGameRecord, count,
        game_date=date(2026, 1, 1), mode="custom", answer="apple", status="ended",
    )


def seed_anagram(db, count):
    return seed_sessions(
        db, AnagramGameRecord, count,
        game_date=date(2026, 1, 1), mode="custom", answer="GARDEN", status="ended",
        created_by="adminuser",
    )


def seed_connections(db, count):
    return seed_sessions(
        db, ConnectionsGameRecord, count,
        game_date=date(2026, 1, 1), status="ended", created_by="adminuser",
    )


def seed_trivia(db, count, *, status="ended"):
    return seed_sessions(
        db, TriviaDropQuestionRecord, count,
        question_text="Which planet is known as the Red Planet?",
        status=status, created_by="adminuser",
    )


PAGES = {
    # endpoint, envelope key
    "/admin/wordle": "games",
    "/admin/anagram": "games",
    "/admin/connections": "games",
    "/admin/trivia-drop": "questions",
}


def test_history_lists_answer_page_envelopes_not_bare_lists(db, admin_client):
    seed_wordle(db, 1)
    seed_anagram(db, 1)
    seed_connections(db, 1)
    seed_trivia(db, 1)

    for path, key in PAGES.items():
        response = admin_client.get(path)
        assert response.status_code == 200, f"{path} -> {response.text}"
        body = response.json()
        assert set(body) >= {key, "total", "skip", "limit"}, path
        assert body["skip"] == 0
        assert body["limit"] == 50
        assert body["total"] == 1, path
        assert len(body[key]) == 1, path


def test_history_limits_are_clamped(db, admin_client):
    seed_wordle(db, 1)
    for path in PAGES:
        response = admin_client.get(path, params={"limit": 1000})
        assert response.status_code == 422, path


def test_skip_and_limit_walk_newest_first_without_holding_everything(db, admin_client):
    seed_wordle(db, 5)
    seed_anagram(db, 5)
    seed_connections(db, 5)
    seed_trivia(db, 5)

    for path, key in PAGES.items():
        first = admin_client.get(path, params={"skip": 0, "limit": 2}).json()
        second = admin_client.get(path, params={"skip": 2, "limit": 2}).json()
        third = admin_client.get(path, params={"skip": 4, "limit": 2}).json()
        assert first["total"] == 5, path
        assert len(first[key]) == 2, path
        assert len(second[key]) == 2, path
        assert len(third[key]) == 1, path
        seen = [row["id"] for page in (first, second, third) for row in page[key]]
        assert len(set(seen)) == 5, f"{path} re-read a row or skipped one"


def test_trivia_status_narrows_the_working_set_from_history(db, admin_client):
    seed_trivia(db, 2, status="draft")
    seed_trivia(db, 1, status="scheduled")
    seed_trivia(db, 3, status="ended")
    seed_trivia(db, 1, status="cancelled")

    everything = admin_client.get("/admin/trivia-drop").json()
    assert everything["total"] == 7

    working = admin_client.get(
        "/admin/trivia-drop", params={"status": "draft,scheduled,active"}).json()
    assert working["total"] == 3

    history = admin_client.get(
        "/admin/trivia-drop", params={"status": "ended,cancelled"}).json()
    assert history["total"] == 4
    assert len(history["questions"]) == 4


def test_trivia_unknown_status_is_refused(db, admin_client):
    seed_trivia(db, 1)
    response = admin_client.get("/admin/trivia-drop", params={"status": "forwarded"})
    assert response.status_code == 422, response.text
