from ..logger import get_logger
from sqlalchemy import func, or_, select
from ..postgres import PostgresClient
from ..db_models import ConfessionCommentRecord, ConfessionRecord, ReportRecord, UserRecord
from ..models import Confession, ConfessionComment, ConfessionCreate, CommentCreate, UserDetails, UserInfo, ReportCreate, ConfessionUpdate
import datetime
from datetime import timezone
from typing import Optional, List

logger = get_logger(__name__)

class ConfessionService:
    def __init__(self):
        self.client = PostgresClient()
        self.sessions = self.client.sessions
        self.REACTION_TYPES = ["heart", "haha", "whoa", "heartbreak"]

    def _reporter_ids(self, target_id: str) -> List[str]:
        with self.sessions() as session:
            return [str(report.reported_by_id) for report in session.scalars(select(ReportRecord).where(or_(
                ReportRecord.confession_id == str(target_id), ReportRecord.comment_id == str(target_id)
            ))).all()]

    def _get_user_info(self, user_id: str) -> Optional[UserInfo]:
        with self.sessions() as session:
            user_doc = session.get(UserRecord, str(user_id))
        if user_doc:
            return UserInfo(
                id=str(user_doc.id), name=user_doc.name or "Anonymous User", avatar=user_doc.emoji
            )
        return None

    @staticmethod
    def _confession_payload(row):
        return {"id": str(row.id), "body": row.body, "recipient_name": row.recipient_name,
            "anonymous": row.anonymous, "comments_enabled": row.comments_enabled, "created_at": row.created_at,
            "author_id": str(row.author_id), "approved": row.approved, "report_count": row.report_count,
            "reactions": row.reactions or {}, "heart_count": row.heart_count, "haha_count": row.haha_count,
            "whoa_count": row.whoa_count, "heartbreak_count": row.heartbreak_count,
            "comment_count": row.comment_count}

    @staticmethod
    def _comment_payload(row, user=None):
        return {"id": str(row.id), "confession_id": str(row.confession_id), "user_id": str(row.user_id),
            "user_info": {"id": str(row.user_id), "name": user.name if user else "User", "avatar": user.emoji if user else None},
            "body": row.body, "created_at": row.created_at, "likes": [str(x) for x in (row.likes or [])],
            "dislikes": [str(x) for x in (row.dislikes or [])], "like_count": row.like_count,
            "dislike_count": row.dislike_count, "report_count": row.report_count}

    def create_confession(self, confession_data: ConfessionCreate, user_id: str):
        confession_dict = confession_data.dict()
        recipient_name = confession_dict.get("recipient_name")
        if isinstance(recipient_name, str):
            confession_dict["recipient_name"] = recipient_name.strip()
        created_at = datetime.datetime.now(datetime.timezone.utc)
        with self.sessions.begin() as session:
            row = ConfessionRecord(
                body=confession_dict["body"], recipient_name=confession_dict.get("recipient_name"),
                anonymous=confession_dict["anonymous"], comments_enabled=confession_dict["comments_enabled"],
                created_at=created_at, author_id=user_id, reactions={reaction: [] for reaction in self.REACTION_TYPES},
            )
            session.add(row)
            session.flush()
            confession_id = str(row.id)
        logger.info(f"Created confession {confession_id}")
        return self.get_confession(confession_id, user_id)

    def get_confessions(self, sort_by: str = 'popularity', user_id: Optional[str] = None) -> List[Confession]:
        with self.sessions() as session:
            confession_models = session.scalars(select(ConfessionRecord)).all()
            comment_models = session.scalars(select(ConfessionCommentRecord).order_by(ConfessionCommentRecord.created_at.desc())).all()
            user_map = {user.id: user for user in session.scalars(select(UserRecord)).all()}
            confession_rows = [self._confession_payload(row) for row in confession_models]
            comments_by_confession = {}
            for row in comment_models:
                comment = self._comment_payload(row, user_map.get(row.user_id))
                comments_by_confession.setdefault(comment["confession_id"], []).append(comment)

        for confession in confession_rows:
            confession["reported_by"] = self._reporter_ids(confession["id"])
            confession["report_count"] = len(confession["reported_by"])
            confession_id = str(confession["id"])
            for comment in comments_by_confession.get(confession_id, []):
                comment["reported_by"] = self._reporter_ids(comment["id"])
                comment["report_count"] = len(comment["reported_by"])
            confession["comments"] = sorted(
                comments_by_confession.get(confession_id, []),
                key=lambda comment: comment.get("created_at") or datetime.datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            author = user_map.get(str(confession.get("author_id")))
            confession["user_info"] = {
                "id": str(confession.get("author_id")),
                "name": author.name if author else "Anonymous User",
                "avatar": author.emoji if author else None,
            }

        def timestamp_key(row):
            value = row.get("created_at")
            return value or datetime.datetime.min.replace(tzinfo=timezone.utc)

        if sort_by == "popularity":
            confession_rows.sort(key=lambda row: timestamp_key(row))
            confession_rows.sort(key=lambda row: sum(row.get(f"{kind}_count", 0) or 0 for kind in self.REACTION_TYPES), reverse=True)
        elif sort_by == "comments":
            confession_rows.sort(key=timestamp_key)
            confession_rows.sort(key=lambda row: row.get("comment_count", 0) or 0, reverse=True)
        elif sort_by == "time":
            confession_rows.sort(key=lambda row: (row.get("user_info", {}).get("name") or "").lower())
            confession_rows.sort(key=timestamp_key, reverse=True)
        else:
            confession_rows.sort(key=timestamp_key, reverse=True)

        confessions = []
        for confession_doc in confession_rows:
            comments_list = []
            for comment_doc in sorted(confession_doc.get('comments', []), key=lambda c: c['created_at'], reverse=True):
                user_comment_reaction = None
                if user_id:
                    if user_id in comment_doc.get('likes', []):
                        user_comment_reaction = 'like'
                    elif user_id in comment_doc.get('dislikes', []):
                        user_comment_reaction = 'dislike'
                comment_doc['user_reaction'] = user_comment_reaction
                comments_list.append(ConfessionComment(**comment_doc))
            confession_doc['comments'] = comments_list

            user_reaction = None
            if user_id and 'reactions' in confession_doc:
                for reaction_type, user_ids in confession_doc['reactions'].items():
                    if user_id in user_ids:
                        user_reaction = reaction_type
                        break
            confession_doc['user_reaction'] = user_reaction
            
            confessions.append(Confession(**confession_doc))
            
        logger.info(f"Retrieved {len(confessions)} confessions sorted by {sort_by}")
        return confessions

    def get_confession(self, confession_id: str, user_id: Optional[str] = None) -> Optional[Confession]:
        with self.sessions() as session:
            row = session.get(ConfessionRecord, str(confession_id))
            confession_doc = self._confession_payload(row) if row else None
        if confession_doc:
            confession_doc["reported_by"] = self._reporter_ids(confession_id)
            confession_doc["report_count"] = len(confession_doc["reported_by"])
            user_reaction = None
            if user_id and 'reactions' in confession_doc:
                for reaction_type, user_ids in confession_doc['reactions'].items():
                    if user_id in user_ids:
                        user_reaction = reaction_type
                        break
            
            confession_doc['user_reaction'] = user_reaction
            
            with self.sessions() as session:
                comment_models = session.scalars(select(ConfessionCommentRecord).where(
                    ConfessionCommentRecord.confession_id == str(confession_id)
                ).order_by(ConfessionCommentRecord.created_at.desc())).all()
                comment_users = {u.id: u for u in session.scalars(select(UserRecord).where(
                    UserRecord.id.in_({c.user_id for c in comment_models})
                )).all()} if comment_models else {}
            comments_cursor = [self._comment_payload(c, comment_users.get(c.user_id)) for c in comment_models]
            comments_list = []
            for comment_doc in comments_cursor:
                comment_doc["reported_by"] = self._reporter_ids(comment_doc["id"])
                comment_doc["report_count"] = len(comment_doc["reported_by"])
                user_comment_reaction = None
                if user_id:
                    if user_id in comment_doc.get('likes', []):
                        user_comment_reaction = 'like'
                    elif user_id in comment_doc.get('dislikes', []):
                        user_comment_reaction = 'dislike'
                comment_doc['user_reaction'] = user_comment_reaction
                comments_list.append(ConfessionComment(**comment_doc))
            confession_doc['comments'] = comments_list

            # Add user_info to single confession fetch
            author_info = self._get_user_info(confession_doc['author_id'])
            if author_info:
                confession_doc['user_info'] = author_info.dict()

            return Confession(**confession_doc)
        return None

    def update_confession(self, confession_id: str, update_data: ConfessionUpdate, user_id: str) -> Optional[Confession]:
        """
        Used to update a confession's settings, such as toggling comments or anonymity.
        """
        with self.sessions() as session:
            row = session.get(ConfessionRecord, str(confession_id))
            confession = self._confession_payload(row) if row else None

        if not confession:
            logger.error(f"Confession {confession_id} not found for update.")
            return None

        if confession.get("author_id") != user_id:
            logger.warning(f"User {user_id} attempted to edit confession {confession_id} owned by {confession.get('author_id')}")
            return None

        update_fields = {}
        update_data_dict = update_data.dict(exclude_unset=True)

        # Handle 'comments_enabled' update - this is straightforward
        if "comments_enabled" in update_data_dict:
            update_fields["comments_enabled"] = update_data_dict["comments_enabled"]

        # Handle 'anonymous' update with explicit logic
        if "anonymous" in update_data_dict:
            is_currently_anonymous = confession.get("anonymous", False)
            wants_to_be_anonymous = update_data_dict["anonymous"]

            # The only permitted change is from anonymous (True) to public (False).
            if is_currently_anonymous and not wants_to_be_anonymous:
                update_fields["anonymous"] = False
            # All other cases (e.g., trying to make a public post anonymous) are ignored for security.

        if not update_fields:
            # No valid changes were made, so we can return early.
            return self.get_confession(confession_id, user_id)

        with self.sessions.begin() as session:
            row = session.get(ConfessionRecord, str(confession_id))
            if row:
                if "comments_enabled" in update_fields: row.comments_enabled = update_fields["comments_enabled"]
                if "anonymous" in update_fields: row.anonymous = update_fields["anonymous"]

        logger.info(f"User {user_id} updated confession {confession_id} with data: {update_fields}")
        return self.get_confession(confession_id, user_id)

    def get_total_confessions_count(self) -> int:
        """
        Used to get the total number of confessions in the database.
        """
        with self.sessions() as session:
            count = session.scalar(select(func.count()).select_from(ConfessionRecord)) or 0
        logger.info(f"Retrieved total confession count: {count}")
        return count

    def react_to_confession(self, confession_id: str, reaction: str, user_id: str):
        if reaction not in self.REACTION_TYPES:
            return None 

        with self.sessions.begin() as session:
            row = session.get(ConfessionRecord, str(confession_id))
            confession = self._confession_payload(row) if row else None
            if not row:
                return None
            current_reactions = row.reactions or {r: [] for r in self.REACTION_TYPES}
            user_previous_reaction = next((kind for kind, ids in current_reactions.items() if user_id in ids), None)
            for ids in current_reactions.values():
                if user_id in ids: ids.remove(user_id)
            if user_previous_reaction != reaction:
                current_reactions.setdefault(reaction, []).append(user_id)
            row.reactions = current_reactions
            for kind in self.REACTION_TYPES:
                setattr(row, f"{kind}_count", len(current_reactions.get(kind, [])))
        if not confession:
            return None

        logger.info(f"User {user_id} reacted to confession {confession_id} with {reaction}")
        return self.get_confession(confession_id, user_id)

    def add_comment_to_confession(self, confession_id: str, comment_data: CommentCreate, user: UserDetails) -> any:
        with self.sessions() as session:
            row = session.get(ConfessionRecord, str(confession_id))
            confession = self._confession_payload(row) if row else None
        if not confession:
            return "CONFESSION_NOT_FOUND"
        if not confession.get("comments_enabled", True):
            logger.warning(f"User {user.id} tried to comment on disabled confession {confession_id}")
            return "COMMENTS_DISABLED"

        user_id = str(user.id)
        with self.sessions() as session:
            existing_comments_count = session.scalar(select(func.count()).select_from(ConfessionCommentRecord).where(
                ConfessionCommentRecord.confession_id == str(confession_id),
                ConfessionCommentRecord.user_id == user_id,
            )) or 0
        if existing_comments_count >= 3:
            logger.warning(f"User {user_id} tried to post more than 3 comments on confession {confession_id}")
            return "COMMENT_LIMIT_REACHED"

        user_info = UserInfo(id=user_id, name=user.name, avatar=user.emoji)

        with self.sessions.begin() as session:
            row = ConfessionCommentRecord(confession_id=str(confession_id), user_id=user_id,
                body=comment_data.body, created_at=datetime.datetime.now(datetime.timezone.utc))
            session.add(row)
            confession_row = session.get(ConfessionRecord, str(confession_id))
            if confession_row: confession_row.comment_count += 1
            session.flush()
            new_comment = self._comment_payload(row)
            new_comment["user_info"].update({"name": user.name, "avatar": user.emoji})
        logger.info(f"Added comment {new_comment['id']} to confession {confession_id}")
        return ConfessionComment(**new_comment)

    def _react_to_comment(self, comment_id: str, user_id: str, reaction_type: str) -> Optional[ConfessionComment]:
        user_new_reaction = None
        with self.sessions.begin() as session:
            row = session.get(ConfessionCommentRecord, str(comment_id))
            if not row:
                return None
            likes = [str(value) for value in (row.likes or [])]
            dislikes = [str(value) for value in (row.dislikes or [])]
            if reaction_type == 'likes':
                if user_id in dislikes: dislikes.remove(user_id)
                if user_id in likes: likes.remove(user_id)
                else: likes.append(user_id); user_new_reaction = 'like'
            else:
                if user_id in likes: likes.remove(user_id)
                if user_id in dislikes: dislikes.remove(user_id)
                else: dislikes.append(user_id); user_new_reaction = 'dislike'
            row.likes = likes
            row.dislikes = dislikes
            row.like_count = len(likes)
            row.dislike_count = len(dislikes)
        
        logger.info(f"User {user_id} reacted to comment {comment_id} with {reaction_type}")
        
        with self.sessions() as session:
            row = session.get(ConfessionCommentRecord, str(comment_id))
            user = session.get(UserRecord, row.user_id) if row else None
        if not row:
            return None
        updated_comment = self._comment_payload(row, user)
        updated_comment["reported_by"] = self._reporter_ids(comment_id)
        updated_comment["report_count"] = len(updated_comment["reported_by"])
        updated_comment["user_reaction"] = user_new_reaction
        return ConfessionComment(**updated_comment)

    def like_comment(self, comment_id: str, user_id: str) -> Optional[ConfessionComment]:
        return self._react_to_comment(comment_id, user_id, 'likes')

    def dislike_comment(self, comment_id: str, user_id: str) -> Optional[ConfessionComment]:
        return self._react_to_comment(comment_id, user_id, 'dislikes')

    def report_comment(self, comment_id: str, report_data: ReportCreate, user: UserDetails) -> Optional[ConfessionComment]:
        user_id = str(user.id)
        with self.sessions() as session:
            comment = session.get(ConfessionCommentRecord, str(comment_id))
        if not comment:
            return None

        with self.sessions() as session:
            duplicate = session.scalar(select(ReportRecord.id).where(
                ReportRecord.comment_id == str(comment_id), ReportRecord.reported_by_id == user_id
            ).limit(1))
        if duplicate:
            return None
        with self.sessions.begin() as session:
            session.add(ReportRecord(comment_id=str(comment_id), reported_by_id=user_id,
                reported_by_name=user.name, reason=report_data.reason,
                created_at=datetime.datetime.now(datetime.timezone.utc)))
            comment_row = session.get(ConfessionCommentRecord, str(comment_id))
            if comment_row: comment_row.report_count += 1

        logger.info(f"User {user_id} reported comment {comment_id}")
        with self.sessions() as session:
            row = session.get(ConfessionCommentRecord, str(comment_id))
            author = session.get(UserRecord, row.user_id) if row else None
        if not row: return None
        updated_comment_doc = self._comment_payload(row, author)
        updated_comment_doc["reported_by"] = self._reporter_ids(comment_id)
        updated_comment_doc["report_count"] = len(updated_comment_doc["reported_by"])
        return ConfessionComment(**updated_comment_doc)

    def report_confession(self, confession_id: str, report_data: ReportCreate, user: UserDetails) -> Optional[Confession]:
        user_id = str(user.id)
        with self.sessions() as session:
            confession = session.get(ConfessionRecord, str(confession_id))
        if not confession:
            return None

        with self.sessions() as session:
            duplicate = session.scalar(select(ReportRecord.id).where(
                ReportRecord.confession_id == str(confession_id), ReportRecord.reported_by_id == user_id
            ).limit(1))
        if duplicate:
            return None
        with self.sessions.begin() as session:
            session.add(ReportRecord(confession_id=str(confession_id), reported_by_id=user_id,
                reported_by_name=user.name, reason=report_data.reason,
                created_at=datetime.datetime.now(datetime.timezone.utc)))
            confession_row = session.get(ConfessionRecord, str(confession_id))
            if confession_row: confession_row.report_count += 1

        logger.info(f"User {user_id} reported confession {confession_id}")
        return self.get_confession(confession_id, user_id)
