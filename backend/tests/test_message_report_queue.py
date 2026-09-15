"""The chat message report queue.

Message reports had a queue before the unified Reports Review screen, but nothing tested it: no
module anywhere filed one or read the queue back. The rules the other two queues are held to --
the content travels with the report, the envelope matches, closing a report records who closed
it and when, and a dismissal leaves the content alone -- were therefore unenforced here.

These tests fill that gap, and they pin the two names the unified screen standardised on: the
filter arrives as ``?status=`` (it used to be ``?status_filter=``) and close-out answers
``{id, status}`` (it used to answer a sentence). No client called either endpoint before the
unified page existed, so both renames were free, and a rename nobody called is exactly the kind
of change that silently regresses.

Closing a report has two outcomes rather than three. ``dismissed`` records that a human looked
and the message stays. ``removed`` takes the message down while keeping the row, drops it from
both participants' transcripts and from the conversation preview, and notifies the sender and
the reporter. Most of what follows is about that removal, and the recurring question is whether
it destroyed anything: it must not, because the report is about this message and a decision
nobody can audit afterwards is a decision that gets repeated.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db_models import (
    ConversationRecord,
    MatchRecord,
    MessageRecord,
    MessageReportRecord,
    NotificationRecord,
    UserRecord,
)
from app.main import app
from app.models import AccountType, UserDetails
from app.postgres import new_id
from app.services.auth_service import get_current_user

WINDOW = timedelta(hours=4)
REASON = "this is hateful"
RECIPIENT = "receiver"


def make_user(db, regno, gender="other"):
    with db.sessions.begin() as session:
        session.add(UserRecord(
            regno=regno, name=f"User {regno}", email=f"{regno}@test.com",
            class_name="Test_Class", gender=gender,
        ))


def as_user(db, regno) -> UserDetails:
    with db.sessions() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
    return UserDetails(
        id=str(row.id), regno=row.regno, name=row.name, email=row.email,
        class_name=row.class_name, gender=row.gender,
        matchmaking_enabled=row.matchmaking_enabled,
        notifications_enabled=row.notifications_enabled,
        is_blocked=row.is_blocked, account_type=AccountType.USERS.value,
    )


def make_accepted_conversation(db, sender, receiver):
    """A conversation already accepted.

    Reporting requires it: a message in a conversation that was never accepted cannot be
    reported at all, which is why the fixture builds the match as well as the conversation.
    """
    now = datetime.now(timezone.utc)
    with db.sessions.begin() as session:
        match = MatchRecord(
            user_1_regno=sender, user_2_regno=receiver, created_at=now, expires_at=now + WINDOW,
        )
        session.add(match)
        session.flush()
        conversation = ConversationRecord(
            match_id=str(match.id), initiator_regno=sender, receiver_regno=receiver,
            status="accepted", created_at=now,
        )
        session.add(conversation)
        session.flush()
        return str(conversation.id)


def message_id_for(db, body) -> str:
    with db.sessions() as session:
        row = session.scalar(select(MessageRecord).where(MessageRecord.body == body))
    return str(row.id)


def only_report(response):
    """The single report in the queue, so a test cannot quietly pass on the wrong row."""
    reports = response.json()["reports"]
    assert len(reports) == 1, f"expected one report, got {reports}"
    return reports[0]


@pytest.fixture
def login_as(db):
    """Run the calls inside the block as a named participant.

    The shared override resolves to one fixed account, which is enough for "a signed-in user"
    but not for a rule about *which* user: only the recipient may report a message, so the
    sender has to be the caller for the refusal to mean anything.
    """

    @contextmanager
    def _login(regno: str):
        user = as_user(db, regno)
        previous = app.dependency_overrides.get(get_current_user)
        app.dependency_overrides[get_current_user] = lambda: user
        try:
            yield
        finally:
            app.dependency_overrides[get_current_user] = previous

    return _login


@pytest.fixture
def thread(db, client, login_as):
    """The standing cast: ``sender`` writes one message to ``receiver``, who reports it.

    Filed through the HTTP route rather than by writing the row, so the queue tests also cover
    the path a report actually arrives by -- the requirement being pinned is that a filed report
    shows up, not that a row can be selected.
    """
    make_user(db, "sender")
    make_user(db, RECIPIENT)
    conversation_id = make_accepted_conversation(db, "sender", RECIPIENT)

    with login_as("sender"):
        sent = client.post("/messages/send", json={"conversation_id": conversation_id, "body": "a message"})
    message_id = sent.json()["id"]

    with login_as(RECIPIENT):
        filed = client.post("/messages/report", json={"message_id": message_id, "reason": REASON})

    return {
        "message_id": message_id,
        "report_id": filed.json()["id"],
        "conversation_id": conversation_id,
        "filed": filed,
    }


# ------------------------------------------------------------------ filing and reading


def test_filing_a_report_is_accepted_and_records_the_reporter_and_the_sender(thread, db):
    assert thread["filed"].status_code == 201, thread["filed"].text

    with db.sessions() as session:
        report = session.get(MessageReportRecord, thread["report_id"])
        assert report.reporter_regno == RECIPIENT
        assert report.reported_user_regno == "sender"
        assert report.reason == REASON
        assert report.status == "pending"
        assert report.reviewed_at is None and report.reviewed_by_regno is None


def test_a_reported_message_arrives_with_the_message_and_both_parties(admin_client, thread):
    report = only_report(admin_client.get("/admin/messages/reports"))

    assert report["body"] == "a message"
    assert report["message_id"] == thread["message_id"]
    assert report["conversation_id"] == thread["conversation_id"]
    assert report["reporter"]["regno"] == RECIPIENT
    assert report["reported_user"]["regno"] == "sender"
    assert report["reason"] == REASON
    assert report["status"] == "pending"
    assert report["reported_at"] is not None
    assert report["reviewed_at"] is None
    assert report["reviewed_by_regno"] is None


def test_the_queue_answers_the_envelope_the_other_two_queues_answer(admin_client, thread):
    body = admin_client.get("/admin/messages/reports").json()

    assert body["total"] == 1
    assert body["skip"] == 0
    assert body["limit"] == 50


def test_the_filter_arrives_as_status(admin_client, thread):
    """The wire name, not the Python parameter name.

    ``status_filter`` is the parameter because ``status`` is the fastapi module in that router;
    the client sends ``?status=`` like the other two queues. Asserted on the wire, so a client
    that sends ``status_filter`` fails here rather than silently filtering nothing.
    """
    matching = admin_client.get("/admin/messages/reports?status=pending")

    assert matching.status_code == 200
    assert only_report(matching)["id"] == thread["report_id"]
    assert admin_client.get("/admin/messages/reports?status=dismissed").json()["reports"] == []
    assert admin_client.get("/admin/messages/reports?status=removed").json()["reports"] == []


def test_the_newest_report_comes_first(admin_client, db, client, login_as):
    """Ordered by ``reported_at``.

    Three reports are filed a day apart by backdating the column, because the alternative --
    three reports written in the same millisecond -- would be asserting how PostgreSQL breaks a
    tie rather than what the queue promises.
    """
    make_user(db, "sender")
    make_user(db, RECIPIENT)
    conversation_id = make_accepted_conversation(db, "sender", RECIPIENT)
    filed = []

    for index in range(3):
        with login_as("sender"):
            sent = client.post("/messages/send", json={
                "conversation_id": conversation_id, "body": f"message {index}",
            })
        with login_as(RECIPIENT):
            report = client.post("/messages/report", json={
                "message_id": sent.json()["id"], "reason": f"reason {index}",
            })
        with db.sessions.begin() as session:
            session.get(MessageReportRecord, report.json()["id"]).reported_at = (
                datetime(2026, 3, 14, 12, 0, tzinfo=timezone.utc) + timedelta(days=index)
            )
        filed.append(report.json()["id"])

    listed = [item["id"] for item in admin_client.get("/admin/messages/reports").json()["reports"]]

    assert listed == list(reversed(filed))


# ---------------------------------------------------------------------------- closing out


def test_closing_a_report_records_who_closed_it_and_when(admin_client, thread):
    response = admin_client.patch(f"/admin/messages/reports/{thread['report_id']}?status=dismissed", json={})

    assert response.status_code == 200
    assert response.json() == {
        "id": thread["report_id"], "status": "dismissed", "content_removed": False,
    }

    report = only_report(admin_client.get("/admin/messages/reports"))
    assert report["status"] == "dismissed"
    assert report["reviewed_by_regno"] is not None
    assert report["reviewed_at"] is not None


def test_the_older_word_for_dismissing_is_still_accepted(admin_client, thread):
    """``reviewed`` was the word this queue used before the screen grew a remove button.

    It is stored as sent, so the row's own history stays honest, but it takes nothing down: the
    action it named did not change, and that action was "a human looked and it stays". Answering
    a stale caller beats a 422 over a rename.
    """
    response = admin_client.patch(f"/admin/messages/reports/{thread['report_id']}?status=reviewed", json={})

    assert response.status_code == 200
    assert response.json() == {
        "id": thread["report_id"], "status": "reviewed", "content_removed": False,
    }
    assert only_report(admin_client.get("/admin/messages/reports"))["content_removed"] is False


def test_closing_a_report_leaves_the_message_alone(admin_client, thread, db):
    """A dismissal changes nothing about the message. A report is one person's word, and the
    message is judged in its own right -- a queue that could destroy the evidence on dismissal
    would remove the reason to read it at all."""
    admin_client.patch(f"/admin/messages/reports/{thread['report_id']}?status=dismissed", json={})

    with db.sessions() as session:
        message = session.get(MessageRecord, thread["message_id"])
        assert message is not None
        assert message.body == "a message"
        assert message.removed_at is None


# --------------------------------------------------------------- taking the message down


def test_removing_a_message_keeps_both_the_message_and_the_report(admin_client, thread, db):
    """The load-bearing assertion of soft removal: nothing is destroyed.

    The report is about a message, and somebody has to answer for this decision later. If the
    removal deleted the row, the report would cascade away with it and there would be nothing
    left to answer with.
    """
    response = admin_client.patch(f"/admin/messages/reports/{thread['report_id']}?status=removed", json={})

    assert response.status_code == 200
    assert response.json()["content_removed"] is True
    with db.sessions() as session:
        message = session.get(MessageRecord, thread["message_id"])
        assert message is not None
        assert message.body == "a message"
        assert message.removed_at is not None
        assert message.removed_by_regno is not None
        report = session.get(MessageReportRecord, thread["report_id"])
        assert report is not None
        assert report.status == "removed"


def test_a_removed_message_leaves_the_conversation_for_both_participants(
    thread, client, login_as, admin_client
):
    """Neither party can still read it.

    The recipient reported it, so checking only the sender would be checking the easy half: the
    sender has to lose access to their own message too, or "removed" would mean only "hidden from
    the person who complained".
    """
    admin_client.patch(f"/admin/messages/reports/{thread['report_id']}?status=removed", json={})

    for regno in ("sender", RECIPIENT):
        with login_as(regno):
            history = client.get(f"/messages/{thread['conversation_id']}").json()
        bodies = [message["body"] for message in history["messages"]]
        assert "a message" not in bodies, f"the removed message is still visible to {regno}"


def test_removing_a_message_moves_the_conversation_preview_off_it(thread, db, admin_client):
    """The conversation preview is a copy of the newest message's text.

    Leaving it pointing at removed content would keep showing the ruled-against message in the
    conversation list to both participants -- the one place a participant would still read
    something a moderator has taken down.
    """
    admin_client.patch(f"/admin/messages/reports/{thread['report_id']}?status=removed", json={})

    with db.sessions() as session:
        conversation = session.get(ConversationRecord, thread["conversation_id"])
        assert "a message" not in (conversation.last_message_preview or "")


def test_a_removed_message_is_still_visible_to_staff(thread, admin_client):
    """Staff keep their moderation read, or an appeal could never be answered.

    The transcript is the appeal record, so it also has to say which messages are gone rather
    than leaving a moderator to infer it from a hole in the list.
    """
    admin_client.patch(f"/admin/messages/reports/{thread['report_id']}?status=removed", json={})

    detail = admin_client.get(f"/admin/conversations/{thread['conversation_id']}").json()

    assert [message["body"] for message in detail["messages"]] == ["a message"]
    assert detail["messages"][0]["removed_at"] is not None
    assert detail["messages"][0]["removed_by_regno"] is not None


def test_removing_a_message_twice_reports_that_it_removed_nothing(admin_client, thread, db):
    """The response says whether *this* call took the content down.

    A second click must not report success again: a moderator clicking twice would be told
    twice that they removed something that had been gone since before they arrived, and
    ``removed_at`` must keep pointing at when it actually came down.
    """
    admin_client.patch(f"/admin/messages/reports/{thread['report_id']}?status=removed", json={})
    with db.sessions() as session:
        first = session.get(MessageRecord, thread["message_id"]).removed_at

    again = admin_client.patch(f"/admin/messages/reports/{thread['report_id']}?status=removed", json={})

    assert again.json()["content_removed"] is False
    with db.sessions() as session:
        assert session.get(MessageRecord, thread["message_id"]).removed_at == first


def test_removing_a_message_tells_the_sender_and_the_reporter(thread, db, admin_client):
    """Both parties hear the outcome.

    The reporter here is the receiver, so this is also the check that the notice reaches the
    person who received the message rather than the one who wrote it.
    """
    admin_client.patch(f"/admin/messages/reports/{thread['report_id']}?status=removed", json={})

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

    assert headings("sender") == ["Message removed"]
    assert headings(RECIPIENT) == ["Your report was acted on"]


def test_pending_is_not_a_destination(admin_client, thread):
    """The regex is the enforcement, so a client cannot reopen a report by sending it back."""
    response = admin_client.patch(f"/admin/messages/reports/{thread['report_id']}?status=pending", json={})

    assert response.status_code == 422
    assert only_report(admin_client.get("/admin/messages/reports"))["status"] == "pending"


def test_a_malformed_report_id_is_a_400_and_leaves_the_queue_readable(admin_client, thread):
    assert admin_client.patch("/admin/messages/reports/not-a-uuid?status=dismissed", json={}).status_code == 400
    assert admin_client.get("/admin/messages/reports").status_code == 200


def test_a_report_that_does_not_exist_is_a_404(admin_client):
    response = admin_client.patch(f"/admin/messages/reports/{new_id()}?status=dismissed", json={})

    assert response.status_code == 404


# -------------------------------------------------------------------------- permissions


def test_reading_the_queue_needs_the_reports_capability(client, thread):
    assert client.get("/admin/messages/reports").status_code == 403


def test_a_moderator_can_work_the_whole_queue(moderator_client, thread):
    assert moderator_client.get("/admin/messages/reports").status_code == 200
    assert moderator_client.patch(
        f"/admin/messages/reports/{thread['report_id']}?status=dismissed", json={},
    ).status_code == 200


def test_opening_the_queue_is_not_the_same_permission_as_ruling_on_it(admin_client, thread, monkeypatch):
    """Read and rule are separate capabilities, and the point of separating them is that an
    account can have one without the other.

    ``has_capability`` decides from the account table alone, so there is no such account to
    build here; the seam is stubbed to the account that would exist -- ``reports:review`` and
    nothing else -- and the assertion is that the queue opens and the judgement is refused.
    """
    from app.services import authorization

    monkeypatch.setattr(
        authorization, "has_capability",
        lambda account_type, capability: capability.value != "reports:moderate",
    )

    assert admin_client.get("/admin/messages/reports").status_code == 200
    assert admin_client.patch(
        f"/admin/messages/reports/{thread['report_id']}?status=dismissed", json={},
    ).status_code == 403
    assert only_report(admin_client.get("/admin/messages/reports"))["status"] == "pending"


# --------------------------------------------------------------------- filing the report


def test_the_sender_cannot_report_their_own_message(db, client, login_as):
    """A report means "this was sent to me", so filing one against your own message is refused
    rather than recorded."""
    make_user(db, "sender")
    make_user(db, RECIPIENT)
    conversation_id = make_accepted_conversation(db, "sender", RECIPIENT)

    with login_as("sender"):
        sent = client.post("/messages/send", json={"conversation_id": conversation_id, "body": "my own words"})
        response = client.post("/messages/report", json={
            "message_id": sent.json()["id"], "reason": REASON,
        })

    assert response.status_code == 403
    with db.sessions() as session:
        assert session.scalar(select(MessageReportRecord)) is None


def test_the_same_message_cannot_be_reported_twice_by_the_same_person(thread, db, client, login_as):
    with login_as(RECIPIENT):
        again = client.post("/messages/report", json={
            "message_id": thread["message_id"], "reason": REASON,
        })

    assert again.status_code == 409
    with db.sessions() as session:
        assert len(session.scalars(select(MessageReportRecord)).all()) == 1


def test_a_message_that_does_not_exist_is_a_404(db, client, login_as):
    make_user(db, RECIPIENT)

    with login_as(RECIPIENT):
        response = client.post("/messages/report", json={"message_id": new_id(), "reason": REASON})

    assert response.status_code == 404
