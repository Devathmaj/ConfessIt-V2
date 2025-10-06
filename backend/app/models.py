# app/models.py

import logging
from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from bson import ObjectId

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

class ConfessionUpdate(BaseModel):
    """
    Model for updating a confession's settings.
    """
    is_anonymous: Optional[bool] = None
    is_comment: Optional[bool] = None

class ReactionCreate(BaseModel):
    reaction: str

class CommentCreate(BaseModel):
    message: str

class ReportCreate(BaseModel):
    """
    Model for creating a report (input from the client).
    """
    reason: str

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
    id: ObjectId = Field(default_factory=ObjectId, alias="_id")
    confession_id: str
    user_info: UserInfo
    message: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    
    likes: List[str] = Field(default_factory=list)
    dislikes: List[str] = Field(default_factory=list)
    like_count: int = 0
    dislike_count: int = 0
    
    user_reaction: Optional[str] = None
    
    report_count: int = 0
    reported_by: List[str] = Field(default_factory=list)


    class Config:
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}
        populate_by_name = True


class Confession(BaseModel):
    """
    Represents a single confession post within the application.
    """
    id: ObjectId = Field(default_factory=ObjectId, alias="_id")
    confession: str
    is_anonymous: bool
    is_comment: bool
    timestamp: Optional[datetime] = None
    user_id: str
    user_info: Optional[UserInfo] = None
    report_count: int = 0
    reported_by: List[str] = Field(default_factory=list)
    reactions: Dict[str, List[str]] = Field(default_factory=dict)
    
    comments: List[ConfessionComment] = Field(default_factory=list)

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

class Report(BaseModel):
    """
    Represents a report made by a user for a comment or a confession.
    """
    id: ObjectId = Field(default_factory=ObjectId, alias="_id")
    content_id: str
    content_type: str # 'comment' or 'confession'
    reported_by_id: str
    reported_by_name: str
    reason: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}
        populate_by_name = True


class UserDetails(BaseModel):
    """
    Stores detailed information for a user profile.
    """
    id: ObjectId = Field(default_factory=ObjectId, alias="_id")
    Regno: str
    Name: str
    email: EmailStr
    username: Optional[str] = None
    emoji: Optional[str] = None
    bio: Optional[str] = None
    which_class: str
    profile_picture_id: Optional[str] = None
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
    last_matchmaking_time: Optional[datetime] = None

    class Config:
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}
        populate_by_name = True

class LoginToken(BaseModel):
    """
    Represents a securely stored authentication token in the database.
    """
    id: ObjectId = Field(default_factory=ObjectId, alias="_id")
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

class LoveNoteTemplate(BaseModel):
    """
    Stores predefined templates for Love Notes.
    """
    id: ObjectId = Field(default_factory=ObjectId, alias="_id")
    name: str
    preview_image: str
    description: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}
        populate_by_name = True

class LoveNote(BaseModel):
    """
    Represents an individual Love Note sent from one user to another.
    Note: image_base64 field now stores the Cloudinary URL (not actual base64).
    """
    id: ObjectId = Field(default_factory=ObjectId, alias="_id")
    sender_id: ObjectId
    recipient_id: ObjectId
    image_base64: str  # Stores Cloudinary URL (legacy field name kept for compatibility)
    message_text: str
    is_anonymous: bool = False
    status: str = "pending_review"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    read_at: Optional[datetime] = None

    class Config:
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}
        populate_by_name = True


class Match(BaseModel):
    """
    Represents a match between two users from matchmaking.
    """
    id: ObjectId = Field(default_factory=ObjectId, alias="_id")
    user_1_regno: str
    user_2_regno: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime

    class Config:
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}
        populate_by_name = True

class Conversation(BaseModel):
    """
    Represents a conversation request and its state between two matched users.
    """
    id: ObjectId = Field(default_factory=ObjectId, alias="_id")
    matchId: ObjectId
    initiatorId: str
    receiverId: str
    status: str = "pending"  # Can be 'pending', 'accepted', 'rejected'
    acceptedAt: Optional[datetime] = None
    createdAt: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}
        populate_by_name = True

class Message(BaseModel):
    """
    Defines the structure for a single message within a conversation.
    """
    id: ObjectId = Field(default_factory=ObjectId, alias="_id")
    matchId: ObjectId
    senderId: str
    receiverId: str
    text: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        arbitrary_types_allowed = True
        json_encoders = {ObjectId: str}
        populate_by_name = True

class ConversationCreate(BaseModel):
    """
    Model for creating a conversation request.
    """
    matchId: str


# Update forward references to allow nested Pydantic models
Confession.update_forward_refs()
