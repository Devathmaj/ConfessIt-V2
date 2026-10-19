"""Engagement counters must not lose an update when two requests land at once.

Three counters are denormalised onto the rows they describe: the reaction map on a
confession, the number of comments on that confession, and the like/dislike lists on a
comment. Each was written by reading the row, changing it in Python, and writing the whole
row back. Under PostgreSQL's READ COMMITTED two requests that read before either wrote
both pass their check, and the second write erases the first: a participant's reaction
disappears, a comment is not counted, a like is lost -- and because the counts are
recomputed from the same lossy value, the count and the thing it counts disagree.

The participant-facing limit had the same shape: "three comments per participant per
confession" was a COUNT read in one transaction and an insert in another, so two comments
sent at the same moment both read "two so far" and both went in.

The races below are arranged rather than hoped for. The test takes the row lock the
requests are about to contend for, starts them, and waits until every one of them is
parked inside its statement -- which means each has already read what it was going to
write. Releasing the lock then lets them all through together.

Against the unfixed code every one of those requests read the same value and the last
write won: one reaction instead of twenty, one counted comment instead of ten, four
comments instead of three, one like instead of ten.
"""

import datetime
import threading
import time
import uuid

import pytest
from sqlalchemy import func, select, text

from app.accounts import AccountType
from app.db_models import (
    ConfessionCommentRecord,
    ConfessionRecord,
    UserRecord,
)
from app.models import CommentCreate, UserDetails
from app.postgres import PostgresClient
from app.services.confession_service import ConfessionService

REACTION_KINDS = ["heart", "haha", "whoa", "heartbreak"]

# How many requests race in each test. The shared test client has a pool of four plus
# four, which is fewer connections than a cast this size needs -- the surplus threads
# would queue for a connection instead of reaching the row, and the arrangement would
# stop being the one it claims to be. The client below has room for all of them.
RACERS = 20


@pytest.fixture
def wide_db():
    """A client with a pool big enough for the whole cast."""
    client = PostgresClient(pool_size=RACERS + 4, max_overflow=0, pool_timeout=30)
    try:
        yield client
    finally:
        client.close()


# --- fixtures ---------------------------------------------------------------------


def make_user(db, regno) -> str:
    user_id = str(uuid.uuid4())
    with db.sessions.begin() as session:
        session.add(
            UserRecord(
                id=user_id,
                regno=regno,
                name=f"User {regno}",
                email=f"{regno}@test.com",
                year=2,
                department="Test",
                gender="other",
            )
        )
    return user_id


def user_details(db, regno) -> UserDetails:
    with db.sessions() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
    return UserDetails(
        id=str(row.id),
        regno=row.regno,
        name=row.name,
        email=row.email,
        year=row.year,
        department=row.department,
        gender=row.gender,
        matchmaking_enabled=row.matchmaking_enabled,
        notifications_enabled=row.notifications_enabled,
        account_type=AccountType.USERS.value,
        last_matchmaking_at=row.last_matchmaking_at,
    )


def make_confession(db, author_id, *, comments_enabled=False) -> str:
    confession_id = str(uuid.uuid4())
    with db.sessions.begin() as session:
        session.add(
            ConfessionRecord(
                id=confession_id,
                body="hello",
                author_id=author_id,
                created_at=datetime.datetime.now(datetime.timezone.utc),
                reactions={kind: [] for kind in REACTION_KINDS},
                comments_enabled=comments_enabled,
            )
        )
    return confession_id


def make_comment(db, confession_id, author_id) -> str:
    comment_id = str(uuid.uuid4())
    with db.sessions.begin() as session:
        session.add(
            ConfessionCommentRecord(
                id=comment_id,
                confession_id=confession_id,
                user_id=author_id,
                body="a reply",
                created_at=datetime.datetime.now(datetime.timezone.utc),
            )
        )
    return comment_id


# --- arranging the race -----------------------------------------------------------


def hold_row(db, table: str, row_id: str):
    """Take the row lock the requests are about to contend for.

    ``FOR NO KEY UPDATE``, not ``FOR UPDATE``: inserting a comment takes a ``FOR KEY
    SHARE`` lock on the confession it points at, and ``FOR UPDATE`` conflicts with that.
    The requests would then queue on the insert instead of on the write they are racing,
    which is a different arrangement from the one this test claims to make.
    ``FOR NO KEY UPDATE`` conflicts with the write itself -- the row update takes the
    same mode -- and leaves the foreign-key check alone.
    """
    connection = db.engine.connect()
    transaction = connection.begin()
    connection.execute(
        text(f"SELECT id FROM {table} WHERE id = :id FOR NO KEY UPDATE"), {"id": row_id}
    )
    return connection, transaction


def wait_for_blocked(db, pattern: str, expected: int, timeout=60.0) -> int:
    """Wait until ``expected`` requests are parked inside a statement matching ``pattern``.

    ``pg_stat_activity`` reports a backend that is waiting for a lock together with the
    statement it is waiting in, and both matter: the statement says which step of the
    write the request is in, and the wait says it has not finished. Seeing all of them is
    the barrier -- every request has read what it is about to write.
    """
    deadline = time.monotonic() + timeout
    waiting = 0
    while time.monotonic() < deadline:
        with db.engine.connect() as connection:
            waiting = connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' "
                    "AND query ~* :pattern"
                ),
                {"pattern": pattern},
            )
        if waiting >= expected:
            return int(waiting)
        time.sleep(0.02)
    raise AssertionError(
        f"only {waiting} of {expected} requests reached the row lock within {timeout}s"
    )


