"""The separated games model: featured vs sessions vs schedule vs availability.

Every test below pins one acceptance criterion of the refactor, with
deterministic UTC timestamps wherever a calendar boundary is involved:

* featured is a spotlight only -- one game per date, no effect on sessions;
* each game owns its reset cadence (daily / 2 / 3 / 5 days / weekly Monday);
* Update means terminate-then-create, never a mutation, with history kept;
* manual mode runs pending until configured, scheduling is horizon-bounded;
* disabled refuses play but keeps the lifecycle; hidden refuses play, vanishes
  from selection, and stops session creation;
* the scheduler is idempotent and safe against races with admin Updates.

Client identities are switched explicitly per phase (``_set_admin`` /
``_set_user``) because the auth override is application-global: one test may
drive the admin console and then play as a participant.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.accounts import AccountType
from app.db_models import (
    AnagramGameRecord,
    CoupleGameRecord,
    CoupleGameSessionQuestionRecord,
    CoupleGameSessionRecord,
    FeaturedGameRecord,
    ScheduledGameContentRecord,
    WordleGameRecord,
    WordleUserGameRecord,
)
from app.limits import (
    COUPLE_SESSION_EXCLUSIVE_PER_PLAYER,
    COUPLE_SESSION_SHARED_QUESTIONS,
    MAX_FUTURE_SCHEDULE_DAYS,
)
from app.main import app
from app.models import UserDetails
from app.services import game_scheduler
from app.services.auth_service import get_current_user
from app.services.couple_service import seed_couple_questions
from app.services.wordle_service import solution_words
from conftest import override_get_admin_user, override_get_current_user

UTC = timezone.utc

# October 2026: Mon 5, Tue 6, Wed 7, Thu 8, Fri 9, Sat 10, Sun 11, Mon 12.
MON = date(2026, 10, 5)
TUE = date(2026, 10, 6)
WED = date(2026, 10, 7)
THU = date(2026, 10, 8)
FRI = date(2026, 10, 9)
SAT = date(2026, 10, 10)
SUN = date(2026, 10, 11)
NEXT_MON = date(2026, 10, 12)
NEXT_TUE = date(2026, 10, 13)

COUPLE_TOTAL = 25

# The games the reset loop opens sessions for. Reaction Time shares the
# session lifecycle -- its sessions carry no content, so a new period simply
# opens a playable session -- and its summary entries carry the same session
# actions as the other three.
SESSION_GAMES = ("wordle", "anagram", "couple-questionnaire", "reaction-time")


def at(day: date, hour: int = 0, minute: int = 1) -> datetime:
    """An aware UTC instant on ``day`` -- the scheduler's 00:01 by default."""
    return datetime.combine(day, time(hour, minute), tzinfo=UTC)


def _set_admin() -> None:
    app.dependency_overrides[get_current_user] = override_get_admin_user


def _set_user(user: UserDetails) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


def make_admin_client() -> TestClient:
    _set_admin()
    return TestClient(app, follow_redirects=False)


def make_player(db, user_id: str, regno: str, name: str = "Player") -> UserDetails:
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, regno, name, email, year, department, gender) "
                "VALUES (:id, :regno, :name, :email, 2, 'Class', 'other') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": user_id, "regno": regno, "name": name, "email": f"{regno}@t.com"},
        )
    return UserDetails(
        id=user_id,
        regno=regno,
        name=name,
        email=f"{regno}@t.com",
        year=2,
        department="Class",
        gender="other",
        account_type=AccountType.USERS.value,
    )


def make_player_client(user: UserDetails) -> TestClient:
    _set_user(user)
    return TestClient(app, follow_redirects=False)


