"""The participant feed is strictly paginated on the database itself.

``GET /confessions`` used to return every visible confession in one response -- every
comment, every user row and two report lookups per row attached. A feed with thousands
of posts made every visit load the whole table, and the entire users table with it.
These tests pin the replacement contract:

* every read answers ``{confessions, total, skip, limit}`` and never a bare list;
* ``limit`` is clamped (anything over 100 is a 422), so no client can ask for the feed;
* ``skip``/``limit`` are a SQL OFFSET/LIMIT on the sorted visible posts, not a Python
  slice of a full load;
* the sort picker orders in SQL (recent, most reactions, most comments), with the total
  counting the same visible posts so the screen pages the feed it asked for;
* removed posts stay out of the page and out of the total;
* each post on the page still carries its comments, author snapshot and reporter ids --
  resolved with bulk reads for the page alone, never the whole tables.
"""

from datetime import datetime, timedelta, timezone

from app.db_models import ConfessionCommentRecord, ConfessionRecord, ReportRecord, UserRecord

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


def make_confession(db, author, *, body="a confession", created_at=None):
    with db.sessions.begin() as session:
        row = ConfessionRecord(
            body=body,
            recipient_name="Someone In Particular",
            anonymous=False,
            comments_enabled=True,
            created_at=created_at or datetime.now(timezone.utc),
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


def set_counters(db, confession_id, **counters):
    with db.sessions.begin() as session:
        row = session.get(ConfessionRecord, str(confession_id))
        for column, value in counters.items():
            setattr(row, column, value)


def file_report(db, confession_id, reporter, *, reason="harmful content"):
    with db.sessions.begin() as session:
        row = ReportRecord(
            confession_id=str(confession_id),
            reported_by_id=reporter["id"],
            reported_by_name=reporter["name"],
            reason=reason,
            created_at=datetime.now(timezone.utc),
            status="pending",
        )
        session.add(row)
        session.flush()
        return str(row.id)


def bodies(page):
    return [item["body"] for item in page["confessions"]]


def test_the_feed_answers_a_page_envelope_not_a_list(db, client):
    author = make_user(db, "author")
    make_confession(db, author, body="hello there")

    response = client.get("/confessions")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) >= {"confessions", "total", "skip", "limit"}
    assert body["skip"] == 0
    assert body["limit"] == 50
    assert body["total"] == 1
    assert bodies(body) == ["hello there"]


def test_limit_is_clamped(db, client):
    author = make_user(db, "author")
    make_confession(db, author)

    assert client.get("/confessions", params={"limit": 1000}).status_code == 422


def test_skip_and_limit_walk_newest_first(db, client):
    author = make_user(db, "author")
    base = datetime.now(timezone.utc)
    for index in range(5):
        make_confession(
            db, author, body=f"post {index}",
            created_at=base - timedelta(minutes=index),
        )

    first = client.get("/confessions", params={"skip": 0, "limit": 2}).json()
    second = client.get("/confessions", params={"skip": 2, "limit": 2}).json()
    third = client.get("/confessions", params={"skip": 4, "limit": 2}).json()

    assert first["total"] == 5
    assert bodies(first) == ["post 0", "post 1"]
    assert bodies(second) == ["post 2", "post 3"]
    assert bodies(third) == ["post 4"]
    seen = [item["id"] for page in (first, second, third) for item in page["confessions"]]
    assert len(set(seen)) == 5, "paging re-read a confession or skipped one"


def test_sort_orders_by_reactions_and_comments(db, client):
    author = make_user(db, "author")
    base = datetime.now(timezone.utc)
    quiet = make_confession(db, author, body="quiet", created_at=base - timedelta(hours=3))
    loud = make_confession(db, author, body="loud", created_at=base - timedelta(hours=2))
    busy = make_confession(db, author, body="busy", created_at=base - timedelta(hours=1))
    set_counters(db, loud, heart_count=5)
    set_counters(db, busy, comment_count=4)

    # Popularity only counts reactions: the two silent posts tie at zero and keep
    # oldest-first order between them, exactly as the old Python sort did.
    assert bodies(client.get("/confessions", params={"sort_by": "popularity"}).json()) == [
        "loud", "quiet", "busy",
    ]
    # Same tiebreak for comment counts: the two uncommented posts tie at zero and
    # keep oldest-first order between them.
    assert bodies(client.get("/confessions", params={"sort_by": "comments"}).json()) == [
        "busy", "quiet", "loud",
    ]
    assert bodies(client.get("/confessions").json()) == ["busy", "loud", "quiet"]


