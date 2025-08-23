# app/models.py

import logging
from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List, Dict, Any
from datetime import datetime
from bson import ObjectId

# Custom ObjectId class with a Pydantic validator
class PyObjectId(ObjectId):
    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, v, _):
        """
        Used to validate that the provided value is a valid ObjectId.
        It handles cases where the value is already an ObjectId or a string.
        """
        if isinstance(v, ObjectId):
            return v
        if not ObjectId.is_valid(v):
            raise ValueError("Invalid objectid")
        return ObjectId(v)

    @classmethod
    def __get_pydantic_json_schema__(cls, field_schema):
        field_schema.update(type="string")

class Token(BaseModel):
    """
    Pydantic model for the JWT access token response sent to the client.
    """
    access_token: str
    token_type: str

class ConfessionCreate(BaseModel):
    """
    Model for creating a confession (input from the client).
    """
    confession: str
    is_anonymous: bool
    is_comment: bool

class ReactionCreate(BaseModel):
    reaction: str

class CommentCreate(BaseModel):
    message: str

class UserInfo(BaseModel):
    """
    Represents denormalized user information to be embedded in comments.
    """
    id: str
    username: str
    avatar: Optional[str] = None


class ConfessionComment(BaseModel):
    """
    Represents a comment on a confession post.
    """
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    confession_id: str
    user_info: UserInfo
    message: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    
    likes: List[str] = Field(default_factory=list)
    dislikes: List[str] = Field(default_factory=list)
    like_count: int = 0
    dislike_count: int = 0
    
    user_reaction: Optional[str] = None

    class Config:
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}
        populate_by_name = True


class Confession(BaseModel):
    """
    Represents a single confession post within the application.
    """
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    confession: str
    is_anonymous: bool
    is_comment: bool
    timestamp: Optional[datetime] = None
    user_id: str
    times_reported: Optional[int] = 0
    comments: List[ConfessionComment] = Field(default_factory=list)

    reactions: Dict[str, List[str]] = Field(default_factory=dict)

    heart_count: int = 0
    haha_count: int = 0
    whoa_count: int = 0
    heartbreak_count: int = 0
    comment_count: int = 0
    
    user_reaction: Optional[str] = None

    class Config:
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}
        populate_by_name = True


class UserDetails(BaseModel):
    """
    Stores detailed information for a user profile.
    """
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    Regno: str
    Name: str
    email: EmailStr
    username: Optional[str] = None
    emoji: Optional[str] = None
    bio: Optional[str] = None
    which_class: str
    profile_picture_id: str
    gender: str
    interests: Optional[List[str]] = []
    isMatchmaking: bool
    isNotifications: bool
    isLovenotesRecieve: bool = True
    isLovenotesSend: bool = False
    reported_count: int = 0
    last_login_time: Optional[datetime] = None
    last_login_ip: Optional[str] = None
    user_role: str = "user" 

    class Config:
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}
        populate_by_name = True

class LoginToken(BaseModel):
    """
    Represents a securely stored authentication token in the database.
    """
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    user_id: str
    token_hash: str
    issued_at: datetime
    expires_at: datetime
    consumed_at: Optional[datetime] = None
    used: bool = False
    revoked: bool = False
    request_ip: str
    request_user_agent: str
    consume_ip: Optional[str] = None
    consume_user_agent: Optional[str] = None
    attempt_count: int = 0
    metadata: Optional[Dict[str, Any]] = None

    class Config:
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}
        populate_by_name = True

class MatchRequest(BaseModel):
    """
    Models a matchmaking request, capturing the initial interaction between two users.
    """
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    from_user: PyObjectId
    to_user: PyObjectId
    message: str
    status: str = "pending"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    decision_at: Optional[datetime] = None
    conversation_id: Optional[PyObjectId] = None

    class Config:
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}
        populate_by_name = True

class Conversation(BaseModel):
    """
    Represents a private conversation between two matched users.
    """
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    participants: List[PyObjectId]
    status: str = "active"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    last_message: Optional[dict] = None

    class Config:
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}
        populate_by_name = True

class Message(BaseModel):
    """
    Defines the structure for a single message within a conversation.
    """
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    conversation_id: PyObjectId
    sender: PyObjectId
    text: str
    sent_at: datetime = Field(default_factory=datetime.utcnow)
    read_by: List[PyObjectId] = []

    class Config:
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}
        populate_by_name = True

class LoveNoteTemplate(BaseModel):
    """
    Stores predefined templates for Love Notes.
    """
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    name: str
    preview_image: str
    description: str
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}
        populate_by_name = True

class LoveNote(BaseModel):
    """
    Represents an individual Love Note sent from one user to another.
    """
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    sender_id: PyObjectId
    recipient_id: PyObjectId
    image_base64: str
    message_text: str
    is_anonymous: bool = False
    status: str = "pending_review"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    read_at: Optional[datetime] = None

    class Config:
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}
        populate_by_name = True


# Update forward references to allow nested Pydantic models
Confession.update_forward_refs()