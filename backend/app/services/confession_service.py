from ..logger import get_logger
from pymongo import MongoClient, DESCENDING
from ..models import Confession, ConfessionComment, ConfessionCreate, CommentCreate, UserDetails, UserInfo
from bson.objectid import ObjectId
import datetime
from ..config import settings
from typing import Optional, List

logger = get_logger(__name__)

class ConfessionService:
    def __init__(self):
        self.client = MongoClient(settings.MONGO_URI)
        self.db = self.client[settings.MONGO_DB_NAME]
        self.confessions_collection = self.db["Confessions"]
        self.comments_collection = self.db["ConfessionComments"]
        self.users_collection = self.db["UserDetails"]
        self.REACTION_TYPES = ["heart", "haha", "whoa", "heartbreak"]

    def _get_user_info(self, user_id: str) -> Optional[UserInfo]:
        user_doc = self.users_collection.find_one({"_id": ObjectId(user_id)})
        if user_doc:
            # FIXED: Use the 'Name' field for the comment author's display name.
            display_name = user_doc.get("Name", "Anonymous User")
            return UserInfo(
                id=str(user_doc["_id"]),
                username=display_name,
                avatar=user_doc.get("emoji")
            )
        return None

    def create_confession(self, confession_data: ConfessionCreate, user_id: str):
        confession_dict = confession_data.dict()
        confession_dict["_id"] = ObjectId()
        confession_dict["user_id"] = user_id 
        confession_dict["timestamp"] = datetime.datetime.utcnow()
        confession_dict["reactions"] = {reaction: [] for reaction in self.REACTION_TYPES}
        for reaction_type in self.REACTION_TYPES:
            confession_dict[f"{reaction_type}_count"] = 0
        confession_dict["times_reported"] = 0
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
                "$lookup": {
                    "from": "UserDetails",
                    "localField": "user_id",
                    "foreignField": "_id",
                    "as": "user_info_docs"
                }
            },
            {
                "$addFields": {
                    "user_info": { "$arrayElemAt": ["$user_info_docs", 0] }
                }
            }
        ]

        # Sorting logic
        if sort_by == 'popularity':
            pipeline.append({"$sort": {"total_reactions": -1, "timestamp": 1}})
        elif sort_by == 'time':
            pipeline.append({"$sort": {"timestamp": -1, "user_info.Name": 1}})
        elif sort_by == 'comments':
            pipeline.append({"$sort": {"comment_count": -1, "timestamp": 1}})
        else: # Default sorting
            pipeline.append({"$sort": {"timestamp": -1}})

        confessions_cursor = self.confessions_collection.aggregate(pipeline)
        
        confessions = []
        for confession_doc in confessions_cursor:
            confession_doc['id'] = str(confession_doc['_id'])
            
            # Process comments
            comments_list = []
            for comment_doc in sorted(confession_doc.get('comments', []), key=lambda c: c['timestamp'], reverse=True):
                comment_doc['id'] = str(comment_doc['_id'])
                user_comment_reaction = None
                if user_id:
                    if user_id in comment_doc.get('likes', []):
                        user_comment_reaction = 'like'
                    elif user_id in comment_doc.get('dislikes', []):
                        user_comment_reaction = 'dislike'
                comment_doc['user_reaction'] = user_comment_reaction
                comments_list.append(ConfessionComment(**comment_doc))
            confession_doc['comments'] = comments_list

            # Determine user reaction for the confession
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
            confession_doc['id'] = str(confession_doc['_id'])
            
            user_reaction = None
            if user_id and 'reactions' in confession_doc:
                for reaction_type, user_ids in confession_doc['reactions'].items():
                    if user_id in user_ids:
                        user_reaction = reaction_type
                        break
            
            confession_doc['user_reaction'] = user_reaction
            return Confession(**confession_doc)
        return None

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

    def add_comment_to_confession(self, confession_id: str, comment_data: CommentCreate, user: UserDetails) -> Optional[ConfessionComment]:
        user_id = str(user.id)
        existing_comments_count = self.comments_collection.count_documents({
            "confession_id": confession_id,
            "user_info.id": user_id
        })
        if existing_comments_count >= 3:
            logger.warning(f"User {user_id} tried to post more than 3 comments on confession {confession_id}")
            return None

        # FIXED: Use the user's full name for the comment display name.
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
            "dislike_count": 0
        }

        result = self.comments_collection.insert_one(comment_dict)
        logger.info(f"Added comment {result.inserted_id} to confession {confession_id}")
        
        # Increment the comment count for the confession
        self.confessions_collection.update_one(
            {"_id": ObjectId(confession_id)},
            {"$inc": {"comment_count": 1}}
        )

        new_comment = self.comments_collection.find_one({"_id": result.inserted_id})
        if new_comment:
            new_comment['id'] = str(new_comment['_id'])
            return ConfessionComment(**new_comment)
        return None

    def _react_to_comment(self, comment_id: str, user_id: str, reaction_type: str) -> Optional[ConfessionComment]:
        comment = self.comments_collection.find_one({"_id": ObjectId(comment_id)})
        if not comment:
            return None

        likes = comment.get('likes', [])
        dislikes = comment.get('dislikes', [])

        # Determine the user's new reaction state
        user_new_reaction = None
        
        # If user is reacting, remove potential opposite reaction
        if reaction_type == 'likes':
            if user_id in dislikes:
                dislikes.remove(user_id)
            if user_id in likes:
                likes.remove(user_id) # Toggle off
            else:
                likes.append(user_id) # Toggle on
                user_new_reaction = 'like'
        elif reaction_type == 'dislikes':
            if user_id in likes:
                likes.remove(user_id)
            if user_id in dislikes:
                dislikes.remove(user_id) # Toggle off
            else:
                dislikes.append(user_id) # Toggle on
                user_new_reaction = 'dislike'

        # Update the database
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
        
        # Fetch the updated comment to ensure consistency
        updated_comment = self.comments_collection.find_one({"_id": ObjectId(comment_id)})
        if updated_comment:
            updated_comment['id'] = str(updated_comment['_id'])
            # **THE FIX**: Explicitly set the user_reaction field for the response.
            updated_comment['user_reaction'] = user_new_reaction
            return ConfessionComment(**updated_comment)
        return None

    def like_comment(self, comment_id: str, user_id: str) -> Optional[ConfessionComment]:
        return self._react_to_comment(comment_id, user_id, 'likes')

    def dislike_comment(self, comment_id: str, user_id: str) -> Optional[ConfessionComment]:
        return self._react_to_comment(comment_id, user_id, 'dislikes')