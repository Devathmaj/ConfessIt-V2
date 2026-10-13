"""What ``GET /admin/stats`` reports, and what it deliberately does not.

The dashboard's tiles are the only place an admin sees the shape of the platform at a glance,
so a figure that is quietly wrong here is worse than one that is missing: it is read as fact.
The tests below pin the parts that had to be reasoned about rather than merely counted.

**The report backlog is counted once, and split three ways.** ``pending_reports`` is the sum of
the three queues and ``pending_report_queues`` is that sum's parts, both read through
``pending_report_totals``. The last test in this file is the one that matters most: the
dashboard's split must equal the ``total`` each queue's own endpoint reports for ``pending``,
so a card and the screen behind it cannot disagree.

**Only pending work counts.** A report that has been dismissed, upheld or taken down is not
something anybody is waiting on, and a love note awaiting approval is not a report at all --
it has its own tile, so counting it in the backlog would double-count work under two headings
and inflate the number an admin reads as their queue.

Nothing here asserts what a report *should* be ruled on, or that any ruling happens at all.
These are figures to be read.
"""

from datetime import datetime, timedelta, timezone

from app.db_models import (
    ConfessionRecord,
    ConversationRecord,
    LoveNoteRecord,
    LoveNoteReportRecord,
    MatchRecord,
    MessageRecord,
    MessageReportRecord,
    ReportRecord,
    UserRecord,
)
from tests.chat_helpers import make_message_record

REACTIONS = {"heart": [], "haha": [], "whoa": [], "heartbreak": []}
REASON = "this breaks the rules"


# --- builders ------------------------------------------------------------------


def make_user(db, regno, name=None):
    name = name or f"User {regno}"
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno,
            name=name,
            email=f"{regno}@test.com",
            year=2, department="Test_Class",
            gender="other",
        )
        session.add(row)
        session.flush()
        return {"id": str(row.id), "regno": regno, "name": name}


def make_confession(db, author, *, body="a confession"):
    with db.sessions.begin() as session:
        row = ConfessionRecord(
            body=body,
            recipient_name="Someone In Particular",
            anonymous=False,
            comments_enabled=True,
            created_at=datetime.now(timezone.utc),
            author_id=author["id"],
            reactions=dict(REACTIONS),
        )
        session.add(row)
        session.flush()
        return str(row.id)


def make_note(db, sender, recipient, *, status="pending_review", body="a love note"):
    with db.sessions.begin() as session:
        row = LoveNoteRecord(
            sender_id=sender["id"],
            recipient_id=recipient["id"],
            image_public_id="love_notes/mock-note-id/image", image_format="png",
            body=body,
            anonymous=False,
            status=status,
            created_at=datetime.now(timezone.utc),
        )
        session.add(row)
        session.flush()
        return str(row.id)


def make_message(db, sender, receiver, *, body="hello"):
    """One message in an accepted conversation. ``message_reports`` hangs off ``messages``."""
    now = datetime.now(timezone.utc)
    with db.sessions.begin() as session:
        match = MatchRecord(
            user_1_regno=sender["regno"], user_2_regno=receiver["regno"],
            created_at=now, expires_at=now + timedelta(days=30),
        )
        session.add(match)
        session.flush()
        conversation = ConversationRecord(
            match_id=str(match.id),
            initiator_regno=sender["regno"],
            receiver_regno=receiver["regno"],
            status="accepted",
            created_at=now,
        )
        session.add(conversation)
        session.flush()
        row = make_message_record(
            conversation_id=str(conversation.id),
            sender_regno=sender["regno"],
            receiver_regno=receiver["regno"],
            body=body,
            created_at=now,
            delete_after=now + timedelta(days=1),
            is_read=False,
        )
        session.add(row)
        session.flush()
        return str(row.id)


def confession_report(db, confession_id, reporter, *, status="pending"):
    with db.sessions.begin() as session:
        row = ReportRecord(
            confession_id=confession_id,
            reported_by_id=reporter["id"],
            reported_by_name=reporter["name"],
            reason=REASON,
            created_at=datetime.now(timezone.utc),
            status=status,
        )
        session.add(row)
        session.flush()
        return str(row.id)


def love_note_report(db, note_id, reporter_regno, *, status="pending"):
    with db.sessions.begin() as session:
        row = LoveNoteReportRecord(
            love_note_id=note_id,
            reporter_regno=reporter_regno,
            reason=REASON,
            status=status,
            reported_at=datetime.now(timezone.utc),
        )
        session.add(row)
        session.flush()
        return str(row.id)


