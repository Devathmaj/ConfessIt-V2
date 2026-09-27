"""Reporting a love note you received.

A love note is the one piece of content in this app that arrives in somebody's inbox without
the sender's name attached, so it is also the one a recipient most needs a way to complain
about: without this, everything that can be reported is either public or two-way, and the
one channel a stranger can use to reach an inbox unopened is the one with no route out.

These tests pin the rules rather than the plumbing. The recipient may report their own note
and nobody else may; the same note cannot be reported twice; and a report is a queue item that
a human closes, not an action on the note. That last one is the rule most likely to be
quietly undone later by someone tidying up the endpoint, so it is asserted directly.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.accounts import AccountType
from app.db_models import (
    LoveNoteRecord,
    LoveNoteReportRecord,
    NotificationRecord,
    UserRecord,
)
from app.main import app
from app.models import UserDetails
from app.services.auth_service import get_current_user
from app.services.storage_service import storage_service

REASON = "harassment"
RECIPIENT = "recipient"


@pytest.fixture(autouse=True)
def destroyed_images(monkeypatch):
    """Record Cloudinary destroys instead of performing them.

    Deleting a love note now destroys the image it owns before the row goes, so any test that
    reaches the delete route has a side effect outside the database. Without this stub such a
    test would make a real API call for a fixture object that never existed.
    """
    calls = []

    def _destroy(public_id, **kwargs):
        calls.append((public_id, kwargs.get("type"), kwargs.get("invalidate")))
        return {"result": "ok"}

    monkeypatch.setattr(
        "app.services.storage_service.cloudinary_destroy", _destroy, raising=True
    )
    return calls


def make_user(db, regno, name=None):
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno,
            name=name or f"User {regno}",
            email=f"{regno}@test.com",
            year=2, department="Test_Class",
            gender="other",
        )
        session.add(row)
        session.flush()
        return {"id": str(row.id), "regno": row.regno}


def make_note(db, sender, recipient, *, status="approved", body="a love note", anonymous=False):
    with db.sessions.begin() as session:
        row = LoveNoteRecord(
            id=str(uuid4()),
            sender_id=sender["id"],
            recipient_id=recipient["id"],
            # The object's identity is derived from the note's own id in production, so the
            # fixture builds it that way rather than inventing a shared placeholder. A row whose
            # stored identity is not its own would fail the shape check on the delete path and
            # make this file's tests depend on how forgiving that check is.
            image_public_id=None,
            image_format="png",
            body=body,
            anonymous=anonymous,
            status=status,
            created_at=datetime.now(timezone.utc),
        )
        row.image_public_id = storage_service.love_note_public_id(str(row.id))
        session.add(row)
        session.flush()
        return str(row.id)


def make_bystander(db):
    """A participant with no part in the note, who still has a session and an account."""
    return make_user(db, "bystander")


def note_between(db, *, anonymous=False, status="approved", body="a love note"):
    """The module's standing cast: ``sender`` writes a note to ``recipient``, and ``bystander``
    is a third account with no part in it.

    All three are created by every test that sets up a note because the reporting rule is about
    which of them is the caller, and a test that wanted to try one role should not first have
    to go and create that account.
    """
    make_bystander(db)
    return make_note(
        db,
        make_user(db, "sender"),
        make_user(db, RECIPIENT),
        anonymous=anonymous,
        status=status,
        body=body,
    )


@pytest.fixture
def login_as(db):
    """Run the calls inside the block as a named participant.

    The shared override in conftest resolves to one fixed account, which is enough for "a
    signed-in user" but not for a rule about *which* user: whether a note can be reported
    turns on being its recipient, so the sender and an uninvolved bystander both have to be
    the caller. The previous override is put back on the way out so a test can hold both a
    participant and a staff account in the same body.
    """

    @contextmanager
    def _login(regno: str):
        with db.sessions() as session:
            row = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
        assert row is not None, f"no participant named {regno} to sign in as"
        previous = app.dependency_overrides.get(get_current_user)
        app.dependency_overrides[get_current_user] = lambda: UserDetails(
            id=str(row.id), regno=row.regno, name=row.name, email=row.email,
            year=row.year, department=row.department, gender=row.gender,
            matchmaking_enabled=row.matchmaking_enabled,
            notifications_enabled=row.notifications_enabled,
            account_type=AccountType.USERS.value,
        )
        try:
            yield
        finally:
            app.dependency_overrides[get_current_user] = previous

    return _login


def file_report(client, login_as, note_id, *, reason=REASON, as_user=RECIPIENT):
    """File a report the way the note's recipient would, and hand back the response."""
    with login_as(as_user):
        return client.post(f"/love-notes/inbox/{note_id}/report", json={"reason": reason})


