# app/services/message_archival_service.py

import gzip
import json
import os
import re
import tempfile
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

# Server-side streaming batch for the archival SELECT. Rows are fetched in batches
# of this size instead of .all(), so a runaway backlog costs buffer memory, not
# the process.
_ARCHIVE_STREAM_BATCH = 1000

# DELETE ... WHERE id IN (...) is chunked at this many ids per statement: PostgreSQL
# caps a statement at 65535 bind parameters, and one giant IN list also plans badly.
_DELETE_ID_CHUNK = 1000

# Archive files hold chat bodies -- PII -- so they are owner-only on disk. The
# directory is 0o700 for the same reason: the old 0o777 default made every
# archived conversation readable (and the directory writable) by any local user.
_ARCHIVE_FILE_MODE = 0o600
_ARCHIVE_DIR_MODE = 0o700


class MessageArchivalService:
    """Service for archiving old messages to compressed JSON files"""
    
    def __init__(self, archive_dir: str = "/app/archives"):
        self.archive_dir = archive_dir
        self._ensure_archive_dir()
    
    def _ensure_archive_dir(self):
        """Create the archive directory owner-only if it doesn't exist.

        Mode 0o700 (and an explicit chmod afterwards, which also tightens a
        directory created earlier under the old world-readable default or by a
        bind mount): archives hold chat bodies, so other local users get neither
        read nor listing. A chmod failure is a warning rather than fatal -- the
        writes that follow will fail loudly on their own if the directory is
        truly unusable.
        """
        os.makedirs(self.archive_dir, mode=_ARCHIVE_DIR_MODE, exist_ok=True)
        try:
            os.chmod(self.archive_dir, _ARCHIVE_DIR_MODE)
        except OSError as e:
            logger.warning(f"Could not chmod archive directory: {e}")
        logger.info(f"Archive directory: {self.archive_dir}")

    def _fsync_dir(self) -> None:
        """Force the archive directory entry itself to durable storage.

        os.replace() only guarantees the rename is atomic, not that it survives
        a power cut: the directory entry has to be fsynced too. Best effort --
        a platform without directory fsync still gets the atomic rename.
        """
        try:
            fd = os.open(self.archive_dir, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as e:
            logger.warning(f"Could not fsync archive directory: {e}")

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
        """Move messages older than ``hours_old`` from PostgreSQL into per-day gzip files.

        Ordering is the whole safety argument, so it is stated plainly:

        1. SELECT the doomed set once, streamed (never ``.all()``), grouped by day.
        2. Write every day's file first -- atomically (temp + fsync + rename),
           owner-only, merged with whatever the file already holds. If any write
           fails, nothing is deleted: database rows imply archived bytes, so an
           aborted run is retried whole by the next one.
        3. DELETE exactly the archived row ids (chunked ``IN``), never a
           re-derived cutoff: rows inserted between the SELECT and the DELETE are
           not in the id set and survive. The deleted total is verified against
           the archived total; a mismatch is logged critical and reported.

        Returns the archived/deleted counts, the files written and the cutoff, or
        an ``error`` entry when nothing was deleted.
        """
        try:
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours_old)

            # Streamed, not .all(): a far-behind scheduler must not materialise
            # the whole backlog in one list. Ordered by id so retries and logs
            # see a stable sequence.
            old_messages: List[Dict[str, Any]] = []
            archived_ids: List[str] = []
            with db.sessions() as session:
                result = session.execute(
                    select(MessageRecord)
                    .where(MessageRecord.created_at < cutoff_time)
                    .order_by(MessageRecord.id)
                    .execution_options(
                        yield_per=_ARCHIVE_STREAM_BATCH, stream_results=True
                    )
                )
                for row in result.scalars():
                    archived_ids.append(row.id)
                    old_messages.append({
                        "id": row.id,
                        "conversation_id": row.conversation_id,
                        "sender_regno": row.sender_regno,
                        "receiver_regno": row.receiver_regno,
                        "body": row.body,
                        "created_at": row.created_at,
                        "is_read": row.is_read,
                    })

            if not old_messages:
                logger.info("No messages to archive")
                return {
                    "archived_count": 0,
                    "message": "No messages to archive"
                }

            # Group messages by date
            messages_by_date: Dict[str, List[Dict[str, Any]]] = {}
            for msg in old_messages:
                date_key = msg["created_at"].strftime("%Y-%m-%d")
                messages_by_date.setdefault(date_key, []).append(msg)

            archived_count = 0
            archive_files = []

            # Files before deletes: a failed write must leave the rows where a
            # retry will find them. Any single failure aborts the whole delete
            # below, and the merge in _archive_messages_to_file makes the retry
            # reproduce exactly the bytes a partial run already wrote.
            for date_key, messages in messages_by_date.items():
                archive_file = self._archive_messages_to_file(date_key, messages)
                if not archive_file:
                    logger.error(
                        f"Archive write failed for {date_key}; "
                        f"keeping {len(old_messages)} rows in PostgreSQL"
                    )
                    return {
                        "error": f"Archive write failed for {date_key}",
                        "archived_count": 0,
                        "deleted_count": 0,
                    }
                archive_files.append(archive_file)
                archived_count += len(messages)

            # Delete exactly what was archived, by id: rows that arrived after
            # the SELECT carry other ids and are untouched even when their
            # created_at predates the cutoff.
            deleted_count = 0
            with db.sessions.begin() as session:
                for i in range(0, len(archived_ids), _DELETE_ID_CHUNK):
                    chunk = archived_ids[i:i + _DELETE_ID_CHUNK]
                    result = session.execute(
                        delete(MessageRecord).where(MessageRecord.id.in_(chunk))
                    )
                    deleted_count += result.rowcount or 0

            if deleted_count != archived_count:
                # The files hold every archived row, so nothing is lost; but a
                # gap means something else deleted rows concurrently (or a chunk
                # silently dropped), which must not pass quietly.
                logger.critical(
                    f"Archive/delete count mismatch: archived {archived_count} "
                    f"messages but deleted {deleted_count}"
                )
                return {
                    "error": "Archive/delete count mismatch",
                    "archived_count": archived_count,
                    "deleted_count": deleted_count,
                    "archive_files": archive_files,
                    "cutoff_time": cutoff_time.isoformat(),
                }

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
        """Merge one day's messages into its archive file, atomically and owner-only.

        The file is read first (when present) and the new batch merged by id, so
        a retried run after a crash between write and delete reproduces the bytes
        instead of orphaning the first batch under an overwrite. The merged list
        goes to a temp file (mode 0o600) that is fsynced and renamed over the
        target, so a reader never sees a half-written day; the directory is
        fsynced after the rename so the entry itself is durable.

        Returns the filename, or "" when nothing was written -- in which case the
        caller must keep the rows in the database.
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

            # Merge with what the day already holds: without this a second run
            # for the same date (the retry after an aborted delete) would drop
            # the first batch from the archive while its rows are already gone
            # from the database. New rows win on id collision; ids are unique per
            # row, so a collision is always the same row re-archived.
            merged: Dict[str, Dict[str, Any]] = {}
            if os.path.exists(filepath):
                existing_raw = self._read_archive_bytes(filepath)
                existing = json.loads(existing_raw.decode("utf-8"))
                if not isinstance(existing, list) or any(
                    not isinstance(msg, dict) for msg in existing
                ):
                    logger.error(f"Existing archive has an unexpected shape: {filepath}")
                    return ""
                for msg in existing:
                    if msg.get("id") is not None:
                        merged[str(msg["id"])] = msg
            for msg in serializable_messages:
                merged[str(msg["id"])] = msg
            payload = [merged[key] for key in sorted(merged)]

            # Temp file in the same directory (rename stays on one filesystem),
            # owner-only from creation, fsynced before the rename.
            fd, tmp_path = tempfile.mkstemp(
                dir=self.archive_dir, prefix=".tmp_archive_", suffix=".json.gz"
            )
            try:
                os.chmod(tmp_path, _ARCHIVE_FILE_MODE)
                with os.fdopen(fd, "wb") as raw:
                    with gzip.open(raw, "wt", encoding="utf-8") as f:
                        json.dump(payload, f, indent=2)
                    raw.flush()
                    os.fsync(raw.fileno())
                os.chmod(tmp_path, _ARCHIVE_FILE_MODE)
                os.replace(tmp_path, filepath)
                os.chmod(filepath, _ARCHIVE_FILE_MODE)
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            self._fsync_dir()

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
