"""Nobody reports their own content.

A report is a complaint *about someone else's* writing: a moderator reviews what the
author said, and the author is who the counters are charged against. A self-report
therefore has nothing to rule on, and it would let an author move counters on their own
account (or push their own post into the review queue). The rule is server-side for
every reportable kind -- confessions and comments here, and the chat/love-note paths
already enforce their receiver-only variants -- because a client-side guard is only a
convenience.

These tests pin the two refusal answers at the API (403, with a detail the client may
show) and that a refusal is not a side effect: no report row, no counter movement. The
last test pins the rule is authorship-scoped: the same content stays reportable by
everyone else.
"""

import contextlib
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.accounts import AccountType
from app.db_models import ConfessionCommentRecord, ConfessionRecord, ReportRecord, UserRecord
from app.main import app
from app.models import UserDetails
from app.services.auth_service import get_current_user

REACTIONS = {"heart": [], "haha": [], "whoa": [], "heartbreak": []}
REASON = "this breaks the rules"

SELF_CONFESSION_DETAIL = "You cannot report your own confession."
SELF_COMMENT_DETAIL = "You cannot report your own comment."


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


def make_confession(db, author, *, body="a confession", removed=False):
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
        if removed:
            row.removed_at = datetime.now(timezone.utc)
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


@contextlib.contextmanager
def act_as(account):
    """Resolve ``get_current_user`` to this participant for the block."""
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: as_participant(account)
    try:
        yield
    finally:
        if previous is not None:
            app.dependency_overrides[get_current_user] = previous


def filed_reports(db):
    with db.sessions() as session:
        return session.scalars(select(ReportRecord)).all()


def report_counters(db, account):
    """The two confession/comment columns, read straight off the row."""
    with db.sessions() as session:
        row = session.get(UserRecord, account["id"])
        assert row is not None, "the participant row vanished, which no test should cause"
        return row.confession_report_count, row.confession_removal_count


def test_an_author_cannot_report_their_own_confession(db, client):
    author = make_user(db, "author")
    confession_id = make_confession(db, author)

    with act_as(author):
        response = client.post(f"/confessions/{confession_id}/report", json={"reason": REASON})

    assert response.status_code == 403
    assert response.json()["detail"] == SELF_CONFESSION_DETAIL
    # The refusal is a refusal: nothing was written and nothing was counted.
    assert filed_reports(db) == []
    assert report_counters(db, author) == (0, 0)


def test_a_commenter_cannot_report_their_own_comment(db, client):
    author = make_user(db, "author")
    commenter = make_user(db, "commenter")
    confession_id = make_confession(db, author)
    comment_id = make_comment(db, confession_id, commenter)

    with act_as(commenter):
        response = client.post(f"/comments/{comment_id}/report", json={"reason": REASON})

    assert response.status_code == 403
    assert response.json()["detail"] == SELF_COMMENT_DETAIL
    assert filed_reports(db) == []
    assert report_counters(db, commenter) == (0, 0)


def test_a_removed_post_written_by_the_viewer_is_still_refused_as_a_self_report(db, client):
    """The self-report rule is checked before the removed-content guard, so its answer does
    not depend on the state the content happens to be in."""
    author = make_user(db, "author")
    confession_id = make_confession(db, author, removed=True)

    with act_as(author):
        response = client.post(f"/confessions/{confession_id}/report", json={"reason": REASON})

    assert response.status_code == 403
    assert response.json()["detail"] == SELF_CONFESSION_DETAIL


def test_the_same_content_stays_reportable_by_everyone_else(db, client):
    """The guard is authorship, not the content: a third party's report still lands."""
    author = make_user(db, "author")
    reporter = make_user(db, "reporter")
    confession_id = make_confession(db, author)
    comment_id = make_comment(db, confession_id, author)

    with act_as(reporter):
        confession_response = client.post(
            f"/confessions/{confession_id}/report", json={"reason": REASON}
        )
        comment_response = client.post(f"/comments/{comment_id}/report", json={"reason": REASON})

    assert confession_response.status_code == 200, confession_response.text
    assert comment_response.status_code == 200, comment_response.text
    assert len(filed_reports(db)) == 2
    assert report_counters(db, author) == (2, 0)
    # The reporter is not charged for complaining about someone else.
    assert report_counters(db, reporter) == (0, 0)