def call_in_thread(fn, results, name):
    def run():
        try:
            results.append(("ok", fn()))
        except Exception as exc:  # a crash is a failed test, reported not swallowed
            results.append(("error", exc))

    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    return thread


def race_past_the_row_lock(db, table, row_id, pattern, calls):
    """Start ``calls`` together, let them all reach their statement, then release them."""
    connection, transaction = hold_row(db, table, row_id)
    try:
        results = []
        threads = [
            call_in_thread(call, results, name=f"call-{index}")
            for index, call in enumerate(calls)
        ]
        wait_for_blocked(db, pattern, expected=len(calls))
    finally:
        transaction.rollback()
        connection.close()

    for thread in threads:
        thread.join(timeout=120)
        assert not thread.is_alive(), f"{thread.name} never finished"

    errors = [outcome for outcome in results if outcome[0] == "error"]
    assert not errors, errors
    return results


# --- the races --------------------------------------------------------------------


def test_twenty_concurrent_reactions_are_all_stored(wide_db):
    """The finding's own regression: twenty reactions, all twenty present afterwards."""
    service = ConfessionService(wide_db)
    author = make_user(wide_db, "author")
    confession_id = make_confession(wide_db, author)
    reactors = [make_user(wide_db, f"reactor-{index}") for index in range(RACERS)]

    race_past_the_row_lock(
        wide_db,
        "confessions",
        confession_id,
        r"(from|update) confessions\y",
        [
            lambda user_id=user_id: service.react_to_confession(
                confession_id, "heart", user_id
            )
            for user_id in reactors
        ],
    )

    with wide_db.sessions() as session:
        row = session.get(ConfessionRecord, confession_id)
        reactions = row.reactions or {}
        counts = tuple(getattr(row, f"{kind}_count") for kind in REACTION_KINDS)

    assert len(reactions["heart"]) == RACERS, reactions
    assert counts == (RACERS, 0, 0, 0), counts
    for kind in REACTION_KINDS[1:]:
        assert reactions[kind] == [], reactions


def test_concurrent_comments_are_every_one_of_them_counted(wide_db):
    """``comment_count`` is what the feed sorts by, so a lost comment is a lost post."""
    service = ConfessionService(wide_db)
    author = make_user(wide_db, "author")
    confession_id = make_confession(wide_db, author, comments_enabled=True)
    commenters = [make_user(wide_db, f"commenter-{index}") for index in range(10)]
    details = {regno: user_details(wide_db, regno) for regno in
               (f"commenter-{index}" for index in range(10))}

    race_past_the_row_lock(
        wide_db,
        "confessions",
        confession_id,
        r"(from|update) confessions\y",
        [
            lambda index=index: service.add_comment_to_confession(
                confession_id,
                CommentCreate(body=f"a reply from {index}"),
                details[f"commenter-{index}"],
            )
            for index in range(10)
        ],
    )

    with wide_db.sessions() as session:
        row = session.get(ConfessionRecord, confession_id)
        count = row.comment_count
        comments = session.scalar(
            select(func.count())
            .select_from(ConfessionCommentRecord)
            .where(ConfessionCommentRecord.confession_id == confession_id)
        )

    assert comments == 10, comments
    assert count == 10, count


def test_a_participant_cannot_exceed_the_three_comment_limit(wide_db):
    """The limit is a rule, not a suggestion: four comments sent at once leave three."""
    service = ConfessionService(wide_db)
    author = make_user(wide_db, "author")
    commenter = make_user(wide_db, "commenter")
    confession_id = make_confession(wide_db, author, comments_enabled=True)
    commenter_details = user_details(wide_db, "commenter")

    results = race_past_the_row_lock(
        wide_db,
        "confessions",
        confession_id,
        r"(from|update) confessions\y",
        [
            lambda index=index: service.add_comment_to_confession(
                confession_id,
                CommentCreate(body=f"a reply from {index}"),
                commenter_details,
            )
            for index in range(4)
        ],
    )

    with wide_db.sessions() as session:
        comments = session.scalar(
            select(func.count())
            .select_from(ConfessionCommentRecord)
            .where(
                ConfessionCommentRecord.confession_id == confession_id,
                ConfessionCommentRecord.user_id == commenter,
            )
        )

    assert comments == 3, comments
    refused = [outcome for outcome in results if outcome[1] == "COMMENT_LIMIT_REACHED"]
    assert len(refused) == 1, [outcome[1] for outcome in results]


def test_concurrent_likes_on_one_comment_are_all_stored(wide_db):
    """The same read-modify-write, on a comment's like list."""
    service = ConfessionService(wide_db)
    author = make_user(wide_db, "author")
    confession_id = make_confession(wide_db, author, comments_enabled=True)
    comment_id = make_comment(wide_db, confession_id, author)
    likers = [make_user(wide_db, f"liker-{index}") for index in range(10)]

    race_past_the_row_lock(
        wide_db,
        "confession_comments",
        comment_id,
        r"(from|update) confession_comments\y",
        [
            lambda user_id=user_id: service.like_comment(comment_id, user_id)
            for user_id in likers
        ],
    )

    with wide_db.sessions() as session:
        row = session.get(ConfessionCommentRecord, comment_id)
        likes = [str(value) for value in (row.likes or [])]
        like_count = row.like_count

    assert len(likes) == 10, likes
    assert like_count == 10, like_count
