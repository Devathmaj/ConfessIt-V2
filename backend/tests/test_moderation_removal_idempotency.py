"""Taking content down is one event, even when two moderators take it down at once.

Every moderation path in this application ends in the same write: the content row is marked
removed rather than deleted, and the things that follow from that -- the parent confession's
``comment_count``, the author's tally of upheld removals, the automatic block that fires past
a threshold, the notice to the author -- happen once per removal. Not once per click, and not
once per report.

They used to happen once per click *per transaction*, because the transition was a read
followed by a write. Two participants reporting the same comment and both reports being
resolved at the same moment, two moderators opening the same report, a double click on
"hide": each transaction read ``removed_at IS NULL`` before either committed, so each one
believed it had taken the content down. Each then decremented the parent's comment count,
charged the author a second upheld removal, and sent a second notice. The tally an appeal
is argued from came out inflated, the comment count disagreed with the thread, and an
account crossed the automatic-block line one removal early.

The transition is now a single conditional ``UPDATE`` -- ``WHERE removed_at IS NULL`` -- which
PostgreSQL settles by re-checking the predicate after the row lock is taken, so of two racers
exactly one wins and the loser is told the content was already down.

**The race is arranged, not hoped for.** These tests line the removals up on the write
itself: every caller waits at a barrier immediately before the transition, so both have read
their row before either has written it. That is the window the bug lived in, held open
deliberately instead of depending on a sleep being long enough. The barrier sits *around* the
real transition, not inside it -- the function under test is the one that decides who wins.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

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
    NotificationRecord,
    ReportRecord,
    UserRecord,
)
from app.services import report_resolution_service
from app.services.report_counters import (
    CONFESSION,
    LOVE_NOTE,
    MESSAGE,
    removal_column,
)
from app.services.report_resolution_service import (
    hide_comment,
    hide_confession,
    resolve_confession_report,
    resolve_love_note_report,
    resolve_message_report,
)
from tests.chat_helpers import make_message_record

REACTIONS = {"heart": [], "haha": [], "whoa": [], "heartbreak": []}
REASON = "this breaks the rules"


# --- builders ---------------------------------------------------------------------


def make_user(db, regno, name=None):
    name = name or f"User {regno}"
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno,
            name=name,
            email=f"{regno}@test.com",
            year=2,
            department="Test_Class",
            gender="other",
        )
        session.add(row)
        session.flush()
        return {"id": str(row.id), "regno": row.regno, "name": name}


def make_confession(db, author, *, body="a confession", comment_count=0):
    with db.sessions.begin() as session:
        row = ConfessionRecord(
            body=body,
            recipient_name="Someone In Particular",
            anonymous=False,
            comments_enabled=True,
            created_at=datetime.now(timezone.utc),
            author_id=author["id"],
            reactions=dict(REACTIONS),
            comment_count=comment_count,
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


def make_report(db, target_id, reporter, *, kind="confession"):
    """Report a confession or one of its comments; the table is polymorphic on the column."""
    with db.sessions.begin() as session:
        row = ReportRecord(
            reason=REASON,
            created_at=datetime.now(timezone.utc),
            reported_by_id=reporter["id"],
            reported_by_name=reporter["name"],
            **{f"{kind}_id": target_id},
        )
        session.add(row)
        session.flush()
        return str(row.id)


def make_note(db, sender, recipient, *, body="a love note"):
    with db.sessions.begin() as session:
        row = LoveNoteRecord(
            sender_id=sender["id"],
            recipient_id=recipient["id"],
            image_public_id="love_notes/mock-note-id/image",
            image_format="png",
            body=body,
            anonymous=False,
            status="approved",
            created_at=datetime.now(timezone.utc),
        )
        session.add(row)
        session.flush()
        return str(row.id)


def love_note_report(db, note_id, reporter):
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


def make_message(db, sender, receiver, *, body="hello"):
    """One message in an accepted conversation. ``message_reports`` hangs off ``messages``."""
    now = datetime.now(timezone.utc)
    with db.sessions.begin() as session:
        match = MatchRecord(
            user_1_regno=sender["regno"],
            user_2_regno=receiver["regno"],
            created_at=now,
            expires_at=now + timedelta(days=30),
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
        )
        session.add(row)
        session.flush()
        return str(row.id)


def message_report(db, message_id, reporter, reported_user_regno):
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


def reviewer():
    """The ``AccountRef`` the love note resolver takes."""
    from app.accounts import AccountRef

    return AccountRef(
        regno="mod", account_id="mod-id", account_type=AccountType.MODERATOR.value
    )


# --- readers ----------------------------------------------------------------------


def comment_count(db, confession_id):
    with db.sessions() as session:
        return session.get(ConfessionRecord, str(confession_id)).comment_count


def removal_count(db, user_id, kind):
    with db.sessions() as session:
        row = session.get(UserRecord, str(user_id))
        assert row is not None, "the participant row vanished, which no test should cause"
        return getattr(row, removal_column(kind))


def is_blocked(db, user_id):
    with db.sessions() as session:
        return session.get(UserRecord, str(user_id)).is_blocked


def notices(db, regno):
    """Every notification addressed to ``regno``, newest first."""
    with db.sessions() as session:
        return list(
            session.scalars(
                select(NotificationRecord)
                .where(NotificationRecord.recipient_regno == regno)
                .order_by(NotificationRecord.created_at.desc())
            ).all()
        )


def headings(db, regno):
    return [notice.heading for notice in notices(db, regno)]


def removed_at_of(db, model, row_id):
    with db.sessions() as session:
        return session.get(model, str(row_id)).removed_at


# --- the race ---------------------------------------------------------------------


def _run_at_once(calls):
    """Run ``calls`` in their own threads and return their results in order."""
    results = [None] * len(calls)
    errors = []

    def worker(index, call):
        try:
            results[index] = call()
        except Exception as exc:  # pragma: no cover - surfaced by the assertions
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(index, call))
        for index, call in enumerate(calls)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "a removal never finished"
    assert not errors, errors
    return results


def _race(monkeypatch, calls):
    """Run several removals at once, lined up on the transition itself.

    Every caller waits at a barrier immediately before it stamps its row, so all of them have
    read the content as live before any of them has written it. The barrier is around the
    real transition rather than inside it: ``_stamp_removal`` is still the code that decides
    who wins, and the arrangement only removes the luck from the question of whether the
    window was wide enough.
    """
    real_stamp = report_resolution_service._stamp_removal
    barrier = threading.Barrier(len(calls))

    def lined_up(session, row, reviewer_regno):
        barrier.wait(timeout=30)
        return real_stamp(session, row, reviewer_regno)

    monkeypatch.setattr(report_resolution_service, "_stamp_removal", lined_up)
    try:
        return _run_at_once(calls)
    finally:
        monkeypatch.setattr(report_resolution_service, "_stamp_removal", real_stamp)


def _hold_row(db, sql, row_id):
    """Lock a row in another connection, so the removals contend for it in the database.

    Returns the connection and its transaction; rolling the transaction back releases the
    lock. This is the honest kind of contention -- the race is fought over a row lock rather
    than over a sleep.
    """
    blocker = db.engine.connect()
    transaction = blocker.begin()
    blocker.execute(text(sql), {"id": row_id})
    return blocker, transaction


def _wait_for_locked_writers(db, expected, timeout=30.0):
    """Wait until ``expected`` sessions are parked waiting for a row lock.

    The race is only a race if both transactions are inside the window at once, and asking
    PostgreSQL which sessions are waiting is more honest than sleeping long enough and
    hoping.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with db.engine.connect() as connection:
            waiting = connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND usename = current_user "
                    "AND wait_event_type = 'Lock' "
                    "AND state = 'active'"
                )
            )
        if waiting >= expected:
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------------
# Two reports on the same comment, resolved at the same moment
# ---------------------------------------------------------------------------------


