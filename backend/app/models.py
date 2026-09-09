# app/models.py

from pydantic import BaseModel, EmailStr, Field, validator
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from .postgres import new_id
from .limits import (
    COMMENT_BODY_MAX,
    CONFESSION_BODY_MAX,
    REACTION_MAX,
    RECIPIENT_NAME_MAX,
    REPORT_REASON_MAX,
    USER_BIO_MAX,
    USER_CLASS_NAME_MAX,
    USER_EMOJI_MAX,
    USER_GENDER_MAX,
    USER_NAME_MAX,
    USER_REGNO_MAX,
    USER_ROLE_MAX,
)

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
    body: str = Field(max_length=CONFESSION_BODY_MAX)
    anonymous: bool
    comments_enabled: bool
    recipient_name: str = Field(max_length=RECIPIENT_NAME_MAX)

    @validator("recipient_name")
    def _validate_recipient_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("The name of the person you are confessing to cannot be empty.")
        return normalized

class ConfessionUpdate(BaseModel):
    """
    Model for updating a confession's settings.
    """
    anonymous: Optional[bool] = None
    comments_enabled: Optional[bool] = None

class ReactionCreate(BaseModel):
    reaction: str = Field(max_length=REACTION_MAX)

class CommentCreate(BaseModel):
    body: str = Field(max_length=COMMENT_BODY_MAX)

class ReportCreate(BaseModel):
    """
    Model for creating a report (input from the client).
    """
    reason: str = Field(max_length=REPORT_REASON_MAX)

class UserInfo(BaseModel):
    """
    Represents denormalized user information to be embedded in comments.
    """
    id: str
    name: str
    avatar: Optional[str] = None


class ConfessionComment(BaseModel):
    """
    Represents a comment on a confession post.
    """
    id: str = Field(default_factory=new_id)
    confession_id: str
    user_info: UserInfo
    body: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    
    likes: List[str] = Field(default_factory=list)
    dislikes: List[str] = Field(default_factory=list)
    like_count: int = 0
    dislike_count: int = 0
    
    user_reaction: Optional[str] = None
    
    report_count: int = 0
    reported_by: List[str] = Field(default_factory=list)


    class Config:
        arbitrary_types_allowed = True
        
        populate_by_name = True


class Confession(BaseModel):
    """
    Represents a single confession post within the application.
    """
    id: str = Field(default_factory=new_id)
    body: str
    recipient_name: Optional[str] = None
    anonymous: bool
    comments_enabled: bool
    created_at: Optional[datetime] = None
    author_id: str
    approved: bool = True
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
        
        populate_by_name = True

class AdminUserBase(BaseModel):
    """
    Shared attributes for creating or updating user accounts via the admin APIs.
    """
    regno: str = Field(max_length=USER_REGNO_MAX)
    name: str = Field(max_length=USER_NAME_MAX)
    email: EmailStr
    emoji: Optional[str] = Field(default=None, max_length=USER_EMOJI_MAX)
    bio: Optional[str] = Field(default=None, max_length=USER_BIO_MAX)
    class_name: str = Field(max_length=USER_CLASS_NAME_MAX)
    gender: str = Field(max_length=USER_GENDER_MAX)
    profile_picture_url: Optional[str] = None
    interests: Optional[List[str]] = None
    matchmaking_enabled: bool = True
    notifications_enabled: bool = True
    love_notes_receive_enabled: bool = True
    love_notes_send_enabled: bool = False
    role: str = Field(default="user", max_length=USER_ROLE_MAX)


class AdminUserCreate(AdminUserBase):
    """
    Payload for creating a new user or admin account via the admin APIs.
    """


