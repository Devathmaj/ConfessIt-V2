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
    """Service for handling Redis Pub/Sub operations"""

    def __init__(self):
        self.redis_url = settings.REDIS_URL
        self._client: Optional[redis.Redis] = None
        self._pool: Optional[ConnectionPool] = None
        self._lock = threading.Lock()

    def _get_client(self) -> redis.Redis:
        """Get or create Redis client (thread-safe, pooled)."""
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            extra: dict[str, Any] = {}
            if self.redis_url.lower().startswith("rediss://"):
                # TLS is the point of the scheme, so it is enforced rather than
                # negotiated: the server must present a valid certificate for its
                # hostname, and a handshake that cannot prove that fails instead of
                # falling back to cleartext. Anything that must cross a network --
                # chat traffic included -- belongs on a rediss:// URL.
                # Password (from the URL userinfo) and TLS are both honoured by
                # from_url; nothing here downgrades or strips them.
                extra["ssl_cert_reqs"] = ssl.CERT_REQUIRED
                extra["ssl_check_hostname"] = True
            # Shared pool: one process-wide pool, bounded so a burst of
            # websocket subscribers cannot open unbounded sockets.
            self._pool = redis.connection.ConnectionPool.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
                socket_keepalive=True,
                max_connections=50,
                health_check_interval=30,
                **extra,
            )
            self._client = redis.Redis(
                connection_pool=self._pool,
                retry=Retry(ExponentialBackoff(), 3),
                retry_on_error=[ConnectionError, TimeoutError],
            )
            logger.info("Redis client initialized (pooled, max_connections=50)")
        return self._client
    
    @property
    def client(self) -> redis.Redis:
        """Property to access Redis client"""
        return self._get_client()
    
    def publish_message(self, conversation_id: str, message_data: dict) -> bool:
        """
        Publish a message to a conversation channel.

        Args:
            conversation_id: PostgreSQL conversation str as string
            message_data: Message data dictionary

        Returns:
            True if published successfully, False on outage (explicit, never
            silent: failures are logged at error level so a chat outage pages
            rather than hiding behind a boolean the caller ignores).
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
        Subscribe to a conversation channel
        
        Args:
            conversation_id: PostgreSQL conversation str as string
        
        Returns:
            PubSub object or None
        """
        try:
            pubsub = self.client.pubsub()
            channel = f"conversation:{conversation_id}"
            pubsub.subscribe(channel)
            logger.info(f"Subscribed to {channel}")
            return pubsub
        except Exception as e:
            logger.error(f"Error subscribing to conversation: {e}")
            return None
    
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
            if self._client is not None:
                try:
                    self._client.close()
                except Exception as e:
                    logger.error(f"Error closing Redis client: {e}")
                finally:
                    self._client = None
            if self._pool is not None:
                try:
                    self._pool.disconnect()
                except Exception as e:
                    logger.error(f"Error disconnecting Redis pool: {e}")
                finally:
                    self._pool = None
            logger.info("Redis client closed")


# Global instance
redis_service = RedisService()