def test_two_reports_on_the_same_comment_resolved_at_once_count_one_removal(db, monkeypatch):
    """The headline case: two participants report one comment, both reports are upheld.

    One comment came down, so one comment left the parent's count, the author was charged one
    upheld removal, and the author was told once. Every one of those is a number an appeal
    is argued from, and a number that says two when the truth is one is worse than no number
    at all.
    """
    author = make_user(db, "author")
    commenter = make_user(db, "commenter")
    confession_id = make_confession(db, author, comment_count=2)
    comment_id = make_comment(db, confession_id, commenter)
    make_comment(db, confession_id, commenter, body="a comment nobody reported")
    first = make_report(db, comment_id, make_user(db, "reporter-one"), kind="comment")
    second = make_report(db, comment_id, make_user(db, "reporter-two"), kind="comment")

    results = _race(
        monkeypatch,
        [
            lambda: resolve_confession_report(db, first, "removed", "mod"),
            lambda: resolve_confession_report(db, second, "removed", "mod"),
        ],
    )

    # Exactly one of the two rulings took the comment down, and it says so.
    assert sorted(result["content_removed"] for result in results) == [False, True]
    # The parent's count moved by exactly one.
    assert comment_count(db, confession_id) == 1
    # The author was charged exactly one upheld removal.
    assert removal_count(db, commenter["id"], CONFESSION) == 1
    # And told once. A second notice would read as a second event, and there was only one.
    assert headings(db, "commenter") == ["Comment removed"]
    # Each reporter is told their own complaint was upheld, which is not the same thing.
    assert headings(db, "reporter-one") == ["Your report was acted on"]
    assert headings(db, "reporter-two") == ["Your report was acted on"]
    # And the takedown is stamped, so the audit trail says when the comment came down.
    assert removed_at_of(db, ConfessionCommentRecord, comment_id) is not None


