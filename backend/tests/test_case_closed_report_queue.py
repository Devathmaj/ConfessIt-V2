"""The Case Closed detective-thread report queue.

A participant can now report a partner's turn, and that report shows up in the
same "Chat messages" tab a moderator already works for matchmaking chat. This
module pins the whole loop the feature promises: only the partner who received a
turn may report it (once), the report travels to the admin queue carrying the
turn and both participants with ``kind == "case_closed"``, a ``removed`` ruling
takes the turn out of the transcript for both participants while keeping the row
for audit, and an Azure-hidden turn can be restored while a moderator-hidden one
cannot.

The store is the real PostgreSQL database and the endpoints are exercised through
the app; Content Safety is mocked only where the automated-removal path is under
test (the autouse safe default covers everything else).
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db_models import (
    CaseClosedMessageRecord,
    CaseClosedMessageReportRecord,
    ConversationRecord,
    MatchRecord,
    MessageRecord,
    MessageReportRecord,
    NotificationRecord,
)
from app.main import app
from app.services import content_safety_service as css
from app.services.auth_service import get_current_user
from app.services.report_resolution_service import pending_report_totals
from conftest import override_get_current_user

from test_case_closed import (  # noqa: E402
    A_ID,
    B_ID,
    C_ID,
    make_mutual_pair,
    make_user,
    seed_event,
)

CLEAN = {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 0}
AUTHOR_REGNO = "cc_player_a"
PARTNER_REGNO = "cc_player_b"
OUTSIDER_REGNO = "cc_player_c"
REASON = "this turn is out of line"
WINDOW = timedelta(hours=4)


def _mock_moderation(monkeypatch, scores):
    monkeypatch.setattr(
        css,
        "moderate_content",
        lambda content_type, text, *, config=None: css._decision_for(content_type, dict(scores)),
    )


@pytest.fixture
def cc_players(db):
    from app.services.couple_service import seed_couple_questions

    seed_couple_questions(db)
    return {
        "a": make_user(db, A_ID, AUTHOR_REGNO, "Alice", "female"),
        "b": make_user(db, B_ID, PARTNER_REGNO, "Bob", "male"),
        "c": make_user(db, C_ID, OUTSIDER_REGNO, "Cara", "female"),
    }


@pytest.fixture
def cc_http():
    client = TestClient(app, follow_redirects=False)
    try:
        yield client
    finally:
        app.dependency_overrides[get_current_user] = override_get_current_user


@contextmanager
def acting_as(user):
    """Run the calls inside the block as this user, then restore the last identity.

    The admin fixture and the participants share one ``get_current_user`` override,
    so a participant call without a restore would leave the admin client acting as a
    participant and every admin read would 403.
    """
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        yield
    finally:
        app.dependency_overrides[get_current_user] = previous


def _start_session(db, http, players):
    # ``make_mutual_pair`` drives the questionnaire through per-user clients that leave the
    # shared auth override pointing at a participant, so the identity is restored here before
    # the test body hands control to the admin client.
    previous = app.dependency_overrides.get(get_current_user)
    try:
        seed_event(db)
        make_mutual_pair(http, players)
        with acting_as(players["a"]):
            assert http.post("/games/case-closed/start").status_code == 200
        with acting_as(players["b"]):
            assert http.post("/games/case-closed/start").status_code == 200
    finally:
        app.dependency_overrides[get_current_user] = previous


def _send(http, sender, body):
    with acting_as(sender):
        return http.post("/games/case-closed/messages", json={"body": body})


def _report(db, message_id):
    with db.sessions() as session:
        return session.scalar(
            select(CaseClosedMessageReportRecord).where(
                CaseClosedMessageReportRecord.message_id == str(message_id)
            )
        )


def _message(db, body) -> CaseClosedMessageRecord:
    with db.sessions() as session:
        return session.scalar(
            select(CaseClosedMessageRecord).where(CaseClosedMessageRecord.body == body)
        )


def _only_report(admin_client):
    reports = admin_client.get("/admin/messages/reports").json()["reports"]
    assert len(reports) == 1, f"expected one report, got {reports}"
    return reports[0]


def _seed_matchmaking_report(db, sender_regno, reporter_regno, reported_at):
    """A matchmaking report written directly, so the merge can be tested beside Case Closed."""
    now = datetime.now(timezone.utc)
    with db.sessions.begin() as session:
        match = MatchRecord(
            user_1_regno=sender_regno, user_2_regno=reporter_regno,
            created_at=now, expires_at=now + WINDOW,
        )
        session.add(match)
        session.flush()
        conversation = ConversationRecord(
            match_id=str(match.id), initiator_regno=sender_regno,
            receiver_regno=reporter_regno, status="accepted", created_at=now,
        )
        session.add(conversation)
        session.flush()
        message = MessageRecord(
            conversation_id=str(conversation.id), sender_regno=sender_regno,
            receiver_regno=reporter_regno, body="a matchmaking message",
            created_at=now, is_read=False,
        )
        session.add(message)
        session.flush()
        report = MessageReportRecord(
            message_id=str(message.id), reporter_regno=reporter_regno,
            reported_user_regno=sender_regno, reason="mm reason",
            reported_at=reported_at, status="pending",
        )
        session.add(report)
        session.flush()
        return str(report.id)


# ------------------------------------------------------------------ filing


def test_a_partner_can_report_a_turn_and_it_stays_visible(db, cc_http, cc_players):
    _start_session(db, cc_http, cc_players)
    sent = _send(cc_http, cc_players["a"], "a suspicious claim")
    assert sent.status_code == 201, sent.text

    with acting_as(cc_players["b"]):
        filed = cc_http.post(
            "/games/case-closed/messages/report",
            json={"message_id": sent.json()["id"], "reason": REASON},
        )
    assert filed.status_code == 201, filed.text

    report = _report(db, sent.json()["id"])
    assert report is not None
    assert report.reporter_regno == PARTNER_REGNO
    assert report.reported_user_regno == AUTHOR_REGNO
    assert report.reason == REASON
    assert report.status == "pending"
    assert report.source == "user"
    assert report.reviewed_at is None

    # Filing a report hides nothing: the turn is still there for both participants.
    with acting_as(cc_players["b"]):
        page = cc_http.get("/games/case-closed/messages").json()
    assert [m["body"] for m in page["messages"]] == ["a suspicious claim"]


def test_the_sender_cannot_report_their_own_turn(db, cc_http, cc_players):
    _start_session(db, cc_http, cc_players)
    sent = _send(cc_http, cc_players["a"], "my own words")

    with acting_as(cc_players["a"]):
        response = cc_http.post(
            "/games/case-closed/messages/report",
            json={"message_id": sent.json()["id"], "reason": REASON},
        )

    assert response.status_code == 403
    with db.sessions() as session:
        assert session.scalar(select(CaseClosedMessageReportRecord)) is None


def test_a_non_participant_cannot_report_a_turn(db, cc_http, cc_players):
    _start_session(db, cc_http, cc_players)
    sent = _send(cc_http, cc_players["a"], "a private claim")

    with acting_as(cc_players["c"]):
        response = cc_http.post(
            "/games/case-closed/messages/report",
            json={"message_id": sent.json()["id"], "reason": REASON},
        )

    assert response.status_code == 403
    with db.sessions() as session:
        assert session.scalar(select(CaseClosedMessageReportRecord)) is None


def test_the_same_turn_cannot_be_reported_twice_by_the_same_partner(db, cc_http, cc_players):
    _start_session(db, cc_http, cc_players)
    sent = _send(cc_http, cc_players["a"], "report me once")

    with acting_as(cc_players["b"]):
        first = cc_http.post(
            "/games/case-closed/messages/report",
            json={"message_id": sent.json()["id"], "reason": REASON},
        )
        again = cc_http.post(
            "/games/case-closed/messages/report",
            json={"message_id": sent.json()["id"], "reason": "again"},
        )

    assert first.status_code == 201
    assert again.status_code == 409
    with db.sessions() as session:
        assert len(session.scalars(select(CaseClosedMessageReportRecord)).all()) == 1


# ------------------------------------------------------------------ the queue


def test_a_case_closed_report_arrives_in_the_chat_messages_queue(db, cc_http, cc_players, admin_client):
    _start_session(db, cc_http, cc_players)
    sent = _send(cc_http, cc_players["a"], "the reported turn")
    with acting_as(cc_players["b"]):
        cc_http.post(
            "/games/case-closed/messages/report",
            json={"message_id": sent.json()["id"], "reason": REASON},
        )

    report = _only_report(admin_client)

    assert report["kind"] == "case_closed"
    assert report["body"] == "the reported turn"
    assert report["message_id"] == sent.json()["id"]
    assert report["reporter"]["regno"] == PARTNER_REGNO
    assert report["reported_user"]["regno"] == AUTHOR_REGNO
    assert report["reason"] == REASON
    assert report["status"] == "pending"
    assert report["content_removed"] is False
    assert report["removed_by_regno"] is None
    assert report["conversation"]["status"] is None
    assert [p["regno"] for p in report["conversation"]["participants"]] == [
        AUTHOR_REGNO,
        PARTNER_REGNO,
    ]
    assert report["conversation"]["message_count"] == 1


def test_the_queue_total_counts_case_closed_reports(db, cc_http, cc_players, admin_client):
    _start_session(db, cc_http, cc_players)
    sent = _send(cc_http, cc_players["a"], "a reported turn")
    with acting_as(cc_players["b"]):
        cc_http.post(
            "/games/case-closed/messages/report",
            json={"message_id": sent.json()["id"], "reason": REASON},
        )

    body = admin_client.get("/admin/messages/reports").json()
    assert body["total"] == 1
    assert pending_report_totals(db)["messages"] == 1


def test_search_matches_a_case_closed_body(db, cc_http, cc_players, admin_client):
    _start_session(db, cc_http, cc_players)
    sent = _send(cc_http, cc_players["a"], "a uniquely searchable claim")
    with acting_as(cc_players["b"]):
        cc_http.post(
            "/games/case-closed/messages/report",
            json={"message_id": sent.json()["id"], "reason": REASON},
        )

    assert only_report_with_body(admin_client, "uniquely searchable")["kind"] == "case_closed"
    assert admin_client.get("/admin/messages/reports?q=no-such-text").json()["reports"] == []


def only_report_with_body(admin_client, needle):
    reports = admin_client.get(f"/admin/messages/reports?q={needle}").json()["reports"]
    assert len(reports) == 1
    return reports[0]


def test_the_two_message_kinds_merge_newest_first(db, cc_http, cc_players, admin_client):
    _start_session(db, cc_http, cc_players)
    sent = _send(cc_http, cc_players["a"], "the case closed turn")
    with acting_as(cc_players["b"]):
        cc_http.post(
            "/games/case-closed/messages/report",
            json={"message_id": sent.json()["id"], "reason": REASON},
        )
    cc_report = _report(db, sent.json()["id"])

    # A matchmaking report dated between the two backdates nothing; it is simply newer.
    older = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    newer = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)
    with db.sessions.begin() as session:
        session.get(CaseClosedMessageReportRecord, str(cc_report.id)).reported_at = older
    _seed_matchmaking_report(db, AUTHOR_REGNO, PARTNER_REGNO, newer)

    reports = admin_client.get("/admin/messages/reports").json()["reports"]

    assert [report["kind"] for report in reports] == ["matchmaking", "case_closed"]
    assert admin_client.get("/admin/messages/reports").json()["total"] == 2


# ------------------------------------------------------------------ ruling


def test_removing_a_case_closed_turn_hides_it_from_both_participants(db, cc_http, cc_players, admin_client):
    _start_session(db, cc_http, cc_players)
    sent = _send(cc_http, cc_players["a"], "the removed turn")
    with acting_as(cc_players["b"]):
        filed = cc_http.post(
            "/games/case-closed/messages/report",
            json={"message_id": sent.json()["id"], "reason": REASON},
        )

    response = admin_client.patch(
        f"/admin/messages/reports/{filed.json()['id']}?status=removed&kind=case_closed", json={}
    )

    assert response.status_code == 200, response.text
    assert response.json()["content_removed"] is True

    with db.sessions() as session:
        message = session.get(CaseClosedMessageRecord, sent.json()["id"])
        assert message is not None
        assert message.body == "the removed turn"
        assert message.removed_at is not None
        assert message.removed_by_regno is not None

    for player in ("a", "b"):
        with acting_as(cc_players[player]):
            page = cc_http.get("/games/case-closed/messages").json()
        assert page["messages"] == [], f"the removed turn is still visible to {player}"


def test_dismissing_a_case_closed_report_leaves_the_turn(db, cc_http, cc_players, admin_client):
    _start_session(db, cc_http, cc_players)
    sent = _send(cc_http, cc_players["a"], "the dismissed turn")
    with acting_as(cc_players["b"]):
        filed = cc_http.post(
            "/games/case-closed/messages/report",
            json={"message_id": sent.json()["id"], "reason": REASON},
        )

    response = admin_client.patch(
        f"/admin/messages/reports/{filed.json()['id']}?status=dismissed&kind=case_closed", json={}
    )

    assert response.status_code == 200
    assert response.json()["content_removed"] is False
    with db.sessions() as session:
        assert session.get(CaseClosedMessageRecord, sent.json()["id"]).removed_at is None
    report = _only_report(admin_client)
    assert report["status"] == "dismissed"
    assert report["content_removed"] is False


def test_removing_a_case_closed_turn_notifies_the_author_and_the_reporter(db, cc_http, cc_players, admin_client):
    _start_session(db, cc_http, cc_players)
    sent = _send(cc_http, cc_players["a"], "the notified turn")
    with acting_as(cc_players["b"]):
        filed = cc_http.post(
            "/games/case-closed/messages/report",
            json={"message_id": sent.json()["id"], "reason": REASON},
        )

    admin_client.patch(
        f"/admin/messages/reports/{filed.json()['id']}?status=removed&kind=case_closed", json={}
    )

    def headings(regno):
        with db.sessions() as session:
            return [
                row.heading
                for row in session.scalars(
                    select(NotificationRecord)
                    .where(NotificationRecord.recipient_regno == regno)
                    .order_by(NotificationRecord.created_at.desc())
                ).all()
            ]

    assert "Message removed" in headings(AUTHOR_REGNO)
    assert "Your report was acted on" in headings(PARTNER_REGNO)


def test_the_kind_defaults_to_matchmaking_so_a_case_closed_id_is_not_found(admin_client, db, cc_http, cc_players):
    """The default keeps every matchmaking caller working and never guesses the table."""
    _start_session(db, cc_http, cc_players)
    sent = _send(cc_http, cc_players["a"], "a defaulted turn")
    with acting_as(cc_players["b"]):
        filed = cc_http.post(
            "/games/case-closed/messages/report",
            json={"message_id": sent.json()["id"], "reason": REASON},
        )

    response = admin_client.patch(f"/admin/messages/reports/{filed.json()['id']}?status=dismissed", json={})

    assert response.status_code == 404


# ------------------------------------------------------------------ restore


def test_an_azure_hidden_turn_can_be_restored(db, cc_http, cc_players, monkeypatch, admin_client):
    _start_session(db, cc_http, cc_players)
    _mock_moderation(monkeypatch, {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 6})
    refused = _send(cc_http, cc_players["a"], "hidden then restored")
    assert refused.status_code == 422

    hidden = _message(db, "hidden then restored")
    assert hidden is not None and hidden.removed_at is not None

    response = admin_client.patch(
        f"/admin/messages/{hidden.id}/unhide?kind=case_closed", json={}
    )

    assert response.status_code == 200, response.text
    assert response.json()["already_visible"] is False
    with db.sessions() as session:
        restored = session.get(CaseClosedMessageRecord, str(hidden.id))
        assert restored.removed_at is None
        assert restored.removed_by_regno is None

    with acting_as(cc_players["b"]):
        page = cc_http.get("/games/case-closed/messages").json()
    assert [m["body"] for m in page["messages"]] == ["hidden then restored"]


def test_a_moderator_hidden_turn_cannot_be_restored(db, cc_http, cc_players, admin_client):
    _start_session(db, cc_http, cc_players)
    sent = _send(cc_http, cc_players["a"], "a moderator removal")
    with acting_as(cc_players["b"]):
        filed = cc_http.post(
            "/games/case-closed/messages/report",
            json={"message_id": sent.json()["id"], "reason": REASON},
        )
    admin_client.patch(
        f"/admin/messages/reports/{filed.json()['id']}?status=removed&kind=case_closed", json={}
    )

    response = admin_client.patch(
        f"/admin/messages/{sent.json()['id']}/unhide?kind=case_closed", json={}
    )

    assert response.status_code == 409


def test_restoring_a_visible_turn_answers_already_visible(db, cc_http, cc_players, admin_client):
    _start_session(db, cc_http, cc_players)
    sent = _send(cc_http, cc_players["a"], "still visible")

    response = admin_client.patch(
        f"/admin/messages/{sent.json()['id']}/unhide?kind=case_closed", json={}
    )

    assert response.status_code == 200
    assert response.json()["already_visible"] is True
