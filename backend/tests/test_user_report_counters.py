"""Per-participant report counts: how often their content was reported, and how often it came down.

A report is filed against a *post*, but the thing an administrator needs to answer is "is this
person a problem", and those are not the same question. Without a tally on the account, the only
way to find out is to remember which posts somebody wrote and grep the three queues for them --
which no reviewer does and none of them is equipped to do.

So each participant carries two counters per kind of content, and the tests below are mostly
about the boundary between them:

* ``*_report_count`` moves the moment a report is filed, so it counts complaints.
* ``*_removal_count`` moves only when content is actually taken down, so it counts upheld ones.

Those two diverge on purpose, and the assertions below mostly pin *where* they diverge: a
dismissed report must move the first and not the second, or the "authoritative" figure would be
no more trustworthy than the raw one.

**A direct hide counts as a removal.** ``hide_confession`` and ``hide_comment`` write the same
``removed_at`` a ``removed`` ruling does, so they move the removal count too. If they did not, a
moderator could take somebody's whole history down from the review screen and their tally would
never move -- which is asserted directly rather than left to the implementation.

**Confessions and comments share one pair of columns.** A complaint about a comment is a
complaint about the same person, and a moderator reads the two queues as one screen; the tests
assert both land in the same bucket so a future split cannot happen silently.

Nothing here asserts an automatic ban or any other automatic consequence. These are counts to be
read; blocking stays a decision a human makes.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.accounts import AccountType
from app.db_models import (
    ConfessionCommentRecord,
    ConfessionRecord,
    ConversationRecord,
    LoveNoteRecord,
    LoveNoteReportRecord,
    MatchRecord,
    MessageRecord,
    MessageReportRecord,
    ModeratorRecord,
    ReportRecord,
    UserRecord,
)
from app.models import ReportCreate, UserDetails
from app.services.confession_service import ConfessionService
from app.services.love_note_service import LoveNoteReportCreate, report_love_note_service
from app.services.message_service import message_service
from app.services.report_counters import (
    CONFESSION,
    LOVE_NOTE,
    MESSAGE,
    record_removal,
    record_report_filed,
    removal_column,
    report_column,
    tally_for_row,
)
from app.services.report_resolution_service import (
    hide_comment,
    hide_confession,
    resolve_confession_report,
    resolve_love_note_report,
    resolve_message_report,
)

REACTIONS = {"heart": [], "haha": [], "whoa": [], "heartbreak": []}
REASON = "this breaks the rules"


# --- fixtures / builders ---------------------------------------------------------


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


def make_moderator(db, regno="mod"):
    with db.sessions.begin() as session:
        row = ModeratorRecord(
            regno=regno,
            name=f"Mod {regno}",
            email=f"{regno}@test.com",
        )
        session.add(row)
        session.flush()
        return {"id": str(row.id), "regno": regno}


def as_participant(account):
    """A ``UserDetails`` for a participant row, which is what the report paths expect."""
    return UserDetails(
        id=account["id"],
        regno=account["regno"],
        name=account["name"],
        email=f"{account['regno']}@test.com",
        gender="other",
        year=2, department="Test_Class",
        account_type=AccountType.USERS.value,
    )


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


def make_comment(db, confession_id, author, *, body="a comment"):
    with db.sessions.begin() as session:
        row = ConfessionCommentRecord(
            confession_id=confession_id,
            user_id=author["id"],
            body=body,
            created_at=datetime.now(timezone.utc),
            likes=[],
            dislikes=[],
        )
        session.add(row)
        session.flush()
        return str(row.id)


def make_note(db, sender, recipient, *, status="approved", body="a love note"):
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
        row = MessageRecord(
            conversation_id=str(conversation.id),
            sender_regno=sender["regno"],
            receiver_regno=receiver["regno"],
            body=body,
            created_at=now,
            is_read=False,
        )
        session.add(row)
        session.flush()
        return str(row.id)


def report_row(db, target_id, reporter, *, kind=CONFESSION):
    """A pending report row, for the tests that go straight to the ruling they care about."""
    with db.sessions.begin() as session:
        row = ReportRecord(
            confession_id=target_id if kind == CONFESSION else None,
            comment_id=target_id if kind == "comment" else None,
            reported_by_id=reporter["id"],
            reported_by_name=reporter["name"],
            reason=REASON,
            created_at=datetime.now(timezone.utc),
            status="pending",
        )
        session.add(row)
        session.flush()
        return str(row.id)


def love_note_report_row(db, note_id, reporter):
    with db.sessions.begin() as session:
        row = LoveNoteReportRecord(
            love_note_id=note_id,
            reporter_regno=reporter["regno"],
            reason=REASON,
            status="pending",
            reported_at=datetime.now(timezone.utc),
        )
        session.add(row)
        session.flush()
        return str(row.id)


def message_report_row(db, message_id, reporter, reported_user_regno):
    with db.sessions.begin() as session:
        row = MessageReportRecord(
            message_id=message_id,
            reporter_regno=reporter["regno"],
            reported_user_regno=reported_user_regno,
            reason=REASON,
            status="pending",
            reported_at=datetime.now(timezone.utc),
        )
        session.add(row)
        session.flush()
        return str(row.id)


def filed_love_note_report_id(db, note_id):
    """The report the filing service created for this note.

    Read back rather than inserted by the test, because ``love_note_reports`` is unique on
    ``(love_note_id, reporter_regno)`` -- a note has exactly one recipient, so there is only ever
    one report against it and a second insert is a constraint violation rather than a fixture.
    """
    with db.sessions() as session:
        return str(
            session.scalar(
                select(LoveNoteReportRecord.id).where(LoveNoteReportRecord.love_note_id == note_id)
            )
        )


def filed_message_report_id(db, message_id):
    """The report the filing service created for this message, read back for the same reason."""
    with db.sessions() as session:
        return str(
            session.scalar(
                select(MessageReportRecord.id).where(MessageReportRecord.message_id == message_id)
            )
        )


def reviewer():
    """The ``AccountRef`` the love note resolver takes.

    A regno plus the table it was read from plus the primary key that pins the row -- the three
    together, because a regno on its own is only unique within one account table.
    """
    from app.accounts import AccountRef

    return AccountRef(
        regno="mod", account_id="mod-id", account_type=AccountType.MODERATOR.value
    )


def counts(db, account_id):
    """The six counters, read straight off the row, as a flat ``{column: n}``."""
    with db.sessions() as session:
        row = session.get(UserRecord, str(account_id))
        assert row is not None, "the participant row vanished, which no test should cause"
        return {
            column: getattr(row, column)
            for column in (
                report_column(CONFESSION), removal_column(CONFESSION),
                report_column(LOVE_NOTE), removal_column(LOVE_NOTE),
                report_column(MESSAGE), removal_column(MESSAGE),
            )
        }


def only(db, account_id, kind, which):
    """One counter, as ``(reports, removals)`` for that kind."""
    values = counts(db, account_id)
    return values[report_column(kind)], values[removal_column(kind)]


def assert_clean(db, account_id):
    """Every counter at zero. The starting condition for a test that then moves one of them."""
    assert set(counts(db, account_id).values()) == {0}


# --- filing: the total moves, the removal does not --------------------------------


def test_reporting_a_confession_counts_against_its_author(db):
    author = make_user(db, "author")
    reporter = make_user(db, "reporter")
    confession_id = make_confession(db, author)

    ConfessionService(db).report_confession(
        confession_id, ReportCreate(reason=REASON), as_participant(reporter)
    )

    assert only(db, author["id"], CONFESSION, None) == (1, 0)


def test_reporting_a_comment_counts_against_the_commenter(db):
    """A complaint about a comment is a complaint about the person who wrote it.

    And it lands in the *same* bucket as a confession, because a moderator reviews the two as
    one screen and an administrator reading the tally should not have to add two rows up.
    """
    author = make_user(db, "author")
    commenter = make_user(db, "commenter")
    reporter = make_user(db, "reporter")
    confession_id = make_confession(db, author)
    comment_id = make_comment(db, confession_id, commenter)

    ConfessionService(db).report_comment(
        comment_id, ReportCreate(reason=REASON), as_participant(reporter)
    )

    assert only(db, commenter["id"], CONFESSION, None) == (1, 0)
    # The confession's author said nothing, so their count must not have moved.
    assert_clean(db, author["id"])


def test_a_confession_and_a_comment_share_one_bucket(db):
    """Two reports, two kinds of content, one person -- one tally of two, not two tallies of one."""
    person = make_user(db, "person")
    reporter = make_user(db, "reporter")
    confession_id = make_confession(db, person)
    comment_id = make_comment(db, confession_id, person)
    service = ConfessionService(db)

    service.report_confession(confession_id, ReportCreate(reason=REASON), as_participant(reporter))
    service.report_comment(comment_id, ReportCreate(reason=REASON), as_participant(make_user(db, "r2")))

    assert only(db, person["id"], CONFESSION, None) == (2, 0)


def test_reporting_a_love_note_counts_against_the_sender(db):
    """The recipient is the reporter, so the sender is who gets the complaint."""
    sender = make_user(db, "sender")
    recipient = make_user(db, "recipient")
    note_id = make_note(db, sender, recipient)

    report_love_note_service(
        note_id, LoveNoteReportCreate(reason=REASON), as_participant(recipient), db
    )

    assert only(db, sender["id"], LOVE_NOTE, None) == (1, 0)
    # The person who made the complaint must not be charged with it.
    assert_clean(db, recipient["id"])


def test_reporting_a_message_counts_against_its_sender(db):
    sender = make_user(db, "sender")
    receiver = make_user(db, "receiver")
    message_id = make_message(db, sender, receiver)

    message_service.report_message(db, message_id, as_participant(receiver), REASON)

    assert only(db, sender["id"], MESSAGE, None) == (1, 0)
    assert_clean(db, receiver["id"])


def test_filing_does_not_move_any_removal_count(db):
    """The whole point of having two numbers: filing is not a finding.

    Asserted across all three kinds together because the failure mode being guarded against is
    a counter that happens to be wired to the wrong helper in one place, and one kind is not
    evidence about the other two.
    """
    author = make_user(db, "author")
    reporter = make_user(db, "reporter")
    service = ConfessionService(db)
    confession_id = make_confession(db, author)
    note_id = make_note(db, author, reporter)
    message_id = make_message(db, author, reporter)

    service.report_confession(confession_id, ReportCreate(reason=REASON), as_participant(reporter))
    report_love_note_service(
        note_id, LoveNoteReportCreate(reason=REASON), as_participant(reporter), db
    )
    message_service.report_message(db, message_id, as_participant(reporter), REASON)

    assert [counts(db, author["id"])[report_column(k)] for k in (CONFESSION, LOVE_NOTE, MESSAGE)] == [1, 1, 1]
    assert [counts(db, author["id"])[removal_column(k)] for k in (CONFESSION, LOVE_NOTE, MESSAGE)] == [0, 0, 0]


def test_a_refused_duplicate_report_does_not_move_the_count(db):
    """A second complaint is refused, so it must not be counted as a second complaint."""
    author = make_user(db, "author")
    reporter = make_user(db, "reporter")
    confession_id = make_confession(db, author)
    service = ConfessionService(db)

    first = service.report_confession(
        confession_id, ReportCreate(reason=REASON), as_participant(reporter)
    )
    second = service.report_confession(
        confession_id, ReportCreate(reason=REASON), as_participant(reporter)
    )

    assert first is not None
    assert second is None
    assert only(db, author["id"], CONFESSION, None) == (1, 0)


def test_two_different_reporters_each_count_once(db):
    author = make_user(db, "author")
    confession_id = make_confession(db, author)
    service = ConfessionService(db)

    service.report_confession(
        confession_id, ReportCreate(reason=REASON), as_participant(make_user(db, "r1"))
    )
    service.report_confession(
        confession_id, ReportCreate(reason=REASON), as_participant(make_user(db, "r2"))
    )

    assert only(db, author["id"], CONFESSION, None) == (2, 0)


# --- removals: the upheld count moves --------------------------------------------


def test_upholding_a_confession_report_counts_a_removal(db):
    author = make_user(db, "author")
    reporter = make_user(db, "reporter")
    confession_id = make_confession(db, author)
    report_id = report_row(db, confession_id, reporter)

    result = resolve_confession_report(db, report_id, "removed", "mod")

    assert result["content_removed"] is True
    assert only(db, author["id"], CONFESSION, None) == (0, 1)


def test_upholding_a_comment_report_counts_a_removal(db):
    confession_author = make_user(db, "author")
    commenter = make_user(db, "commenter")
    confession_id = make_confession(db, confession_author)
    comment_id = make_comment(db, confession_id, commenter)
    report_id = report_row(db, comment_id, make_user(db, "reporter"), kind="comment")

    result = resolve_confession_report(db, report_id, "removed", "mod")

    assert result["content_removed"] is True
    assert only(db, commenter["id"], CONFESSION, None) == (0, 1)
    # The person whose post the comment sat on is not charged for their commenter's remark.
    assert_clean(db, confession_author["id"])


def test_upholding_a_love_note_report_counts_a_removal(db):
    sender = make_user(db, "sender")
    recipient = make_user(db, "recipient")
    note_id = make_note(db, sender, recipient)
    report_id = love_note_report_row(db, note_id, recipient)

    result = resolve_love_note_report(db, report_id, "removed", reviewer())

    assert result["content_removed"] is True
    assert only(db, sender["id"], LOVE_NOTE, None) == (0, 1)


def test_upholding_a_message_report_counts_a_removal(db):
    sender = make_user(db, "sender")
    receiver = make_user(db, "receiver")
    message_id = make_message(db, sender, receiver)
    report_id = message_report_row(db, message_id, receiver, sender["regno"])

    result = resolve_message_report(db, report_id, "removed", "mod")

    assert result["content_removed"] is True
    assert only(db, sender["id"], MESSAGE, None) == (0, 1)


def test_a_dismissal_counts_a_report_but_not_a_removal(db):
    """The distinction the two columns exist to hold.

    This is the case the split was drawn for: somebody's content was complained about, a
    moderator looked and found nothing wrong. The participant was reported once and was cleared,
    and an administrator reading only ``removals`` must not see them as a problem.
    """
    author = make_user(db, "author")
    reporter = make_user(db, "reporter")
    confession_id = make_confession(db, author)
    report_id = report_row(db, confession_id, reporter)

    resolve_confession_report(db, report_id, "dismissed", "mod")

    assert only(db, author["id"], CONFESSION, None) == (0, 0)


def test_a_dismissed_report_still_stands_as_a_filed_report(db):
    """Dismissal is not retroactive: the report was filed, and the count of filings includes it.

    The report row itself was created outside the filing service here so the ruling could be
    tested on its own, so this drives the real filing path to show the total that a dismissal
    leaves behind.
    """
    author = make_user(db, "author")
    reporter = make_user(db, "reporter")
    confession_id = make_confession(db, author)
    service = ConfessionService(db)
    service.report_confession(confession_id, ReportCreate(reason=REASON), as_participant(reporter))

    with db.sessions() as session:
        report_id = str(session.scalar(select(ReportRecord.id).where(ReportRecord.confession_id == confession_id)))
    resolve_confession_report(db, report_id, "dismissed", "mod")

    assert only(db, author["id"], CONFESSION, None) == (1, 0)


def test_re_deciding_an_removed_report_does_not_count_twice(db):
    """A second ruling on the same report is not a second takedown.

    Same gate as the notification and the ``comment_count`` decrement, and it matters more here:
    without it a moderator re-opening a report would inflate somebody's authoritative removal
    count on every click, which is the one number a decision is supposed to rest on.
    """
    author = make_user(db, "author")
    confession_id = make_confession(db, author)
    report_id = report_row(db, confession_id, make_user(db, "reporter"))

    first = resolve_confession_report(db, report_id, "removed", "mod")
    second = resolve_confession_report(db, report_id, "removed", "mod")

    assert first["content_removed"] is True
    assert second["content_removed"] is False
    assert only(db, author["id"], CONFESSION, None) == (0, 1)


def test_hiding_a_confession_directly_counts_a_removal(db):
    """No report was upheld, but the content came down, so the count moves.

    If this did not count, the authoritative figure would measure which screen a ruling happened
    on rather than what was done to the person -- and a moderator could clear out somebody's
    entire history from the review screen without their tally moving at all.
    """
    author = make_user(db, "author")
    confession_id = make_confession(db, author)

    result = hide_confession(db, confession_id, "mod")

    assert result["content_removed"] is True
    assert only(db, author["id"], CONFESSION, None) == (0, 1)


def test_hiding_a_comment_directly_counts_a_removal(db):
    author = make_user(db, "author")
    commenter = make_user(db, "commenter")
    confession_id = make_confession(db, author)
    comment_id = make_comment(db, confession_id, commenter)

    result = hide_comment(db, comment_id, "mod")

    assert result["content_removed"] is True
    assert only(db, commenter["id"], CONFESSION, None) == (0, 1)
    assert_clean(db, author["id"])


def test_hiding_twice_counts_one_removal(db):
    author = make_user(db, "author")
    confession_id = make_confession(db, author)

    hide_confession(db, confession_id, "mod")
    second = hide_confession(db, confession_id, "mod")

    assert second["already_hidden"] is True
    assert second["content_removed"] is False
    assert only(db, author["id"], CONFESSION, None) == (0, 1)


def test_a_filing_and_an_upheld_removal_both_count(db):
    """The ordinary case: reported once, content taken down, one of each."""
    author = make_user(db, "author")
    reporter = make_user(db, "reporter")
    confession_id = make_confession(db, author)
    service = ConfessionService(db)
    service.report_confession(confession_id, ReportCreate(reason=REASON), as_participant(reporter))

    with db.sessions() as session:
        report_id = str(session.scalar(select(ReportRecord.id).where(ReportRecord.confession_id == confession_id)))
    resolve_confession_report(db, report_id, "removed", "mod")

    assert only(db, author["id"], CONFESSION, None) == (1, 1)


# --- isolation between people and kinds -------------------------------------------


def test_counts_do_not_leak_between_participants(db):
    """Two people, one reported. The other must read zero.

    The obvious wrong implementation is a single global counter or a per-kind counter with no
    author in its WHERE clause; both would pass a single-actor test.
    """
    author = make_user(db, "author")
    bystander = make_user(db, "bystander")
    confession_id = make_confession(db, author)

    ConfessionService(db).report_confession(
        confession_id, ReportCreate(reason=REASON), as_participant(make_user(db, "reporter"))
    )

    assert only(db, author["id"], CONFESSION, None) == (1, 0)
    assert_clean(db, bystander["id"])


def test_counts_do_not_leak_between_kinds(db):
    """A love note removal must not show up on somebody's chat or confession figures."""
    person = make_user(db, "person")
    other = make_user(db, "other")
    note_id = make_note(db, person, other)

    report_love_note_service(
        note_id, LoveNoteReportCreate(reason=REASON), as_participant(other), db
    )
    resolve_love_note_report(db, filed_love_note_report_id(db, note_id), "removed", reviewer())

    assert only(db, person["id"], LOVE_NOTE, None) == (1, 1)
    assert only(db, person["id"], CONFESSION, None) == (0, 0)
    assert only(db, person["id"], MESSAGE, None) == (0, 0)