def only_report(response):
    """The single report in the queue, so a test cannot quietly pass on the wrong row."""
    reports = response.json()["reports"]
    assert len(reports) == 1, f"expected one report, got {reports}"
    return reports[0]


# ---------------------------------------------------------------- filing a report


def test_the_recipient_can_report_a_note_they_received(client, login_as, db):
    note_id = note_between(db)

    response = file_report(client, login_as, note_id)

    assert response.status_code == 201, response.text
    with db.sessions() as session:
        report = session.scalar(select(LoveNoteReportRecord))
        assert report is not None
        assert str(report.love_note_id) == note_id
        assert report.reporter_regno == RECIPIENT
        assert report.reason == REASON
        assert report.status == "pending"
        assert report.reviewed_at is None and report.reviewed_by_regno is None


def test_a_note_sent_to_somebody_else_cannot_be_reported(client, login_as, db):
    """The whole authorisation rule: a note is private to two people, and only the one who
    did not write it has standing to complain about it."""
    note_id = note_between(db)

    response = file_report(client, login_as, note_id, as_user="bystander")

    assert response.status_code == 403
    assert response.json()["detail"] == "You can only report love notes sent to you"
    with db.sessions() as session:
        assert session.scalar(select(LoveNoteReportRecord)) is None


def test_the_sender_cannot_report_their_own_note(client, login_as, db):
    """Filing a report against your own note is not reporting, it is deletion with extra
    steps -- and deletion is a staff decision."""
    note_id = note_between(db)

    response = file_report(client, login_as, note_id, as_user="sender")

    assert response.status_code == 403
    with db.sessions() as session:
        assert session.scalar(select(LoveNoteReportRecord)) is None


def test_the_recipient_is_the_only_caller_the_rule_admits(client, login_as, db):
    """The same note, refused twice for two different reasons, then accepted.

    Worth separating from the cases above because a check written as "not the sender" would
    pass both of them while still letting an uninvolved account report somebody's inbox.
    """
    note_id = note_between(db)

    assert file_report(client, login_as, note_id, as_user="bystander").status_code == 403
    assert file_report(client, login_as, note_id, as_user="sender").status_code == 403
    assert file_report(client, login_as, note_id).status_code == 201


def test_a_note_cannot_be_reported_twice(client, login_as, db):
    note_id = note_between(db)

    first = file_report(client, login_as, note_id)
    second = file_report(client, login_as, note_id)

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["detail"] == "You have already reported this love note"
    with db.sessions() as session:
        assert len(session.scalars(select(LoveNoteReportRecord)).all()) == 1


def test_an_unknown_note_is_a_404(client, login_as, db):
    make_user(db, RECIPIENT)

    response = file_report(client, login_as, "11111111-1111-1111-1111-111111111111")

    assert response.status_code == 404
    assert response.json()["detail"] == "Love note not found"


def test_an_id_that_is_not_a_uuid_is_refused_before_the_database_is_asked(client, login_as, db):
    """Asking the database to cast a non-UUID is a 500; the id is the caller's input and is
    validated like any other."""
    make_user(db, RECIPIENT)

    response = file_report(client, login_as, "not-a-note-id")

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid love note id"


@pytest.mark.parametrize("reason", ["", "   ", "\n\t"])
def test_a_report_without_a_reason_is_refused(client, login_as, db, reason):
    """A blank reason is a queue item nobody can act on."""
    note_id = note_between(db)

    response = file_report(client, login_as, note_id, reason=reason)

    assert response.status_code == 400
    with db.sessions() as session:
        assert session.scalar(select(LoveNoteReportRecord)) is None


def test_an_oversized_reason_is_refused(client, login_as, db):
    note_id = note_between(db)

    response = file_report(client, login_as, note_id, reason="r" * 1001)

    assert response.status_code == 422