def test_a_removal_raced_against_its_own_report_does_not_block_the_author_early(
    db, monkeypatch
):
    """The automatic block must not fire one removal early.

    More than five upheld removals blocks the account, so five is ordinary and six takes it
    away. The author here already has four on the record; the removal that crosses the line
    is the one being raced. Charged twice -- once by each of two racers -- it would put the
    total at six and block an account the rule says is still standing.
    """
    author = make_user(db, "author")
    commenter = make_user(db, "commenter")
    # Four upheld removals already on the commenter's record, from their own posts.
    for index in range(4):
        hide_confession(db, make_confession(db, commenter), "mod")
    assert removal_count(db, commenter["id"], CONFESSION) == 4
    assert is_blocked(db, commenter["id"]) is False

    confession_id = make_confession(db, author, comment_count=1)
    comment_id = make_comment(db, confession_id, commenter)
    first = make_report(db, comment_id, make_user(db, "reporter-one"), kind="comment")
    second = make_report(db, comment_id, make_user(db, "reporter-two"), kind="comment")

    _race(
        monkeypatch,
        [
            lambda: resolve_confession_report(db, first, "removed", "mod"),
            lambda: resolve_confession_report(db, second, "removed", "mod"),
        ],
    )

    # Five removals: still an ordinary account.
    assert removal_count(db, commenter["id"], CONFESSION) == 5
    assert is_blocked(db, commenter["id"]) is False


def test_two_different_comments_removed_at_once_both_leave_the_parent_count(db, monkeypatch):
    """Two comments taken off one confession in the same moment: the count drops by two.

    The count is denormalised, so it is written by a decrement rather than recomputed. Two
    decrements that both read the same starting value and both write their own answer lose
    one of the two, and the feed then advertises a comment nobody can open.
    """
    author = make_user(db, "author")
    commenter = make_user(db, "commenter")
    confession_id = make_confession(db, author, comment_count=2)
    first_comment = make_comment(db, confession_id, commenter, body="the first")
    second_comment = make_comment(db, confession_id, commenter, body="the second")
    first = make_report(db, first_comment, make_user(db, "reporter-one"), kind="comment")
    second = make_report(db, second_comment, make_user(db, "reporter-two"), kind="comment")

    _race(
        monkeypatch,
        [
            lambda: resolve_confession_report(db, first, "removed", "mod"),
            lambda: resolve_confession_report(db, second, "removed", "mod"),
        ],
    )

    assert comment_count(db, confession_id) == 0
    # And two removals, one for each comment.
    assert removal_count(db, commenter["id"], CONFESSION) == 2


