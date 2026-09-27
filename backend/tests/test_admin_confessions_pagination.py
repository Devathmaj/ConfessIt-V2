"""The admin confession list is strictly paginated on the database itself.

``GET /admin/confessions`` used to return every confession in one response: one unbounded
SELECT with every comment, report and reactor row attached, serialised whole. With
thousands of confessions that one request materialises them all at once. These tests pin
the replacement contract:

* every read answers ``{confessions, total, skip, limit}`` and never a bare list;
* ``limit`` is clamped (anything over 100 is a 422), so no client can ask for the table;
* ``skip``/``limit`` are a SQL OFFSET/LIMIT on the filtered, ordered query, not a Python
  slice of a full load;
* ``q``, ``filter`` and ``sort`` are SQL WHERE/ORDER BY for the same reason, with the
  total counted under the same filter so the screen can load more downwards page by page;
* searching an anonymous author's name surfaces nothing: the sender clauses apply only
  to non-anonymous confessions, or the search would be an oracle for the identity the
  response itself withholds.
"""

from datetime import datetime, timedelta, timezone

from app.db_models import ConfessionCommentRecord, ConfessionRecord, UserRecord

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


def make_confession(db, author, *, anonymous=False, body="a confession",
                    recipient="Someone", created_at=None):
    with db.sessions.begin() as session:
        row = ConfessionRecord(
            body=body,
            recipient_name=recipient,
            anonymous=anonymous,
            comments_enabled=True,
            created_at=created_at or datetime.now(timezone.utc),
            author_id=author["id"],
            reactions=dict(REACTIONS),
        )
        session.add(row)
        session.flush()
        return str(row.id)


def set_counters(db, confession_id, **counters):
    """Write denormalised counters directly, so sort tests order by known values."""
    with db.sessions.begin() as session:
        row = session.get(ConfessionRecord, str(confession_id))
        for column, value in counters.items():
            setattr(row, column, value)


def bodies(page):
    return [item["body"] for item in page["confessions"]]


def test_the_confession_list_answers_a_page_envelope_not_a_list(db, admin_client):
    author = make_user(db, "author")
    make_confession(db, author, body="hello there")

    response = admin_client.get("/admin/confessions")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) >= {"confessions", "total", "skip", "limit"}
    assert body["skip"] == 0
    assert body["limit"] == 50
    assert body["total"] == 1
    assert bodies(body) == ["hello there"]


def test_limit_is_clamped_and_unknown_filter_and_sort_are_refused(db, admin_client):
    author = make_user(db, "author")
    make_confession(db, author)

    assert admin_client.get("/admin/confessions", params={"limit": 1000}).status_code == 422
    assert admin_client.get("/admin/confessions", params={"filter": "forwarded"}).status_code == 422
    assert admin_client.get("/admin/confessions", params={"sort": "random"}).status_code == 422


def test_skip_and_limit_walk_newest_first_without_holding_everything(db, admin_client):
    author = make_user(db, "author")
    base = datetime.now(timezone.utc)
    for index in range(5):
        make_confession(
            db, author, body=f"post {index}",
            created_at=base - timedelta(minutes=index),
        )

    first = admin_client.get("/admin/confessions", params={"skip": 0, "limit": 2}).json()
    second = admin_client.get("/admin/confessions", params={"skip": 2, "limit": 2}).json()
    third = admin_client.get("/admin/confessions", params={"skip": 4, "limit": 2}).json()

    assert first["total"] == 5
    assert bodies(first) == ["post 0", "post 1"]
    assert bodies(second) == ["post 2", "post 3"]
    assert bodies(third) == ["post 4"]
    seen = [item["id"] for page in (first, second, third) for item in page["confessions"]]
    assert len(set(seen)) == 5, "paging re-read a confession or skipped one"


def test_search_matches_body_recipient_and_public_sender(db, admin_client):
    author = make_user(db, "author", name="Ada Lovelace")
    make_confession(db, author, body="a quiet post", recipient="Grace")
    make_confession(db, author, body="the lighthouse post", recipient="Nobody")
    make_confession(db, author, body="an unrelated post", recipient="Nobody Else")

    assert bodies(admin_client.get("/admin/confessions", params={"q": "lighthouse"}).json()) == [
        "the lighthouse post"
    ]
    assert bodies(admin_client.get("/admin/confessions", params={"q": "grace"}).json()) == [
        "a quiet post"
    ]
    found = admin_client.get("/admin/confessions", params={"q": "lovelace"}).json()
    assert found["total"] == 3


def test_searching_an_anonymous_authors_name_surfaces_nothing(db, admin_client):
    author = make_user(db, "ghostwriter", name="Ghost Writer")
    make_confession(db, author, anonymous=True, body="a post with no name on it")

    found = admin_client.get("/admin/confessions", params={"q": "ghost writer"}).json()
    assert found["total"] == 0
    assert found["confessions"] == []


def test_filter_narrows_reported_anonymous_and_commented(db, admin_client):
    author = make_user(db, "author")
    plain = make_confession(db, author, body="plain")
    flagged = make_confession(db, author, body="flagged")
    set_counters(db, flagged, report_count=2)
    anon = make_confession(db, author, anonymous=True, body="faceless")
    with_comments = make_confession(db, author, body="discussed")
    with db.sessions.begin() as session:
        session.add(ConfessionCommentRecord(
            confession_id=with_comments, user_id=author["id"], body="a reply",
            created_at=datetime.now(timezone.utc),
        ))

    # `plain` matches nothing, so it only shows up in the unfiltered total below.
    assert bodies(admin_client.get(
        "/admin/confessions", params={"filter": "reported"}).json()) == ["flagged"]
    assert bodies(admin_client.get(
        "/admin/confessions", params={"filter": "anonymous"}).json()) == ["faceless"]
    assert bodies(admin_client.get(
        "/admin/confessions", params={"filter": "with-comments"}).json()) == ["discussed"]
    assert admin_client.get("/admin/confessions").json()["total"] == 4


def test_sort_orders_by_reports_comments_reactions_and_recency(db, admin_client):
    author = make_user(db, "author")
    base = datetime.now(timezone.utc)
    quiet = make_confession(db, author, body="quiet", created_at=base - timedelta(hours=3))
    loud = make_confession(db, author, body="loud", created_at=base - timedelta(hours=2))
    busy = make_confession(db, author, body="busy", created_at=base - timedelta(hours=1))
    set_counters(db, loud, report_count=5)
    set_counters(db, busy, comment_count=4, heart_count=10)

    assert bodies(admin_client.get("/admin/confessions", params={"sort": "reports"}).json()) == [
        "loud", "busy", "quiet",
    ]
    assert bodies(admin_client.get("/admin/confessions", params={"sort": "comments"}).json()) == [
        "busy", "loud", "quiet",
    ]
    assert bodies(admin_client.get("/admin/confessions", params={"sort": "reactions"}).json()) == [
        "busy", "loud", "quiet",
    ]
    assert bodies(admin_client.get("/admin/confessions").json()) == ["busy", "loud", "quiet"]


def test_a_hidden_confession_comes_back_flagged_on_its_page(db, admin_client, moderator_client):
    author = make_user(db, "author")
    confession_id = make_confession(db, author, body="take this down")
    moderator_client.patch(f"/admin/confessions/{confession_id}/hide")

    page = admin_client.get("/admin/confessions").json()
    assert page["total"] == 1
    assert page["confessions"][0]["content_removed"] is True
