# app/services/supabase_service.py

import logging
import jwt
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List
from supabase import create_client, Client
from ..config import settings

logger = logging.getLogger(__name__)


class SupabaseService:
    """Service for handling Supabase operations"""
    
    def __init__(self):
        self.supabase_url = settings.SUPABASE_URL
        self.anon_key = settings.SUPABASE_ANON_KEY  # Public anon key
        self.service_role_key = settings.SUPABASE_SERVICE_ROLE_KEY  # For API access
        self.jwt_secret = settings.SUPABASE_JWT_SECRET  # For signing tokens
        self._client: Optional[Client] = None
    
    def _is_configured(self) -> bool:
        """Return True when service credentials are available."""
        return bool(self.supabase_url and self.service_role_key)

    def _get_client(self) -> Optional[Client]:
        """Return a Supabase client when configuration is present."""
        if not self._is_configured():
            logger.warning("Supabase service is not configured; skipping client creation.")
            return None

        if self._client is None:
            self._client = create_client(self.supabase_url, self.service_role_key)
        return self._client

    @property
    def client(self) -> Client:
        """Get or create Supabase client, raising if unavailable."""
        client = self._get_client()
        if client is None:
            raise ValueError("Supabase URL and Service Role Key must be configured")
        return client
    
    def generate_ephemeral_token(
        self, 
        user_id: str, 
        conversation_id: str,
        expires_in_hours: int = 4
    ) -> str:
        """
        Generate an ephemeral JWT token for frontend to access Supabase
        
        Args:
            user_id: The user's Regno (registration number)
            conversation_id: The Supabase conversation UUID
            expires_in_hours: Token expiration time in hours (default 4 to match match expiry)
        
        Returns:
            JWT token string
        """
        if not (self.jwt_secret and self.supabase_url):
            raise ValueError("Supabase JWT secret or URL not configured")
        
        now = datetime.now(timezone.utc)
        exp = now + timedelta(hours=expires_in_hours)
        
        # JWT payload with Supabase-required claims
        # The JWT must include 'iss' (issuer) matching Supabase project URL
        project_ref = self.supabase_url.split("://")[1].split(".")[0]  # Extract project ref from URL
        payload = {
            "iss": "supabase",  # Issuer for custom JWT
            "sub": user_id,  # Subject (user's Regno for authentication)
            "aud": project_ref,  # Audience (project reference)
            "role": "anon",  # Role for anonymous access with JWT
            "conversation_id": conversation_id,  # Custom claim for RLS
            "user_regno": user_id,  # Custom claim for RLS (redundant with sub, but explicit)
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp())
        }
        
        token = jwt.encode(payload, self.jwt_secret, algorithm="HS256")
        logger.info(f"Generated ephemeral token for user {user_id}, conversation {conversation_id}")
        
        return token
    
    def sync_conversation_to_supabase(
        self, 
        mongo_conversation_id: str,
        match_id: str,
        initiator_id: str,
        receiver_id: str,
        status: str,
        created_at: datetime,
        accepted_at: Optional[datetime] = None
    ) -> Optional[str]:
        """
        Sync a conversation record from MongoDB to Supabase
        
        Args:
            mongo_conversation_id: MongoDB ObjectId as string
            match_id: MongoDB match ObjectId as string (stored as text)
            initiator_id: Initiator's Regno (stored as text)
            receiver_id: Receiver's Regno (stored as text)
            status: Conversation status (pending, accepted, rejected)
            created_at: Creation timestamp
            accepted_at: Acceptance timestamp (optional)
        
        Returns:
            Supabase conversation UUID or None if failed
        """
        client = self._get_client()
        if client is None:
            logger.warning("Supabase sync skipped because the service is not configured")
            return None

        try:
            # No conversion needed - store as-is since Supabase uses text fields
            result = client.table("conversations").select("id").eq("match_id", match_id).execute()
            
            conversation_data = {
                "match_id": match_id,  # MongoDB ObjectId as text
                "initiator_id": initiator_id,  # Regno as text
                "receiver_id": receiver_id,  # Regno as text
                "status": status,
                "created_at": created_at.isoformat(),
                "accepted_at": accepted_at.isoformat() if accepted_at else None
            }
            
            if result.data and len(result.data) > 0:
                # Update existing conversation
                supabase_id = result.data[0]["id"]
                client.table("conversations").update(conversation_data).eq("id", supabase_id).execute()
                logger.info(f"Updated existing conversation in Supabase: {supabase_id}")
                return supabase_id
            else:
                # Insert new conversation
                insert_result = client.table("conversations").insert(conversation_data).execute()
                if insert_result.data and len(insert_result.data) > 0:
                    supabase_id = insert_result.data[0]["id"]
                    logger.info(f"Created new conversation in Supabase: {supabase_id}")
                    return supabase_id
                else:
                    logger.error("Failed to insert conversation to Supabase")
                    return None
                    
        except Exception as e:
            logger.error(f"Error syncing conversation to Supabase: {e}")
            return None
    
    def update_conversation_status_in_supabase(
        self,
        match_id: str,
        status: str,
        accepted_at: Optional[datetime] = None
    ) -> bool:
        """
        Update conversation status in Supabase
        
        Args:
            match_id: MongoDB match ObjectId as string (stored as text)
            status: New status (accepted, rejected, expired)
            accepted_at: Acceptance timestamp (optional)
        
        Returns:
            True if successful, False otherwise
        """
        client = self._get_client()
        if client is None:
            logger.warning("Supabase status update skipped because the service is not configured")
            return False

        try:
            update_data = {"status": status}
            if accepted_at:
                update_data["accepted_at"] = accepted_at.isoformat()
            
            result = client.table("conversations").update(update_data).eq("match_id", match_id).execute()
            
            if result.data:
                logger.info(f"Updated conversation status in Supabase for match {match_id}: {status}")
                return True
            else:
                logger.warning(f"No conversation found in Supabase for match {match_id}")
                return False
                
        except Exception as e:
            logger.error(f"Error updating conversation status in Supabase: {e}")
            return False
    
    def get_conversation_by_match_id(self, match_id: str) -> Optional[Dict[str, Any]]:
        """
        Get conversation from Supabase by match_id
        
        Args:
            match_id: MongoDB match ObjectId as string (stored as text)
        
        Returns:
            Conversation data dict or None if not found
        """
        client = self._get_client()
        if client is None:
            logger.warning("Supabase conversation lookup skipped because the service is not configured")
            return None

        try:
            result = client.table("conversations").select("*").eq("match_id", match_id).execute()
            
            if result.data and len(result.data) > 0:
                return result.data[0]
            else:
                return None
                
        except Exception as e:
            logger.error(f"Error fetching conversation from Supabase: {e}")
            return None
    
    def block_conversation(
        self,
        match_id: str,
        blocked_by: str
    ) -> bool:
        """
        Block a conversation in Supabase
        
        Args:
            match_id: MongoDB match ObjectId as string
            blocked_by: Regno of user who is blocking
        
        Returns:
            True if successful, False otherwise
        """
        client = self._get_client()
        if client is None:
            logger.warning("Supabase block conversation skipped because the service is not configured")
            return False

        try:
            update_data = {
                "is_blocked": True,
                "blocked_by": blocked_by,
                "blocked_at": datetime.now(timezone.utc).isoformat()
            }
            
            result = client.table("conversations").update(update_data).eq("match_id", match_id).execute()
            
            if result.data:
                logger.info(f"Blocked conversation for match {match_id} by {blocked_by}")
                return True
            else:
                logger.warning(f"No conversation found in Supabase for match {match_id}")
                return False
                
        except Exception as e:
            logger.error(f"Error blocking conversation in Supabase: {e}")
            return False
    
    def unblock_conversation(
        self,
        match_id: str,
        user_id: str
    ) -> bool:
        """
        Unblock a conversation in Supabase
        Only the user who blocked can unblock
        
        Args:
            match_id: MongoDB match ObjectId as string
            user_id: Regno of user attempting to unblock
        
        Returns:
            True if successful, False otherwise
        """
        client = self._get_client()
        if client is None:
            logger.warning("Supabase unblock conversation skipped because the service is not configured")
            return False

        try:
            # First check if user is the one who blocked
            conv = self.get_conversation_by_match_id(match_id)
            if not conv:
                logger.warning(f"No conversation found for match {match_id}")
                return False
            
            if conv.get("blocked_by") != user_id:
                logger.warning(f"User {user_id} cannot unblock - not the blocker")
                return False
            
            update_data = {
                "is_blocked": False,
                "blocked_by": None,
                "blocked_at": None
            }
            
            result = client.table("conversations").update(update_data).eq("match_id", match_id).execute()
            
            if result.data:
                logger.info(f"Unblocked conversation for match {match_id} by {user_id}")
                return True
            else:
                logger.warning(f"Failed to unblock conversation for match {match_id}")
                return False
                
        except Exception as e:
            logger.error(f"Error unblocking conversation in Supabase: {e}")
            return False

    def get_messages_for_conversation(
        self,
        conversation_id: str,
        limit: int = 200
    ) -> List[Dict[str, Any]]:
        """Fetch messages for a conversation using the service role credentials."""
        client = self._get_client()
        if client is None:
            logger.warning("Supabase message fetch skipped because the service is not configured")
            return []

        try:
            result = (
                client
                .table("messages")
                .select("*")
                .eq("conversation_id", conversation_id)
                .order("timestamp", desc=False)
                .limit(limit)
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching messages from Supabase for conversation {conversation_id}: {e}")
            return []

    def get_latest_message_for_conversation(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """Return the most recent message for a conversation if one exists."""
        client = self._get_client()
        if client is None:
            logger.warning("Supabase latest message fetch skipped because the service is not configured")
            return None

        try:
            result = (
                client
                .table("messages")
                .select("*")
                .eq("conversation_id", conversation_id)
                .order("timestamp", desc=True)
                .limit(1)
                .execute()
            )
            if result.data:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error fetching latest message for conversation {conversation_id}: {e}")
            return None

    def terminate_conversation(
        self,
        match_id: str,
        reason: Optional[str] = None
    ) -> bool:
        """Mark a conversation as terminated and blocked in Supabase."""
        client = self._get_client()
        if client is None:
            logger.warning("Supabase terminate conversation skipped because the service is not configured")
            return False

        try:
            update_data: Dict[str, Any] = {
                "status": "terminated",
                "is_blocked": True,
                "blocked_by": "admin",
                "blocked_at": datetime.now(timezone.utc).isoformat(),
            }
            if reason:
                update_data["termination_reason"] = reason

            result = client.table("conversations").update(update_data).eq("match_id", match_id).execute()

            if result.data:
                logger.info(f"Terminated conversation for match {match_id}")
                return True

            logger.warning(f"No Supabase conversation found to terminate for match {match_id}")
            return False
        except Exception as e:
            logger.error(f"Error terminating conversation {match_id} in Supabase: {e}")
            return False


# Global instance
supabase_service = SupabaseService()
