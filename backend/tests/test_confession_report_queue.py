"""The confession and comment report queue.

Confession and comment reports were the one kind with no queue of its own. They existed only
embedded in ``GET /admin/confessions``, which meant a report was something you found by
scanning every post in the application rather than something that arrived for you, and there
was no way to record that anyone had looked at one.

These tests pin the queue's rules. The two target kinds arrive in one stream because they are
reviewed with the same judgement; each row carries the content that was reported because the
question a reviewer opens the screen to answer is "what did this actually say"; and the author
of an anonymous confession stays withheld here exactly as it is on the confession review
screen. That last one is the assertion most worth having: a second reader of the same promise
is a second way to break it.

Closing a report now has exactly two outcomes rather than three. ``dismissed`` records that a
human looked and the content stays. ``removed`` takes the content down -- and the tests below
are mostly about what that does and does not do: the row survives so the report it is the
subject of still means something, participants stop seeing it, staff do not, and exactly one
notification goes to the author and one to the reporter.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db_models import (
    ConfessionCommentRecord,
    ConfessionRecord,
    NotificationRecord,
    ReportRecord,
    UserRecord,
)

REASONS = {"confession": "this is hateful", "comment": "this is spam"}
REACTIONS = {"heart": [], "haha": [], "whoa": [], "heartbreak": []}


def make_user(db, regno, name=None):
    name = name or f"User {regno}"
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno,
            name=name,
            email=f"{regno}@test.com",
            class_name="Test_Class",
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


def make_report(db, target_id, reporter, *, kind="confession", reason=None, filed_at=None):
    """Report a confession or one of its comments; the table is polymorphic on the column."""
    with db.sessions.begin() as session:
        row = ReportRecord(
            reason=reason if reason is not None else REASONS[kind],
            created_at=filed_at or datetime.now(timezone.utc),
            reported_by_id=reporter["id"],
            reported_by_name=reporter["name"],
            **{f"{kind}_id": target_id},
        )
        session.add(row)
        session.flush()
        return str(row.id)


def only_report(response):
    reports = response.json()["reports"]
    assert len(reports) == 1, f"expected one report, got {reports}"
    return reports[0]


def find_report(items, report_id):
    matched = [item for item in items if item["id"] == report_id]
    assert len(matched) == 1, f"expected exactly one report {report_id}"
    return matched[0]


def find_confession(items, confession_id):
    matched = [item for item in items if item["id"] == confession_id]
    assert len(matched) == 1, f"expected exactly one confession {confession_id}"
    return matched[0]


# ---------------------------------------------------------------- one queue, two kinds


def test_a_reported_confession_arrives_with_its_content_and_its_author(admin_client, db):
    author = make_user(db, "author", name="The Author")
    reporter = make_user(db, "reporter", name="The Reporter")
    confession_id = make_confession(db, author, body="the reported confession")
    report_id = make_report(db, confession_id, reporter, kind="confession")

    report = only_report(admin_client.get("/admin/confessions/reports"))

    assert report["id"] == report_id
    assert report["target_type"] == "confession"
    assert report["target_id"] == confession_id
    assert report["content"] == "the reported confession"
    assert report["reason"] == REASONS["confession"]
    assert report["author"] == {"id": author["id"], "name": "The Author", "regno": "author"}
    assert report["reporter"]["name"] == "The Reporter"
    assert report["status"] == "pending"
    assert report["reviewed_at"] is None


def test_a_reported_comment_arrives_with_its_content_and_its_author(admin_client, db):
    author = make_user(db, "author", name="The Author")
    commenter = make_user(db, "commenter", name="The Commenter")
    reporter = make_user(db, "reporter")
    confession_id = make_confession(db, author)
    comment_id = make_comment(db, confession_id, commenter, body="the reported comment")
    make_report(db, comment_id, reporter, kind="comment")

    report = only_report(admin_client.get("/admin/confessions/reports"))

    assert report["target_type"] == "comment"
    assert report["target_id"] == comment_id
    assert report["content"] == "the reported comment"
    assert report["author"]["name"] == "The Commenter"
    # The confession it sits under travels too, because a comment read out of context is not
    # something anybody can rule on.
    assert report["confession_id"] == confession_id


def test_both_kinds_arrive_in_one_stream(admin_client, db):
    """One queue, not two. A reviewer should not have to read half the reports on one screen
    and the other half somewhere they were not looking."""
    author = make_user(db, "author")
    reporter = make_user(db, "reporter")
    confession_id = make_confession(db, author)
    comment_id = make_comment(db, confession_id, author)
    confession_report = make_report(db, confession_id, reporter, kind="confession")
    comment_report = make_report(db, comment_id, reporter, kind="comment")

    response = admin_client.get("/admin/confessions/reports")

    kinds = {report["id"]: report["target_type"] for report in response.json()["reports"]}
    assert kinds == {confession_report: "confession", comment_report: "comment"}
    assert response.json()["total"] == 2


def test_an_anonymous_confession_is_still_anonymous_in_the_queue(admin_client, db):
    """The rule the confession review screen already keeps, restated for a second reader.

    The identity is not filtered out of the payload after the fact: the author row is never
    loaded, so there is nothing here to redact and a future field cannot leak it either.
    """
    author = make_user(db, "author", name="The Anonymous Author")
    reporter = make_user(db, "reporter")
    confession_id = make_confession(db, author, anonymous=True, body="an anonymous confession")
    make_report(db, confession_id, reporter, kind="confession")

    response = admin_client.get("/admin/confessions/reports")

    report = only_report(response)
    assert report["anonymous"] is True
    assert report["author"] is None
    assert report["author_hidden"] is True
    # Asserted on the raw body rather than on the parsed field: a redaction that only blanks
    # ``author.name`` still leaks through some other key, and the substring check catches all
    # of them at once.
    assert "The Anonymous Author" not in response.text
    assert author["id"] not in response.text


def test_a_comment_on_an_anonymous_confession_still_names_its_author(admin_client, db):
    """Anonymity covers the post, not the people replying to it in the open."""
    author = make_user(db, "author", name="The Anonymous Author")
    commenter = make_user(db, "commenter", name="The Commenter")
    reporter = make_user(db, "reporter")
    confession_id = make_confession(db, author, anonymous=True)
    comment_id = make_comment(db, confession_id, commenter)
    make_report(db, comment_id, reporter, kind="comment")

    report = only_report(admin_client.get("/admin/confessions/reports"))

    assert report["anonymous"] is True
    assert report["author"]["name"] == "The Commenter"
    assert report["author_hidden"] is False


# ---------------------------------------------------------------- reading the queue


def test_the_newest_report_comes_first(admin_client, db):
    reporter = make_user(db, "reporter")
    filed = datetime(2026, 3, 14, 12, 0, tzinfo=timezone.utc)
    # Timestamps a day apart, so the assertion is about the ordering and not about how
    # PostgreSQL breaks a tie between three rows written in the same millisecond.
    ordered = [
        make_report(db, make_confession(db, make_user(db, f"author{i}")), reporter, filed_at=filed + timedelta(days=i))
        for i in range(3)
    ]

    reported_ids = [report["id"] for report in admin_client.get("/admin/confessions/reports").json()["reports"]]

    assert reported_ids == list(reversed(ordered))


def test_the_queue_can_be_filtered_by_status(admin_client, moderator_client, db):
    reporter = make_user(db, "reporter")
    confession_id = make_confession(db, make_user(db, "author"))
    kept = make_report(db, confession_id, reporter)
    closed = make_report(db, make_confession(db, make_user(db, "author2")), reporter)
    moderator_client.patch(f"/admin/confessions/reports/{closed}", params={"status": "dismissed"})

    pending = admin_client.get("/admin/confessions/reports", params={"status": "pending"}).json()
    dismissed = admin_client.get("/admin/confessions/reports", params={"status": "dismissed"}).json()

    assert [report["id"] for report in pending["reports"]] == [kept]
    assert [report["id"] for report in dismissed["reports"]] == [closed]


def test_an_empty_queue_is_an_empty_queue_not_an_error(admin_client):
    response = admin_client.get("/admin/confessions/reports")

    assert response.status_code == 200
    assert response.json() == {"reports": [], "total": 0, "skip": 0, "limit": 50}


# ---------------------------------------------------------------- closing one out


def test_a_moderator_can_dismiss_a_report_and_it_records_who_did(moderator_client, db):
    reporter = make_user(db, "reporter")
    confession_id = make_confession(db, make_user(db, "author"))
    report_id = make_report(db, confession_id, reporter)

    response = moderator_client.patch(f"/admin/confessions/reports/{report_id}", params={"status": "dismissed"})

    assert response.status_code == 200
    assert response.json() == {"id": report_id, "status": "dismissed", "content_removed": False}
    with db.sessions() as session:
        report = session.get(ReportRecord, report_id)
        assert report.status == "dismissed"
        # The column is plain text rather than a foreign key: the value names an account in
        # ``moderator``, which a foreign key to ``users`` could never point at.
        assert report.reviewed_by_regno == "moduser"
        assert report.reviewed_at is not None


def test_dismissing_a_report_changes_nothing_about_the_content(moderator_client, db):
    """A dismissal is one person's word going unanswered. The post stays exactly as it was."""
    confession_id = make_confession(db, make_user(db, "author"), body="still here")
    report_id = make_report(db, confession_id, make_user(db, "reporter"))

    moderator_client.patch(f"/admin/confessions/reports/{report_id}", params={"status": "dismissed"})

    with db.sessions() as session:
        confession = session.get(ConfessionRecord, confession_id)
        assert confession is not None
        assert confession.body == "still here"
        assert confession.removed_at is None