def test_surrounding_whitespace_is_trimmed_off_the_reason(client, login_as, db):
    """The reason is stored as the person wrote it, minus the padding a text field carries."""
    note_id = note_between(db)

    response = file_report(client, login_as, note_id, reason="  a real reason  ")

    assert response.status_code == 201
    with db.sessions() as session:
        assert session.scalar(select(LoveNoteReportRecord)).reason == "a real reason"


# ---------------------------------------------------------------- the staff queue


def test_a_report_reaches_the_queue_carrying_the_note_and_its_sender(admin_client, client, login_as, db):
    """A report nobody can read is not a report, so the queue has to carry the note itself."""
    sender = make_user(db, "sender", name="Sender Name")
    recipient = make_user(db, RECIPIENT, name="Recipient Name")
    note_id = make_note(db, sender, recipient, body="the note that was reported")

    assert file_report(client, login_as, note_id).status_code == 201
    report = only_report(admin_client.get("/admin/love-notes/reports"))

    assert report["love_note_id"] == note_id
    assert report["body"] == "the note that was reported"
    assert report["anonymous"] is False
    assert report["note_status"] == "approved"
    assert report["reporter"] == {"regno": RECIPIENT, "name": "Recipient Name"}
    # The recipient never learned who sent the note, but a report is only actionable if
    # staff can see, so the sender travels with it.
    assert report["reported_user"]["name"] == "Sender Name"
    assert report["reported_user"]["regno"] == "sender"
    assert report["reason"] == REASON
    assert report["status"] == "pending"
    assert report["reviewed_at"] is None


def test_the_queue_reports_an_anonymous_note_as_anonymous(admin_client, client, login_as, db):
    """The flag is carried through so a reviewer knows why a sender name may be missing in
    the participant's own view of the note, rather than inferring it from the payload."""
    note_id = note_between(db, anonymous=True)

    file_report(client, login_as, note_id)

    assert only_report(admin_client.get("/admin/love-notes/reports"))["anonymous"] is True


def test_a_plain_account_cannot_read_the_report_queue(client):
    assert client.get("/admin/love-notes/reports").status_code == 403


def test_a_moderator_can_read_and_close_a_report(moderator_client, client, login_as, db):
    note_id = note_between(db)
    file_report(client, login_as, note_id)
    report_id = only_report(moderator_client.get("/admin/love-notes/reports"))["id"]

    closed = moderator_client.patch(f"/admin/love-notes/reports/{report_id}", params={"status": "dismissed"})

    assert closed.status_code == 200
    assert closed.json() == {"id": report_id, "status": "dismissed", "content_removed": False}
    with db.sessions() as session:
        report = session.get(LoveNoteReportRecord, report_id)
        assert report.status == "dismissed"
        # The audit trail names the staff account, which is why the column is plain text:
        # it lives in ``moderator``, where a foreign key to ``users`` could never point.
        assert report.reviewed_by_regno == "moduser"
        assert report.reviewed_at is not None


def test_the_older_word_for_dismissing_is_still_accepted(moderator_client, client, login_as, db):
    """``reviewed`` was the word this endpoint used before the screen grew a remove button.

    Answering a stale caller beats refusing it: the action it means is unchanged, so a 422 would
    break a working client over a rename without preventing anything. It is stored as sent,
    which keeps the row's own history honest.
    """
    note_id = note_between(db)
    file_report(client, login_as, note_id)
    report_id = only_report(moderator_client.get("/admin/love-notes/reports"))["id"]

    response = moderator_client.patch(f"/admin/love-notes/reports/{report_id}", params={"status": "reviewed"})

    assert response.status_code == 200
    assert response.json() == {"id": report_id, "status": "reviewed", "content_removed": False}
    with db.sessions() as session:
        assert session.get(LoveNoteReportRecord, report_id).status == "reviewed"
        # Still a dismissal, not a removal: the note is untouched.
        assert session.get(LoveNoteRecord, note_id).removed_at is None


def test_the_queue_can_be_filtered_by_status(moderator_client, client, login_as, db):
    note_id = note_between(db)
    file_report(client, login_as, note_id)
    report_id = only_report(moderator_client.get("/admin/love-notes/reports"))["id"]
    moderator_client.patch(f"/admin/love-notes/reports/{report_id}", params={"status": "dismissed"})

    assert moderator_client.get("/admin/love-notes/reports", params={"status": "pending"}).json()["reports"] == []
    dismissed = moderator_client.get("/admin/love-notes/reports", params={"status": "dismissed"}).json()["reports"]
    assert [report["id"] for report in dismissed] == [report_id]


