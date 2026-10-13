"""Searching the three staff report queues in the database.

Each report queue already pages in SQL; without a search a moderator finds one report by
reading every page. These tests pin that ``q`` narrows each queue in SQL -- reason,
reported content and the parties named on the row -- with the total counted under the
same filter so the screen pages the question it asked rather than the queue behind it.

One boundary matters more than the matching itself: searching an anonymous confession
author's name must surface nothing. That identity is withheld from staff everywhere, and
a search that matched it would be an oracle for it -- asking for the name would return a
row the response itself refuses to name.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.db_models import (
    ConfessionCommentRecord,
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
from app.services.storage_service import storage_service
from app.utils import escape_like_literal
from tests.chat_helpers import make_message_record

REACTIONS = {"heart": [], "haha": [], "whoa": [], "heartbreak": []}


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
        return {"id": str(row.id), "regno": row.regno, "name": name}


def make_confession(db, author, *, anonymous=False, body="a confession"):
    with db.sessions.begin() as session:
        row = ConfessionRecord(
            body=body,
            recipient_name="Someone In Particular",
            anonymous=anonymous,
            comments_enabled=True,
            created_at=datetime.now(timezone.utc),
            author_id=author["id"],
            reactions=dict(REACTIONS),
        )
        session.add(row)
        session.flush()
        return str(row.id)


def make_comment(db, confession_id, author, body="a comment"):
    with db.sessions.begin() as session:
        row = ConfessionCommentRecord(
            confession_id=confession_id,
            user_id=author["id"],
            body=body,
            created_at=datetime.now(timezone.utc),
        )
        session.add(row)
        session.flush()
        return str(row.id)


def file_confession_report(db, target_id, reporter, *, kind="confession", reason="harmful content"):
    with db.sessions.begin() as session:
        row = ReportRecord(
            confession_id=target_id if kind == "confession" else None,
            comment_id=target_id if kind == "comment" else None,
            reported_by_id=reporter["id"],
            reported_by_name=reporter["name"],
            reason=reason,
            created_at=datetime.now(timezone.utc),
            status="pending",
        )
        session.add(row)
        session.flush()
        return str(row.id)


def make_note(db, sender, recipient, *, body="a love note"):
    with db.sessions.begin() as session:
        row = LoveNoteRecord(
            id=str(uuid4()),
            sender_id=sender["id"],
            recipient_id=recipient["id"],
            image_public_id=None,
            image_format="png",
            body=body,
            moderation_body=body,
            anonymous=False,
            status="approved",
            created_at=datetime.now(timezone.utc),
        )
        row.image_public_id = storage_service.love_note_public_id(str(row.id))
        session.add(row)
        session.flush()
        return str(row.id)


def file_note_report(db, note_id, reporter, *, reason="harassment"):
    with db.sessions.begin() as session:
        row = LoveNoteReportRecord(
            love_note_id=note_id,
            reporter_regno=reporter["regno"],
            reason=reason,
            status="pending",
            reported_at=datetime.now(timezone.utc),
        )
        session.add(row)
        session.flush()
        return str(row.id)


def make_message(db, sender, receiver, *, body="hello"):
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


def file_message_report(db, message_id, reporter, reported_user_regno, *, reason="threats"):
    with db.sessions.begin() as session:
        row = MessageReportRecord(
            message_id=message_id,
            reporter_regno=reporter["regno"],
            reported_user_regno=reported_user_regno,
            reason=reason,
            status="pending",
            reported_at=datetime.now(timezone.utc),
        )
        session.add(row)
        session.flush()
        return str(row.id)


def reasons(page):
    return [report["reason"] for report in page["reports"]]


def test_confession_search_matches_reason_reporter_and_reported_bodies(db, admin_client):
    author = make_user(db, "author", name="The Author")
    reporter = make_user(db, "reporter", name="The Reporter")
    lighthouse = make_confession(db, author, body="the lighthouse post")
    file_confession_report(db, lighthouse, reporter, reason="glowing spam")
    remark_id = make_comment(db, lighthouse, reporter, body="a snide remark")
    file_confession_report(db, remark_id, author, kind="comment", reason="rude reply")

    assert reasons(admin_client.get(
        "/admin/confessions/reports", params={"q": "glowing"}).json()) == ["glowing spam"]
    assert reasons(admin_client.get(
        "/admin/confessions/reports", params={"q": "lighthouse"}).json()) == ["glowing spam"]
    assert reasons(admin_client.get(
        "/admin/confessions/reports", params={"q": "snide"}).json()) == ["rude reply"]
    # Each report is found by the name of the account that filed it: the confession
    # report by "The Reporter", the comment report by "The Author".
    assert reasons(admin_client.get(
        "/admin/confessions/reports", params={"q": "the reporter"}).json()) == ["glowing spam"]
    assert reasons(admin_client.get(
        "/admin/confessions/reports", params={"q": "the author"}).json()) == ["rude reply"]
    missed = admin_client.get("/admin/confessions/reports", params={"q": "nothing like this"}).json()
    assert missed["total"] == 0
    assert missed["reports"] == []


def test_confession_search_never_matches_an_anonymous_author(db, admin_client):
    ghost = make_user(db, "ghostwriter", name="Ghost Writer")
    witness = make_user(db, "witness", name="Witness Reader")
    faceless = make_confession(db, ghost, anonymous=True, body="a post with no name on it")
    file_confession_report(db, faceless, witness, reason="suspicious post")

    found = admin_client.get("/admin/confessions/reports", params={"q": "ghost writer"}).json()
    assert found["total"] == 0
    assert found["reports"] == []
    # The report itself is still findable -- by its own words, not by its author's.
    assert reasons(admin_client.get(
        "/admin/confessions/reports", params={"q": "suspicious"}).json()) == ["suspicious post"]


def test_confession_search_pages_the_filtered_question(db, admin_client):
    author = make_user(db, "author")
    reporter = make_user(db, "reporter")
    for index in range(3):
        target = make_confession(db, author, body=f"searchable post {index}")
        file_confession_report(db, target, reporter, reason=f"complaint {index}")

    first = admin_client.get(
        "/admin/confessions/reports", params={"q": "searchable", "skip": 0, "limit": 2}).json()
    second = admin_client.get(
        "/admin/confessions/reports", params={"q": "searchable", "skip": 2, "limit": 2}).json()
    assert first["total"] == 3
    assert len(first["reports"]) == 2
    assert len(second["reports"]) == 1
    seen = [report["id"] for page in (first, second) for report in page["reports"]]
    assert len(set(seen)) == 3


def test_love_note_search_matches_reason_body_reporter_and_sender(db, admin_client):
    sender = make_user(db, "romeo", name="Romeo Sender")
    recipient = make_user(db, "juliet", name="Juliet Reader")
    note_id = make_note(db, sender, recipient, body="a moonlit verse")
    file_note_report(db, note_id, recipient, reason="creepy verse")

    assert reasons(admin_client.get(
        "/admin/love-notes/reports", params={"q": "creepy"}).json()) == ["creepy verse"]
    assert reasons(admin_client.get(
        "/admin/love-notes/reports", params={"q": "moonlit"}).json()) == ["creepy verse"]
    assert reasons(admin_client.get(
        "/admin/love-notes/reports", params={"q": "juliet"}).json()) == ["creepy verse"]
    assert reasons(admin_client.get(
        "/admin/love-notes/reports", params={"q": "romeo"}).json()) == ["creepy verse"]
    missed = admin_client.get("/admin/love-notes/reports", params={"q": "nothing like this"}).json()
    assert missed["total"] == 0


def test_message_search_matches_reason_and_both_regnos_but_not_the_body(db, admin_client):
    sender = make_user(db, "loudmouth")
    receiver = make_user(db, "quietone")
    message_id = make_message(db, sender, receiver, body="midnight threats here")
    file_message_report(db, message_id, receiver, sender["regno"], reason="threatening tone")

    assert reasons(admin_client.get(
        "/admin/messages/reports", params={"q": "threatening"}).json()) == ["threatening tone"]
    assert reasons(admin_client.get(
        "/admin/messages/reports", params={"q": "quietone"}).json()) == ["threatening tone"]
    assert reasons(admin_client.get(
        "/admin/messages/reports", params={"q": "loudmouth"}).json()) == ["threatening tone"]
    # The body is ciphertext at rest. Searching it would require decrypting every
    # reported message for the query, so it is deliberately not a search surface.
    assert admin_client.get(
        "/admin/messages/reports", params={"q": "midnight"}).json()["total"] == 0
    missed = admin_client.get("/admin/messages/reports", params={"q": "nothing like this"}).json()
    assert missed["total"] == 0


def test_like_wildcards_match_literally_not_as_wildcards(db, admin_client):
    # "%" and "_" in q must match themselves. Unescaped, q="%" is pattern "%%%",
    # which matches every row in the queue, and q="a_b" matches "axb" too --
    # both overmatches that also defeat any index on the column. The reasons
    # below separate the readings; message bodies are ciphertext and are not
    # part of this search at all.
    sender = make_user(db, "wildmouth")
    receiver = make_user(db, "wildone")
    plain_id = make_message(db, sender, receiver, body="midnight plot here")
    file_message_report(db, plain_id, receiver, sender["regno"], reason="plain threats")
    wild_id = make_message(db, sender, receiver, body="a_b plan")
    file_message_report(db, wild_id, receiver, sender["regno"], reason="100% certain")
    decoy_id = make_message(db, sender, receiver, body="axb decoy")
    file_message_report(db, decoy_id, receiver, sender["regno"], reason="a_b plan")

    # Literal "%" matches only the row containing one (unescaped: all three).
    assert reasons(admin_client.get(
        "/admin/messages/reports", params={"q": "%"}).json()) == ["100% certain"]
    # Literal "_" matches only the row containing one (unescaped: all three).
    assert reasons(admin_client.get(
        "/admin/messages/reports", params={"q": "_"}).json()) == ["a_b plan"]
    # "a%b" is not a substring of anything here (unescaped: matches a_b plan).
    assert admin_client.get("/admin/messages/reports", params={"q": "a%b"}).json()["total"] == 0
    # "a_b" matches the literal holder only (unescaped: also "axb decoy").
    assert reasons(admin_client.get(
        "/admin/messages/reports", params={"q": "a_b"}).json()) == ["a_b plan"]
    assert reasons(admin_client.get(
        "/admin/messages/reports", params={"q": "100%"}).json()) == ["100% certain"]


def test_overlong_search_is_rejected_with_422(admin_client):
    # q is capped at 100 characters: an unbounded ILIKE pattern is both a
    # ReDoS-shaped input to the planner and a way to exfiltrate by stuffing.
    too_long = "x" * 101
    assert admin_client.get(
        "/admin/messages/reports", params={"q": too_long}).status_code == 422
    assert admin_client.get(
        "/admin/love-notes/reports", params={"q": too_long}).status_code == 422
    assert admin_client.get(
        "/admin/confessions/reports", params={"q": too_long}).status_code == 422
    assert admin_client.get(
        "/admin/confessions", params={"q": too_long}).status_code == 422
    assert admin_client.get(
        "/admin/users", params={"q": too_long}).status_code == 422


def test_escape_like_literal_escapes_only_metacharacters():
    assert escape_like_literal("plain text 123") == "plain text 123"
    assert escape_like_literal("100%") == "100\\%"
    assert escape_like_literal("a_b") == "a\\_b"
    assert escape_like_literal("back\\slash") == "back\\\\slash"
    assert escape_like_literal("%_\\") == "\\%\\_\\\\"
