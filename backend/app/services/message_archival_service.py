# app/services/message_archival_service.py

import logging
import gzip
import json
import os
from datetime import datetime, timedelta
from typing import List, Dict, Any
from pymongo.database import Database
from bson import ObjectId

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
            db: MongoDB database
            hours_old: Archive messages older than this many hours (default 4)
        
        Returns:
            Dict with archive statistics
        """
        try:
            cutoff_time = datetime.utcnow() - timedelta(hours=hours_old)
            
            # Find old messages
            old_messages = list(
                db["messages"].find({
                    "timestamp": {"$lt": cutoff_time}
                })
            )
            
            if not old_messages:
                logger.info("No messages to archive")
                return {
                    "archived_count": 0,
                    "message": "No messages to archive"
                }
            
            # Group messages by date
            messages_by_date = {}
            for msg in old_messages:
                date_key = msg["timestamp"].strftime("%Y-%m-%d")
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
            
            # Delete archived messages from MongoDB
            if archived_count > 0:
                result = db["messages"].delete_many({
                    "timestamp": {"$lt": cutoff_time}
                })
                deleted_count = result.deleted_count
                
                logger.info(
                    f"Archived {archived_count} messages to {len(archive_files)} files, "
                    f"deleted {deleted_count} from MongoDB"
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
            
            # Convert ObjectId to string for JSON serialization
            serializable_messages = []
            for msg in messages:
                msg_copy = msg.copy()
                msg_copy["_id"] = str(msg_copy["_id"])
                msg_copy["conversation_id"] = str(msg_copy["conversation_id"])
                msg_copy["timestamp"] = msg_copy["timestamp"].isoformat()
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
                    if msg.get("sender_id") == user_regno or 
                       msg.get("receiver_id") == user_regno
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
                    
                    archives.append({
                        "filename": filename,
                        "size_kb": round(file_size / 1024, 2),
                        "modified_at": datetime.fromtimestamp(file_mtime).isoformat(),
                        "date": filename.replace("messages_", "").replace(".json.gz", "")
                    })
            
            archives.sort(key=lambda x: x["date"], reverse=True)
            return archives
            
        except Exception as e:
            logger.error(f"Error listing archives: {e}")
            return []


# Global instance
message_archival_service = MessageArchivalService()
