"""An anonymous confession is anonymous to staff and participants alike.

The review screen is the one place a moderator could previously learn who wrote a post that
every other user sees as unattributed, which makes the ``anonymous`` flag a promise the
application only kept for participants. These tests pin the corrected rule: an anonymous
confession comes back with no sender, to an admin and to a moderator alike, while everything
else the reviewer acts on -- body, timestamps, reports, comments, engagement -- is unchanged,
and a confession that was never anonymous still names its author.

The participant-facing half below is the same promise on the public feed, search and the
react/report responses. Those payloads carry no real author id, name, regno or email for an
anonymous post; only a fixed placeholder and a server-computed ``is_mine`` flag. The stronger
assertion is on the raw response body rather than on the parsed sender fields. A redaction
that only blanks ``user_info.name`` still leaks through ``author_id``, a comment, or any
future field, and the substring check fails on all of those at once.
"""

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.db_models import ConfessionCommentRecord, ConfessionRecord, ReportRecord, UserRecord
from app.main import app
from app.services.admin_service import get_all_confessions_data
from app.services.auth_service import get_current_user

REACTIONS = {"heart": [], "haha": [], "whoa": [], "heartbreak": []}


def make_user(db, regno, name):
    """A participant, with an identity distinctive enough to search for in a response body."""
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


def react(db, confession_id, user_id, key="heart"):
    """Record a reaction, keeping the denormalised counter in step the way the service does."""
    with db.sessions.begin() as session:
        row = session.get(ConfessionRecord, confession_id)
        reactions = dict(row.reactions or {})
        reactions[key] = [*reactions.get(key, []), str(user_id)]
        row.reactions = reactions
        row.heart_count += 1
        session.flush()


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
def anonymous_client():
    """A client with the auth override removed, to read the public feed and search."""
    override = app.dependency_overrides.pop(get_current_user, None)
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        if override is not None:
            app.dependency_overrides[get_current_user] = override


def user_id_for(db, regno: str) -> str:
    with db.sessions() as session:
        return str(session.scalar(select(UserRecord.id).where(UserRecord.regno == regno)))


def assert_anonymous_author_absent(response_text: str, author_id: str) -> None:
    """No real identity for the author anywhere in the raw JSON, in any field."""
    assert author_id not in response_text
    assert "Ghost Writer" not in response_text
    assert "ghostwriter" not in response_text
    assert "ghostwriter@test.com" not in response_text


def assert_anonymous_user_info(item) -> None:
    assert item["author_id"] is None
    assert item["user_info"] == {
        "id": "anonymous",
        "name": "Anonymous",
        "profile_picture_url": None,
    }


@pytest.fixture
def anonymous_post(db):
    """One anonymous confession, with a comment on it and a report against it."""
    author_id = make_user(db, "ghostwriter", "Ghost Writer")
    commenter_id = make_user(db, "onlooker", "On Looker")
    confession_id = make_confession(db, author_id, anonymous=True)
    make_comment(db, confession_id, commenter_id)
    make_report(db, confession_id, commenter_id, "On Looker")
    return confession_id


@pytest.mark.parametrize("client_fixture", ["client", "anonymous_client"])
def test_the_participant_feed_withholds_an_anonymous_author(
    db, anonymous_post, request, client_fixture
):
    author_id = user_id_for(db, "ghostwriter")
    commenter_id = user_id_for(db, "onlooker")
    client = request.getfixturevalue(client_fixture)

    response = client.get("/confessions")
    assert response.status_code == 200, response.text
    item = find(response.json()["confessions"], anonymous_post)

    assert_anonymous_user_info(item)
    assert item["is_mine"] is False
    assert_anonymous_author_absent(response.text, author_id)
    # A commenter is a different population from the author and is intentionally named.
    assert item["comments"][0]["user_info"] == {
        "id": commenter_id,
        "name": "On Looker",
        "profile_picture_url": None,
    }