def test_closing_an_unknown_report_is_a_404(moderator_client):
    response = moderator_client.patch(
        "/admin/love-notes/reports/11111111-1111-1111-1111-111111111111", params={"status": "dismissed"}
    )

    assert response.status_code == 404


def test_a_report_cannot_be_closed_in_a_state_that_does_not_exist(moderator_client, client, login_as, db):
    """``pending`` is where a report is born, not a state a human can put it back into, so
    re-opening a closed report stays a deliberate future decision."""
    note_id = note_between(db)
    file_report(client, login_as, note_id)
    report_id = only_report(moderator_client.get("/admin/love-notes/reports"))["id"]

    response = moderator_client.patch(f"/admin/love-notes/reports/{report_id}", params={"status": "pending"})

    assert response.status_code == 422


# ---------------------------------------------------------------- what a report is not


def test_reporting_a_note_changes_nothing_about_the_note(client, login_as, db):
    """The load-bearing assertion. Filing a report is a request for attention, not a verdict:
    the note keeps its status and stays where it is until staff act on the note in its own
    right, because a report is one person's word and the note is judged by the queue."""
    note_id = note_between(db, status="approved")

    assert file_report(client, login_as, note_id).status_code == 201

    with db.sessions() as session:
        assert session.get(LoveNoteRecord, note_id).status == "approved"


def test_deleting_a_note_takes_its_report_with_it(admin_client, client, login_as, db, destroyed_images):
    """The foreign key cascades, so a report cannot outlive the note it is about and the
    queue can never show a note that no longer exists.

    This is why ruling on a report soft-removes instead of calling this route: deleting the
    note would delete the record of the report and the decision made on it.
    """
    note_id = note_between(db)
    file_report(client, login_as, note_id)

    assert admin_client.delete(f"/admin/love-notes/{note_id}").status_code == 200
    with db.sessions() as session:
        assert session.scalar(select(LoveNoteReportRecord)) is None
    assert admin_client.get("/admin/love-notes/reports").json()["reports"] == []
    # The image went with it, as an authenticated object with the cache dropped -- otherwise the
    # CDN could still serve the picture of a note the platform has just deleted.
    assert destroyed_images == [(f"love_notes/{note_id}/image", "authenticated", True)]


# ---------------------------------------------------------- a rejection is final


def notifications_for(db, regno):
    """Every notification heading a given account has, oldest first, so a test can assert on
    what a person was told without depending on the order rows came back in."""
    with db.sessions() as session:
        rows = session.scalars(
            select(NotificationRecord)
            .where(NotificationRecord.recipient_regno == regno)
            .order_by(NotificationRecord.created_at)
        ).all()
    return [(row.heading, row.body) for row in rows]


def test_a_rejected_note_cannot_be_approved_afterwards(admin_client, db):
    """The judgement that a note was never acceptable has to stay made. Without this, a
    second click delivers a note that broke the rules -- and the screen's own rule is that a
    rejected note offers no control at all, so this is the server half of that promise."""
    note_id = note_between(db, status="pending_review")

    assert admin_client.put(
        f"/admin/love-notes/{note_id}/status", params={"status": "rejected"}
    ).status_code == 200
    refused = admin_client.put(
        f"/admin/love-notes/{note_id}/status", params={"status": "approved"}
    )

    assert refused.status_code == 409
    assert refused.json()["detail"] == "This love note was rejected and cannot be approved later."
    with db.sessions() as session:
        assert session.get(LoveNoteRecord, note_id).status == "rejected"


def test_a_rejected_note_cannot_be_put_back_to_pending(admin_client, db):
    """There is no "un-decide": a note that has been ruled on has been ruled on, and letting
    it return to the queue would put an unapprovable note back in front of a moderator."""
    note_id = note_between(db, status="pending_review")
    admin_client.put(f"/admin/love-notes/{note_id}/status", params={"status": "rejected"})

    refused = admin_client.put(
        f"/admin/love-notes/{note_id}/status", params={"status": "pending_review"}
    )

    assert refused.status_code == 409
    with db.sessions() as session:
        assert session.get(LoveNoteRecord, note_id).status == "rejected"