def message_report(db, message_id, reporter, reported, *, status="pending"):
    with db.sessions.begin() as session:
        row = MessageReportRecord(
            message_id=message_id,
            reporter_regno=reporter["regno"],
            reported_user_regno=reported["regno"],
            reason=REASON,
            status=status,
            reported_at=datetime.now(timezone.utc),
        )
        session.add(row)
        session.flush()
        return str(row.id)


# --- the payload ---------------------------------------------------------------


def test_an_empty_platform_reports_no_backlog(admin_client):
    body = admin_client.get("/admin/stats").json()

    assert body["pending_reports"] == 0
    assert body["pending_report_queues"] == {"confessions": 0, "love_notes": 0, "messages": 0}
    assert body["pending_love_notes"] == 0


def test_the_headline_is_the_sum_of_the_three_queues(admin_client, db):
    reporter = make_user(db, "reporter")
    author = make_user(db, "author")

    confession_report(db, make_confession(db, author), reporter)
    confession_report(db, make_confession(db, author), reporter)
    love_note_report(db, make_note(db, author, reporter), reporter["regno"])
    message_report(db, make_message(db, author, reporter), reporter, author)

    body = admin_client.get("/admin/stats").json()

    assert body["pending_report_queues"] == {"confessions": 2, "love_notes": 1, "messages": 1}
    assert body["pending_reports"] == 4


def test_a_report_that_has_been_ruled_on_stops_counting(admin_client, db):
    """Dismissed, upheld and removed are all closed, so all three leave the backlog.

    Every closed status is asserted because they are separate strings in three separate
    tables. One of them being missed would show a queue as still waiting on a ruling that has
    already been made.
    """
    reporter = make_user(db, "reporter")
    author = make_user(db, "author")

    confession_report(db, make_confession(db, author), reporter, status="dismissed")
    confession_report(db, make_confession(db, author), reporter, status="reviewed")
    love_note_report(db, make_note(db, author, reporter), reporter["regno"], status="removed")
    message_report(db, make_message(db, author, reporter), reporter, author, status="dismissed")

    body = admin_client.get("/admin/stats").json()

    assert body["pending_reports"] == 0
    assert body["pending_report_queues"] == {"confessions": 0, "love_notes": 0, "messages": 0}


def test_a_note_awaiting_approval_is_not_a_report(admin_client, db):
    """The two are different queues and get their own tiles.

    A note nobody has reported is still a note waiting on a decision. Counting it as a report
    would put the same piece of work under two headings, so the report backlog would read
    higher than the number of reports anybody actually filed.
    """
    sender = make_user(db, "sender")
    recipient = make_user(db, "recipient")
    make_note(db, sender, recipient, status="pending_review")
    make_note(db, sender, recipient, status="pending_review")
    make_note(db, sender, recipient, status="approved")

    body = admin_client.get("/admin/stats").json()

    assert body["pending_love_notes"] == 2
    assert body["pending_reports"] == 0


def test_the_split_agrees_with_each_queue_endpoint(admin_client, db):
    """The drift guard: the dashboard's split is the queues' own pending totals.

    Read from the three report endpoints rather than from the database, because those are what
    a reviewer opens. If the dashboard ever counted a different slice of a queue than the one
    the queue screen lists, this fails -- which is the whole reason the figures are read
    through the same helper instead of being written out again here.
    """
    reporter = make_user(db, "reporter")
    author = make_user(db, "author")

    confession_report(db, make_confession(db, author), reporter)
    confession_report(db, make_confession(db, author), reporter)
    confession_report(db, make_confession(db, author), reporter, status="dismissed")
    love_note_report(db, make_note(db, author, reporter), reporter["regno"])
    message_report(db, make_message(db, author, reporter), reporter, author)
    message_report(db, make_message(db, author, reporter), reporter, author, status="reviewed")

    queues = admin_client.get("/admin/stats").json()["pending_report_queues"]

    assert queues["confessions"] == admin_client.get(
        "/admin/confessions/reports", params={"status": "pending", "limit": 1}
    ).json()["total"]
    assert queues["love_notes"] == admin_client.get(
        "/admin/love-notes/reports", params={"status": "pending", "limit": 1}
    ).json()["total"]
    assert queues["messages"] == admin_client.get(
        "/admin/messages/reports", params={"status": "pending", "limit": 1}
    ).json()["total"]