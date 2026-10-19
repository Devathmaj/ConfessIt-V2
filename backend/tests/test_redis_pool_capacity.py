"""The Redis pools: a chat socket must not be able to starve the publishers.

F-10. The service used to run every connection -- the publishes, the login rate
limiter, and one subscription per open chat socket -- through a single
process-wide pool of fifty. A subscriber holds its connection for the life of
its socket, so fifty open chats drained the pool for everybody on that worker:
the REST send still stored the message and still answered 201, but the publish
could not get a connection, so the live echo never went out. Every
conversation on the worker went quietly poll-only, the sender's own message
appeared to vanish, and the login rate limiter degraded with it.

These tests pin the two halves of the fix: the subscribers have their own pool,
so a full one cannot stop a publish (and the message still arrives live), and
the send says out loud whether the live delivery happened.
"""

import json
import time

import pytest

from app.services.redis_service import RedisService


@pytest.fixture
def service():
    """A service with pools small enough to saturate in a test.

    The ceilings are the deployment's in production (see app.config); here they
    are small so the test can fill one on purpose. The separation is what is
    under test, not the sizes.
    """
    return RedisService(command_pool_max=1, pubsub_pool_max=2)


def _drain(pubsub, timeout=3.0):
    """The first message delivered to a subscription, or None."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = pubsub.get_message(timeout=0.2)
        if message and message.get("type") == "message":
            return message
    return None


def test_the_subscriber_pool_is_not_the_publisher_pool(service):
    # Two pools, not one: the whole point is that a socket's connection cannot
    # be the one a publish needs. The pools are built on first use, so the
    # client is created before they can be compared.
    service.client
    assert service.command_pool_max == 1
    assert service.pubsub_pool_max == 2
    assert service._pool is not None
    assert service._pubsub_pool is not None
    assert service._pool is not service._pubsub_pool
    assert service._client is not service._pubsub_client
    service.close()


def test_a_full_subscriber_pool_still_delivers_live(service):
    """The regression itself: open chats everywhere, and the send still lands.

    Two subscriptions are opened -- more than the command pool has connections
    -- and then a message is published. With one shared pool the second
    subscription would already have failed and the publish would have had
    nothing to send on; with two pools the message goes out and the open
    subscription receives it.
    """
    if not service.ping():
        pytest.skip("Redis is not reachable")

    subscriptions = []
    try:
        for index in range(service.pubsub_pool_max):
            pubsub = service.subscribe_to_conversation(f"capacity-probe-{index}")
            assert pubsub is not None, "a subscription must be possible below the ceiling"
            subscriptions.append(pubsub)

        # The subscriber pool is now full. A publish must still work: it draws
        # its connection from the other pool.
        assert service.publish_message("capacity-probe-1", {"id": "live-1"}) is True

        delivered = _drain(subscriptions[1])
        assert delivered is not None, "the live delivery never arrived"
        assert json.loads(delivered["data"])["id"] == "live-1"
    finally:
        for index, pubsub in enumerate(subscriptions):
            service.unsubscribe(pubsub, f"capacity-probe-{index}")
        service.close()


def test_a_refused_subscription_is_reported_as_capacity(service):
    """Past the ceiling the socket is refused, and the refusal is named.

    The point is not that the socket is refused -- a worker has to say no
    somewhere -- but that the refusal is a capacity statement rather than a
    quiet degradation of every conversation on the worker.
    """
    if not service.ping():
        pytest.skip("Redis is not reachable")

    subscriptions = []
    try:
        for index in range(service.pubsub_pool_max):
            pubsub = service.subscribe_to_conversation(f"capacity-full-{index}")
            assert pubsub is not None
            subscriptions.append(pubsub)

        assert service.subscriber_capacity_reached() is True

        # One more than the ceiling: refused, and the service knows why.
        refused = service.subscribe_to_conversation("capacity-full-overflow")
        assert refused is None
        assert service.subscriber_capacity_reached() is True

        # The publishers are untouched by the refusal.
        assert service.publish_message("capacity-full-0", {"id": "live-2"}) is True
    finally:
        for index, pubsub in enumerate(subscriptions):
            service.unsubscribe(pubsub, f"capacity-full-{index}")
        service.close()


def test_the_send_response_says_whether_the_live_delivery_happened(db, client, monkeypatch):
    """The degradation is stated in the response, not left to be inferred.

    The message is stored either way, so the response is the only place a caller
    can learn that the live echo did not go out -- which is what turns "my
    message vanished" into "live updates are down".
    """
    from app.db_models import ConversationRecord, MatchRecord, UserRecord
    from app.models import AccountType, UserDetails
    from app.services.auth_service import get_current_user
    from app.main import app
    from sqlalchemy import select
    from datetime import datetime, timedelta, timezone

    with db.sessions.begin() as session:
        for regno in ("pool_sender", "pool_receiver"):
            session.add(UserRecord(
                regno=regno, name=regno, email=f"{regno}@test.com",
                year=2, department="Test_Class", gender="other",
            ))
    # The match's foreign keys name the accounts, so they have to be committed
    # before the match is written.
    with db.sessions.begin() as session:
        now = datetime.now(timezone.utc)
        match = MatchRecord(
            user_1_regno="pool_sender", user_2_regno="pool_receiver",
            created_at=now, expires_at=now + timedelta(hours=4),
        )
        session.add(match)
        session.flush()
        conversation = ConversationRecord(
            match_id=str(match.id), initiator_regno="pool_sender",
            receiver_regno="pool_receiver", status="accepted", created_at=now,
        )
        session.add(conversation)
        session.flush()
        conversation_id = str(conversation.id)

    with db.sessions() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.regno == "pool_sender"))
    sender = UserDetails(
        id=str(row.id), regno="pool_sender", name="pool_sender", email="pool_sender@test.com",
        year=2, department="Test_Class", gender="other",
        matchmaking_enabled=True, notifications_enabled=True,
        is_blocked=False, account_type=AccountType.USERS.value,
    )

    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: sender
    try:
        from app.services import redis_service as redis_module

        monkeypatch.setattr(redis_module.redis_service, "publish_message", lambda *a, **k: False)
        degraded = client.post("/messages/send", json={"conversation_id": conversation_id, "body": "hi"})
        assert degraded.status_code == 201, degraded.text
        assert degraded.json()["realtime"] == "unavailable"

        monkeypatch.setattr(redis_module.redis_service, "publish_message", lambda *a, **k: True)
        delivered = client.post("/messages/send", json={"conversation_id": conversation_id, "body": "hi again"})
        assert delivered.status_code == 201, delivered.text
        assert delivered.json()["realtime"] == "delivered"
    finally:
        if previous is not None:
            app.dependency_overrides[get_current_user] = previous
        else:
            app.dependency_overrides.pop(get_current_user, None)