def test_a_report_cannot_be_closed_in_a_state_that_does_not_exist(moderator_client, db):
    """``pending`` is where a report is born, not a state a human can put it back into, so
    re-opening a closed report stays a deliberate future decision."""
    confession_id = make_confession(db, make_user(db, "author"))
    report_id = make_report(db, confession_id, make_user(db, "reporter"))

    for status_value in ("pending", "deleted", "removed "):
        response = moderator_client.patch(
            f"/admin/confessions/reports/{report_id}", params={"status": status_value}
        )
        assert response.status_code == 422, f"{status_value!r} was accepted"


def test_the_older_word_for_dismissing_is_still_accepted(moderator_client, db):
    """``reviewed`` was the word this endpoint used before the screen grew a remove button.

    Answering a stale caller beats refusing it: the action it means is unchanged, so a 422 would
    break a working client over a rename without preventing anything. It is stored as sent,
    which keeps the row's own history honest, and it is a dismissal -- the post stays.
    """
    confession_id = make_confession(db, make_user(db, "author"), body="still here")
    report_id = make_report(db, confession_id, make_user(db, "reporter"))

    response = moderator_client.patch(
        f"/admin/confessions/reports/{report_id}", params={"status": "reviewed"}
    )

    assert response.status_code == 200
    assert response.json() == {"id": report_id, "status": "reviewed", "content_removed": False}
    with db.sessions() as session:
        assert session.get(ReportRecord, report_id).status == "reviewed"
        # Still a dismissal, not a removal: the post is untouched.
        assert session.get(ConfessionRecord, confession_id).removed_at is None