@pytest.mark.parametrize("client_fixture", ["client", "anonymous_client"])
def test_search_withholds_an_anonymous_author(
    db, anonymous_post, request, client_fixture, monkeypatch
):
    monkeypatch.setattr(settings, "EMBEDDING_SEARCH_ENABLED", False)
    author_id = user_id_for(db, "ghostwriter")
    client = request.getfixturevalue(client_fixture)

    response = client.post("/confessions/search", json={"query": "confession"})
    assert response.status_code == 200, response.text
    item = find(response.json()["keyword"]["items"], anonymous_post)

    assert_anonymous_user_info(item)
    assert_anonymous_author_absent(response.text, author_id)


@pytest.mark.parametrize(
    "action,payload",
    [
        ("react", {"reaction": "heart"}),
        ("report", {"reason": "harmful content"}),
    ],
)
def test_the_react_and_report_responses_withhold_an_anonymous_author(
    db, anonymous_post, _confession_author_client, action, payload
):
    author_id = user_id_for(db, "ghostwriter")

    response = _confession_author_client.post(
        f"/confessions/{anonymous_post}/{action}", json=payload
    )
    assert response.status_code == 200, response.text
    item = response.json()

    assert_anonymous_user_info(item)
    assert_anonymous_author_absent(response.text, author_id)


def test_the_author_recognises_their_own_anonymous_post_without_an_author_id(
    db, _confession_author_client
):
    author_id = "33333333-3333-3333-3333-333333333333"
    anonymous_id = make_confession(db, author_id, anonymous=True, body="my anonymous post")
    public_id = make_confession(db, author_id, anonymous=False, body="my public post")

    response = _confession_author_client.get("/confessions")
    assert response.status_code == 200, response.text
    items = response.json()["confessions"]

    anonymous_item = find(items, anonymous_id)
    assert anonymous_item["is_mine"] is True
    assert_anonymous_user_info(anonymous_item)

    # Ownership is still observable on a public post, where naming the author is the point.
    public_item = find(items, public_id)
    assert public_item["is_mine"] is True
    assert public_item["author_id"] == author_id
    assert public_item["user_info"]["name"] == "Author"


def test_the_author_can_still_update_their_own_anonymous_post(db, _confession_author_client):
    """Stripping author_id must not break the ownership check an edit depends on."""
    author_id = "33333333-3333-3333-3333-333333333333"
    confession_id = make_confession(
        db, author_id, anonymous=True, body="ready to be public"
    )

    response = _confession_author_client.put(
        f"/confessions/{confession_id}", json={"anonymous": False}
    )
    assert response.status_code == 200, response.text
    item = response.json()

    assert item["anonymous"] is False
    assert item["author_id"] == author_id
    assert item["is_mine"] is True
    assert item["user_info"]["name"] == "Author"


def test_a_public_post_still_names_its_author_to_participants(db, client):
    author_id = make_user(db, "openbook", "Open Book")
    commenter_id = make_user(db, "commenter", "Comment Writer")
    confession_id = make_confession(db, author_id, anonymous=False, body="a public post")
    make_comment(db, confession_id, commenter_id)

    item = find(client.get("/confessions").json()["confessions"], confession_id)

    assert item["author_id"] == author_id
    assert item["user_info"] == {
        "id": author_id,
        "name": "Open Book",
        "profile_picture_url": None,
    }
    assert item["comments"][0]["user_info"]["name"] == "Comment Writer"


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
    item = find(response.json()["confessions"], anonymous_post)
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


def test_reactors_are_resolved_by_name_for_a_reviewer(db):
    """The point of the reactor list is to name an account a reviewer can act on."""
    author_id = make_user(db, "author", "Post Author")
    reactor_id = make_user(db, "reactor", "Heart Reactor")
    confession_id = make_confession(db, author_id, anonymous=False)
    react(db, confession_id, reactor_id, key="heart")

    item = find(get_all_confessions_data(db), confession_id)
    assert item["reaction_users"]["heart"] == [
        {"id": reactor_id, "name": "Heart Reactor", "withheld_reason": None}
    ]
    # The raw id list is retained alongside it: the counters are a separate column, so anything
    # auditing engagement needs both rather than one derived from the other.
    assert item["reactions"]["heart"] == [reactor_id]


