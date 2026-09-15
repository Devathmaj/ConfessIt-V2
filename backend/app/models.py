# app/models.py

from pydantic import BaseModel, EmailStr, Field, computed_field, field_validator, model_validator, validator
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
)
from .accounts import AccountType, capabilities_for, normalize_account_type

class Token(BaseModel):
    """
    Pydantic model for the JWT access token response sent to the client.
    """
    access_token: str
    token_type: str

class ConfessionCreate(BaseModel):
    """
    Model for creating a confession (input from the client).

    ``recipient_name`` is optional: most confessions are addressed to someone, but a
    confession is also something a user simply wants to put down. So a blank or absent
    recipient is stored as NULL rather than rejected -- the client is not asked to invent
    a name to satisfy a constraint that has no product meaning.
    """
    body: str = Field(max_length=CONFESSION_BODY_MAX)
    anonymous: bool
    comments_enabled: bool
    recipient_name: Optional[str] = Field(default=None, max_length=RECIPIENT_NAME_MAX)

    @validator("recipient_name")
    def _validate_recipient_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        # A field the client left blank and a field it left absent mean the same thing,
        # so both land in the column as NULL rather than as "".
        normalized = value.strip()
        return normalized or None

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

class AdminUserCreate(BaseModel):
    """Payload for creating an account in any of the three account tables.

    ``account_type`` is the table the row is written to, and it is the only way an account
    becomes staff: there is no ``role`` to set, so a payload cannot half-create a staff
    account by naming a privilege instead of a table.

    The participant-only fields are optional here and required by the validator below only
    when ``account_type`` is ``users``. Declaring them conditionally in the type system is
    not something Pydantic can express without a discriminated union, and a union would
    make the OpenAPI schema unreadable for a payload that is, in practice, one form.

    ``email`` sits with the shared fields rather than the participant-only ones, because every
    account can have one and it is required only of a participant.
    """
    account_type: AccountType = AccountType.USERS
    regno: str = Field(max_length=USER_REGNO_MAX)
    name: str = Field(max_length=USER_NAME_MAX)
    email: Optional[EmailStr] = None
    profile_picture_url: Optional[str] = None
    gender: str = Field(max_length=USER_GENDER_MAX)
    notifications_enabled: bool = True
    is_blocked: bool = False
    # participants only
    emoji: Optional[str] = Field(default=None, max_length=USER_EMOJI_MAX)
    bio: Optional[str] = Field(default=None, max_length=USER_BIO_MAX)
    class_name: Optional[str] = Field(default=None, max_length=USER_CLASS_NAME_MAX)
    interests: Optional[List[str]] = None
    matchmaking_enabled: bool = True
    love_notes_receive_enabled: bool = True
    love_notes_send_enabled: bool = False

    @model_validator(mode="after")
    def _require_participant_fields(self) -> "AdminUserCreate":
        if self.account_type is not AccountType.USERS:
            return self
        missing = [
            name
            for name in ("email", "class_name")
            if getattr(self, name) in (None, "")
        ]
        if missing:
            raise ValueError(
                f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} "
                f"required for a participant account."
            )
        return self