def test_filtering_for_dismissed_includes_rows_stored_as_reviewed(admin_client, moderator_client, db):
    """The screen says "Dismissed", but legacy rows are stored as "reviewed".

    Filtering on ``status=dismissed`` must return them too, or the list goes empty once an old
    batch is filtered. That is a read-side only change, and the write side must continue to
    accept ``reviewed`` on PATCH.
    """
    c1 = make_confession(db, make_user(db, "a1"), body="one")
    c2 = make_confession(db, make_user(db, "a2"), body="two")
    r1 = make_report(db, c1, make_user(db, "r1"))
    r2 = make_report(db, c2, make_user(db, "r2"))

    moderator_client.patch(f"/admin/confessions/reports/{r1}", params={"status": "reviewed"})
    moderator_client.patch(f"/admin/confessions/reports/{r2}", params={"status": "dismissed"})

    dismissed = admin_client.get("/admin/confessions/reports", params={"status": "dismissed"}).json()
    ids = {report["id"] for report in dismissed["reports"]}
    assert {r1, r2} <= ids
    removed = admin_client.get("/admin/confessions/reports", params={"status": "removed"}).json()
    assert removed["reports"] == []


def test_closing_an_unknown_report_is_a_404(moderator_client):
    response = moderator_client.patch(
        "/admin/confessions/reports/11111111-1111-1111-1111-111111111111", params={"status": "dismissed"}
    )

    assert response.status_code == 404


def test_closing_a_report_that_is_not_a_uuid_is_refused(moderator_client):
    response = moderator_client.patch(
        "/admin/confessions/reports/not-a-report-id", params={"status": "dismissed"}
    )

    assert response.status_code == 400


# ---------------------------------------------------------------- who may read it


def test_a_plain_account_cannot_read_the_queue(client):
    assert client.get("/admin/confessions/reports").status_code == 403


def test_a_plain_account_cannot_close_a_report(client, db):
    confession_id = make_confession(db, make_user(db, "author"))
    report_id = make_report(db, confession_id, make_user(db, "reporter"))

    response = client.patch(f"/admin/confessions/reports/{report_id}", params={"status": "dismissed"})

    assert response.status_code == 403


def test_reading_the_queue_does_not_need_the_confession_capability(moderator_client, db):
    """``reports:review`` is the gate, not ``confessions:review``. Ruling on reports and
    reviewing posts are separately grantable, and the queue does not quietly require both."""
    confession_id = make_confession(db, make_user(db, "author"))
    make_report(db, confession_id, make_user(db, "reporter"))

    assert moderator_client.get("/admin/confessions/reports").status_code == 200
    assert moderator_client.get("/admin/confessions").status_code == 200


# ---------------------------------------------------------------- removal


def test_removing_a_report_keeps_the_row_and_the_report(moderator_client, db):
    """Removal is soft, and the reason is the foreign key.

    The report's ``confession_id`` cascades from ``confessions.id``, so deleting the row to take
    the post down would delete the record of the complaint about it. The row stays; what
    changes is who can see it.
    """
    confession_id = make_confession(db, make_user(db, "author"), body="the offending post")
    report_id = make_report(db, confession_id, make_user(db, "reporter"))

    response = moderator_client.patch(
        f"/admin/confessions/reports/{report_id}", params={"status": "removed"}
    )

    assert response.status_code == 200
    assert response.json() == {"id": report_id, "status": "removed", "content_removed": True}
    with db.sessions() as session:
        confession = session.get(ConfessionRecord, confession_id)
        assert confession is not None
        assert confession.body == "the offending post"
        assert confession.removed_at is not None
        assert confession.removed_by_regno == "moduser"
        report = session.get(ReportRecord, report_id)
        assert report is not None
        assert report.status == "removed"


def test_removed_content_leaves_the_participant_feed(db, client, moderator_client):
    """The feed is the participant's view of the application, so a removal has to close it.

    Filtered rather than emptied: the row is still in the database, which is the previous
    test's assertion. This one is about what a participant can reach.
    """
    confession_id = make_confession(db, make_user(db, "author"), body="still visible")
    report_id = make_report(db, confession_id, make_user(db, "reporter"))
    assert any(row["id"] == confession_id for row in client.get("/confessions").json())

    moderator_client.patch(f"/admin/confessions/reports/{report_id}", params={"status": "removed"})

    assert client.get("/confessions").json() == []
    assert client.get("/confessions/total_count").json() == 0


def test_removed_content_is_still_visible_to_staff(admin_client, moderator_client, db):
    """Staff have to be able to see what is already removed.

    Without this a moderator cannot tell a report against something that was never touched
    from one against something a colleague has already taken down, and cannot check a removal
    they are reviewing. So the staff views flag rather than filter.
    """
    confession_id = make_confession(db, make_user(db, "author"), body="the offending post")
    report_id = make_report(db, confession_id, make_user(db, "reporter"))
    moderator_client.patch(f"/admin/confessions/reports/{report_id}", params={"status": "removed"})

    reviewed = [row for row in admin_client.get("/admin/confessions").json() if row["id"] == confession_id]
    assert len(reviewed) == 1
    assert reviewed[0]["content_removed"] is True
    assert reviewed[0]["removed_by_regno"] == "moduser"

    queued = find_report(admin_client.get("/admin/confessions/reports").json()["reports"], report_id)
    assert queued["content_removed"] is True


