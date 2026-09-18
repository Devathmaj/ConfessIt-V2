import datetime
import uuid

import pytest

from app.db_models import ConfessionRecord, UserRecord
from app.services.confession_service import ConfessionService

REACTION_KINDS = ["heart", "haha", "whoa", "heartbreak"]


def _make_user(db, name: str) -> str:
    user_id = str(uuid.uuid4())
    with db.sessions.begin() as session:
        session.add(UserRecord(
            id=user_id, regno=f"reg-{user_id}", name=name, email=f"{user_id}@test.com",
            year=2, department="Test", gender="other",
        ))
    return user_id


def _make_confession(db, author_id: str) -> str:
    confession_id = str(uuid.uuid4())
    with db.sessions.begin() as session:
        session.add(ConfessionRecord(
            id=confession_id, body="hello", author_id=author_id,
            created_at=datetime.datetime.now(datetime.timezone.utc),
            reactions={kind: [] for kind in REACTION_KINDS},
        ))
    return confession_id


def _stored(db, confession_id: str):
    """Read the row back in a fresh session, so only committed state is observed."""
    with db.sessions() as session:
        row = session.get(ConfessionRecord, confession_id)
        return row.reactions, tuple(
            getattr(row, f"{kind}_count") for kind in REACTION_KINDS
        )


@pytest.mark.parametrize("kind", REACTION_KINDS)
def test_reaction_persists_for_many_users(db, kind):
    service = ConfessionService(db)
    author = _make_user(db, "author")
    confession_id = _make_confession(db, author)

    for index in range(3):
        user_id = _make_user(db, f"reactor-{index}")
        service.react_to_confession(confession_id, kind, user_id)
        reactions, counts = _stored(db, confession_id)
        assert len(reactions[kind]) == index + 1, f"reaction {index + 1} was lost: {reactions}"
        assert counts[REACTION_KINDS.index(kind)] == index + 1


def test_counters_stay_consistent_with_map(db):
    service = ConfessionService(db)
    author = _make_user(db, "author")
    confession_id = _make_confession(db, author)

    for index in range(3):
        service.react_to_confession(confession_id, "heart", _make_user(db, f"h-{index}"))
    service.react_to_confession(confession_id, "haha", _make_user(db, "haha-1"))

    reactions, counts = _stored(db, confession_id)
    assert [len(reactions[kind]) for kind in REACTION_KINDS] == list(counts)


def test_toggling_a_reaction_off_decrements_the_count(db):
    service = ConfessionService(db)
    author = _make_user(db, "author")
    confession_id = _make_confession(db, author)
    user_id = _make_user(db, "toggler")

    service.react_to_confession(confession_id, "heart", user_id)
    service.react_to_confession(confession_id, "heart", user_id)

    reactions, counts = _stored(db, confession_id)
    assert reactions["heart"] == []
    assert counts == (0, 0, 0, 0)


def test_switching_reaction_moves_the_user_between_maps(db):
    service = ConfessionService(db)
    author = _make_user(db, "author")
    confession_id = _make_confession(db, author)
    user_id = _make_user(db, "switcher")

    service.react_to_confession(confession_id, "heart", user_id)
    service.react_to_confession(confession_id, "whoa", user_id)

    reactions, counts = _stored(db, confession_id)
    assert reactions["heart"] == []
    assert reactions["whoa"] == [user_id]
    assert counts == (0, 0, 1, 0)


def test_a_user_occupies_only_one_reaction_at_a_time(db):
    service = ConfessionService(db)
    author = _make_user(db, "author")
    confession_id = _make_confession(db, author)
    user_id = _make_user(db, "one-at-a-time")

    for kind in REACTION_KINDS:
        service.react_to_confession(confession_id, kind, user_id)

    reactions, counts = _stored(db, confession_id)
    reacted_kinds = [kind for kind in REACTION_KINDS if user_id in reactions[kind]]
    assert reacted_kinds == ["heartbreak"]
    assert counts == (0, 0, 0, 1)


def test_reaction_from_a_second_user_is_added_not_replaced(db):
    service = ConfessionService(db)
    author = _make_user(db, "author")
    confession_id = _make_confession(db, author)
    first = _make_user(db, "first")
    second = _make_user(db, "second")

    service.react_to_confession(confession_id, "heart", first)
    service.react_to_confession(confession_id, "heart", second)

    reactions, counts = _stored(db, confession_id)
    assert sorted(reactions["heart"]) == sorted([first, second])
    assert counts == (2, 0, 0, 0)