def test_an_approved_note_can_still_be_taken_back_down(admin_client, db):
    """The other half, and the reason the refusal above is not simply "reject is permanent".

    ``approved -> rejected`` is a different act under the same status: the note was delivered
    and is being pulled back out of the recipient's inbox. Refusing it would leave a delivered
    note no way to be withdrawn.
    """
    note_id = note_between(db, status="approved")

    assert admin_client.put(
        f"/admin/love-notes/{note_id}/status", params={"status": "rejected"}
    ).status_code == 200
    with db.sessions() as session:
        assert session.get(LoveNoteRecord, note_id).status == "rejected"


def test_a_refused_approval_sends_the_sender_nothing(admin_client, db):
    """A refusal is not a moderation action, so it must not read as one. The sender has
    already been told the note was not delivered; a second notice arriving from a click that
    changed nothing would be a second event for a decision that was not re-made."""
    note_id = note_between(db, status="pending_review")
    admin_client.put(f"/admin/love-notes/{note_id}/status", params={"status": "rejected"})

    admin_client.put(f"/admin/love-notes/{note_id}/status", params={"status": "approved"})

    assert notifications_for(db, "sender") == [(
        "❌ Love Note Not Delivered",
        "Your love note could not be sent. It may have violated our community guidelines.",
    )]


def test_rejecting_before_delivery_still_says_it_could_not_be_sent(admin_client, db):
    """The wording the sender was already getting, unchanged for the case it was written for."""
    note_id = note_between(db, status="pending_review")

    admin_client.put(f"/admin/love-notes/{note_id}/status", params={"status": "rejected"})

    assert notifications_for(db, "sender") == [(
        "❌ Love Note Not Delivered",
        "Your love note could not be sent. It may have violated our community guidelines.",
    )]


def test_taking_down_a_delivered_note_says_it_was_taken_down(admin_client, db):
    """The sender has already seen "Love Note Delivered!" and may have watched it be opened.
    Telling them now that it "could not be sent" states something untrue about a note that
    demonstrably arrived, so the notice names what actually happened instead."""
    note_id = note_between(db, status="approved")

    admin_client.put(f"/admin/love-notes/{note_id}/status", params={"status": "rejected"})

    headings = notifications_for(db, "sender")
    assert ("💔 Love Note Taken Down", "Your love note was taken down by a moderator "
            "and is no longer available to the recipient.") in headings
    assert not any("Not Delivered" in heading for heading, _body in headings), (
        "a note that was delivered must not be reported as never having been sent"
    )


def test_taking_down_a_delivered_note_does_not_notify_the_recipient_again(admin_client, db):
    """Hiding is a decision about what the sender wrote, not about the recipient. They are
    not the author of it and are not the party who acted, so a second notice would only
    announce that somebody's message had been pulled back."""
    note_id = note_between(db, status="approved")

    admin_client.put(f"/admin/love-notes/{note_id}/status", params={"status": "rejected"})

    assert not any("Taken Down" in heading for heading, _body in notifications_for(db, RECIPIENT))


# ------------------------------------------------------- removing content for a report


def remove_report(client, moderator_client, login_as, db, *, status="removed"):
    """File one report against a fresh note, rule on it with ``status``, hand back the ids.

    Returns ``(note_id, report_id, response_body)`` so a test can assert against either side of
    the decision without repeating the four calls.
    """
    note_id = note_between(db)
    file_report(client, login_as, note_id)
    report_id = only_report(moderator_client.get("/admin/love-notes/reports"))["id"]
    response = moderator_client.patch(
        f"/admin/love-notes/reports/{report_id}", params={"status": status}
    )
    assert response.status_code == 200
    return note_id, report_id, response.json()


def test_removing_a_note_keeps_both_the_note_and_the_report(client, moderator_client, login_as, db):
    """The load-bearing assertion of soft removal: nothing is destroyed.

    A hard delete would take the note with it and cascade the report away, leaving no way to
    show what was reported, what was decided, or by whom.
    """
    note_id, report_id, body = remove_report(client, moderator_client, login_as, db)

    assert body == {"id": report_id, "status": "removed", "content_removed": True}
    with db.sessions() as session:
        note = session.get(LoveNoteRecord, note_id)
        assert note is not None
        assert note.body == "a love note"
        assert note.removed_at is not None
        assert note.removed_by_regno == "moduser"
        report = session.get(LoveNoteReportRecord, report_id)
        assert report is not None
        assert report.status == "removed"