def test_removing_a_comment_also_fixes_the_confessions_comment_count(moderator_client, db):
    """``comment_count`` is denormalised onto the parent, so removing a comment has to walk
    back up and decrement it.

    Otherwise the feed advertises a comment count for a thread with fewer comments in it than
    it claims, which is a smaller lie than showing the removed comment but is still a lie.
    """
    confession_id = make_confession(db, make_user(db, "author"))
    commenter = make_user(db, "commenter")
    comment_id = make_comment(db, confession_id, commenter)
    with db.sessions.begin() as session:
        session.get(ConfessionRecord, confession_id).comment_count = 1
    report_id = make_report(db, comment_id, make_user(db, "reporter"), kind="comment")

    response = moderator_client.patch(
        f"/admin/confessions/reports/{report_id}", params={"status": "removed"}
    )

    assert response.json()["content_removed"] is True
    with db.sessions() as session:
        assert session.get(ConfessionCommentRecord, comment_id).removed_at is not None
        assert session.get(ConfessionRecord, confession_id).comment_count == 0


def test_removing_a_confession_leaves_its_comments_alone_but_invisible(moderator_client, client, db):
    """A removed confession is not a reason to destroy the replies under it.

    The comments keep their rows and their own removal timestamps stay unset -- they were not
    ruled on. They simply stop being reachable, because the parent they hang off is gone.
    """
    confession_id = make_confession(db, make_user(db, "author"))
    comment_id = make_comment(db, confession_id, make_user(db, "commenter"), body="a reply")
    report_id = make_report(db, confession_id, make_user(db, "reporter"))

    moderator_client.patch(f"/admin/confessions/reports/{report_id}", params={"status": "removed"})

    with db.sessions() as session:
        comment = session.get(ConfessionCommentRecord, comment_id)
        assert comment is not None
        assert comment.body == "a reply"
        assert comment.removed_at is None
    # The feed is the only participant read of a confession, so it is the only place a removed
    # post can still leak from.
    assert client.get("/confessions").json() == []


def test_removed_content_takes_no_further_reactions_or_comments(db, client, moderator_client):
    """Writes into a post nobody can see are refused, rather than accepted into the dark.

    Otherwise engagement keeps accruing on removed content and the counts disagree with every
    list a participant can actually open.
    """
    confession_id = make_confession(db, make_user(db, "author"))
    report_id = make_report(db, confession_id, make_user(db, "reporter"))
    moderator_client.patch(f"/admin/confessions/reports/{report_id}", params={"status": "removed"})

    assert client.post(f"/confessions/{confession_id}/react", json={"reaction_type": "heart"}).status_code >= 400


# ---------------------------------------------------------------- who hears about it


def notices_for(db, regno):
    """Every notification addressed to ``regno``, newest first."""
    with db.sessions() as session:
        return session.scalars(
            select(NotificationRecord)
            .where(NotificationRecord.recipient_regno == regno)
            .order_by(NotificationRecord.created_at.desc())
        ).all()


def test_removing_content_tells_the_author_and_the_reporter(moderator_client, db):
    """Both parties learn the outcome, and they are told different things.

    The author needs to know their post is gone and why. The reporter needs to know their
    report was upheld -- silence on a upheld report reads exactly like being ignored.
    """
    confession_id = make_confession(db, make_user(db, "author"))
    report_id = make_report(db, confession_id, make_user(db, "reporter"))
    moderator_client.patch(f"/admin/confessions/reports/{report_id}", params={"status": "removed"})

    to_author = notices_for(db, "author")
    to_reporter = notices_for(db, "reporter")
    assert [notice.heading for notice in to_author] == ["Confession removed"]
    assert "policies" in to_author[0].body
    assert [notice.heading for notice in to_reporter] == ["Your report was acted on"]
    assert "taken down" in to_reporter[0].body.lower()


def test_a_dismissal_tells_only_the_reporter(moderator_client, db):
    """The author is not told their post was cleared.

    There is nothing that happened to them: a report that was dismissed leaves their post
    exactly where it was, and a notification saying so would tell a participant that a stranger
    complained about them and that staff looked -- which is the one piece of this the report
    system exists to keep from them.
    """
    confession_id = make_confession(db, make_user(db, "author"))
    report_id = make_report(db, confession_id, make_user(db, "reporter"))
    moderator_client.patch(f"/admin/confessions/reports/{report_id}", params={"status": "dismissed"})

    assert notices_for(db, "author") == []
    reporter_notices = notices_for(db, "reporter")
    assert [notice.heading for notice in reporter_notices] == ["We reviewed your report"]
    assert "not removed" in reporter_notices[0].body


def test_a_removal_does_not_leak_the_reporter_to_the_author(moderator_client, db):
    """The author is told their content broke the rules, never who reported it."""
    confession_id = make_confession(db, make_user(db, "author"))
    report_id = make_report(db, confession_id, make_user(db, "reporter"))
    moderator_client.patch(f"/admin/confessions/reports/{report_id}", params={"status": "removed"})

    body = notices_for(db, "author")[0].body
    assert "reporter" not in body.lower()


def test_a_notification_never_embeds_a_wall_clock_time(moderator_client, db):
    """``created_at`` already renders in the reader's local time.

    A time written into the body at send time would be the server's clock, not theirs, and the
    app has a test holding exactly that for the matchmaking notice. A notification that states
    *when it was taken down* is the same trap with a worse consequence: the reader is being told
    about a moderation decision and will treat the stated time as the moment of it.
    """
    confession_id = make_confession(db, make_user(db, "author"))
    report_id = make_report(db, confession_id, make_user(db, "reporter"))
    moderator_client.patch(f"/admin/confessions/reports/{report_id}", params={"status": "removed"})

    bodies = [notice.body for notice in notices_for(db, "author") + notices_for(db, "reporter")]
    month_names = (
        "january february march april may june july august september "
        "october november december"
    ).split()
    for body in bodies:
        lowered = body.lower()
        assert not any(name in lowered for name in month_names), body
        assert ":" not in body, body