def test_removed_posts_stay_out_of_the_page_and_the_total(db, client, moderator_client):
    author = make_user(db, "author")
    reporter = make_user(db, "reporter")
    gone = make_confession(db, author, body="taken down")
    file_report(db, gone, reporter)
    report_id = moderator_client.get("/admin/confessions/reports").json()["reports"][0]["id"]
    moderator_client.patch(f"/admin/confessions/reports/{report_id}", params={"status": "removed"})
    make_confession(db, author, body="still up")

    page = client.get("/confessions").json()
    assert page["total"] == 1
    assert bodies(page) == ["still up"]


def test_a_thread_opens_with_a_bounded_window_beside_the_true_total(db, client):
    author = make_user(db, "author")
    commenter = make_user(db, "commenter")
    confession_id = make_confession(db, author, body="a long thread")
    for index in range(12):
        make_comment(db, confession_id, commenter, body=f"reply {index}")
    # Direct inserts bypass the write path that maintains the denormalised counter.
    set_counters(db, confession_id, comment_count=12)

    page = client.get("/confessions").json()
    item = next(row for row in page["confessions"] if row["id"] == confession_id)
    # Twelve replies exist; the feed embeds only the ten newest. The count stays true
    # so the dialog knows how many more the thread holds.
    assert item["comment_count"] == 12
    assert [comment["body"] for comment in item["comments"]] == [
        f"reply {index}" for index in range(11, 1, -1)
    ]


def test_a_thread_pages_newest_first_with_its_total(db, client):
    author = make_user(db, "author")
    commenter = make_user(db, "commenter")
    confession_id = make_confession(db, author, body="a discussed post")
    for index in range(12):
        make_comment(db, confession_id, commenter, body=f"reply {index}")

    first = client.get(f"/confessions/{confession_id}/comments", params={"skip": 0, "limit": 5}).json()
    second = client.get(f"/confessions/{confession_id}/comments", params={"skip": 5, "limit": 5}).json()
    third = client.get(f"/confessions/{confession_id}/comments", params={"skip": 10, "limit": 5}).json()

    assert set(first) >= {"comments", "total", "skip", "limit"}
    assert first["total"] == 12
    assert [comment["body"] for comment in first["comments"]] == [
        f"reply {index}" for index in range(11, 6, -1)
    ]
    assert [comment["body"] for comment in second["comments"]] == [
        f"reply {index}" for index in range(6, 1, -1)
    ]
    assert [comment["body"] for comment in third["comments"]] == ["reply 1", "reply 0"]
    seen = [comment["id"] for page in (first, second, third) for comment in page["comments"]]
    assert len(set(seen)) == 12
    # Thread rows carry what the dialog renders: author snapshot, counts and reactions.
    assert first["comments"][0]["user_info"]["name"] == "User commenter"
    assert all("user_reaction" in comment for comment in first["comments"])


def set_comment_votes(db, comment_id, likes, dislikes):
    """Ballot-box state for sort tests, the way likes and dislikes leave it."""
    from app.db_models import ConfessionCommentRecord
    with db.sessions.begin() as session:
        row = session.get(ConfessionCommentRecord, str(comment_id))
        row.like_count = likes
        row.dislike_count = dislikes