def test_removing_a_note_leaves_its_own_approval_status_alone(client, moderator_client, login_as, db):
    """``status`` says whether the note was judged as a love note; ``removed_at`` says whether
    it was taken down for a report. Overwriting one with the other is how the note list and the
    report queue end up disagreeing about the same note."""
    note_id, _, _ = remove_report(client, moderator_client, login_as, db)

    with db.sessions() as session:
        assert session.get(LoveNoteRecord, note_id).status == "approved"


def test_a_removed_note_leaves_the_recipients_inbox(client, moderator_client, db, login_as):
    note_id = note_between(db)
    file_report(client, login_as, note_id)
    report_id = only_report(moderator_client.get("/admin/love-notes/reports"))["id"]
    moderator_client.patch(f"/admin/love-notes/reports/{report_id}", params={"status": "removed"})

    with login_as(RECIPIENT):
        inbox = client.get("/love-notes/inbox").json()

    assert [note["id"] for note in inbox] == []


def test_a_removed_note_is_still_listed_for_staff(moderator_client, client, login_as, db):
    """Staff have to be able to see what is already removed, both to avoid re-opening an old
    decision and to audit it afterwards."""
    note_id = note_between(db)
    file_report(client, login_as, note_id)
    report_id = only_report(moderator_client.get("/admin/love-notes/reports"))["id"]
    moderator_client.patch(f"/admin/love-notes/reports/{report_id}", params={"status": "removed"})

    queued = only_report(moderator_client.get("/admin/love-notes/reports"))
    assert queued["id"] == report_id
    assert queued["content_removed"] is True

    listed = moderator_client.get("/admin/love-notes").json()["notes"]
    matching = [note for note in listed if note["id"] == note_id]
    assert matching, "the note vanished from the staff list instead of being flagged removed"
    assert matching[0]["content_removed"] is True


def test_the_review_list_carries_the_words_and_nothing_about_the_people(admin_client, db):
    """A reviewer judges the note, not the exchange behind it.

    Every identifying field is absent from the payload rather than blanked in the client, so
    a later screen cannot surface what this one no longer receives. The image goes too: a
    reviewer can only rule on what they can read, and minting a delivery URL for an image
    this screen does not show would hand staff a way to fetch every note's picture anyway.
    """
    sender = make_user(db, "sender", name="Sender Name")
    recipient = make_user(db, RECIPIENT, name="Recipient Name")
    note_id = make_note(
        db, sender, recipient, body="the words under review", anonymous=True
    )

    listed = admin_client.get("/admin/love-notes").json()["notes"]

    assert [note["id"] for note in listed] == [note_id]
    note = listed[0]
    assert note["body"] == "the words under review"
    for withheld in (
        "sender", "recipient", "image_url", "anonymous", "read_at",
        "sender_id", "recipient_id",
    ):
        assert withheld not in note, f"{withheld} is still handed to the review screen"


def test_the_review_list_still_carries_what_moderation_needs(admin_client, db):
    """Redaction must not cost the reviewer the ability to act or to be audited: every action
    takes the note id, so without it the screen could not approve anything, and the removal
    fields are the record of who took a note down."""
    note_id = note_between(db)

    listed = admin_client.get("/admin/love-notes").json()["notes"]

    assert listed[0]["id"] == note_id
    assert listed[0]["status"] == "approved"
    assert listed[0]["created_at"] is not None
    assert listed[0]["content_removed"] is False
    assert listed[0]["removed_at"] is None
    assert listed[0]["removed_by_regno"] is None


def test_removing_a_note_twice_keeps_the_first_timestamp(client, moderator_client, login_as, db):
    """``removed_at`` is when the content came down. Re-deciding an already-removed report
    must not re-stamp it, or the audit trail would blame the second click for the first."""
    note_id, report_id, _ = remove_report(client, moderator_client, login_as, db)
    with db.sessions() as session:
        first = session.get(LoveNoteRecord, note_id).removed_at

    again = moderator_client.patch(
        f"/admin/love-notes/reports/{report_id}", params={"status": "removed"}
    )

    assert again.json()["content_removed"] is False, "the second call removed nothing"
    with db.sessions() as session:
        assert session.get(LoveNoteRecord, note_id).removed_at == first