def test_removing_a_comment_notifies_the_commenter_not_the_post_author(moderator_client, db):
    """A comment report is about the comment.

    Notifying the parent post's author would tell somebody whose post was not removed that they
    had lost something, because of a reply they did not write.
    """
    post_author = make_user(db, "author")
    commenter = make_user(db, "commenter")
    confession_id = make_confession(db, post_author)
    comment_id = make_comment(db, confession_id, commenter)
    report_id = make_report(db, comment_id, make_user(db, "reporter"), kind="comment")
    moderator_client.patch(f"/admin/confessions/reports/{report_id}", params={"status": "removed"})

    assert [notice.heading for notice in notices_for(db, "commenter")] == ["Comment removed"]
    assert notices_for(db, "author") == []


def test_removing_twice_keeps_the_first_timestamp(moderator_client, db):
    """``removed_at`` records when the content came down.

    A second click on a report that is already closed as ``removed`` re-stamping the column
    would make the audit trail say the second decision was the one that removed it.
    """
    confession_id = make_confession(db, make_user(db, "author"))
    report_id = make_report(db, confession_id, make_user(db, "reporter"))
    moderator_client.patch(f"/admin/confessions/reports/{report_id}", params={"status": "removed"})
    with db.sessions() as session:
        first = session.get(ConfessionRecord, confession_id).removed_at

    moderator_client.patch(f"/admin/confessions/reports/{report_id}", params={"status": "removed"})

    with db.sessions() as session:
        assert session.get(ConfessionRecord, confession_id).removed_at == first


# ---------------------------------------------------------------- the target's own lifecycle


def test_deleting_a_confession_takes_its_reports_with_it(admin_client, db):
    """The foreign key cascades, so a report cannot outlive the post it is about."""
    confession_id = make_confession(db, make_user(db, "author"))
    make_report(db, confession_id, make_user(db, "reporter"))

    assert admin_client.delete(f"/admin/confessions/{confession_id}").status_code == 200
    assert admin_client.get("/admin/confessions/reports").json()["reports"] == []


def test_deleting_a_confession_takes_the_reports_on_its_comments_too(admin_client, db):
    confession_id = make_confession(db, make_user(db, "author"))
    comment_id = make_comment(db, confession_id, make_user(db, "commenter"))
    make_report(db, comment_id, make_user(db, "reporter"), kind="comment")

    admin_client.delete(f"/admin/confessions/{confession_id}")

    assert admin_client.get("/admin/confessions/reports").json()["reports"] == []


# --------------------------------------------------- hiding from the confession review screen


def test_hiding_a_confession_keeps_the_row_and_the_reports(admin_client, db, moderator_client):
    """The soft half of the split, and the reason the review screen offers hide rather than delete.

    ``DELETE /admin/confessions/{id}`` cascades the reports away with the post, which would
    destroy the record that the post was reported and ruled on. Hiding stamps ``removed_at``
    and leaves every row standing, so this asserts the reports are still there afterwards --
    the failure this guards against is the one where the review screen quietly becomes a way to
    delete a reported post and its evidence.
    """
    confession_id = make_confession(db, make_user(db, "author"), body="hidden but kept")
    report_id = make_report(db, confession_id, make_user(db, "reporter"))

    response = moderator_client.patch(f"/admin/confessions/{confession_id}/hide")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == confession_id
    assert body["content_removed"] is True
    assert body["already_hidden"] is False
    with db.sessions() as session:
        confession = session.get(ConfessionRecord, confession_id)
        assert confession is not None
        assert confession.body == "hidden but kept"
        assert confession.removed_at is not None
        assert confession.removed_by_regno == "moduser"
        assert session.get(ReportRecord, report_id) is not None


def test_a_hidden_confession_reads_back_as_already_hidden_from_the_review_screen(
    admin_client, db, moderator_client
):
    """``content_removed`` is what the screen keys its "Already hidden" state off.

    Taken from the review payload rather than from the hide response, because the state has to
    survive a page reload: a reviewer who navigates away and comes back must not find the Hide
    button offered again on a post that is already down.
    """
    confession_id = make_confession(db, make_user(db, "author"))
    moderator_client.patch(f"/admin/confessions/{confession_id}/hide")

    item = find_confession(admin_client.get("/admin/confessions").json(), confession_id)

    assert item["content_removed"] is True
    assert item["removed_at"] is not None
    assert item["removed_by_regno"] == "moduser"


def test_hiding_and_removing_through_a_report_land_in_the_same_state(db, moderator_client):
    """One column, so the two doors cannot disagree about what hidden means.

    A confession hidden from the review screen and one taken down by ruling on a report are the
    same outcome reached two ways. Anything that filtered only on one path -- the participant
    feed, the total count, the staff payload -- would be a hole in one of them.
    """
    hidden_directly = make_confession(db, make_user(db, "author-1"))
    removed_by_report = make_confession(db, make_user(db, "author-2"))
    report_id = make_report(db, removed_by_report, make_user(db, "reporter"))

    moderator_client.patch(f"/admin/confessions/{hidden_directly}/hide")
    moderator_client.patch(f"/admin/confessions/reports/{report_id}", params={"status": "removed"})

    with db.sessions() as session:
        direct = session.get(ConfessionRecord, hidden_directly)
        via_report = session.get(ConfessionRecord, removed_by_report)
        assert (direct.removed_at is None) == (via_report.removed_at is None) is False


def test_hiding_a_confession_takes_it_out_of_the_participant_feed(db, client, moderator_client):
    """Same guarantee as a report-driven removal, asserted on the other door."""
    confession_id = make_confession(db, make_user(db, "author"))
    assert any(row["id"] == confession_id for row in client.get("/confessions").json())

    moderator_client.patch(f"/admin/confessions/{confession_id}/hide")

    assert client.get("/confessions").json() == []
    assert client.get("/confessions/total_count").json() == 0