def set_config(admin: TestClient, game_id: str, payload: dict) -> dict:
    _set_admin()
    response = admin.put(f"/admin/games/config/{game_id}", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def active_rows(db, model) -> list:
    with db.sessions() as session:
        return session.scalars(
            select(model).where(model.status.in_(("active", "pending")))
        ).all()


# --- 1. one featured game per date --------------------------------------------


def test_only_one_featured_game_per_date(db):
    admin = make_admin_client()
    first = admin.post(
        "/admin/featured", json={"game_date": THU.isoformat(), "game_id": "wordle"}
    )
    assert first.status_code == 201, first.text

    # Another game on the same date is refused...
    clash = admin.post(
        "/admin/featured", json={"game_date": THU.isoformat(), "game_id": "anagram"}
    )
    assert clash.status_code == 409

    # ...while the same game on other dates is fine.
    for day in (FRI, SAT):
        response = admin.post(
            "/admin/featured",
            json={"game_date": day.isoformat(), "game_id": "wordle"},
        )
        assert response.status_code == 201, response.text

    listing = admin.get("/admin/featured")
    assert listing.status_code == 200
    assert len(listing.json()) == 3


def test_the_featured_uniqueness_is_a_database_constraint(db):
    with db.sessions.begin() as session:
        session.add(
            FeaturedGameRecord(game_date=THU, game_id="wordle", created_by="adminuser")
        )
    try:
        with db.sessions.begin() as session:
            session.add(
                FeaturedGameRecord(
                    game_date=THU, game_id="anagram", created_by="adminuser"
                )
            )
        raise AssertionError("expected an IntegrityError")
    except IntegrityError:
        pass


# --- 2. sessions are independent of the spotlight -----------------------------


def test_sessions_do_not_follow_the_featured_game(db):
    admin = make_admin_client()
    seed_couple_questions(db)
    admin.post(
        "/admin/featured", json={"game_date": THU.isoformat(), "game_id": "wordle"}
    )

    summary = game_scheduler.ensure_game_sessions(db, at(THU))
    assert all(
        info["action"] == "created"
        for game_id, info in summary.items()
        if game_id in SESSION_GAMES
    )

    moved = admin.post(
        "/admin/featured", json={"game_date": FRI.isoformat(), "game_id": "anagram"}
    )
    assert moved.status_code == 201, moved.text

    # The spotlight moved; no session was touched.
    assert len(active_rows(db, WordleGameRecord)) == 1
    assert len(active_rows(db, AnagramGameRecord)) == 1
    assert len(active_rows(db, CoupleGameRecord)) == 1

    # The next calendar boundary rolls every daily session purely by date --
    # featuring Anagram changed nothing about that either.
    summary = game_scheduler.ensure_game_sessions(db, at(FRI))
    assert all(
        info["action"] == "created"
        for game_id, info in summary.items()
        if game_id in SESSION_GAMES
    )
    for model in (WordleGameRecord, AnagramGameRecord, CoupleGameRecord):
        assert len(active_rows(db, model)) == 1
        with db.sessions() as session:
            total = session.scalar(select(func.count()).select_from(model))
            assert total == 2  # one ended period plus the current one


# --- 3. automatic daily random sessions ---------------------------------------


def test_daily_random_sessions_open_automatically(db):
    created = game_scheduler.ensure_single_game(db, "wordle", at(THU))
    assert created["action"] == "created"
    assert created["period_start"] == THU.isoformat()
    assert created["period_end"] == FRI.isoformat()

    with db.sessions() as session:
        row = session.get(WordleGameRecord, created["session_id"])
        assert row.status == "active"
        assert row.answer in solution_words()

    rolled = game_scheduler.ensure_single_game(db, "wordle", at(FRI))
    assert rolled["action"] == "created"
    assert rolled["session_id"] != created["session_id"]

    with db.sessions() as session:
        old = session.get(WordleGameRecord, created["session_id"])
        assert old.status == "ended"


def test_repeated_random_words_need_no_dedup(db):
    day = THU
    ids = set()
    for _ in range(4):
        info = game_scheduler.ensure_single_game(db, "wordle", at(day))
        ids.add(info["session_id"])
        day += timedelta(days=1)
    # Four boundaries opened four distinct sessions; whether the drawn words
    # repeat is irrelevant and unchecked by design.
    assert len(ids) == 4


# --- 4. manual pending sessions ------------------------------------------------


def test_manual_mode_opens_a_pending_session(db):
    admin = make_admin_client()
    set_config(admin, "wordle", {"content_mode": "manual"})

    info = game_scheduler.ensure_single_game(db, "wordle", at(THU))
    assert info["action"] == "created"
    assert info["status"] == "pending"

    with db.sessions() as session:
        row = session.get(WordleGameRecord, info["session_id"])
        assert row.answer is None

    user = make_player(db, "11111111-1111-1111-1111-111111111111", "pendingplayer")
    client = make_player_client(user)
    body = client.get("/games/wordle")
    assert body.status_code == 200, body.text
    payload = body.json()
    assert payload["session_status"] == "pending"
    assert payload["participation"] is None
    assert "not been set" in (payload["message"] or "")

    guess = client.post(
        "/games/wordle/guess",
        json={"game_id": info["session_id"], "guess": "cigar"},
    )
    assert guess.status_code == 409
    assert "not been set" in guess.json()["detail"]


# --- 5. future scheduling horizon ----------------------------------------------


def test_future_scheduling_is_bounded(db):
    admin = make_admin_client()
    horizon = date.today() + timedelta(days=MAX_FUTURE_SCHEDULE_DAYS)
    beyond = horizon + timedelta(days=1)

    _set_admin()
    ok = admin.post(
        "/admin/wordle/schedule",
        json={"target_date": horizon.isoformat(), "word": solution_words()[3]},
    )
    assert ok.status_code == 201, ok.text

    refused = admin.post(
        "/admin/wordle/schedule",
        json={"target_date": beyond.isoformat(), "word": solution_words()[4]},
    )
    assert refused.status_code == 400
    assert str(MAX_FUTURE_SCHEDULE_DAYS) in refused.json()["detail"]

    past = admin.post(
        "/admin/wordle/schedule",
        json={
            "target_date": (date.today() - timedelta(days=1)).isoformat(),
            "word": solution_words()[5],
        },
    )
    assert past.status_code == 400


def test_a_scheduled_word_activates_its_period(db):
    admin = make_admin_client()
    set_config(admin, "anagram", {"content_mode": "manual"})
    # A pending session opens first...
    pending = game_scheduler.ensure_single_game(db, "anagram", at(THU))
    assert pending["status"] == "pending"

    # ...then the scheduled word for its period activates it without moving
    # the boundary.
    from app.services.scheduled_contents import schedule_word

    schedule_word(db, "anagram", THU, "ELEPHANT", "adminuser", today=THU)
    activated = game_scheduler.ensure_single_game(db, "anagram", at(THU))
    assert activated["action"] == "activated"
    with db.sessions() as session:
        row = session.get(AnagramGameRecord, activated["session_id"])
        assert row.status == "active"
        assert row.answer == "ELEPHANT"
        assert row.period_start_date == THU
        assert row.period_end_date == FRI
        assert (
            session.scalar(
                select(func.count()).select_from(ScheduledGameContentRecord)
            )
            == 0
        )


# --- 6/7. Update creates a new session; history survives -----------------------


def test_admin_update_creates_a_new_session_and_keeps_history(db, monkeypatch):
    admin = make_admin_client()
    user = make_player(db, "22222222-2222-2222-2222-222222222222", "historyplayer")

    from app.models import WordleGuessRequest
    from app.services import wordle_gameplay

    # The guess path reads the real clock; pin it to the test's fixed date so
    # this test is green on any calendar day, not just October 8th.
    monkeypatch.setattr(wordle_gameplay, "_today", lambda: THU)

    game_scheduler.ensure_single_game(db, "wordle", at(THU))
    with db.sessions() as session:
        first = session.scalar(
            select(WordleGameRecord).where(WordleGameRecord.status == "active")
        )
        first_id = str(first.id)
        answer = first.answer
    wordle_gameplay.submit_guess(
        db, user.id, WordleGuessRequest(game_id=first_id, guess=answer)
    )

    _set_admin()
    second = admin.post("/admin/wordle", json={"mode": "custom", "word": solution_words()[7]})
    assert second.status_code == 201, second.text
    assert second.json()["id"] != first_id

    with db.sessions() as session:
        old = session.get(WordleGameRecord, first_id)
        assert old.status == "ended"
        kept = session.scalars(
            select(WordleUserGameRecord).where(
                WordleUserGameRecord.wordle_game_id == first_id
            )
        ).all()
        assert len(kept) == 1  # progress stays attached to the old session
        fresh = session.scalars(
            select(WordleUserGameRecord).where(
                WordleUserGameRecord.wordle_game_id == second.json()["id"]
            )
        ).all()
        assert fresh == []  # the new session starts from scratch


# --- 8. daily update near midnight ----------------------------------------------


def test_a_late_update_gets_no_extra_day(db):
    from app.services.wordle_service import update_wordle_session

    game_scheduler.ensure_single_game(db, "wordle", at(THU))
    late = update_wordle_session(
        db,
        mode="custom",
        word=solution_words()[9],
        token=None,
        now=at(THU, 23, 58),
    )
    assert late.period_start_date == THU
    assert late.period_end_date == FRI

    rolled = game_scheduler.ensure_single_game(db, "wordle", at(FRI))
    assert rolled["session_id"] != late.id


# --- 9/10/11. 2/3/5-day calendar counting ---------------------------------------


def test_two_day_sessions_count_calendar_days(db):
    admin = make_admin_client()
    set_config(admin, "wordle", {"reset_cadence": "every_2_days"})
    first = game_scheduler.ensure_single_game(db, "wordle", at(MON))
    assert (first["period_start"], first["period_end"]) == (
        MON.isoformat(),
        WED.isoformat(),
    )
    assert (
        game_scheduler.ensure_single_game(db, "wordle", at(TUE))["session_id"]
        == first["session_id"]
    )
    second = game_scheduler.ensure_single_game(db, "wordle", at(WED))
    assert second["session_id"] != first["session_id"]
    assert second["period_end"] == FRI.isoformat()


def test_three_day_sessions_count_calendar_days(db):
    admin = make_admin_client()
    set_config(admin, "wordle", {"reset_cadence": "every_3_days"})
    first = game_scheduler.ensure_single_game(db, "wordle", at(MON))
    assert (first["period_start"], first["period_end"]) == (
        MON.isoformat(),
        THU.isoformat(),
    )
    assert (
        game_scheduler.ensure_single_game(db, "wordle", at(WED))["session_id"]
        == first["session_id"]
    )
    second = game_scheduler.ensure_single_game(db, "wordle", at(THU))
    assert second["session_id"] != first["session_id"]
    assert second["period_end"] == SUN.isoformat()


def test_five_day_sessions_count_calendar_days(db):
    admin = make_admin_client()
    set_config(admin, "wordle", {"reset_cadence": "every_5_days"})
    first = game_scheduler.ensure_single_game(db, "wordle", at(MON))
    assert (first["period_start"], first["period_end"]) == (
        MON.isoformat(),
        SAT.isoformat(),
    )
    assert (
        game_scheduler.ensure_single_game(db, "wordle", at(FRI))["session_id"]
        == first["session_id"]
    )
    second = game_scheduler.ensure_single_game(db, "wordle", at(SAT))
    assert second["session_id"] != first["session_id"]
    assert second["period_end"] == (SAT + timedelta(days=5)).isoformat()


def test_admin_update_restarts_multi_day_counting(db):
    """An Update on Thursday restarts the 2-day count from Thursday."""
    from app.services.wordle_service import update_wordle_session

    admin = make_admin_client()
    set_config(admin, "wordle", {"reset_cadence": "every_2_days"})
    game_scheduler.ensure_single_game(db, "wordle", at(MON))
    restarted = update_wordle_session(
        db, mode="custom", word=solution_words()[11], token=None, now=at(THU)
    )
    assert (restarted.period_start_date, restarted.period_end_date) == (THU, SAT)
    assert (
        game_scheduler.ensure_single_game(db, "wordle", at(FRI))["session_id"]
        == restarted.id
    )
    assert (
        game_scheduler.ensure_single_game(db, "wordle", at(SAT))["session_id"]
        != restarted.id
    )


# --- 12/13. weekly Monday resets -------------------------------------------------


def test_weekly_sessions_always_end_on_monday(db):
    admin = make_admin_client()
    set_config(admin, "wordle", {"reset_cadence": "weekly"})
    first = game_scheduler.ensure_single_game(db, "wordle", at(WED))
    assert (first["period_start"], first["period_end"]) == (
        WED.isoformat(),
        NEXT_MON.isoformat(),
    )
    assert (
        game_scheduler.ensure_single_game(db, "wordle", at(SUN))["session_id"]
        == first["session_id"]
    )
    second = game_scheduler.ensure_single_game(db, "wordle", at(NEXT_MON))
    assert second["session_id"] != first["session_id"]
    assert (second["period_start"], second["period_end"]) == (
        NEXT_MON.isoformat(),
        (NEXT_MON + timedelta(days=7)).isoformat(),
    )


def test_weekly_update_does_not_move_monday(db):
    from app.services.wordle_service import update_wordle_session

    admin = make_admin_client()
    set_config(admin, "wordle", {"reset_cadence": "weekly"})
    game_scheduler.ensure_single_game(db, "wordle", at(WED))
    friday = update_wordle_session(
        db, mode="custom", word=solution_words()[13], token=None, now=at(FRI)
    )
    assert friday.period_start_date == FRI
    assert friday.period_end_date == NEXT_MON
    assert (
        game_scheduler.ensure_single_game(db, "wordle", at(SUN))["session_id"]
        == friday.id
    )


# --- 14. questionnaire randomization ----------------------------------------------


def _pair_up(db, user_a: UserDetails, user_b: UserDetails) -> str:
    from app.services import couple_gameplay

    couple_gameplay.select_partner(db, user_a, user_b.id)
    state = couple_gameplay.select_partner(db, user_b, user_a.id)
    assert state.session_id is not None
    return state.session_id


def _question_set(db, pair_session_id: str) -> tuple[set, dict]:
    with db.sessions() as session:
        rows = session.scalars(
            select(CoupleGameSessionQuestionRecord).where(
                CoupleGameSessionQuestionRecord.session_id == pair_session_id
            )
        ).all()
        assert len(rows) == COUPLE_TOTAL
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.assignment] = counts.get(row.assignment, 0) + 1
        return {str(row.question_id) for row in rows}, counts


