# app/services/redis_service.py

import redis
import json
import ssl
import threading
from typing import Optional, Callable, Any
from redis.backoff import ExponentialBackoff
from redis.retry import Retry
from redis.connection import ConnectionPool
from ..config import settings
from ..logger import get_logger

logger = get_logger(__name__)


class RedisService:
    """Service for handling Redis Pub/Sub operations.

    Two pools, not one, because the two kinds of connection want opposite
    things. A *command* connection is short-lived (a publish, a rate-limit
    counter, a ping); a *subscriber* connection is held for the life of a chat
    socket, which can be minutes. Sharing one pool meant a busy chat drained
    it for everybody on that worker: the REST send still answered 201, but the
    publish could not get a connection, so the live echo never went out and
    every conversation silently became poll-only -- the sender's own message
    appeared to vanish, and the login rate limiter degraded with it.

    So publishers get a pool of their own and subscribers get a ceiling of
    their own: the number of concurrent live chats one worker serves. Past it
    a new socket is refused, loudly and only for that socket, while the open
    ones keep working.
    """

    def __init__(
        self,
        command_pool_max: Optional[int] = None,
        pubsub_pool_max: Optional[int] = None,
    ):
        self.redis_url = settings.REDIS_URL
        # The ceilings are the deployment's (see app.config). The parameters let
        # a test ask for a small pool instead of the production one.
        self.command_pool_max = int(
            command_pool_max or settings.REDIS_COMMAND_POOL_MAX_CONNECTIONS
        )
        self.pubsub_pool_max = int(
            pubsub_pool_max or settings.REDIS_PUBSUB_POOL_MAX_CONNECTIONS
        )
        self._client: Optional[redis.Redis] = None
        self._pool: Optional[ConnectionPool] = None
        self._pubsub_client: Optional[redis.Redis] = None
        self._pubsub_pool: Optional[ConnectionPool] = None
        self._lock = threading.Lock()
        # How many subscriptions this process currently holds. One per open
        # socket, so it is also the number of live chats the worker serves.
        self._live_subscriptions = 0

    def _build_pool(self, max_connections: int) -> ConnectionPool:
        """A pool with the deployment's connection options."""
        extra: dict[str, Any] = {}
        if self.redis_url.lower().startswith("rediss://"):
            # Require certificate verification; never downgrade to cleartext.
            # URL credentials and TLS settings are passed through to ``from_url``.
            extra["ssl_cert_reqs"] = ssl.CERT_REQUIRED
            extra["ssl_check_hostname"] = True
        return redis.connection.ConnectionPool.from_url(
            self.redis_url,
            decode_responses=True,
            socket_connect_timeout=5,
            # Not a problem for the subscribers: redis-py gives a blocking
            # pub/sub read a read timeout of its own (None), so a socket that
            # hears nothing for minutes is not cut off by this.
            socket_timeout=5,
            socket_keepalive=True,
            max_connections=max_connections,
            health_check_interval=30,
            **extra,
        )

    def _get_client(self) -> redis.Redis:
        """Get or create the command client (thread-safe, pooled).

        Both clients are created here, under one lock, so the two pools can
        never be built twice or drift apart.
        """
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            self._pool = self._build_pool(self.command_pool_max)
            self._client = redis.Redis(
                connection_pool=self._pool,
                retry=Retry(ExponentialBackoff(), 3),
                retry_on_error=[ConnectionError, TimeoutError],
            )
            # The subscribers get their own pool, so a burst of open chats can
            # never starve the publishers (or the rate limiter).
            self._pubsub_pool = self._build_pool(self.pubsub_pool_max)
            self._pubsub_client = redis.Redis(
                connection_pool=self._pubsub_pool,
                retry=Retry(ExponentialBackoff(), 3),
                retry_on_error=[ConnectionError, TimeoutError],
            )
            logger.info(
                "Redis client initialized (command pool max_connections=%s, "
                "subscriber pool max_connections=%s)",
                self.command_pool_max,
                self.pubsub_pool_max,
            )
        return self._client

    def _get_pubsub_client(self) -> redis.Redis:
        """The subscriber client: one connection per open chat socket."""
        self._get_client()
        assert self._pubsub_client is not None
        return self._pubsub_client

    @property
    def client(self) -> redis.Redis:
        """Property to access Redis client"""
        return self._get_client()

    def publish_message(self, conversation_id: str, message_data: dict) -> bool:
        """
        Publish a message to a conversation channel.

        Returns True if published, False on outage -- an explicit signal,
        logged at error level so a chat outage pages rather than hiding behind
        a boolean the caller ignores.
        """
        try:
            channel = f"conversation:{conversation_id}"
            message_json = json.dumps(message_data, default=str)

            # Publish returns number of subscribers
            subscribers = self.client.publish(channel, message_json)
            if subscribers == 0:
                # Not a failure -- persistence already succeeded -- but worth one
                # line: zero subscribers means realtime is dark for this channel
                # (no listener attached) and the message is poll-only.
                logger.warning(f"Published message to {channel} with 0 subscribers")
            else:
                logger.info(f"Published message to {channel}, {subscribers} subscribers")
            return True
        except Exception as e:
            # Explicit outage signal: callers must check the return value (see
            # message_service) and the message row is already committed, so this
            # is a realtime degradation, not a data loss.
            logger.error(f"Redis publish outage on conversation:{conversation_id}: {e}")
            return False

    def subscribe_to_conversation(self, conversation_id: str) -> Optional[redis.client.PubSub]:
        """
        Subscribe to a conversation channel.

        The subscription holds its connection for as long as the socket lives,
        so it comes from the subscriber pool -- never from the publishers'.
        A full subscriber pool means this worker already serves as many live
        chats as it can: the socket is refused, and the refusal says so rather
        than quietly turning the whole worker poll-only.

        Returns:
            PubSub object or None
        """
        channel = f"conversation:{conversation_id}"
        try:
            pubsub = self._get_pubsub_client().pubsub()
            pubsub.subscribe(channel)
        except Exception as e:
            if self.subscriber_capacity_reached():
                logger.error(
                    "Subscriber pool exhausted (%s live subscriptions, ceiling %s); "
                    "refusing a new live subscription on %s: %s",
                    self._live_subscriptions,
                    self.pubsub_pool_max,
                    channel,
                    e,
                )
            else:
                logger.error(f"Error subscribing to conversation: {e}")
            return None
        with self._lock:
            self._live_subscriptions += 1
        logger.info("Subscribed to %s", channel)
        return pubsub

    def subscriber_capacity_reached(self) -> bool:
        """Whether this worker already serves as many live chats as its ceiling.

        Callers read this to tell a capacity refusal from a Redis outage;
        past the ceiling a new socket is refused while open ones carry on.
        """
        with self._lock:
            return self._live_subscriptions >= self.pubsub_pool_max

    def unsubscribe(self, pubsub: redis.client.PubSub, conversation_id: str):
        """Unsubscribe from a conversation channel and release it.

        Always closes the PubSub object, even when unsubscribe itself fails,
        so a websocket disconnect cannot leak a subscribed connection.
        """
        channel = f"conversation:{conversation_id}"
        try:
            try:
                pubsub.unsubscribe(channel)
            finally:
                # Close is the leak guard: an unsubscribe error must not skip it.
                try:
                    pubsub.close()
                except Exception as close_exc:
                    logger.error(f"Error closing pubsub for {channel}: {close_exc}")
            logger.info(f"Unsubscribed from {channel}")
        except Exception as e:
            logger.error(f"Error unsubscribing from {channel}: {e}")
            try:
                pubsub.close()
            except Exception:
                pass
        finally:
            # The connection is released either way, so the live-chat count has
            # to come down either way.
            with self._lock:
                if self._live_subscriptions > 0:
                    self._live_subscriptions -= 1

    def ping(self) -> bool:
        """Check if Redis is available"""
        try:
            return bool(self.client.ping())
        except Exception as e:
            logger.error(f"Redis ping failed: {e}")
            return False

    def close(self):
        """Close pooled Redis connections. Called from app lifespan shutdown."""
        with self._lock:
            for client in (self._client, self._pubsub_client):
                if client is not None:
                    try:
                        client.close()
                    except Exception as e:
                        logger.error(f"Error closing Redis client: {e}")
            for pool in (self._pool, self._pubsub_pool):
                if pool is not None:
                    try:
                        pool.disconnect()
                    except Exception as e:
                        logger.error(f"Error disconnecting Redis pool: {e}")
            self._client = None
            self._pubsub_client = None
            self._pool = None
            self._pubsub_pool = None
            self._live_subscriptions = 0
            logger.info("Redis client closed")


# Global instance
redis_service = RedisService()