def test_hiding_a_confession_tells_its_author_once(db, moderator_client):
    """One removal is one notice, and nobody else is dragged into it.

    There is no reporter here -- nobody complained -- so the author is the only party with an
    interest. Telling anyone else would be announcing a moderator's decision to a bystander.
    """
    confession_id = make_confession(db, make_user(db, "author"))
    bystander = make_user(db, "bystander")

    moderator_client.patch(f"/admin/confessions/{confession_id}/hide")

    assert [notice.heading for notice in notices_for(db, "author")] == ["Confession removed"]
    assert notices_for(db, "bystander") == []


def test_hiding_twice_keeps_the_first_decision(db, moderator_client):
    """``removed_at`` is when the content came down, and the reviewer who did it stays named.

    A double click re-stamping the timestamp would make the audit trail say the second click
    took the post down, and the moderation trail would name whoever happened to click last.
    """
    confession_id = make_confession(db, make_user(db, "author"))
    moderator_client.patch(f"/admin/confessions/{confession_id}/hide")
    with db.sessions() as session:
        first = session.get(ConfessionRecord, confession_id).removed_at

    second = moderator_client.patch(f"/admin/confessions/{confession_id}/hide")

    assert second.status_code == 200, "a double click must not be answered with an error"
    assert second.json()["already_hidden"] is True
    assert second.json()["content_removed"] is False, "this call removed nothing"
    with db.sessions() as session:
        confession = session.get(ConfessionRecord, confession_id)
        assert confession.removed_at == first
        assert confession.removed_by_regno == "moduser"
    # The author was told once. A second notice for one removal reads as two events.
    assert [notice.heading for notice in notices_for(db, "author")] == ["Confession removed"]


def test_a_confession_hidden_by_a_report_then_hidden_again_keeps_the_reports_decision(
    db, moderator_client
):
    """The review screen's Hide cannot overwrite the trail left by a report ruling.

    ``removed_by_regno`` is who is accountable for the post coming down. If hiding an
    already-removed confession restamped it, the moderator who actually ruled on the report
    would stop being the one on record, and the report queue's ``reviewed_by`` would disagree
    with the confession's ``removed_by`` about who did what.
    """
    confession_id = make_confession(db, make_user(db, "author"))
    report_id = make_report(db, confession_id, make_user(db, "reporter"))
    moderator_client.patch(f"/admin/confessions/reports/{report_id}", params={"status": "removed"})

    body = moderator_client.patch(f"/admin/confessions/{confession_id}/hide").json()

    assert body["already_hidden"] is True
    with db.sessions() as session:
        confession = session.get(ConfessionRecord, confession_id)
        assert confession.removed_by_regno == "moduser"


def test_there_is_no_way_to_unhide_a_hidden_confession(moderator_client, db):
    """Hiding is one-way, and that is a property of the API rather than of the UI.

    The confirmation dialog promises there is no bringing a post back, so the claim has to hold
    for any caller. Every write verb is tried here on purpose: a future ``unhide`` endpoint, or
    a DELETE that quietly restores visibility, would make the warning a lie.
    """
    confession_id = make_confession(db, make_user(db, "author"))
    moderator_client.patch(f"/admin/confessions/{confession_id}/hide")

    for method, path, kwargs in (
        ("post", f"/admin/confessions/{confession_id}/unhide", {}),
        ("patch", f"/admin/confessions/{confession_id}/unhide", {}),
        ("put", f"/admin/confessions/{confession_id}/unhide", {}),
        ("patch", f"/admin/confessions/{confession_id}/show", {}),
        ("patch", f"/admin/confessions/{confession_id}/restore", {}),
    ):
        response = moderator_client.request(method, path, **kwargs)
        assert response.status_code == 404, f"{method.upper()} {path} unexpectedly exists"

    with db.sessions() as session:
        assert session.get(ConfessionRecord, confession_id).removed_at is not None


def test_hiding_keeps_the_comments_and_their_own_removal_state(db, moderator_client):
    """Comments are unreachable behind a hidden parent, so nothing needs to be written to them.

    Stamping them would be a second, redundant write -- and it would overwrite the removal state
    of a comment that a report against the comment had already taken down for its own reasons,
    which is the one part of the audit trail a moderator acting on a post has no business
    rewriting.
    """
    confession_id = make_confession(db, make_user(db, "author"))
    comment_id = make_comment(db, confession_id, make_user(db, "commenter"))

    moderator_client.patch(f"/admin/confessions/{confession_id}/hide")

    with db.sessions() as session:
        comment = session.get(ConfessionCommentRecord, comment_id)
        assert comment is not None, "the comment row is not cascaded away by hiding"
        assert comment.removed_at is None


def test_hiding_an_unknown_confession_is_a_404(moderator_client):
    """A well-formed id for a row that is not there.

    Not a malformed id -- that is the 400 below. This is the 404, and it matters because the
    review screen holds ids across a page load: a confession deleted out from under an open tab
    has to fail as "gone", not as a 500 from a lookup that assumed it existed.
    """
    response = moderator_client.patch(
        "/admin/confessions/00000000-0000-4000-8000-000000000000/hide"
    )

    assert response.status_code == 404


def test_hiding_a_confession_that_is_not_a_uuid_is_refused(moderator_client):
    assert moderator_client.patch("/admin/confessions/not-a-uuid/hide").status_code == 400


# --------------------------------- what a direct hide tells the author, versus what a ruling does


