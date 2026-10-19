"""A malformed id names nothing, and must be answered as nothing.

Every id in the confession and comment paths goes straight into a ``uuid`` column.
PostgreSQL does not treat a value that is not a uuid as a miss: it raises
``invalid input syntax for type uuid`` and refuses the statement. Nothing caught
that, so a request that named nothing at all was answered with a 500 -- and on
``GET /confessions/{id}/comments`` that route needs no authentication, so anybody
could ask for one on purpose and burn the error budget with it.

The fix is a guard in the service, where the id meets the database, and the answer
is the one these endpoints already give for a confession or comment that is not
there. A malformed id is an id that names no row, so it takes the same path as a
well-formed one that happens to be absent, and a client that opens a thread by id
gets one answer for "there is no such confession" whatever shape the id had.

The status codes are the endpoints' own: 404 for the routes that answer "not
found", and 400 for the two report routes, whose "the report was not filed" answer
was already 400 for an id that names nothing.
"""

import pytest
from sqlalchemy import func, select
from datetime import datetime, timezone

from app.db_models import ConfessionCommentRecord, ConfessionRecord, UserRecord
from app.services.confession_service import ConfessionService

# A value that is not a uuid, in the several shapes a caller can send. None of them
# names a row, and none of them may reach a uuid column.
#
# A value with a slash in it is not here: it cannot be a single path segment, so it
# never reaches the route at all and the router answers before the service does.
MALFORMED = [
    "not-a-uuid",
    "123",
    "00000000-0000-0000-0000-00000000000",   # one character short of a uuid
    "00000000-0000-0000-0000-0000000000000",  # one character long
    "00000000000000000000000000000000",
]


def make_user(db, regno="author"):
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno, name=f"User {regno}", email=f"{regno}@test.com",
            year=2, department="Test_Class", gender="other",
        )
        session.add(row)
        session.flush()
        return str(row.id)


def make_confession(db, author_id):
    with db.sessions.begin() as session:
        row = ConfessionRecord(
            author_id=author_id, body="a confession", anonymous=False,
            comments_enabled=True, created_at=_now(),
        )
        session.add(row)
        session.flush()
        return str(row.id)


def make_comment(db, confession_id, author_id):
    with db.sessions.begin() as session:
        row = ConfessionCommentRecord(
            confession_id=confession_id, user_id=author_id, body="a comment",
            created_at=_now(),
        )
        session.add(row)
        session.flush()
        return str(row.id)


def _now():
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ the public endpoint


@pytest.mark.parametrize("malformed", MALFORMED)
def test_the_public_comments_endpoint_answers_404_for_a_malformed_id(client, malformed):
    """The regression the finding names, and the one that needs no authentication."""
    response = client.get(f"/confessions/{malformed}/comments")

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Confession not found."


def test_the_public_comments_endpoint_is_open_to_everyone(db, client):
    """The endpoint that made the 500 free to ask for.

    Pinned so the guard cannot be moved behind the authentication gate to "fix" it:
    the route is public, and a public route that answers 500 for a malformed id is a
    500 anybody can cause.
    """
    response = client.get("/confessions/not-a-uuid/comments")
    assert response.status_code != 401, "the endpoint answered an auth error, not the route"
    assert response.status_code != 403, "the endpoint answered an auth error, not the route"
    assert response.status_code == 404


# ------------------------------------------------------------------ the guarded routes


@pytest.mark.parametrize(
    "method, path, payload",
    [
        ("put", "/confessions/{id}", {"comments_enabled": False}),
        ("post", "/confessions/{id}/react", {"reaction": "heart"}),
        ("post", "/confessions/{id}/comment", {"body": "hello"}),
        ("post", "/confessions/{id}/report", {"reason": "spam"}),
        ("post", "/comments/{id}/like", None),
        ("post", "/comments/{id}/dislike", None),
        ("post", "/comments/{id}/report", {"reason": "spam"}),
    ],
)
@pytest.mark.parametrize("malformed", MALFORMED)
def test_no_confession_route_answers_500_for_a_malformed_id(
    client, method, path, payload, malformed
):
    """The same class behind auth: a 500 there is a 500 for a request that named nothing."""
    response = getattr(client, method)(path.format(id=malformed), json=payload)

    assert response.status_code != 500, response.text
    assert response.status_code in (400, 404), response.text


# -------------------------------------------------- a malformed id and a missing one agree


def test_a_malformed_id_answers_exactly_as_a_missing_one_does(client, db):
    """One answer for "there is no such confession", whatever shape the id had.

    A client that opens a thread by id should not have to tell the two apart: both
    mean the same thing, and the endpoint already had an answer for it.
    """
    missing = client.get("/confessions/00000000-0000-0000-0000-000000000000/comments")
    malformed = client.get("/confessions/not-a-uuid/comments")

    assert missing.status_code == malformed.status_code
    assert missing.json()["detail"] == malformed.json()["detail"]


def test_the_service_answers_none_for_a_malformed_confession_id(db):
    """The service's own answer, which is what the router turns into the 404."""
    service = ConfessionService(db)

    assert service.get_confession_comments("not-a-uuid", None) is None
    assert service.get_confession("not-a-uuid", None) is None


def test_the_service_answers_none_for_a_malformed_comment_id(db):
    service = ConfessionService(db)

    assert service._react_to_comment("not-a-uuid", "someone", "likes") is None


# ------------------------------------------------------------------ nothing was written


def test_a_refused_malformed_id_writes_nothing(db, client):
    """A refused request is a request that changed nothing."""
    author_id = make_user(db)
    confession_id = make_confession(db, author_id)
    comment_id = make_comment(db, confession_id, author_id)

    client.post("/confessions/not-a-uuid/comment", json={"body": "hello"})
    client.post("/comments/not-a-uuid/like")
    client.post("/confessions/not-a-uuid/report", json={"reason": "spam"})
    client.post("/comments/not-a-uuid/report", json={"reason": "spam"})

    with db.sessions() as session:
        assert session.scalar(
            select(func.count()).select_from(ConfessionCommentRecord)
        ) == 1, "a comment was written for a malformed id"
        assert session.scalar(
            select(func.count()).select_from(ConfessionRecord)
        ) == 1, "a confession was written for a malformed id"


def test_a_well_formed_id_still_reaches_its_row(db, client):
    """The counterweight: the guard refuses a malformed id and nothing else.

    A guard that also refused well-formed ids would be a guard that broke the
    feature, and the tests above would still pass.
    """
    author_id = make_user(db)
    confession_id = make_confession(db, author_id)
    comment_id = make_comment(db, confession_id, author_id)

    comments = client.get(f"/confessions/{confession_id}/comments")
    assert comments.status_code == 200, comments.text
    assert comments.json()["total"] == 1