def test_a_participant_can_be_reported_for_all_three_kinds(db):
    """The three buckets are independent, so one person's history can span all of them."""
    person = make_user(db, "person")
    reporter = make_user(db, "reporter")
    service = ConfessionService(db)
    confession_id = make_confession(db, person)
    note_id = make_note(db, person, reporter)
    message_id = make_message(db, person, reporter)

    service.report_confession(confession_id, ReportCreate(reason=REASON), as_participant(reporter))
    report_love_note_service(
        note_id, LoveNoteReportCreate(reason=REASON), as_participant(reporter), db
    )
    message_service.report_message(db, message_id, as_participant(reporter), REASON)

    hide_confession(db, confession_id, "mod")
    resolve_love_note_report(
        db, filed_love_note_report_id(db, note_id), "removed", reviewer()
    )
    resolve_message_report(
        db, filed_message_report_id(db, message_id), "removed", "mod"
    )

    assert only(db, person["id"], CONFESSION, None) == (1, 1)
    assert only(db, person["id"], LOVE_NOTE, None) == (1, 1)
    assert only(db, person["id"], MESSAGE, None) == (1, 1)


# --- the helper itself ------------------------------------------------------------


def test_a_missing_author_row_is_a_no_op_not_an_error(db):
    """The report and the ruling both stand; the counter is the only thing that cannot be written.

    Failing the request here would throw away a filed report because the participant's account
    has since gone -- losing the moderation queue entry over a number nobody can read.
    """
    ghost = "00000000-0000-0000-0000-000000000000"

    with db.sessions.begin() as session:
        assert record_report_filed(session, kind=CONFESSION, author_user_id=ghost) is False
        assert record_removal(session, kind=MESSAGE, author_regno="nobody") is False