def test_hiding_a_confession_does_not_tell_the_author_it_violated_the_policies(
    moderator_client, db
):
    """The policy wording belongs to a ruling on a report, and only to a ruling.

    Nothing was cited against this post: no participant complained about it and no moderator
    named a rule. Telling the author it "violated our community policies" would be the
    application asserting a decision nobody made, and it is not something the author can act on
    when they cannot ask which rule.
    """
    confession_id = make_confession(db, make_user(db, "author"))

    moderator_client.patch(f"/admin/confessions/{confession_id}/hide")

    to_author = notices_for(db, "author")
    assert len(to_author) == 1
    assert "policies" not in to_author[0].body.lower()
    assert to_author[0].body == "We removed your confession from ConfessIt."


def test_removing_a_confession_through_a_report_still_cites_the_policies(moderator_client, db):
    """The other half of the split, on the same content and through the other door.

    A report was filed, a moderator read the post and ruled that it broke a rule, and the author
    is owed the reason. If the wording moved to the neutral copy this test fails, because the
    two paths are the only thing distinguishing them.
    """
    confession_id = make_confession(db, make_user(db, "author"))
    report_id = make_report(db, confession_id, make_user(db, "reporter"))

    moderator_client.patch(f"/admin/confessions/reports/{report_id}", params={"status": "removed"})

    to_author = notices_for(db, "author")
    assert len(to_author) == 1
    assert "policies" in to_author[0].body.lower()


def test_a_hide_on_a_reported_confession_is_still_neutral(moderator_client, db):
    """The split is by **path**, not by whether reports happen to exist.

    This is the case that would otherwise be argued about: the post has reports on it, so it
    would be easy to read the hide as a shortcut to the same outcome. But no report was ruled
    on, so no rule was cited, and the author gets the copy that does not claim one. Deciding
    this on the report count rather than the path would make the wording mean two different
    things depending on a moderator's route through the app.
    """
    confession_id = make_confession(db, make_user(db, "author"))
    make_report(db, confession_id, make_user(db, "reporter"))

    moderator_client.patch(f"/admin/confessions/{confession_id}/hide")

    to_author = notices_for(db, "author")
    assert len(to_author) == 1
    assert "policies" not in to_author[0].body.lower()


# --------------------------------------------------- hiding a single comment from the review screen


def test_hiding_a_comment_keeps_the_row_and_the_reports(admin_client, db, moderator_client):
    """The same soft write as hiding a confession, for one comment.

    Asserted on the reports specifically because that is the whole reason this is not a delete:
    a hard delete of the comment would cascade the reports filed against it, destroying the
    record that it was reported.
    """
    confession_id = make_confession(db, make_user(db, "author"))
    comment_id = make_comment(db, confession_id, make_user(db, "commenter"), body="rude but kept")
    report_id = make_report(db, comment_id, make_user(db, "reporter"), kind="comment")

    response = moderator_client.patch(f"/admin/comments/{comment_id}/hide")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == comment_id
    assert body["confession_id"] == confession_id
    assert body["content_removed"] is True
    assert body["already_hidden"] is False
    with db.sessions() as session:
        comment = session.get(ConfessionCommentRecord, comment_id)
        assert comment is not None
        assert comment.body == "rude but kept"
        assert comment.removed_at is not None
        assert comment.removed_by_regno == "moduser"
        assert session.get(ReportRecord, report_id) is not None


def test_hiding_a_comment_leaves_the_confession_up(db, moderator_client):
    """One bad comment in a hundred is not a reason to take the whole post down."""
    confession_id = make_confession(db, make_user(db, "author"))
    comment_id = make_comment(db, confession_id, make_user(db, "commenter"))

    moderator_client.patch(f"/admin/comments/{comment_id}/hide")

    with db.sessions() as session:
        confession = session.get(ConfessionRecord, confession_id)
        assert confession is not None
        assert confession.removed_at is None


def test_hiding_a_comment_takes_it_out_of_the_confession_but_not_the_review_screen(
    admin_client, db, client, moderator_client
):
    """The two doors disagree on purpose: gone for participants, flagged for staff."""
    confession_id = make_confession(db, make_user(db, "author"))
    keep_id = make_comment(db, confession_id, make_user(db, "quiet"), body="stays")
    hide_id = make_comment(db, confession_id, make_user(db, "noisy"), body="goes")

    moderator_client.patch(f"/admin/comments/{hide_id}/hide")

    participant = [
        row for row in client.get("/confessions").json() if row["id"] == confession_id
    ]
    assert len(participant) == 1
    assert [c["id"] for c in participant[0]["comments"]] == [keep_id]
    staff = find_confession(admin_client.get("/admin/confessions").json(), confession_id)
    hidden = {c["id"]: c for c in staff["comments"]}[hide_id]
    assert hidden["content_removed"] is True
    assert hidden["removed_at"] is not None
    assert hidden["removed_by_regno"] == "moduser"


def test_hiding_a_comment_decrements_the_parents_comment_count(db, moderator_client):
    """The feed must stop advertising a comment nobody can open.

    ``comment_count`` is denormalised, so it is the one thing a comment removal has to write
    beyond the comment row itself. Left stale, the confession still claims N comments and shows
    fewer, on every read, for as long as the row survives -- and there is no unhide to correct it.
    """
    confession_id = make_confession(db, make_user(db, "author"))
    comment_id = make_comment(db, confession_id, make_user(db, "commenter"))
    with db.sessions.begin() as session:
        session.get(ConfessionRecord, confession_id).comment_count = 1

    moderator_client.patch(f"/admin/comments/{comment_id}/hide")

    with db.sessions() as session:
        assert session.get(ConfessionRecord, confession_id).comment_count == 0