# ---------------------------------------------------------------------------------
# The same report, opened by two moderators
# ---------------------------------------------------------------------------------


def test_two_moderators_ruling_the_same_comment_report_at_once_count_one_removal(
    db, monkeypatch
):
    """Two reviewers open one report and both press remove.

    One decision, one takedown, one charge against the author. The second reviewer is told
    the content was already down, which is the truth and is also what the screen needs to
    say: a moderator who is told they removed something that was removed by a colleague
    would file a second decision that never happened.
    """
    author = make_user(db, "author")
    commenter = make_user(db, "commenter")
    confession_id = make_confession(db, author, comment_count=1)
    comment_id = make_comment(db, confession_id, commenter)
    report_id = make_report(db, comment_id, make_user(db, "reporter"), kind="comment")

    results = _race(
        monkeypatch,
        [
            lambda: resolve_confession_report(db, report_id, "removed", "mod-one"),
            lambda: resolve_confession_report(db, report_id, "removed", "mod-two"),
        ],
    )

    assert sorted(result["content_removed"] for result in results) == [False, True]
    assert comment_count(db, confession_id) == 0
    assert removal_count(db, commenter["id"], CONFESSION) == 1
    assert headings(db, "commenter") == ["Comment removed"]


def test_two_moderators_ruling_the_same_confession_report_at_once_count_one_removal(
    db, monkeypatch
):
    """The same thing for a confession rather than a comment."""
    author = make_user(db, "author")
    confession_id = make_confession(db, author)
    report_id = make_report(db, confession_id, make_user(db, "reporter"))

    results = _race(
        monkeypatch,
        [
            lambda: resolve_confession_report(db, report_id, "removed", "mod-one"),
            lambda: resolve_confession_report(db, report_id, "removed", "mod-two"),
        ],
    )

    assert sorted(result["content_removed"] for result in results) == [False, True]
    assert removal_count(db, author["id"], CONFESSION) == 1
    assert headings(db, "author") == ["Confession removed"]


def test_two_moderators_ruling_the_same_message_report_at_once_count_one_removal(
    db, monkeypatch
):
    """The same thing for a chat message, which carries a conversation preview as well.

    The preview is recomputed only for the transition: recomputing it twice would move the
    conversation list forward twice for one takedown.
    """
    sender = make_user(db, "sender")
    receiver = make_user(db, "receiver")
    message_id = make_message(db, sender, receiver)
    report_id = message_report(db, message_id, receiver, sender["regno"])

    results = _race(
        monkeypatch,
        [
            lambda: resolve_message_report(db, report_id, "removed", "mod-one"),
            lambda: resolve_message_report(db, report_id, "removed", "mod-two"),
        ],
    )

    assert sorted(result["content_removed"] for result in results) == [False, True]
    assert removal_count(db, sender["id"], MESSAGE) == 1
    assert headings(db, "sender") == ["Message removed"]