def test_no_identifier_at_all_is_a_no_op(db):
    """Some content rows name no author; there is nobody to charge."""
    with db.sessions.begin() as session:
        assert record_report_filed(session, kind=CONFESSION) is False
        assert record_removal(session, kind=LOVE_NOTE, author_user_id=None, author_regno=None) is False


def test_an_unknown_kind_is_refused_rather_than_guessed(db):
    """A typo must not land the increment in no column at all, which would read as zero.

    Left unguarded, an unrecognised kind would either raise from a subscript deep inside the
    service at filing time -- where it looks like a database problem -- or, worse, quietly skip
    the update and leave the count low forever.
    """
    with db.sessions.begin() as session:
        with pytest.raises(ValueError, match="unknown report kind"):
            record_report_filed(session, kind="confessions", author_regno="whoever")
        with pytest.raises(ValueError, match="unknown report kind"):
            record_removal(session, kind="messagee", author_regno="whoever")


def test_a_participant_can_be_named_by_regno_as_well_as_by_id(db):
    """``messages`` names its author by regno while the other tables use the primary key.

    Supporting both is what lets every call site stay free of a kind-specific lookup; a caller
    holding the other kind of identifier still lands on the right row.
    """
    person = make_user(db, "person")

    with db.sessions.begin() as session:
        assert record_report_filed(session, kind=MESSAGE, author_regno=person["regno"]) is True

    assert only(db, person["id"], MESSAGE, None) == (1, 0)