def test_each_questionnaire_session_deals_fresh_questions(db, monkeypatch):
    from app.services import couple_gameplay

    # Partner selection reads the real clock to find the playable session; pin
    # it to the test's fixed date so this test holds on any calendar day.
    monkeypatch.setattr(couple_gameplay, "_today", lambda: THU)

    seed_couple_questions(db)
    user_a = make_player(db, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1", "qa_one", "Al")
    user_b = make_player(db, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2", "qb_two", "Bo")

    game_scheduler.ensure_single_game(db, "couple-questionnaire", at(THU))
    first_pair = _pair_up(db, user_a, user_b)
    first_set, first_counts = _question_set(db, first_pair)
    assert first_counts == {
        "shared": COUPLE_SESSION_SHARED_QUESTIONS,
        "user_a": COUPLE_SESSION_EXCLUSIVE_PER_PLAYER,
        "user_b": COUPLE_SESSION_EXCLUSIVE_PER_PLAYER,
    }

    game_scheduler.ensure_single_game(db, "couple-questionnaire", at(FRI))
    with db.sessions() as session:
        pairs = session.scalars(
            select(CoupleGameSessionRecord).where(
                CoupleGameSessionRecord.user_a_id.in_([user_a.id, user_b.id])
            )
        ).all()
        assert len(pairs) == 2
        second_pair = next(p for p in pairs if str(p.id) != first_pair)
    second_set, second_counts = _question_set(db, str(second_pair.id))
    assert second_counts == first_counts
    assert second_set != first_set  # a fresh random draw per session

    # Progress never carries over: the new pair session starts unanswered.
    from app.db_models import CoupleGameSessionAnswerRecord

    with db.sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(CoupleGameSessionAnswerRecord)
                .where(CoupleGameSessionAnswerRecord.session_id == str(second_pair.id))
            )
            == 0
        )


