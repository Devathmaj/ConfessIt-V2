# app/services/redis_service.py

import logging
import redis
import json
from typing import Optional, Callable, Any
from ..config import settings

logger = logging.getLogger(__name__)


class RedisService:
    """Service for handling Redis Pub/Sub operations"""
    
    def __init__(self):
        self.redis_url = getattr(settings, 'REDIS_URL', 'redis://localhost:6379')
        self._client: Optional[redis.Redis] = None
        self._pubsub: Optional[redis.client.PubSub] = None
    
    def _get_client(self) -> redis.Redis:
        """Get or create Redis client"""
        if self._client is None:
            self._client = redis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
                retry_on_timeout=True,
                health_check_interval=30
            )
            logger.info("Redis client initialized")
        return self._client
    
    @property
    def client(self) -> redis.Redis:
        """Property to access Redis client"""
        return self._get_client()
    
    def publish_message(self, conversation_id: str, message_data: dict) -> bool:
        """
        Publish a message to a conversation channel
        
        Args:
            conversation_id: MongoDB conversation ObjectId as string
            message_data: Message data dictionary
        
        Returns:
            True if published successfully
        """
        try:
            channel = f"conversation:{conversation_id}"
            message_json = json.dumps(message_data, default=str)
            
            # Publish returns number of subscribers
            subscribers = self.client.publish(channel, message_json)
            logger.info(f"Published message to {channel}, {subscribers} subscribers")
            return True
        except Exception as e:
            logger.error(f"Error publishing message to Redis: {e}")
            return False
    
    def subscribe_to_conversation(self, conversation_id: str) -> Optional[redis.client.PubSub]:
        """
        Subscribe to a conversation channel
        
        Args:
            conversation_id: MongoDB conversation ObjectId as string
        
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
        """Unsubscribe from a conversation channel"""
        try:
            channel = f"conversation:{conversation_id}"
            pubsub.unsubscribe(channel)
            pubsub.close()
            logger.info(f"Unsubscribed from {channel}")
        except Exception as e:
            logger.error(f"Error unsubscribing: {e}")
    
    def ping(self) -> bool:
        """Check if Redis is available"""
        try:
            return self.client.ping()
        except Exception as e:
            logger.error(f"Redis ping failed: {e}")
            return False
    
    def close(self):
        """Close Redis connections"""
        if self._pubsub:
            self._pubsub.close()
        if self._client:
            self._client.close()
            logger.info("Redis client closed")


# Global instance
redis_service = RedisService()