def test_the_id_wins_when_both_identifiers_are_supplied(db):
    """They name the same person wherever both are known, so there is nothing to reconcile."""
    person = make_user(db, "person")

    with db.sessions.begin() as session:
        record_report_filed(
            session, kind=CONFESSION, author_user_id=person["id"], author_regno=person["regno"]
        )

    assert only(db, person["id"], CONFESSION, None) == (1, 0)


# --- what staff are shown ----------------------------------------------------------


def test_tally_reports_all_three_kinds_for_a_participant(db):
    person = make_user(db, "person")
    service = ConfessionService(db)
    confession_id = make_confession(db, person)
    service.report_confession(
        confession_id, ReportCreate(reason=REASON), as_participant(make_user(db, "reporter"))
    )

    with db.sessions() as session:
        tally = tally_for_row(session.get(UserRecord, person["id"]))

    # All three present even at zero, so the client can render a row per kind without
    # guessing which ones exist.
    assert set(tally) == {CONFESSION, LOVE_NOTE, MESSAGE}
    assert tally[CONFESSION] == {"reports": 1, "removals": 0}
    assert tally[LOVE_NOTE] == {"reports": 0, "removals": 0}
    assert tally[MESSAGE] == {"reports": 0, "removals": 0}


def test_tally_pairs_the_two_numbers_together(db):
    """A removals figure with no reports figure beside it invites comparing it against nothing."""
    with db.sessions() as session:
        for kind in (CONFESSION, LOVE_NOTE, MESSAGE):
            assert set(tally_for_row(session.get(UserRecord, make_user(db, f"u-{kind}")["id"]))[kind]) == {
                "reports", "removals",
            }