def test_the_questionnaire_has_no_update_action(db):
    admin = make_admin_client()
    _set_admin()
    response = admin.post(
        "/admin/couple", json={"game_date": THU.isoformat(), "mode": "custom"}
    )
    assert response.status_code == 422


# --- 15/16. disabled and hidden endpoint blocking -----------------------------------


def test_disabled_games_refuse_direct_play_but_keep_sessions(db):
    admin = make_admin_client()
    user = make_player(db, "33333333-3333-3333-3333-333333333333", "offplayer")
    game_scheduler.ensure_single_game(db, "wordle", at(datetime.now(UTC).date()))
    set_config(admin, "wordle", {"enabled": False})

    client = make_player_client(user)
    current = client.get("/games/wordle")
    assert current.status_code == 403
    assert current.json()["code"] == "game_disabled"

    body = client.get("/games/select")
    assert body.status_code == 200
    entry = next(game for game in body.json()["games"] if game["game_id"] == "wordle")
    assert entry["playable"] is False
    assert "unavailable" in (entry["message"] or "").lower()

    # The lifecycle continues while play is refused.
    info = game_scheduler.ensure_single_game(db, "wordle", at(datetime.now(UTC).date()))
    assert info["action"] in ("current", "created")


def test_hidden_games_refuse_direct_play(db):
    admin = make_admin_client()
    user = make_player(db, "44444444-4444-4444-4444-444444444444", "hidplayer")
    game_scheduler.ensure_single_game(db, "anagram", at(datetime.now(UTC).date()))
    set_config(admin, "anagram", {"is_hidden": True})

    client = make_player_client(user)
    current = client.get("/games/anagram")
    assert current.status_code == 403
    assert current.json()["code"] == "game_hidden"