def test_two_moderators_ruling_the_same_love_note_report_at_once_count_one_removal(
    db, monkeypatch
):
    """The same thing for a love note, whose image is destroyed once and only once.

    A note has one recipient, so there is only ever one report against it -- which makes the
    two-moderators-on-one-report the only way this can be raced.
    """
    sender = make_user(db, "sender")
    recipient = make_user(db, "recipient")
    note_id = make_note(db, sender, recipient)
    report_id = love_note_report(db, note_id, recipient)
    destroyed = []
    monkeypatch.setattr(
        report_resolution_service.storage_service,
        "delete_love_note_asset",
        lambda public_id: destroyed.append(public_id) or True,
    )

    results = _race(
        monkeypatch,
        [
            lambda: resolve_love_note_report(db, report_id, "removed", reviewer()),
            lambda: resolve_love_note_report(db, report_id, "removed", reviewer()),
        ],
    )

    assert sorted(result["content_removed"] for result in results) == [False, True]
    assert removal_count(db, sender["id"], LOVE_NOTE) == 1
    assert headings(db, "sender") == ["Love note removed"]
    # The image is destroyed once: a second call would destroy an object that is already
    # gone, and would leave the audit trail showing two takedowns of one note.
    assert len(destroyed) == 1


# ---------------------------------------------------------------------------------
# The direct hides, which is where a double click lands
# ---------------------------------------------------------------------------------


def test_two_hides_of_the_same_confession_at_once_count_one_removal(db, monkeypatch):
    """A double click on "hide" is one removal, not two.

    The direct hide has no report behind it, so there is no second moderator to explain the
    duplicate: a double click is enough, and it is the case a moderator produces by accident
    every time they are unsure whether the first click landed.
    """
    author = make_user(db, "author")
    confession_id = make_confession(db, author)

    results = _race(
        monkeypatch,
        [
            lambda: hide_confession(db, confession_id, "mod"),
            lambda: hide_confession(db, confession_id, "mod"),
        ],
    )

    assert sorted(result["content_removed"] for result in results) == [False, True]
    assert sorted(result["already_hidden"] for result in results) == [False, True]
    # Both racers see the takedown in the audit trail, not only the winner. The loser must
    # not be told that nothing was removed at all: the content is down, somebody else took
    # it down, and the screen is about to render that as the trail it is.
    assert all(result["removed_at"] is not None for result in results)
    assert all(result["removed_by_regno"] == "mod" for result in results)
    assert removal_count(db, author["id"], CONFESSION) == 1
    assert headings(db, "author") == ["Confession removed"]


def test_two_hides_of_the_same_comment_at_once_count_one_removal(db, monkeypatch):
    """The same for a comment, which also has a parent's count to take off."""
    author = make_user(db, "author")
    commenter = make_user(db, "commenter")
    confession_id = make_confession(db, author, comment_count=1)
    comment_id = make_comment(db, confession_id, commenter)

    results = _race(
        monkeypatch,
        [
            lambda: hide_comment(db, comment_id, "mod"),
            lambda: hide_comment(db, comment_id, "mod"),
        ],
    )

    assert sorted(result["content_removed"] for result in results) == [False, True]
    assert all(result["removed_at"] is not None for result in results)
    assert comment_count(db, confession_id) == 0
    assert removal_count(db, commenter["id"], CONFESSION) == 1
    assert headings(db, "commenter") == ["Comment removed"]


# ---------------------------------------------------------------------------------
# The same race, fought over a row lock instead of a barrier
# ---------------------------------------------------------------------------------


