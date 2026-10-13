"""Azure AI Content Safety for Case Closed detective-thread turns.

The fourth text item moderated by the central dispatcher, and it follows the
same principal as confessions, their comments and matchmaking chat: everything
>1 is flagged + hidden and reported. Because a turn is delivered the moment it
is persisted, moderation runs before persistence and a flagged turn is born
hidden -- never published, never in the transcript -- yet it still counts
against the sender's allowance, exactly like a removed chat message (removing a
turn must not hand the attempt back). The Content Safety API is mocked;
persistence, visibility (removed_at), the audit report and quotas run against
the real PostgreSQL database.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db_models import (
    CaseClosedMessageRecord,
    CaseClosedMessageReportRecord,
)
from app.main import app
from app.services import content_safety_service as css
from app.services.auth_service import get_current_user
from conftest import override_get_current_user

# Reuse the Case Closed harness rather than re-deriving mutually-paired couples,
# a published case and an active event from scratch.
from test_case_closed import (  # noqa: E402
    A_ID,
    B_ID,
    client_for,
    make_mutual_pair,
    make_user,
    seed_event,
)

CLEAN = {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 0}
SENDER_REGNO = "cc_player_a"


def _mock_moderation(monkeypatch, scores: dict):
    def _fake_decision(content_type, text, *, config=None):
        return css._decision_for(content_type, dict(scores))

    monkeypatch.setattr(css, "moderate_content", _fake_decision)


def _mock_moderation_failure(monkeypatch, code: str = "unavailable"):
    def _raise_decision(content_type, text, *, config=None):
        raise css.ContentSafetyError("provider down", code=code)

    monkeypatch.setattr(css, "moderate_content", _raise_decision)


@pytest.fixture
def cc_players(db):
    from app.services.couple_service import seed_couple_questions

    seed_couple_questions(db)
    return {
        "a": make_user(db, A_ID, SENDER_REGNO, "Alice", "female"),
        "b": make_user(db, B_ID, "cc_player_b", "Bob", "male"),
    }


@pytest.fixture
def cc_http():
    client = TestClient(app, follow_redirects=False)
    try:
        yield client
    finally:
        app.dependency_overrides[get_current_user] = override_get_current_user


def _start_session(db, http, players):
    seed_event(db)
    make_mutual_pair(http, players)
    ca = client_for(http, players["a"])
    cb = client_for(http, players["b"])
    assert ca.post("/games/case-closed/start").status_code == 200
    assert cb.post("/games/case-closed/start").status_code == 200
    return ca, cb


def _message(db, body) -> CaseClosedMessageRecord:
    from tests.chat_helpers import read_case_closed_body

    with db.sessions() as session:
        for row in session.scalars(select(CaseClosedMessageRecord)).all():
            try:
                if read_case_closed_body(row) == body:
                    return row
            except Exception:
                continue
    return None


def _report(db, message_id):
    with db.sessions() as session:
        return session.scalar(
            select(CaseClosedMessageReportRecord).where(
                CaseClosedMessageReportRecord.message_id == str(message_id)
            )
        )


# ------------------------------------------------------------------ delivery


def test_clean_turn_is_delivered_and_unreported(db, cc_http, cc_players, monkeypatch):
    ca, cb = _start_session(db, cc_http, cc_players)
    _mock_moderation(monkeypatch, CLEAN)

    response = ca.post("/games/case-closed/messages", json={"body": "check the footage"})
    assert response.status_code == 201, response.text

    row = _message(db, "check the footage")
    assert row is not None and row.removed_at is None
    assert _report(db, row.id) is None

    got = cb.get("/games/case-closed/messages").json()
    assert [m["body"] for m in got["messages"]] == ["check the footage"]


@pytest.mark.parametrize(
    "scores",
    [
        {"Hate": 2, "SelfHarm": 0, "Sexual": 0, "Violence": 0},
        {"Hate": 0, "SelfHarm": 4, "Sexual": 0, "Violence": 0},
        {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 6},
    ],
)
def test_flagged_turn_is_refused_hidden_and_reported(db, cc_http, cc_players, monkeypatch, scores):
    ca, _ = _start_session(db, cc_http, cc_players)
    _mock_moderation(monkeypatch, scores)

    response = ca.post("/games/case-closed/messages", json={"body": "bad text"})
    assert response.status_code == 422
    assert "violated our content policies" in response.json()["detail"]

    row = _message(db, "bad text")
    assert row is not None
    assert row.removed_at is not None
    assert row.removed_by_regno == css.AUTOMATED_REMOVED_BY

    report = _report(db, row.id)
    assert report is not None
    assert report.source == css.AUTOMATED_SOURCE
    assert report.status == "removed"
    assert report.reporter_regno is None
    assert report.reported_user_regno == SENDER_REGNO
    assert report.reviewed_by_regno == css.AUTOMATED_REMOVED_BY
    assert (
        css.build_report_reason_for(
            css.CONTENT_CASE_CLOSED_MESSAGE, scores, max(scores.values())
        )
        in report.reason
    )


def test_flagged_turn_is_hidden_but_still_spends_the_allowance(db, cc_http, cc_players, monkeypatch):
    ca, cb = _start_session(db, cc_http, cc_players)
    _mock_moderation(monkeypatch, {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 6})

    assert ca.post("/games/case-closed/messages", json={"body": "secret threat"}).status_code == 422

    # Invisible to both participants -- the sender included -- but the attempt is spent.
    page_a = ca.get("/games/case-closed/messages").json()
    assert page_a["total"] == 0 and page_a["messages"] == []
    assert page_a["quota"]["conversation_count"] == 1
    assert page_a["quota"]["participant_count"] == 1

    page_b = cb.get("/games/case-closed/messages").json()
    assert page_b["total"] == 0 and page_b["messages"] == []
    assert page_b["quota"]["conversation_count"] == 1
    assert page_b["quota"]["participant_count"] == 0

    # A later clean turn still sends, and the removed one keeps counting.
    _mock_moderation(monkeypatch, CLEAN)
    ok = ca.post("/games/case-closed/messages", json={"body": "clean now"})
    assert ok.status_code == 201, ok.text
    assert ok.json()["quota"]["conversation_count"] == 2
    assert ok.json()["quota"]["participant_count"] == 2


def test_moderation_failure_aborts_before_persistence(db, cc_http, cc_players, monkeypatch):
    ca, _ = _start_session(db, cc_http, cc_players)
    _mock_moderation_failure(monkeypatch, code="unavailable")

    response = ca.post("/games/case-closed/messages", json={"body": "never stored"})
    assert response.status_code == 503
    assert _message(db, "never stored") is None


# ------------------------------------------------------------------ central contract


def test_case_closed_path_uses_no_local_configuration():
    import inspect

    from app.services import case_closed_messaging

    src = inspect.getsource(case_closed_messaging.send_message)
    assert "moderate_content" in src
    assert "CONTENT_SAFETY_ENDPOINT" not in src
    assert "CONTENT_SAFETY_API_KEY" not in src
    assert "config_from_settings" not in src


def test_moderate_case_closed_message_text_wrapper_agrees(monkeypatch):
    _mock_moderation(monkeypatch, {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 6})
    decision = css.moderate_case_closed_message_text("wrapper check")
    assert decision.content_type == css.CONTENT_CASE_CLOSED_MESSAGE
    assert decision.should_report is True
    assert decision.should_hide is True
    assert decision.should_block is False