# --- 17/18. selection visibility and hidden session creation ------------------------


def test_hidden_games_vanish_from_selection_and_stop_sessions(db):
    admin = make_admin_client()
    user = make_player(db, "55555555-5555-5555-5555-555555555555", "selplayer")
    set_config(admin, "couple-questionnaire", {"is_hidden": True})

    client = make_player_client(user)
    body = client.get("/games/select")
    assert body.status_code == 200
    assert all(
        game["game_id"] != "couple-questionnaire" for game in body.json()["games"]
    )

    summary = game_scheduler.ensure_game_sessions(db, at(datetime.now(UTC).date()))
    assert summary["couple-questionnaire"]["action"] == "skipped_hidden"
    assert active_rows(db, CoupleGameRecord) == []

    # Unhiding resumes the lifecycle on the next run.
    set_config(admin, "couple-questionnaire", {"is_hidden": False})
    resumed = game_scheduler.ensure_single_game(
        db, "couple-questionnaire", at(datetime.now(UTC).date())
    )
    assert resumed["action"] == "created"


# --- 19/20. idempotent and race-safe scheduling --------------------------------------


def test_a_repeated_scheduler_run_creates_nothing(db):
    seed_couple_questions(db)
    first = game_scheduler.ensure_game_sessions(db, at(THU))
    second = game_scheduler.ensure_game_sessions(db, at(THU))
    assert all(
        info["action"] == "current"
        for game_id, info in second.items()
        if game_id in SESSION_GAMES
    )
    for model in (WordleGameRecord, AnagramGameRecord, CoupleGameRecord):
        assert len(active_rows(db, model)) == 1
    assert {
        info["session_id"] for game_id, info in first.items() if game_id in SESSION_GAMES
    } == {
        info["session_id"] for game_id, info in second.items() if game_id in SESSION_GAMES
    }


def test_concurrent_scheduler_runs_mint_one_session(db):
    barrier = threading.Barrier(3)

    def run(_: int) -> dict:
        barrier.wait(timeout=30)
        return game_scheduler.ensure_single_game(db, "wordle", at(THU))

    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = list(pool.map(run, range(3)))
    assert len({outcome["session_id"] for outcome in outcomes}) == 1
    assert len(active_rows(db, WordleGameRecord)) == 1


def test_scheduler_racing_an_update_leaves_one_active_session(db):
    from app.services.wordle_service import update_wordle_session

    game_scheduler.ensure_single_game(db, "wordle", at(THU))
    barrier = threading.Barrier(2)

    def reset() -> dict:
        barrier.wait(timeout=30)
        return game_scheduler.ensure_single_game(db, "wordle", at(FRI))

    def update():
        barrier.wait(timeout=30)
        return update_wordle_session(
            db, mode="custom", word=solution_words()[15], token=None, now=at(FRI)
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_reset = pool.submit(reset)
        future_update = pool.submit(update)
        future_reset.result(timeout=60)
        future_update.result(timeout=60)
    assert len(active_rows(db, WordleGameRecord)) == 1