class AdminUserUpdate(BaseModel):
    """
    Payload for updating existing user accounts via the admin APIs.
    """
    regno: Optional[str] = Field(default=None, max_length=USER_REGNO_MAX)
    name: Optional[str] = Field(default=None, max_length=USER_NAME_MAX)
    email: Optional[EmailStr] = None
    emoji: Optional[str] = Field(default=None, max_length=USER_EMOJI_MAX)
    bio: Optional[str] = Field(default=None, max_length=USER_BIO_MAX)
    class_name: Optional[str] = Field(default=None, max_length=USER_CLASS_NAME_MAX)
    gender: Optional[str] = Field(default=None, max_length=USER_GENDER_MAX)
    profile_picture_url: Optional[str] = None
    interests: Optional[List[str]] = None
    matchmaking_enabled: Optional[bool] = None
    notifications_enabled: Optional[bool] = None
    love_notes_receive_enabled: Optional[bool] = None
    love_notes_send_enabled: Optional[bool] = None
    role: Optional[str] = Field(default=None, max_length=USER_ROLE_MAX)
    is_blocked: Optional[bool] = None


class UserDetails(BaseModel):
    """
    Stores detailed information for a user profile.
    """
    id: str = Field(default_factory=new_id)
    regno: str
    name: str
    email: EmailStr
    emoji: Optional[str] = None
    bio: Optional[str] = None
    class_name: str
    profile_picture_url: Optional[str] = None
    gender: str
    interests: Optional[List[str]] = Field(default_factory=list)
    matchmaking_enabled: bool
    notifications_enabled: bool
    love_notes_receive_enabled: bool = True
    love_notes_send_enabled: bool = False
    is_blocked: bool = False
    reported_count: int = 0
    last_login_at: Optional[datetime] = None
    last_login_ip: Optional[str] = None
    last_login_user_agent: Optional[str] = None
    role: str = "user"
    last_matchmaking_at: Optional[datetime] = None

    class Config:
        arbitrary_types_allowed = True
        
        populate_by_name = True

class LoginToken(BaseModel):
    """
    Represents a securely stored authentication token in the database.
    """
    id: str = Field(default_factory=new_id)
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
    session_status: str = "issued"
    last_seen_at: Optional[datetime] = None
    device: Optional[str] = None

    class Config:
        arbitrary_types_allowed = True
        
        populate_by_name = True

class LoveNoteTemplate(BaseModel):
    """
    Stores predefined templates for Love Notes.
    """
    id: str = Field(default_factory=new_id)
    name: str
    image_url: str
    description: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        arbitrary_types_allowed = True
        
        populate_by_name = True

class LoveNote(BaseModel):
    """
    Represents an individual Love Note sent from one user to another.
    Persistent image URL for the note.
    """
    id: str = Field(default_factory=new_id)
    sender_id: str
    recipient_id: str
    image_url: str
    body: str
    anonymous: bool = False
    status: str = "pending_review"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    read_at: Optional[datetime] = None

    class Config:
        arbitrary_types_allowed = True
        
        populate_by_name = True


class Match(BaseModel):
    """
    Represents a match between two users from matchmaking.
    """
    id: str = Field(default_factory=new_id)
    user_1_regno: str
    user_2_regno: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime

    class Config:
        arbitrary_types_allowed = True
        
        populate_by_name = True

class Conversation(BaseModel):
    """
    Represents a conversation request and its state between two matched users.
    """
    id: str = Field(default_factory=new_id)
    match_id: str
    initiator_regno: str
    receiver_regno: str
    status: str = "pending"  # Can be 'pending' (not requested yet), 'requested' (waiting for response), 'accepted', 'rejected'
    requested_at: Optional[datetime] = None  # When initiator clicked "Send Message Request"
    accepted_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        arbitrary_types_allowed = True
        
        populate_by_name = True

class Message(BaseModel):
    """
    Defines the structure for a single message within a conversation.
    """
    id: str = Field(default_factory=new_id)
    conversation_id: str
    sender_regno: str
    receiver_regno: str
    body: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_read: bool = False

    class Config:
        arbitrary_types_allowed = True
        
        populate_by_name = True

class ConversationCreate(BaseModel):
    """
    Model for creating a conversation request.
    """
    match_id: str


class Notification(BaseModel):
    """
    Represents a notification for a user.
    """
    id: str = Field(default_factory=new_id)
    recipient_regno: str
    heading: str
    body: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_read: bool = False

    class Config:
        arbitrary_types_allowed = True
        
        populate_by_name = True


# Update forward references to allow nested Pydantic models
Confession.update_forward_refs()
