"""The unattended reset cycle the 00:01 UTC cron sidecar runs.

``run_reset_cycle`` calls the same per-game entry point as boot and the manual
trigger, so this file does not re-prove the session lifecycle itself (that is
``test_game_lifecycle``): it pins the two things that belong to the cron path
alone -- that one pass covers every registered game, and that one game's
unexpected failure is recorded while the rest of the pass (and the league
scan) still run, instead of starving every game listed after it.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db_models import TriviaDropOptionRecord, TriviaDropQuestionRecord
from app.limits import GAME_IDS
from app.services import game_scheduler
from app.services.game_scheduler import reset_cycle_succeeded, run_reset_cycle

UTC = timezone.utc


def test_the_cycle_covers_every_registered_game(db):
    """A future game added to GAME_IDS rides the cron without further wiring."""
    summary = run_reset_cycle(db, datetime(2026, 10, 8, 0, 1, tzinfo=UTC))

    assert set(summary["games"]) == set(GAME_IDS)
    for game_id, info in summary["games"].items():
        assert info["game_id"] == game_id
        assert info["action"] != "error", info
    assert summary["league"]["action"] == "ok"
    assert reset_cycle_succeeded(summary) is True


def test_one_failing_game_does_not_starve_the_rest(db, monkeypatch):
    """Per-game isolation: an unexpected failure is recorded, the pass continues."""
    real_ensure = game_scheduler.ensure_single_game

    def flaky(game_db, game_id, now=None):
        if game_id == "wordle":
            raise RuntimeError("simulated word-list outage")
        return real_ensure(game_db, game_id, now)

    monkeypatch.setattr(game_scheduler, "ensure_single_game", flaky)

    summary = run_reset_cycle(db, datetime(2026, 10, 8, 0, 1, tzinfo=UTC))

    assert summary["games"]["wordle"]["action"] == "error"
    assert "RuntimeError" in summary["games"]["wordle"]["error"]
    for game_id in GAME_IDS:
        if game_id == "wordle":
            continue
        assert summary["games"][game_id]["action"] != "error"
    assert summary["league"]["action"] == "ok"
    assert reset_cycle_succeeded(summary) is False


def test_the_cycle_releases_due_trivia_questions(db):
    """Trivia Drop has no period, but its releases ride the same pass."""
    past_due = datetime.now(UTC) - timedelta(minutes=5)
    with db.sessions.begin() as session:
        row = TriviaDropQuestionRecord(
            question_text="Which planet is known as the Red Planet?",
            status="scheduled",
            scheduled_for=past_due,
            created_by="adminuser",
        )
        session.add(row)
        session.flush()
        question_id = str(row.id)
        for index, option_text in enumerate(["Mars", "Venus", "Jupiter", "Saturn"]):
            session.add(
                TriviaDropOptionRecord(
                    question_id=question_id,
                    option_index=index,
                    option_text=option_text,
                    is_correct=(index == 0),
                )
            )

    summary = run_reset_cycle(db, datetime.now(UTC))

    assert summary["games"]["trivia-drop"]["action"] == "released"
    assert summary["games"]["trivia-drop"]["question_id"] == question_id
    with db.sessions() as session:
        assert (
            session.get(TriviaDropQuestionRecord, question_id).status == "active"
        )


def test_hidden_games_are_skipped_by_the_cycle(db):
    from app.services.game_configs import update_config

    update_config(db, "connections", is_hidden=True)

    summary = run_reset_cycle(db, datetime(2026, 10, 8, 0, 1, tzinfo=UTC))

    assert summary["games"]["connections"]["action"] == "skipped_hidden"
    assert reset_cycle_succeeded(summary) is True


def test_the_succeeded_predicate_reads_errors(db):
    assert reset_cycle_succeeded({"games": {}, "league": {"action": "ok"}}) is True
    assert (
        reset_cycle_succeeded(
            {
                "games": {"wordle": {"action": "error", "error": "boom"}},
                "league": {"action": "ok"},
            }
        )
        is False
    )
    assert (
        reset_cycle_succeeded(
            {
                "games": {"wordle": {"action": "current"}},
                "league": {"action": "error", "error": "boom"},
            }
        )
        is False
    )
    assert reset_cycle_succeeded({}) is False
    assert reset_cycle_succeeded(None) is False  # type: ignore[arg-type]
