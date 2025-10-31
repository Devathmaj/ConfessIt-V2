from ..logger import get_logger
from pymongo import MongoClient, DESCENDING
from ..models import Confession, ConfessionComment, ConfessionCreate, CommentCreate, UserDetails, UserInfo, ReportCreate, Report, ConfessionUpdate
from bson.objectid import ObjectId
import datetime
from ..config import settings
from typing import Optional, List

logger = get_logger(__name__)

class ConfessionService:
    def __init__(self):
        self.client = MongoClient(settings.MONGO_URI)
        self.db = self.client[settings.DATABASE_NAME]
        self.confessions_collection = self.db["Confessions"]
        self.comments_collection = self.db["ConfessionComments"]
        self.users_collection = self.db["UserDetails"]
        self.reports_collection = self.db["Reports"]
        self.REACTION_TYPES = ["heart", "haha", "whoa", "heartbreak"]

    def _get_user_info(self, user_id: str) -> Optional[UserInfo]:
        user_doc = self.users_collection.find_one({"_id": ObjectId(user_id)})
        if user_doc:
            display_name = user_doc.get("Name", "Anonymous User")
            return UserInfo(
                id=str(user_doc["_id"]),
                username=display_name,
                avatar=user_doc.get("emoji")
            )
        return None

    def create_confession(self, confession_data: ConfessionCreate, user_id: str):
        confession_dict = confession_data.dict()
        confessing_to_value = confession_dict.get("confessing_to")
        if isinstance(confessing_to_value, str):
            confession_dict["confessing_to"] = confessing_to_value.strip()
        confession_dict["_id"] = ObjectId()
        confession_dict["user_id"] = user_id 
        confession_dict["timestamp"] = datetime.datetime.utcnow()
        confession_dict["reactions"] = {reaction: [] for reaction in self.REACTION_TYPES}
        for reaction_type in self.REACTION_TYPES:
            confession_dict[f"{reaction_type}_count"] = 0
        confession_dict["report_count"] = 0
        confession_dict["reported_by"] = []
        confession_dict["comments"] = []
        confession_dict["comment_count"] = 0

        result = self.confessions_collection.insert_one(confession_dict)
        logger.info(f"Created confession {result.inserted_id}")
        return self.get_confession(str(result.inserted_id), user_id)

    def get_confessions(self, sort_by: str = 'popularity', user_id: Optional[str] = None) -> List[Confession]:
        pipeline = [
            {
                "$addFields": {
                    "total_reactions": {
                        "$sum": [
                            "$heart_count",
                            "$haha_count",
                            "$whoa_count",
                            "$heartbreak_count"
                        ]
                    }
                }
            },
            {
                "$addFields": {
                    "confession_id_str": { "$toString": "$_id" }
                }
            },
            {
                "$lookup": {
                    "from": "ConfessionComments",
                    "localField": "confession_id_str",
                    "foreignField": "confession_id",
                    "as": "comments"
                }
            },
            {
                "$addFields": {
                    "user_id_as_obj": { "$toObjectId": "$user_id" }
                }
            },
            {
                "$lookup": {
                    "from": "UserDetails",
                    "localField": "user_id_as_obj",
                    "foreignField": "_id",
                    "as": "author_details"
                }
            },
            {
                "$unwind": {
                    "path": "$author_details",
                    "preserveNullAndEmptyArrays": True
                }
            },
            {
                "$addFields": {
                    "user_info": {
                        "id": "$user_id",
                        "username": "$author_details.Name",
                        "avatar": "$author_details.emoji"
                    }
                }
            }
        ]

        if sort_by == 'popularity':
            pipeline.append({"$sort": {"total_reactions": -1, "timestamp": 1}})
        elif sort_by == 'time':
            pipeline.append({"$sort": {"timestamp": -1, "user_info.username": 1}})
        elif sort_by == 'comments':
            pipeline.append({"$sort": {"comment_count": -1, "timestamp": 1}})
        else:
            pipeline.append({"$sort": {"timestamp": -1}})

        confessions_cursor = self.confessions_collection.aggregate(pipeline)
        
        confessions = []
        for confession_doc in confessions_cursor:
            comments_list = []
            for comment_doc in sorted(confession_doc.get('comments', []), key=lambda c: c['timestamp'], reverse=True):
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
        confession_doc = self.confessions_collection.find_one({"_id": ObjectId(confession_id)})
        if confession_doc:
            user_reaction = None
            if user_id and 'reactions' in confession_doc:
                for reaction_type, user_ids in confession_doc['reactions'].items():
                    if user_id in user_ids:
                        user_reaction = reaction_type
                        break
            
            confession_doc['user_reaction'] = user_reaction
            
            comments_cursor = self.comments_collection.find({"confession_id": confession_id}).sort("timestamp", DESCENDING)
            comments_list = []
            for comment_doc in comments_cursor:
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
            author_info = self._get_user_info(confession_doc['user_id'])
            if author_info:
                confession_doc['user_info'] = author_info.dict()

            return Confession(**confession_doc)
        return None

    def update_confession(self, confession_id: str, update_data: ConfessionUpdate, user_id: str) -> Optional[Confession]:
        """
        Used to update a confession's settings, such as toggling comments or anonymity.
        """
        confession = self.confessions_collection.find_one({"_id": ObjectId(confession_id)})

        if not confession:
            logger.error(f"Confession {confession_id} not found for update.")
            return None

        if confession.get("user_id") != user_id:
            logger.warning(f"User {user_id} attempted to edit confession {confession_id} owned by {confession.get('user_id')}")
            return None

        update_fields = {}
        update_data_dict = update_data.dict(exclude_unset=True)

        # Handle 'is_comment' update - this is straightforward
        if "is_comment" in update_data_dict:
            update_fields["is_comment"] = update_data_dict["is_comment"]

        # Handle 'is_anonymous' update with explicit logic
        if "is_anonymous" in update_data_dict:
            is_currently_anonymous = confession.get("is_anonymous", False)
            wants_to_be_anonymous = update_data_dict["is_anonymous"]

            # The only permitted change is from anonymous (True) to public (False).
            if is_currently_anonymous and not wants_to_be_anonymous:
                update_fields["is_anonymous"] = False
            # All other cases (e.g., trying to make a public post anonymous) are ignored for security.

        if not update_fields:
            # No valid changes were made, so we can return early.
            return self.get_confession(confession_id, user_id)

        self.confessions_collection.update_one(
            {"_id": ObjectId(confession_id)},
            {"$set": update_fields}
        )

        logger.info(f"User {user_id} updated confession {confession_id} with data: {update_fields}")
        return self.get_confession(confession_id, user_id)

    def get_total_confessions_count(self) -> int:
        """
        Used to get the total number of confessions in the database.
        """
        count = self.confessions_collection.count_documents({})
        logger.info(f"Retrieved total confession count: {count}")
        return count

    def react_to_confession(self, confession_id: str, reaction: str, user_id: str):
        if reaction not in self.REACTION_TYPES:
            return None 

        confession = self.confessions_collection.find_one({"_id": ObjectId(confession_id)})
        if not confession:
            return None

        update_fields = {}
        current_reactions = confession.get('reactions', {r: [] for r in self.REACTION_TYPES})

        user_previous_reaction = None
        for reaction_type, user_ids in current_reactions.items():
            if user_id in user_ids:
                user_previous_reaction = reaction_type
                current_reactions[reaction_type].remove(user_id)
                break
        
        if user_previous_reaction != reaction:
            current_reactions.setdefault(reaction, []).append(user_id)

        update_fields['reactions'] = current_reactions
        for r_type in self.REACTION_TYPES:
            update_fields[f"{r_type}_count"] = len(current_reactions.get(r_type, []))

        self.confessions_collection.update_one(
            {"_id": ObjectId(confession_id)},
            {"$set": update_fields}
        )
        
        logger.info(f"User {user_id} reacted to confession {confession_id} with {reaction}")
        return self.get_confession(confession_id, user_id)

    def add_comment_to_confession(self, confession_id: str, comment_data: CommentCreate, user: UserDetails) -> any:
        confession = self.confessions_collection.find_one({"_id": ObjectId(confession_id)})
        if not confession:
            return "CONFESSION_NOT_FOUND"
        if not confession.get("is_comment", True):
            logger.warning(f"User {user.id} tried to comment on disabled confession {confession_id}")
            return "COMMENTS_DISABLED"

        user_id = str(user.id)
        existing_comments_count = self.comments_collection.count_documents({
            "confession_id": confession_id,
            "user_info.id": user_id
        })
        if existing_comments_count >= 3:
            logger.warning(f"User {user_id} tried to post more than 3 comments on confession {confession_id}")
            return "COMMENT_LIMIT_REACHED"

        user_info = UserInfo(id=user_id, username=user.Name, avatar=user.emoji)

        comment_dict = {
            "_id": ObjectId(),
            "confession_id": confession_id,
            "user_info": user_info.dict(),
            "message": comment_data.message,
            "timestamp": datetime.datetime.utcnow(),
            "likes": [],
            "dislikes": [],
            "like_count": 0,
            "dislike_count": 0,
            "report_count": 0,
            "reported_by": []
        }

        result = self.comments_collection.insert_one(comment_dict)
        logger.info(f"Added comment {result.inserted_id} to confession {confession_id}")
        
        self.confessions_collection.update_one(
            {"_id": ObjectId(confession_id)},
            {"$inc": {"comment_count": 1}}
        )

        new_comment = self.comments_collection.find_one({"_id": result.inserted_id})
        if new_comment:
            return ConfessionComment(**new_comment)
        return None

    def _react_to_comment(self, comment_id: str, user_id: str, reaction_type: str) -> Optional[ConfessionComment]:
        comment = self.comments_collection.find_one({"_id": ObjectId(comment_id)})
        if not comment:
            return None

        likes = comment.get('likes', [])
        dislikes = comment.get('dislikes', [])

        user_new_reaction = None
        
        if reaction_type == 'likes':
            if user_id in dislikes:
                dislikes.remove(user_id)
            if user_id in likes:
                likes.remove(user_id)
            else:
                likes.append(user_id)
                user_new_reaction = 'like'
        elif reaction_type == 'dislikes':
            if user_id in likes:
                likes.remove(user_id)
            if user_id in dislikes:
                dislikes.remove(user_id)
            else:
                dislikes.append(user_id)
                user_new_reaction = 'dislike'

        self.comments_collection.update_one(
            {"_id": ObjectId(comment_id)},
            {"$set": {
                "likes": likes,
                "dislikes": dislikes,
                "like_count": len(likes),
                "dislike_count": len(dislikes)
            }}
        )
        
        logger.info(f"User {user_id} reacted to comment {comment_id} with {reaction_type}")
        
        updated_comment = self.comments_collection.find_one({"_id": ObjectId(comment_id)})
        if updated_comment:
            updated_comment['user_reaction'] = user_new_reaction
            return ConfessionComment(**updated_comment)
        return None

    def like_comment(self, comment_id: str, user_id: str) -> Optional[ConfessionComment]:
        return self._react_to_comment(comment_id, user_id, 'likes')

    def dislike_comment(self, comment_id: str, user_id: str) -> Optional[ConfessionComment]:
        return self._react_to_comment(comment_id, user_id, 'dislikes')

    def report_comment(self, comment_id: str, report_data: ReportCreate, user: UserDetails) -> Optional[ConfessionComment]:
        user_id = str(user.id)
        comment = self.comments_collection.find_one({"_id": ObjectId(comment_id)})
        if not comment:
            return None

        if user_id in comment.get("reported_by", []):
            return None

        report = Report(
            content_id=comment_id,
            content_type='comment',
            reported_by_id=user_id,
            reported_by_name=user.Name,
            reason=report_data.reason
        )
        self.reports_collection.insert_one(report.dict(by_alias=True))

        self.comments_collection.update_one(
            {"_id": ObjectId(comment_id)},
            {
                "$inc": {"report_count": 1},
                "$push": {"reported_by": user_id}
            }
        )

        logger.info(f"User {user_id} reported comment {comment_id}")
        updated_comment_doc = self.comments_collection.find_one({"_id": ObjectId(comment_id)})
        if updated_comment_doc:
            return ConfessionComment(**updated_comment_doc)
        return None

    def report_confession(self, confession_id: str, report_data: ReportCreate, user: UserDetails) -> Optional[Confession]:
        user_id = str(user.id)
        confession = self.confessions_collection.find_one({"_id": ObjectId(confession_id)})
        if not confession:
            return None

        if user_id in confession.get("reported_by", []):
            return None

        report = Report(
            content_id=confession_id,
            content_type='confession',
            reported_by_id=user_id,
            reported_by_name=user.Name,
            reason=report_data.reason
        )
        self.reports_collection.insert_one(report.dict(by_alias=True))

        self.confessions_collection.update_one(
            {"_id": ObjectId(confession_id)},
            {
                "$inc": {"report_count": 1},
                "$push": {"reported_by": user_id}
            }
        )

        logger.info(f"User {user_id} reported confession {confession_id}")
        return self.get_confession(confession_id, user_id)