def test_two_reports_on_the_same_comment_resolved_against_a_locked_row_count_one_removal(db):
    """The same invariant, with the race fought over a row lock in the database.

    The barrier in the other tests holds the window open from the application side. Here the
    comment row is held with ``SELECT ... FOR UPDATE`` in another connection, so both
    resolutions block on it in PostgreSQL itself and the contention is the real thing rather
    than a scheduling accident.
    """
    author = make_user(db, "author")
    commenter = make_user(db, "commenter")
    confession_id = make_confession(db, author, comment_count=1)
    comment_id = make_comment(db, confession_id, commenter)
    first = make_report(db, comment_id, make_user(db, "reporter-one"), kind="comment")
    second = make_report(db, comment_id, make_user(db, "reporter-two"), kind="comment")

    blocker, transaction = _hold_row(
        db, "SELECT id FROM confession_comments WHERE id = :id FOR UPDATE", comment_id
    )
    results = []
    errors = []

    def resolve(report_id):
        try:
            results.append(
                resolve_confession_report(db, report_id, "removed", "mod")
            )
        except Exception as exc:  # pragma: no cover - surfaced by the assertions
            errors.append(exc)

    threads = [
        threading.Thread(target=resolve, args=(first,)),
        threading.Thread(target=resolve, args=(second,)),
    ]
    try:
        for thread in threads:
            thread.start()
        # Both are now parked on the row lock, so neither has written anything yet.
        assert _wait_for_locked_writers(db, 2), "the two resolutions never contended for the row"
        transaction.rollback()
        blocker.close()
        for thread in threads:
            thread.join(timeout=60)
            assert not thread.is_alive(), "a removal never finished"
    finally:
        try:
            blocker.close()
        except Exception:
            pass

    assert not errors, errors
    assert len(results) == 2
    assert sorted(result["content_removed"] for result in results) == [False, True]
    assert comment_count(db, confession_id) == 0
    assert removal_count(db, commenter["id"], CONFESSION) == 1
    assert headings(db, "commenter") == ["Comment removed"]


# ---------------------------------------------------------------------------------
# One after the other: the same double count, without any concurrency at all
# ---------------------------------------------------------------------------------


def test_a_second_report_on_removed_content_does_not_tell_the_author_again(db):
    """Two reports on one comment, upheld one after the other, is one takedown.

    This is the same double count without a race: the second ruling finds the content
    already down, so it charges nothing and says nothing. The author is told once, and each
    reporter is told about their own complaint.
    """
    author = make_user(db, "author")
    commenter = make_user(db, "commenter")
    confession_id = make_confession(db, author, comment_count=1)
    comment_id = make_comment(db, confession_id, commenter)
    first = make_report(db, comment_id, make_user(db, "reporter-one"), kind="comment")
    second = make_report(db, comment_id, make_user(db, "reporter-two"), kind="comment")

    resolve_confession_report(db, first, "removed", "mod")
    again = resolve_confession_report(db, second, "removed", "mod")

    assert again["content_removed"] is False
    assert comment_count(db, confession_id) == 0
    assert removal_count(db, commenter["id"], CONFESSION) == 1
    assert headings(db, "commenter") == ["Comment removed"]
    assert headings(db, "reporter-one") == ["Your report was acted on"]
    assert headings(db, "reporter-two") == ["Your report was acted on"]


def test_re_deciding_the_same_report_does_not_tell_the_author_again(db):
    """Re-opening a report that was already ruled removed is not a second event.

    The reporter is told again, because their complaint is the thing being re-decided and
    silence on it reads like being ignored. The author is not: their content came down once
    and they were told when it did.
    """
    author = make_user(db, "author")
    confession_id = make_confession(db, author)
    report_id = make_report(db, confession_id, make_user(db, "reporter"))

    resolve_confession_report(db, report_id, "removed", "mod")
    again = resolve_confession_report(db, report_id, "removed", "mod")

    assert again["content_removed"] is False
    assert removal_count(db, author["id"], CONFESSION) == 1
    assert headings(db, "author") == ["Confession removed"]
    # The reporter is answered again -- their report is what was re-decided.
    assert headings(db, "reporter") == ["Your report was acted on", "Your report was acted on"]


def test_a_report_upheld_after_the_content_was_hidden_says_nothing_to_the_author(db):
    """A direct hide already told the author. A later ruling on a report must not tell them
    a second time, because the content did not come down again."""
    author = make_user(db, "author")
    confession_id = make_confession(db, author)
    report_id = make_report(db, confession_id, make_user(db, "reporter"))

    hide_confession(db, confession_id, "mod")
    ruling = resolve_confession_report(db, report_id, "removed", "mod")

    assert ruling["content_removed"] is False
    assert removal_count(db, author["id"], CONFESSION) == 1
    assert headings(db, "author") == ["Confession removed"]
    # The reporter still gets an answer: their complaint was upheld.
    assert headings(db, "reporter") == ["Your report was acted on"]