def test_an_anonymous_authors_reaction_on_their_own_post_is_withheld(db):
    """The likeliest reactor on an anonymous post is its author, and naming them would unmask it."""
    author_id = make_user(db, "ghost", "Ghost Writer")
    confession_id = make_confession(db, author_id, anonymous=True)
    react(db, confession_id, author_id, key="heart")

    item = find(get_all_confessions_data(db), confession_id)
    assert item["reaction_users"]["heart"] == [
        {"id": author_id, "name": None, "withheld_reason": "anonymous_author"}
    ]
    assert "Ghost Writer" not in json.dumps(get_all_confessions_data(db))


def test_reactors_on_an_anonymous_post_are_named_apart_from_its_author(db):
    """Withholding the author is not withholding everyone: other reactors are still reviewable."""
    author_id = make_user(db, "ghost2", "Ghost Writer Two")
    reactor_id = make_user(db, "onlooker2", "On Looker Two")
    confession_id = make_confession(db, author_id, anonymous=True)
    react(db, confession_id, author_id, key="haha")
    react(db, confession_id, reactor_id, key="haha")

    item = find(get_all_confessions_data(db), confession_id)
    assert item["reaction_users"]["haha"] == [
        {"id": author_id, "name": None, "withheld_reason": "anonymous_author"},
        {"id": reactor_id, "name": "On Looker Two", "withheld_reason": None},
    ]


def test_anonymous_authorship_is_respected_once_a_reaction_is_named(db):
    """One person, one anonymous post and one public post, reacting in both cases."""
    author_id = make_user(db, "dualreactor", "Dual Reactor")
    anonymous_id = make_confession(db, author_id, anonymous=True, body="an anonymous confession")
    public_id = make_confession(db, author_id, anonymous=False, body="a public confession")
    react(db, anonymous_id, author_id, key="whoa")
    react(db, public_id, author_id, key="whoa")

    items = get_all_confessions_data(db)
    anonymous_item = find(items, anonymous_id)
    public_item = find(items, public_id)
    assert anonymous_item["reaction_users"]["whoa"][0]["name"] is None
    assert public_item["reaction_users"]["whoa"][0]["name"] == "Dual Reactor"


def test_a_reaction_from_a_deleted_account_is_withheld_rather_than_dropped(db):
    """The id outlives the row in the JSONB array; the entry stays so the list still adds up."""
    author_id = make_user(db, "author3", "Post Author Three")
    confession_id = make_confession(db, author_id, anonymous=False)
    react(db, confession_id, "00000000-0000-0000-0000-000000000009", key="heartbreak")

    item = find(get_all_confessions_data(db), confession_id)
    assert item["reaction_users"]["heartbreak"] == [
        {"id": "00000000-0000-0000-0000-000000000009", "name": None, "withheld_reason": "unknown"}
    ]


@pytest.mark.parametrize("staff_fixture", ["admin_client", "moderator_client"])
def test_the_review_endpoint_serves_reaction_users(db, request, staff_fixture):
    client = request.getfixturevalue(staff_fixture)
    author_id = make_user(db, "author4", "Post Author Four")
    reactor_id = make_user(db, "reactor4", "Whoa Reactor")
    confession_id = make_confession(db, author_id, anonymous=False)
    react(db, confession_id, reactor_id, key="whoa")

    response = client.get("/admin/confessions")
    assert response.status_code == 200
    item = find(response.json()["confessions"], confession_id)
    assert item["reaction_users"]["whoa"][0]["name"] == "Whoa Reactor"