def test_a_fresh_participant_reads_zero_across_all_six(db):
    """Nothing counts anybody who has not been reported."""
    person = make_user(db, "person")
    assert set(counts(db, person["id"]).values()) == {0}


def test_the_admin_user_payload_carries_the_tally_for_participants(db, admin_client):
    person = make_user(db, "person")
    ConfessionService(db).report_confession(
        make_confession(db, person), ReportCreate(reason=REASON), as_participant(make_user(db, "r"))
    )

    response = admin_client.get("/admin/users")
    assert response.status_code == 200
    row = next(item for item in response.json()["users"] if item["regno"] == person["regno"])

    assert row["report_history"][CONFESSION] == {"reports": 1, "removals": 0}


def test_the_admin_user_payload_omits_the_tally_for_staff(db, admin_client):
    """Absent, not zero.

    A staff row with ``{"removals": 0}`` in a list rendered beside participants reads as a
    clean record rather than as a field that does not apply to it, and there is no content of
    theirs for it to be a record of.
    """
    moderator = make_moderator(db, "mod")

    response = admin_client.get("/admin/users")
    assert response.status_code == 200
    row = next(item for item in response.json()["users"] if item["regno"] == moderator["regno"])

    assert row["account_type"] == AccountType.MODERATOR.value
    assert "report_history" not in row


def test_the_admin_user_payload_reports_an_upheld_removal(db, admin_client):
    person = make_user(db, "person")
    confession_id = make_confession(db, person)
    report_id = report_row(db, confession_id, make_user(db, "reporter"))
    resolve_confession_report(db, report_id, "removed", "mod")

    response = admin_client.get("/admin/users")
    row = next(item for item in response.json()["users"] if item["regno"] == person["regno"])

    # The removal figure is what an administrator is looking for, so it has to arrive populated.
    assert row["report_history"][CONFESSION]["removals"] == 1