def test_relevant_orders_by_share_of_likes(db, client):
    author = make_user(db, "author")
    commenter = make_user(db, "commenter")
    confession_id = make_confession(db, author, body="a judged thread")
    approved = make_comment(db, confession_id, commenter, body="loved unanimously")
    set_comment_votes(db, approved, likes=3, dislikes=0)
    divisive = make_comment(db, confession_id, commenter, body="divisive remark")
    set_comment_votes(db, divisive, likes=6, dislikes=4)
    set_counters(db, confession_id, comment_count=2)

    page = client.get(
        f"/confessions/{confession_id}/comments", params={"sort": "relevant"}).json()
    # Three of three likes outranks six of ten: relevance is the share of approval,
    # not the pile of it.
    assert [comment["body"] for comment in page["comments"]] == [
        "loved unanimously", "divisive remark",
    ]
    assert page["total"] == 2


def test_relevant_counts_a_voteless_comment_as_zero(db, client):
    author = make_user(db, "author")
    commenter = make_user(db, "commenter")
    confession_id = make_confession(db, author, body="a quiet thread")
    make_comment(db, confession_id, commenter, body="no votes yet")
    liked = make_comment(db, confession_id, commenter, body="one fan")
    set_comment_votes(db, liked, likes=1, dislikes=0)
    set_counters(db, confession_id, comment_count=2)

    page = client.get(
        f"/confessions/{confession_id}/comments", params={"sort": "relevant"}).json()
    # No division by zero: silence scores nothing and sinks below any approval.
    assert [comment["body"] for comment in page["comments"]] == ["one fan", "no votes yet"]


def test_relevant_pages_the_ranked_thread(db, client):
    author = make_user(db, "author")
    commenter = make_user(db, "commenter")
    confession_id = make_confession(db, author, body="a ranked thread")
    for index in range(3):
        comment_id = make_comment(db, confession_id, commenter, body=f"reply {index}")
        set_comment_votes(db, comment_id, likes=index, dislikes=0)
    set_counters(db, confession_id, comment_count=3)

    first = client.get(
        f"/confessions/{confession_id}/comments",
        params={"sort": "relevant", "skip": 0, "limit": 2}).json()
    second = client.get(
        f"/confessions/{confession_id}/comments",
        params={"sort": "relevant", "skip": 2, "limit": 2}).json()
    assert first["total"] == 3
    assert [comment["body"] for comment in first["comments"]] == ["reply 2", "reply 1"]
    assert [comment["body"] for comment in second["comments"]] == ["reply 0"]


def test_unknown_thread_sort_is_refused(db, client):
    author = make_user(db, "author")
    confession_id = make_confession(db, author, body="a thread")
    response = client.get(f"/confessions/{confession_id}/comments", params={"sort": "random"})
    assert response.status_code == 422, response.text


def test_a_missing_or_removed_thread_reads_as_absent(db, client, moderator_client):
    assert client.get("/confessions/11111111-1111-1111-1111-111111111111/comments").status_code == 404

    author = make_user(db, "author")
    reporter = make_user(db, "reporter")
    confession_id = make_confession(db, author, body="taken down")
    file_report(db, confession_id, reporter)
    report_id = moderator_client.get("/admin/confessions/reports").json()["reports"][0]["id"]
    moderator_client.patch(f"/admin/confessions/reports/{report_id}", params={"status": "removed"})

    assert client.get(f"/confessions/{confession_id}/comments").status_code == 404


def test_a_page_carries_its_own_comments_authors_and_reporters(db, client):
    author = make_user(db, "author", name="The Author")
    commenter = make_user(db, "commenter", name="The Commenter")
    reporter = make_user(db, "reporter", name="The Reporter")
    confession_id = make_confession(db, author, body="a discussed post")
    make_comment(db, confession_id, commenter, body="a reply")
    file_report(db, confession_id, reporter)

    page = client.get("/confessions").json()
    assert page["total"] == 1
    item = page["confessions"][0]
    assert item["user_info"]["name"] == "The Author"
    assert [comment["body"] for comment in item["comments"]] == ["a reply"]
    assert item["comments"][0]["user_info"]["name"] == "The Commenter"
    assert item["reported_by"] == [reporter["id"]]
    assert item["report_count"] == 1
