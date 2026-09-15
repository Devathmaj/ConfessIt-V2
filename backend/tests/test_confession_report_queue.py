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
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.db_models import ConfessionCommentRecord, ConfessionRecord, ReportRecord, UserRecord

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


def test_a_moderator_can_close_a_report_and_it_records_who_did(moderator_client, db):
    reporter = make_user(db, "reporter")
    confession_id = make_confession(db, make_user(db, "author"))
    report_id = make_report(db, confession_id, reporter)

    response = moderator_client.patch(f"/admin/confessions/reports/{report_id}", params={"status": "reviewed"})

    assert response.status_code == 200
    assert response.json() == {"id": report_id, "status": "reviewed"}
    with db.sessions() as session:
        report = session.get(ReportRecord, report_id)
        assert report.status == "reviewed"
        # The column is plain text rather than a foreign key: the value names an account in
        # ``moderator``, which a foreign key to ``users`` could never point at.
        assert report.reviewed_by_regno == "moduser"
        assert report.reviewed_at is not None


def test_closing_a_report_changes_nothing_about_the_content(moderator_client, db):
    """A report is one person's word. Recording that a human looked must not remove the post,
    because the post's fate is decided on the confession review screen."""
    confession_id = make_confession(db, make_user(db, "author"), body="still here")
    report_id = make_report(db, confession_id, make_user(db, "reporter"))

    moderator_client.patch(f"/admin/confessions/reports/{report_id}", params={"status": "reviewed"})

    with db.sessions() as session:
        confession = session.get(ConfessionRecord, confession_id)
        assert confession is not None
        assert confession.body == "still here"


def test_a_report_cannot_be_closed_in_a_state_that_does_not_exist(moderator_client, db):
    """``pending`` is where a report is born, not a state a human can put it back into."""
    confession_id = make_confession(db, make_user(db, "author"))
    report_id = make_report(db, confession_id, make_user(db, "reporter"))

    response = moderator_client.patch(f"/admin/confessions/reports/{report_id}", params={"status": "pending"})

    assert response.status_code == 422


def test_closing_an_unknown_report_is_a_404(moderator_client):
    response = moderator_client.patch(
        "/admin/confessions/reports/11111111-1111-1111-1111-111111111111", params={"status": "reviewed"}
    )

    assert response.status_code == 404


def test_closing_a_report_that_is_not_a_uuid_is_refused(moderator_client):
    response = moderator_client.patch(
        "/admin/confessions/reports/not-a-report-id", params={"status": "reviewed"}
    )

    assert response.status_code == 400


# ---------------------------------------------------------------- who may read it


def test_a_plain_account_cannot_read_the_queue(client):
    assert client.get("/admin/confessions/reports").status_code == 403


def test_a_plain_account_cannot_close_a_report(client, db):
    confession_id = make_confession(db, make_user(db, "author"))
    report_id = make_report(db, confession_id, make_user(db, "reporter"))

    response = client.patch(f"/admin/confessions/reports/{report_id}", params={"status": "reviewed"})

    assert response.status_code == 403


def test_reading_the_queue_does_not_need_the_confession_capability(moderator_client, db):
    """``reports:review`` is the gate, not ``confessions:review``. Ruling on reports and
    reviewing posts are separately grantable, and the queue does not quietly require both."""
    confession_id = make_confession(db, make_user(db, "author"))
    make_report(db, confession_id, make_user(db, "reporter"))

    assert moderator_client.get("/admin/confessions/reports").status_code == 200
    assert moderator_client.get("/admin/confessions").status_code == 200


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


@pytest.mark.parametrize("kind", ["confession", "comment"])
def test_a_report_carries_its_reason_verbatim(admin_client, db, kind):
    """A reason that has been reworded in transit is not the reason somebody complained about."""
    author = make_user(db, "author")
    reporter = make_user(db, "reporter")
    confession_id = make_confession(db, author)
    target = confession_id if kind == "confession" else make_comment(db, confession_id, author)
    make_report(db, target, reporter, kind=kind, reason="  exactly this, please  ")

    assert only_report(admin_client.get("/admin/confessions/reports"))["reason"] == "  exactly this, please  "