class AdminUserUpdate(BaseModel):
    """Payload for editing an account that already exists.

    Every field is optional, and the account is found by id across all three tables, so one
    payload shape edits a participant or a staff account.

    There is deliberately no ``account_type`` here. Moving a row between tables is not a
    field edit: eleven other tables hold foreign keys into ``users`` by both id and regno, so
    relocating a row would either orphan them or require rewriting every reference under the
    hood. Which table an account lives in is therefore fixed when it is created; minting a
    staff account is a create, and demoting one is a matter of not using it.
    """
    regno: Optional[str] = Field(default=None, max_length=USER_REGNO_MAX)
    name: Optional[str] = Field(max_length=USER_NAME_MAX)
    profile_picture_url: Optional[str] = None
    gender: Optional[str] = Field(default=None, max_length=USER_GENDER_MAX)
    notifications_enabled: Optional[bool] = None
    is_blocked: Optional[bool] = None
    email: Optional[EmailStr] = None
    # participants only
    emoji: Optional[str] = Field(default=None, max_length=USER_EMOJI_MAX)
    bio: Optional[str] = Field(default=None, max_length=USER_BIO_MAX)
    class_name: Optional[str] = Field(default=None, max_length=USER_CLASS_NAME_MAX)
    interests: Optional[List[str]] = None
    matchmaking_enabled: Optional[bool] = None
    love_notes_receive_enabled: Optional[bool] = None
    love_notes_send_enabled: Optional[bool] = None

    @field_validator("name", "regno", "profile_picture_url", "gender", "class_name", "email")
    @classmethod
    def _blank_is_not_a_value(cls, value):
        """Treat an empty string as "not supplied" rather than as a value to write.

        A form that submits every field sends ``""`` for the ones the admin left untouched.
        Without this, editing a staff account's name through the participant-shaped form
        would blank out fields that were never offered on that form at all.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value


class AccountDetails(BaseModel):
    """The fields every account has, whatever table it lives in.

    This is the shape every authenticated caller is resolved to, because the three tables
    share exactly these columns and nothing more. Participant-only fields are deliberately
    absent rather than null: an admin has no ``class_name``, and returning ``null`` for it
    would describe a value the row does not have.

    ``email`` is shared and optional rather than participant-only and required. Every account
    can be contacted, so a staff account that could not hold an address would be an account
    nobody could reach. It is not required of a staff account, because creating one is a
    deliberate act by someone who already knows the regno, and nothing in sign-in needs it.
    """
    id: str = Field(default_factory=new_id)
    regno: str
    name: str
    email: Optional[EmailStr] = None
    profile_picture_url: Optional[str] = None
    gender: str
    notifications_enabled: bool = True
    is_blocked: bool = False
    last_login_at: Optional[datetime] = None
    last_login_ip: Optional[str] = None
    last_login_user_agent: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # The account table this row was read from. Not a privilege and not a label: the
    # privilege answer is ``capabilities`` below, derived from it.
    account_type: str = AccountType.USERS.value

    class Config:
        arbitrary_types_allowed = True

        populate_by_name = True

    @field_validator("account_type", mode="before")
    @classmethod
    def _normalize_stored_account_type(cls, value: Any) -> str:
        """Resolve the account table read out of the database to a defined one.

        Reduced here so every consumer of an account payload -- the ``/auth/me`` response,
        the conversation payloads, the profile response -- sees one of the three tables and
        cannot be handed a value that matches no branch. Unrecognised values become
        ``users``: an account keeps working, it just keeps no privileges it was never
        validly granted.
        """
        return normalize_account_type(value)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def capabilities(self) -> List[str]:
        """The capabilities this account's table grants, as sent to the client.

        Derived on read rather than stored so it cannot drift from ``account_type``. The
        frontend uses this to decide which moderation screens to render, which keeps the
        browser's idea of what is allowed in step with the server's without the client
        holding a second, separately-maintained copy of the privilege table. It is a
        display concern only: the server re-checks every one of these against the database
        on every request. Sorted so the value is stable and diffable.
        """
        return sorted(capabilities_for(self.account_type))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_staff(self) -> bool:
        """Whether this account is staff rather than a participant.

        Sent so the client can hide participant-only screens without re-deriving the rule.
        The server gates those routes on the account table itself; this is the same answer,
        for display.
        """
        return bool(capabilities_for(self.account_type)) and self.account_type != AccountType.USERS.value


class UserDetails(AccountDetails):
    """A participant account: everything in ``AccountDetails`` plus the participant fields.

    Participant routes resolve the caller to *this* model, via a dependency that refuses any
    account not in the ``users`` table. That is the whole point of the split: a staff
    account cannot reach a participant feature not because a role string said so, but
    because there is no row for it in the table the feature reads.

    ``email`` is narrowed from the shared optional to required: a participant is always
    reachable, and the create payload enforces it.
    """
    email: EmailStr
    emoji: Optional[str] = None
    bio: Optional[str] = None
    class_name: str
    interests: Optional[List[str]] = Field(default_factory=list)
    matchmaking_enabled: bool = True
    love_notes_receive_enabled: bool = True
    love_notes_send_enabled: bool = False
    last_matchmaking_at: Optional[datetime] = None


class LoginToken(BaseModel):
    """
    Represents a securely stored authentication token in the database.
    """
    id: str = Field(default_factory=new_id)
    account_id: str
    account_type: str = AccountType.USERS.value
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
