# app/services/message_archival_service.py

import logging
import gzip
import json
import os
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from sqlalchemy import delete, select
from ..postgres import Database
from ..postgres import new_id, is_valid_id
from ..db_models import MessageRecord
from ..utils import isoformat_datetime

logger = logging.getLogger(__name__)


class MessageArchivalService:
    """Service for archiving old messages to compressed JSON files"""
    
    def __init__(self, archive_dir: str = "/app/archives"):
        self.archive_dir = archive_dir
        self._ensure_archive_dir()
    
    def _ensure_archive_dir(self):
        """Create archive directory if it doesn't exist"""
        os.makedirs(self.archive_dir, exist_ok=True)
        logger.info(f"Archive directory: {self.archive_dir}")
    
    def archive_old_messages(self, db: Database, hours_old: int = 4) -> Dict[str, Any]:
        """
        Archive messages older than specified hours to compressed JSON
        
        Args:
            db: PostgreSQL database
            hours_old: Archive messages older than this many hours (default 4)
        
        Returns:
            Dict with archive statistics
        """
        try:
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours_old)
            
            # Find old messages
            with db.sessions() as session:
                rows = session.scalars(select(MessageRecord).where(MessageRecord.created_at < cutoff_time)).all()
                old_messages = [{"id": row.id, "conversation_id": row.conversation_id,
                    "sender_regno": row.sender_regno, "receiver_regno": row.receiver_regno,
                    "body": row.body, "created_at": row.created_at, "is_read": row.is_read} for row in rows]
            
            if not old_messages:
                logger.info("No messages to archive")
                return {
                    "archived_count": 0,
                    "message": "No messages to archive"
                }
            
            # Group messages by date
            messages_by_date = {}
            for msg in old_messages:
                date_key = msg["created_at"].strftime("%Y-%m-%d")
                if date_key not in messages_by_date:
                    messages_by_date[date_key] = []
                messages_by_date[date_key].append(msg)
            
            archived_count = 0
            archive_files = []
            
            # Archive each day's messages
            for date_key, messages in messages_by_date.items():
                archive_file = self._archive_messages_to_file(date_key, messages)
                if archive_file:
                    archive_files.append(archive_file)
                    archived_count += len(messages)
            
            # Delete archived messages from PostgreSQL
            if archived_count > 0:
                with db.sessions.begin() as session:
                    result = session.execute(delete(MessageRecord).where(MessageRecord.created_at < cutoff_time))
                    deleted_count = result.rowcount or 0
                
                logger.info(
                    f"Archived {archived_count} messages to {len(archive_files)} files, "
                    f"deleted {deleted_count} from PostgreSQL"
                )
                
                return {
                    "archived_count": archived_count,
                    "deleted_count": deleted_count,
                    "archive_files": archive_files,
                    "cutoff_time": cutoff_time.isoformat()
                }
            
            return {
                "archived_count": 0,
                "message": "Failed to archive messages"
            }
            
        except Exception as e:
            logger.error(f"Error archiving messages: {e}")
            return {
                "error": str(e),
                "archived_count": 0
            }
    
    def _archive_messages_to_file(
        self,
        date_key: str,
        messages: List[Dict[str, Any]]
    ) -> str:
        """
        Archive messages to a compressed JSON file
        
        Args:
            date_key: Date string (YYYY-MM-DD)
            messages: List of message documents
        
        Returns:
            Archive filename or empty string if failed
        """
        try:
            filename = f"messages_{date_key}.json.gz"
            filepath = os.path.join(self.archive_dir, filename)
            
            # Convert str to string for JSON serialization
            serializable_messages = []
            for msg in messages:
                msg_copy = msg.copy()
                msg_copy["id"] = str(msg_copy["id"])
                msg_copy["conversation_id"] = str(msg_copy["conversation_id"])
                msg_copy["created_at"] = isoformat_datetime(msg_copy["created_at"])
                serializable_messages.append(msg_copy)
            
            # Write to compressed file
            with gzip.open(filepath, "wt", encoding="utf-8") as f:
                json.dump(serializable_messages, f, indent=2)
            
            file_size = os.path.getsize(filepath)
            logger.info(
                f"Archived {len(messages)} messages to {filename} "
                f"({file_size / 1024:.2f} KB)"
            )
            
            return filename
            
        except Exception as e:
            logger.error(f"Error writing archive file: {e}")
            return ""
    
    def get_archived_messages(
        self,
        date: str,
        user_regno: Optional[str] = None,
        conversation_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Retrieve archived messages from compressed file
        
        Args:
            date: Date string (YYYY-MM-DD)
            user_regno: Filter by user (sender or receiver)
            conversation_id: Filter by conversation
        
        Returns:
            List of messages
        """
        try:
            filename = f"messages_{date}.json.gz"
            filepath = os.path.join(self.archive_dir, filename)
            
            if not os.path.exists(filepath):
                logger.warning(f"Archive file not found: {filename}")
                return []
            
            # Read compressed file
            with gzip.open(filepath, "rt", encoding="utf-8") as f:
                messages = json.load(f)
            
            # Apply filters
            if user_regno:
                messages = [
                    msg for msg in messages
                    if msg.get("sender_regno") == user_regno or 
                       msg.get("receiver_regno") == user_regno
                ]
            
            if conversation_id:
                messages = [
                    msg for msg in messages
                    if msg.get("conversation_id") == conversation_id
                ]
            
            return messages
            
        except Exception as e:
            logger.error(f"Error reading archive file: {e}")
            return []
    
    def list_archives(self) -> List[Dict[str, Any]]:
        """
        List all archive files
        
        Returns:
            List of archive file info
        """
        try:
            archives = []
            for filename in os.listdir(self.archive_dir):
                if filename.endswith(".json.gz"):
                    filepath = os.path.join(self.archive_dir, filename)
                    file_size = os.path.getsize(filepath)
                    file_mtime = os.path.getmtime(filepath)
                    
                    # Convert explicitly to UTC: fromtimestamp() defaults to naive local
                    # time, which would disagree with the UTC-based "date" below and with
                    # every other timestamp this service produces, and would also shift
                    # across DST on servers whose local zone changes.
                    archives.append({
                        "filename": filename,
                        "size_kb": round(file_size / 1024, 2),
                        "modified_at": datetime.fromtimestamp(file_mtime, tz=timezone.utc).isoformat(),
                        "date": filename.replace("messages_", "").replace(".json.gz", "")
                    })
            
            archives.sort(key=lambda x: x["date"], reverse=True)
            return archives
            
        except Exception as e:
            logger.error(f"Error listing archives: {e}")
            return []


# Global instance
message_archival_service = MessageArchivalService()
