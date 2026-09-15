"""An anonymous confession is anonymous to staff too.

The review screen is the one place a moderator could previously learn who wrote a post that
every other user sees as unattributed, which makes the ``anonymous`` flag a promise the
application only kept for participants. These tests pin the corrected rule: an anonymous
confession comes back with no sender, to an admin and to a moderator alike, while everything
else the reviewer acts on -- body, timestamps, reports, comments, engagement -- is unchanged,
and a confession that was never anonymous still names its author.

The stronger assertion is on the raw response body rather than on the parsed sender fields. A
redaction that only blanks ``sender.name`` still leaks through ``sender.regno``, a comment, or
any future field, and the substring check fails on all of those at once.
"""

import json
from datetime import datetime, timezone

import pytest

from app.db_models import ConfessionCommentRecord, ConfessionRecord, ReportRecord, UserRecord
from app.services.admin_service import get_all_confessions_data

REACTIONS = {"heart": [], "haha": [], "whoa": [], "heartbreak": []}


def make_user(db, regno, name):
    """A participant, with an identity distinctive enough to search for in a response body."""
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
        return str(row.id)


def make_confession(db, author_id, anonymous, body="a confession"):
    """A confession with enough engagement on it that losing it would be visible."""
    with db.sessions.begin() as session:
        row = ConfessionRecord(
            body=body,
            recipient_name="Someone In Particular",
            anonymous=anonymous,
            comments_enabled=True,
            created_at=datetime.now(timezone.utc),
            author_id=author_id,
            reactions=REACTIONS,
            report_count=1,
            comment_count=1,
            heart_count=2,
            haha_count=1,
            whoa_count=0,
            heartbreak_count=3,
        )
        session.add(row)
        session.flush()
        return str(row.id)


def make_comment(db, confession_id, commenter_id):
    with db.sessions.begin() as session:
        row = ConfessionCommentRecord(
            confession_id=confession_id,
            user_id=commenter_id,
            body="a comment",
            created_at=datetime.now(timezone.utc),
        )
        session.add(row)
        session.flush()
        return str(row.id)


def make_report(db, target_id, reporter_id, reporter_name, column="confession_id"):
    """Report a confession or one of its comments; the table is polymorphic on the column."""
    with db.sessions.begin() as session:
        row = ReportRecord(
            reason="harmful content",
            created_at=datetime.now(timezone.utc),
            reported_by_id=reporter_id,
            reported_by_name=reporter_name,
        )
        setattr(row, column, target_id)
        session.add(row)
        session.flush()
        return str(row.id)


def find(items, confession_id):
    matched = [item for item in items if item["id"] == confession_id]
    assert len(matched) == 1, f"expected exactly one confession {confession_id}"
    return matched[0]


@pytest.fixture
def anonymous_post(db):
    """One anonymous confession, with a comment on it and a report against it."""
    author_id = make_user(db, "ghostwriter", "Ghost Writer")
    commenter_id = make_user(db, "onlooker", "On Looker")
    confession_id = make_confession(db, author_id, anonymous=True)
    make_comment(db, confession_id, commenter_id)
    make_report(db, confession_id, commenter_id, "On Looker")
    return confession_id


def test_an_anonymous_confession_withholds_its_sender_from_staff(db, anonymous_post):
    item = find(get_all_confessions_data(db), anonymous_post)
    assert item["sender"] == {"id": None, "name": None, "regno": None, "email": None}
    assert item["sender_hidden"] is True


def test_the_withheld_sender_is_absent_from_the_whole_payload(db, anonymous_post):
    """Not just from ``sender``: nowhere in the response, in any field, in any casing."""
    body = json.dumps(get_all_confessions_data(db))
    assert "Ghost Writer" not in body
    assert "ghostwriter" not in body


def test_a_public_confession_still_names_its_author(db):
    author_id = make_user(db, "openbook", "Open Book")
    confession_id = make_confession(db, author_id, anonymous=False)
    item = find(get_all_confessions_data(db), confession_id)
    assert item["anonymous"] is False
    assert item["sender_hidden"] is False
    assert item["sender"] == {
        "id": author_id,
        "name": "Open Book",
        "regno": "openbook",
        "email": "openbook@test.com",
    }


def test_anonymity_is_decided_per_confession_not_per_author(db):
    """One person, one anonymous post and one public post: the second must not unmask the first."""
    author_id = make_user(db, "dual", "Dual Poster")
    hidden_id = make_confession(db, author_id, anonymous=True, body="the hidden one")
    shown_id = make_confession(db, author_id, anonymous=False, body="the public one")

    items = get_all_confessions_data(db)
    assert find(items, hidden_id)["sender"]["regno"] is None
    assert find(items, shown_id)["sender"]["regno"] == "dual"


def test_commenters_on_an_anonymous_confession_are_still_named(db, anonymous_post):
    """A commenter is a different population from the author, and moderation needs to see them."""
    item = find(get_all_confessions_data(db), anonymous_post)
    assert [comment["user"]["name"] for comment in item["comments"]] == ["On Looker"]


def test_everything_a_reviewer_acts_on_survives_on_an_anonymous_confession(db, anonymous_post):
    item = find(get_all_confessions_data(db), anonymous_post)
    assert item["body"] == "a confession"
    assert item["recipient_name"] == "Someone In Particular"
    assert item["comments_enabled"] is True
    assert item["created_at"]
    assert len(item["comments"]) == 1
    assert item["report_count"] == 1
    assert [report["reason"] for report in item["reports"]] == ["harmful content"]
    assert (item["heart_count"], item["haha_count"], item["whoa_count"], item["heartbreak_count"]) == (2, 1, 0, 3)
    assert item["comment_count"] == 1


@pytest.mark.parametrize("staff_fixture", ["admin_client", "moderator_client"])
def test_the_review_endpoint_withholds_the_sender_for_both_staff_tables(
    db, anonymous_post, request, staff_fixture
):
    client = request.getfixturevalue(staff_fixture)
    response = client.get("/admin/confessions")
    assert response.status_code == 200
    item = find(response.json(), anonymous_post)
    assert item["sender_hidden"] is True
    assert set(item["sender"].values()) == {None}
    assert "ghostwriter" not in response.text
    assert "Ghost Writer" not in response.text


@pytest.mark.parametrize("staff_fixture", ["admin_client", "moderator_client"])
def test_an_anonymous_confession_can_still_be_deleted(db, anonymous_post, request, staff_fixture):
    """Removing harmful content does not require knowing who wrote it."""
    client = request.getfixturevalue(staff_fixture)
    assert client.delete(f"/admin/confessions/{anonymous_post}").status_code == 200
    assert get_all_confessions_data(db) == []


def test_a_reported_comment_is_still_actionable_on_an_anonymous_confession(db):
    """The comment report path is separate from the confession report path and must keep working."""
    author_id = make_user(db, "shadow", "Shadow Writer")
    reporter_id = make_user(db, "reporter", "Report Reader")
    confession_id = make_confession(db, author_id, anonymous=True)
    comment_id = make_comment(db, confession_id, reporter_id)
    make_report(db, comment_id, reporter_id, "Report Reader", column="comment_id")

    item = find(get_all_confessions_data(db), confession_id)
    comment = item["comments"][0]
    assert [report["reason"] for report in comment["reports"]] == ["harmful content"]
    assert item["sender"]["name"] is None
