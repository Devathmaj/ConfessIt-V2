# app/services/message_archival_service.py

import gzip
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Tuple
from fastapi import HTTPException, status
from sqlalchemy import delete, select
from ..postgres import Database
from ..postgres import new_id, is_valid_id
from ..db_models import MessageRecord
from ..utils import isoformat_datetime
from ..logger import get_logger

logger = get_logger(__name__)

# The only shape a `date` argument may ever have. The writer mints keys with
# strftime("%Y-%m-%d"), so anything else is either a mistake or a traversal
# attempt -- `../` sails straight through os.path.join, and the `.json.gz`
# suffix never stopped it because the payload sits in the middle of the name.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Ceiling on how many decompressed bytes one archive read may materialise. A day of
# chat text is orders of magnitude below this; anything larger is corruption or a
# decompression bomb, and refusing it bounds one request's memory no matter what is
# on disk. The page applied afterwards bounds the response on top of that.
_MAX_ARCHIVE_READ_BYTES = 32 * 1024 * 1024
_ARCHIVE_READ_CHUNK_BYTES = 64 * 1024

# One page may never materialise a whole day at once, matching the reports queues.
ARCHIVED_MESSAGES_MAX_LIMIT = 200


class MessageArchivalService:
    """Service for archiving old messages to compressed JSON files"""
    
    def __init__(self, archive_dir: str = "/app/archives"):
        self.archive_dir = archive_dir
        self._ensure_archive_dir()
    
    def _ensure_archive_dir(self):
        """Create archive directory if it doesn't exist"""
        os.makedirs(self.archive_dir, exist_ok=True)
        logger.info(f"Archive directory: {self.archive_dir}")

    def _safe_archive_path(self, date: str) -> str:
        """Resolve `messages_<date>.json.gz` strictly inside the archive directory.

        The shape gate rejects everything that is not a calendar date -- including
        `..`, absolute paths and encoded separators, which all fail the regex before
        any path is built. The containment check then resolves symlinks and `..`
        segments that a shape gate alone cannot see (a symlink inside the directory
        pointing out of it), and refuses anything that lands outside. Raises 400 on
        a bad date so the router answers without touching the filesystem.
        """
        if not _DATE_RE.fullmatch(date):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid date, expected YYYY-MM-DD",
            )
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid date, expected YYYY-MM-DD",
            )
        base = os.path.realpath(self.archive_dir)
        candidate = os.path.realpath(os.path.join(base, f"messages_{date}.json.gz"))
        if os.path.commonpath([base, candidate]) != base:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid date",
            )
        return candidate

    def _read_archive_bytes(self, path: str) -> bytes:
        """Stream one archive's decompressed bytes, bounded by the read ceiling.

        Reads in chunks and stops counting at the cap, so a multi-gigabyte bomb
        costs one chunk past the ceiling rather than the machine's memory. Raises
        413 when the ceiling is crossed; the caller never parses unbounded input.
        """
        chunks: List[bytes] = []
        total = 0
        try:
            with gzip.open(path, "rb") as f:
                while True:
                    chunk = f.read(_ARCHIVE_READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_ARCHIVE_READ_BYTES:
                        raise HTTPException(
                            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail="Archive file is too large to read",
                        )
                    chunks.append(chunk)
        except FileNotFoundError:
            raise
        except HTTPException:
            raise
        except OSError as e:
            logger.error(f"Error decompressing archive file: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Archive file could not be read",
            )
        return b"".join(chunks)
    
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
            # Resolved through the same gate as reads: the key is minted with
            # strftime upstream, so this never triggers, but no future caller gets
            # an unvalidated join by accident.
            filepath = self._safe_archive_path(date_key)

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
            filename = os.path.basename(filepath)
            logger.info(
                f"Archived {len(messages)} messages to {filename} "
                f"({file_size / 1024:.2f} KB)"
            )
            
            return filename

        except HTTPException:
            # A rejected date is the caller's mistake, not a failed write: let the
            # 400 travel instead of collapsing it into an empty filename.
            raise
        except Exception as e:
            logger.error(f"Error writing archive file: {e}")
            return ""
    
    def get_archived_messages(
        self,
        date: str,
        user_regno: Optional[str] = None,
        conversation_id: Optional[str] = None,
        skip: int = 0,
        limit: int = 50,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        Retrieve one page of archived messages for a date, with the total beside it.

        The date is validated before anything is read: a non-date answers 400 even
        when no such file exists, so traversal probes cannot hide behind a missing
        file. The file itself is streamed under a byte ceiling and parsed only once
        bounded, so a decompression bomb costs a 413 instead of the process. Filters
        run over the parsed day and the page is sliced last, so `total` counts what
        the filters matched, not what the page holds.

        Args:
            date: Date string (YYYY-MM-DD)
            user_regno: Filter by user (sender or receiver)
            conversation_id: Filter by conversation
            skip: How many matching messages to skip
            limit: How many matching messages to return at most

        Returns:
            Tuple of (page of messages, total matching messages)
        """
        # Validated first: the 400 must not depend on whether the file exists, and no
        # path is ever built from an unvalidated date.
        filepath = self._safe_archive_path(date)

        try:
            raw = self._read_archive_bytes(filepath)
        except FileNotFoundError:
            logger.warning(f"Archive file not found: {os.path.basename(filepath)}")
            return [], 0

        try:
            messages = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            logger.error(f"Archive file is not valid JSON: {filepath}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Archive file could not be read",
            )

        # The writer stores a list of message dicts; anything else is corruption or
        # tampering, and iterating it would fail halfway or, worse, half-succeed.
        if not isinstance(messages, list) or any(
            not isinstance(msg, dict) for msg in messages
        ):
            logger.error(f"Archive file has an unexpected shape: {filepath}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Archive file could not be read",
            )

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

        total = len(messages)
        skip = max(0, skip)
        limit = max(1, min(limit, ARCHIVED_MESSAGES_MAX_LIMIT))
        return messages[skip:skip + limit], total
    
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