def test_hiding_the_same_comment_twice_does_not_double_decrement(db, moderator_client):
    """The decrement belongs to the removal, not to the request.

    Gated on the transition for the same reason ``removed_at`` is not re-stamped: a second click
    is a double click, and subtracting again would take the count below the number of comments
    that are actually visible.
    """
    confession_id = make_confession(db, make_user(db, "author"))
    comment_id = make_comment(db, confession_id, make_user(db, "commenter"))
    with db.sessions.begin() as session:
        session.get(ConfessionRecord, confession_id).comment_count = 1

    moderator_client.patch(f"/admin/comments/{comment_id}/hide")
    second = moderator_client.patch(f"/admin/comments/{comment_id}/hide")

    assert second.status_code == 200, "a double click must not be answered with an error"
    assert second.json()["already_hidden"] is True
    assert second.json()["content_removed"] is False
    with db.sessions() as session:
        assert session.get(ConfessionRecord, confession_id).comment_count == 0
    # Told once. A second notice for one removal reads as two events.
    assert [notice.heading for notice in notices_for(db, "commenter")] == ["Comment removed"]


def test_hiding_a_comment_leaves_its_siblings_untouched(db, moderator_client):
    """Hiding one comment does not rewrite the removal state of the ones beside it.

    Those may each be the subject of their own report, ruled on their own evidence. A moderator
    acting on one comment has no business restamping the others.
    """
    confession_id = make_confession(db, make_user(db, "author"))
    hide_id = make_comment(db, confession_id, make_user(db, "noisy"))
    keep_id = make_comment(db, confession_id, make_user(db, "quiet"))

    moderator_client.patch(f"/admin/comments/{hide_id}/hide")

    with db.sessions() as session:
        kept = session.get(ConfessionCommentRecord, keep_id)
        assert kept is not None
        assert kept.removed_at is None


def test_hiding_a_comment_tells_its_author_it_was_removed(moderator_client, db):
    """Neutral wording, for the same reason a hidden confession gets it.

    No report was ruled on, so there is no policy to cite -- only the fact that the comment is
    gone.
    """
    confession_id = make_confession(db, make_user(db, "author"))
    comment_id = make_comment(db, confession_id, make_user(db, "commenter"))
    bystander = make_user(db, "bystander")

    moderator_client.patch(f"/admin/comments/{comment_id}/hide")

    to_author = notices_for(db, "commenter")
    assert [notice.heading for notice in to_author] == ["Comment removed"]
    assert "policies" not in to_author[0].body.lower()
    assert to_author[0].body == "We removed your comment from ConfessIt."
    assert notices_for(db, "bystander") == [], "nobody else is dragged into it"
    assert notices_for(db, "author") == [], "the confession's author is not a party to this"


def test_there_is_no_way_to_unhide_a_hidden_comment(moderator_client, db):
    """The confirmation dialog promises there is no bringing a comment back, so it must hold.

    Same property as the confession equivalent, asserted on the other content type: a future
    ``unhide`` endpoint would make the warning a lie on this route.
    """
    confession_id = make_confession(db, make_user(db, "author"))
    comment_id = make_comment(db, confession_id, make_user(db, "commenter"))
    moderator_client.patch(f"/admin/comments/{comment_id}/hide")

    for method, path in (
        ("post", f"/admin/comments/{comment_id}/unhide"),
        ("patch", f"/admin/comments/{comment_id}/unhide"),
        ("put", f"/admin/comments/{comment_id}/unhide"),
        ("patch", f"/admin/comments/{comment_id}/show"),
    ):
        response = moderator_client.request(method, path)
        assert response.status_code == 404, f"{method.upper()} {path} unexpectedly exists"

    with db.sessions() as session:
        assert session.get(ConfessionCommentRecord, comment_id).removed_at is not None


def test_hiding_an_unknown_comment_is_a_404(moderator_client):
    """A well-formed id for a row that is not there, as against a malformed id below."""
    response = moderator_client.patch(
        "/admin/comments/00000000-0000-4000-8000-000000000000/hide"
    )

    assert response.status_code == 404


def test_hiding_a_comment_that_is_not_a_uuid_is_refused(moderator_client):
    assert moderator_client.patch("/admin/comments/not-a-uuid/hide").status_code == 400


def test_hiding_a_comment_keeps_the_confession_and_every_other_comment(db, moderator_client):
    """Hiding a comment must not become a way to remove a discussion wholesale.

    There is no hard delete for a comment, and the two rows surviving is the observable form of
    that: a route that deleted would leave ``comment_count`` stale on the parent and destroy the
    reports filed against the comment, which is exactly the pair of problems the soft write exists
    to avoid.
    """
    confession_id = make_confession(db, make_user(db, "author"))
    comment_id = make_comment(db, confession_id, make_user(db, "commenter"))
    other_id = make_comment(db, confession_id, make_user(db, "quiet"))

    moderator_client.patch(f"/admin/comments/{comment_id}/hide")

    with db.sessions() as session:
        assert session.get(ConfessionRecord, confession_id) is not None
        assert session.get(ConfessionCommentRecord, comment_id) is not None
        assert session.get(ConfessionCommentRecord, other_id) is not None


@pytest.mark.parametrize("kind", ["confession", "comment"])
def test_a_report_carries_its_reason_verbatim(admin_client, db, kind):
    """A reason that has been reworded in transit is not the reason somebody complained about."""
    author = make_user(db, "author")
    reporter = make_user(db, "reporter")
    confession_id = make_confession(db, author)
    target = confession_id if kind == "confession" else make_comment(db, confession_id, author)
    make_report(db, target, reporter, kind=kind, reason="  exactly this, please  ")

    assert only_report(admin_client.get("/admin/confessions/reports"))["reason"] == "  exactly this, please  "
