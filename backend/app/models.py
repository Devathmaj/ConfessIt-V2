# app/models.py

from pydantic import BaseModel, ConfigDict, EmailStr, Field, computed_field, field_validator, model_validator
from typing import Annotated, Literal, Optional, List, Dict, Any
from datetime import date, datetime, timezone
from .postgres import new_id
from .limits import (
    ANAGRAM_MAX_ATTEMPTS,
    COMMENT_BODY_MAX,
    CONFESSION_BODY_MAX,
    INTEREST_MAX_SELECTED,
    REACTION_MAX,
    RECIPIENT_NAME_MAX,
    REPORT_REASON_MAX,
    USER_BIO_MAX,
    USER_DEPARTMENT_MAX,
    USER_GENDER_VALUES,
    USER_NAME_MAX,
    USER_REGNO_MAX,
    USER_YEAR_MAX,
    USER_YEAR_MIN,
    WORDLE_ANSWER_LENGTH,
    WORDLE_MAX_ATTEMPTS,
)
from .accounts import AccountType, capabilities_for, normalize_account_type

# Closed ``Literal`` so the OpenAPI schema is an enum and an out-of-set value is a 422 at
# validation. Mirrors ``limits.USER_GENDER_VALUES`` (CHECK constraint + frontend Select):
# all three must agree.
Gender = Literal["male", "female", "other"]

# Bounded ``int`` (1-12 covers Class 1-12) rather than a name ``Literal``, so a school
# deployment needs no re-extension in three mirrored places; ``ge``/``le`` errors name the
# field and the offending value. Not a string, and not open like ``department``: matchmaking
# groups by year, so "2" and "02" would be two cohorts that never meet.
Year = Annotated[int, Field(ge=USER_YEAR_MIN, le=USER_YEAR_MAX)]

# An enum on the array's members rather than a membership test, so "not one of ours" is a
# validation error naming the offending value.
Interest = Literal[
    "Music", "Movies", "Reading", "Gaming", "Traveling", "Cooking",
    "Sports", "Art", "Photography", "Dancing", "Writing", "Coding",
]

class Cohort(BaseModel):
    """Structured ``(year, department)`` cohort returned by ``/love-notes/cohorts``."""
    year: int
    department: str

class Token(BaseModel):
    """The JWT access token response sent to the client."""
    access_token: str
    token_type: str


def _is_non_character(codepoint: int) -> bool:
    """Unicode non-characters: reserved forever, never assigned a meaning.

    Both families: U+FDD0-U+FDEF contiguous, and the last two code points of every plane --
    testing only the BMP pair would accept text a strict reader may replace with U+FFFD.
    """
    if 0xFDD0 <= codepoint <= 0xFDEF:
        return True
    return (codepoint & 0xFFFF) >= 0xFFFE


def _reject_unchangeable_characters(value: Optional[str], field_name: str) -> Optional[str]:
    """Reject text that cannot survive the trip to the database; the length cap covers only half of "storable".

    NUL is rejected by PostgreSQL outright (driver error, not a validation message); unpaired
    surrogates fail UTF-8 encoding at bind time; other C0/DEL/C1 controls feed escape/injection
    payloads. Emoji and non-Latin scripts pass on purpose (not a moderation question here).
    """
    if value is None:
        return None
    for character in value:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise ValueError(f"{field_name} contains an unpaired surrogate character.")
        if codepoint == 0 or (codepoint < 0x20 and character not in "\t\n\r"):
            raise ValueError(f"{field_name} contains a control character.")
        if codepoint == 0x7F or 0x80 <= codepoint <= 0x9F:
            raise ValueError(f"{field_name} contains a control character.")
        if _is_non_character(codepoint):
            raise ValueError(f"{field_name} contains a non-character code point.")
    return value


def clean_interests(value: Optional[List[Interest]]) -> Optional[List[Interest]]:
    """Normalise a submitted interest list, or refuse it; shared by every intake payload so one rule cannot drift.

    Repeats collapse first-occurrence-wins (kept order matches the vocabulary) and the cap
    -- equal to the vocabulary size -- exists to catch duplicate-spam lists, never a real
    selection. ``None`` passes through so callers can tell "none supplied" from "empty set".
    """
    if value is None:
        return None
    collapsed: list[Interest] = []
    for item in value:
        if item not in collapsed:
            collapsed.append(item)
    if len(collapsed) > INTEREST_MAX_SELECTED:
        raise ValueError(f"At most {INTEREST_MAX_SELECTED} interests may be selected.")
    return collapsed


class ConfessionCreate(BaseModel):
    """
    Model for creating a confession (input from the client).

    A blank/absent ``recipient_name`` is stored as NULL rather than rejected -- the client
    is not asked to invent a name with no product meaning.
    """
    body: str = Field(max_length=CONFESSION_BODY_MAX)
    anonymous: bool
    comments_enabled: bool
    recipient_name: Optional[str] = Field(default=None, max_length=RECIPIENT_NAME_MAX)

    @field_validator("recipient_name", mode="after")
    @classmethod
    def _validate_recipient_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        _reject_unchangeable_characters(value, "recipient_name")
        # Blank and absent mean the same thing: both land in the column as NULL, not "".
        normalized = value.strip()
        return normalized or None

    @field_validator("body", mode="after")
    @classmethod
    def _validate_body_characters(cls, value: str) -> str:
        return _reject_unchangeable_characters(value, "body")

class ConfessionUpdate(BaseModel):
    """
    Model for updating a confession's settings.

    Both fields optional (a patch names only what it changes); an explicit null is refused,
    not treated as an omission -- see the validator.
    """

    anonymous: Optional[bool] = None
    comments_enabled: Optional[bool] = None

    @model_validator(mode="after")
    def _reject_explicit_nulls(self) -> "ConfessionUpdate":
        """Refuse a field sent as null, as distinct from one left out (the service reads ``exclude_unset``).

        No safe default exists -- the two defaults are opposites and one is a privacy call.
        Historical misfires: ``comments_enabled: null`` wrote into the NOT NULL column (500);
        ``anonymous: null`` read as "not anonymous" (``not None`` is ``True``), de-anonymising a post.
        """
        for field in ("anonymous", "comments_enabled"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(
                    f"{field} must be true or false; leave the field out to keep it unchanged."
                )
        return self

class ReactionCreate(BaseModel):
    reaction: str = Field(max_length=REACTION_MAX)

class CommentCreate(BaseModel):
    body: str = Field(max_length=COMMENT_BODY_MAX)

class ReportCreate(BaseModel):
    """Model for creating a report (input from the client)."""
    reason: str = Field(max_length=REPORT_REASON_MAX)


class SemanticSearchRequest(BaseModel):
    """Natural-language semantic search over confessions (text-embedding-3-small + pgvector cosine distance).

    ``limit`` is bounded so one request cannot materialize the whole corpus; visibility and
    moderation filters are enforced in SQL, never trusted to the caller.
    """

    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=10, ge=1, le=50)

    @field_validator("query", mode="after")
    @classmethod
    def _validate_query(cls, value: str) -> str:
        normalized = " ".join((value or "").split())
        if not normalized:
            raise ValueError("Search query must not be blank.")
        return normalized


class SemanticSearchResponse(BaseModel):
    """Ranked confessions for a semantic query, in existing public shape.

    Legacy shape kept so older clients and tests importing this name do not break; the live
    endpoint answers :class:`HybridSearchResponse`.
    """

    confessions: List["Confession"] = Field(default_factory=list)
    total: int = 0
    query: str = ""
    limit: int = 10

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)


class KeywordSearchSection(BaseModel):
    """Keyword half of a hybrid answer: trigram matches with their total."""

    items: List["Confession"] = Field(default_factory=list)
    total: int = 0

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)


class SemanticSearchSection(BaseModel):
    """Semantic half of a hybrid answer: vector matches with a status.

    ``status`` is ``ok`` when the embedding pipeline ran, else the reason the half is empty:
    ``disabled`` / ``unavailable`` / ``rate_limited`` / ``skipped_short`` (query too short to embed).
    """

    items: List["Confession"] = Field(default_factory=list)
    status: str = "ok"

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)


class HybridSearchResponse(BaseModel):
    """One search box, both signals: keyword first, semantic underneath.

    Keyword items never reappear in semantic (deduped); both lists carry the public
    confession shape; semantic degradation never fails the keyword half.
    """

    query: str = ""
    limit: int = 10
    keyword: KeywordSearchSection = Field(default_factory=KeywordSearchSection)
    semantic: SemanticSearchSection = Field(default_factory=SemanticSearchSection)

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

class UserInfo(BaseModel):
    """
    Denormalized user information embedded in comments and posts.

    For an anonymous confession the server emits a fixed non-identifying placeholder
    (``id="anonymous"``, ``name="Anonymous"``, no photo) -- the fields never carry the
    identity, so the client is not what protects it.
    """
    id: str
    name: str
    # Derived on read from the stored Cloudinary identity, never stored; always None for
    # an anonymous confession, alongside its placeholder name and id.
    profile_picture_url: Optional[str] = None


class ConfessionComment(BaseModel):
    """
    A comment on a confession post.

    Participant-scoped: ``likes``/``dislikes``/``reported_by`` are always empty on participant
    reads -- the caller's own state travels as ``user_reaction``/``has_reported`` plus the
    public counts, so no participant can enumerate who else voted or reported; staff surfaces
    carry the full lists under the same field names.
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
    has_reported: bool = False


    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)


class Confession(BaseModel):
    """
    A single confession post.

    Participant-scoped: ``reactions``/``reported_by`` are always empty on participant reads
    -- own state travels as ``user_reaction``/``has_reported`` plus public counts; staff
    surfaces carry the full lists under the same field names.
    """
    id: str = Field(default_factory=new_id)
    body: str
    recipient_name: Optional[str] = None
    anonymous: bool
    comments_enabled: bool
    created_at: Optional[datetime] = None
    # None for an anonymous confession; participant responses carry ``is_mine`` so the
    # client never needs the author's id.
    author_id: Optional[str] = None
    approved: bool = True
    user_info: Optional[UserInfo] = None
    # Whether the caller wrote this post: computed server-side, never a client-supplied id.
    is_mine: bool = False
    report_count: int = 0
    reported_by: List[str] = Field(default_factory=list)
    has_reported: bool = False
    reactions: Dict[str, List[str]] = Field(default_factory=dict)

    comments: List[ConfessionComment] = Field(default_factory=list)

    heart_count: int = 0
    haha_count: int = 0
    whoa_count: int = 0
    heartbreak_count: int = 0
    comment_count: int = 0

    user_reaction: Optional[str] = None

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

class AdminUserCreate(BaseModel):
    """Payload for creating an account in any of the three account tables.

    ``account_type`` names the table written to and is the only way an account becomes staff;
    ``email`` is required of every type (a staff row nobody can contact is unrecoverable);
    ``gender``/``year``/``department`` are validator-required for ``users`` only. Bio/interests/
    flags/``is_blocked`` are deliberately absent, refused by name via ``extra="forbid"``, filled
    in by the account itself (``PUT /profile/update``) from the ``UserRecord`` defaults.
    """
    model_config = ConfigDict(extra="forbid")

    account_type: AccountType = AccountType.USERS
    regno: str = Field(max_length=USER_REGNO_MAX)
    name: str = Field(max_length=USER_NAME_MAX)
    email: EmailStr
    # participants only
    gender: Optional[Gender] = None
    year: Optional[Year] = None
    department: Optional[str] = Field(default=None, max_length=USER_DEPARTMENT_MAX)

    @model_validator(mode="after")
    def _require_participant_fields(self) -> "AdminUserCreate":
        if self.account_type is not AccountType.USERS:
            return self
        # NOT NULL on ``users`` and closed to three values; unset would be a 500 from the
        # insert rather than a validation message.
        if not self.gender:
            raise ValueError("gender is required for a participant account.")
        # Split halves of the old single ``class_name`` (two spellings of one cohort were
        # two cohorts); both NOT NULL, so a half left unset is a 500 from the insert.
        if not self.year:
            raise ValueError("year is required for a participant account.")
        if not self.department:
            raise ValueError("department is required for a participant account.")
        return self


class ConfessionCooldown(BaseModel):
    """How long this participant has to wait before their next confession.

    ``status`` shares the ``cooldown``/``eligible`` vocabulary of ``GET /matchmaking/check``
    so a client reading either answer checks one field. Seconds travel beside the formatted
    sentence: a countdown cannot subtract "1 hour and 58 minutes" from a re-rounded string.
    """

    status: str
    message: str
    cooldown_hours: int
    remaining_minutes: int = 0
    remaining_seconds: int = 0


class AdminUserUpdate(BaseModel):
    """Editing an account found by id across all three tables, so one shape edits participant or staff.

    Deliberately no ``account_type``: eleven other tables hold FKs into ``users`` by id and
    regno, so relocating a row would orphan them -- the table is fixed at creation. No
    bio/picture/interests/``is_blocked``/flags either; ``extra="forbid"`` refuses them by name
    (422, not a silent no-op 200); the router drops participant-field edits for staff rows.
    """
    model_config = ConfigDict(extra="forbid")

    regno: Optional[str] = Field(default=None, max_length=USER_REGNO_MAX)
    name: Optional[str] = Field(default=None, max_length=USER_NAME_MAX)
    gender: Optional[Gender] = None
    email: Optional[EmailStr] = None
    # participants only
    year: Optional[Year] = None
    department: Optional[str] = Field(default=None, max_length=USER_DEPARTMENT_MAX)

    @field_validator("name", "regno", "gender", "year", "department", "email")
    @classmethod
    def _blank_is_not_a_value(cls, value):
        """Treat an empty string as "not supplied", never as a value to write.

        Three "leave alone" spellings (absent; ``""`` normalised to None here; explicit null
        dropped by the router) must never become a NULL write: the columns are NOT NULL, so
        that is a 500 from PostgreSQL. ``bio`` is excluded so an intentional ``""`` stays.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value


class AccountDetails(BaseModel):
    """The fields every account has, whatever table it lives in (every authenticated caller resolves to this shape).

    Participant-only fields are deliberately absent, not null: staff rows have no such
    columns, so ``null`` would describe a value the row does not have. ``email`` is required
    everywhere -- a staff row without an address is one nobody can be contacted about.
    """
    id: str = Field(default_factory=new_id)
    regno: str
    name: str
    email: EmailStr
    notifications_enabled: bool = True
    is_blocked: bool = False
    last_login_at: Optional[datetime] = None
    last_login_ip: Optional[str] = None
    last_login_user_agent: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # The table this row was read from -- not a privilege; that answer is ``capabilities``.
    account_type: str = AccountType.USERS.value

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

    @field_validator("account_type", mode="before")
    @classmethod
    def _normalize_stored_account_type(cls, value: Any) -> str:
        """Resolve the account table read out of the database to a defined one.

        Unrecognised values become ``users``, so no consumer is handed a value matching no
        branch -- the account keeps working, just with no privileges it was never granted.
        """
        return normalize_account_type(value)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def capabilities(self) -> List[str]:
        """The capabilities this account's table grants, as sent to the client.

        Derived (never stored) so it cannot drift from ``account_type``; sorted for stable
        diffing. Display-only -- the server re-checks every one against the database per request.
        """
        return sorted(capabilities_for(self.account_type))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_staff(self) -> bool:
        """Whether this account is staff rather than a participant, for display.

        The server gates routes on the account table itself; this is the same answer, so the
        client can hide participant-only screens without re-deriving the rule.
        """
        return bool(capabilities_for(self.account_type)) and self.account_type != AccountType.USERS.value


class UserDetails(AccountDetails):
    """A participant account: everything in ``AccountDetails`` plus the participant fields.

    Participant routes resolve via a dependency that refuses any account not in ``users``.
    ``interests`` and ``year`` stay plain because CHECK constraints added by
    ``apply_check_constraints`` land NOT VALID: a legacy row can hold out-of-set values, and
    refusing them in this response model would make every profile read a 500.
    """
    gender: Gender
    # Derived on read from the stored Cloudinary identity, never stored as a URL. Nullable
    # because never having uploaded is a real state.
    profile_picture_url: Optional[str] = None
    bio: Optional[str] = None
    # Both NOT NULL on ``users``. ``year`` is a plain ``int`` for the same NOT VALID reason
    # as ``interests`` above: write paths enforce the range, not this read shape.
    year: int
    department: str
    interests: List[str] = Field(default_factory=list)
    matchmaking_enabled: bool = True
    love_notes_receive_enabled: bool = True
    love_notes_send_enabled: bool = False
    last_matchmaking_at: Optional[datetime] = None
    last_confession_at: Optional[datetime] = None
    # When the participant last changed their picture; the profile screen shows this wait
    # before anyone picks a file. Nullable: never having uploaded is a real state.
    last_profile_picture_at: Optional[datetime] = None


class LoginToken(BaseModel):
    """A securely stored authentication token in the database."""
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
    superseded_at: Optional[datetime] = None
    superseded_by: Optional[str] = None

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

class LoveNoteTemplate(BaseModel):
    """A predefined Love Notes template."""
    id: str = Field(default_factory=new_id)
    name: str
    image_url: str
    description: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

class LoveNoteImageIdentity(BaseModel):
    """The durable identity of a love note's image, and nothing derived from it.

    A usable address is a short-lived signed URL minted per viewer, so storing one would
    bake a credential into the column (and into every response reading it back); the URL is
    derived at hand-out time, after the viewer is authorised.
    """

    public_id: str
    format: str


class LoveNote(BaseModel):
    """A love note as it is stored: identity for the image, no address for it.

    ``image`` replaces the ``image_url`` this used to carry -- holding an address here would
    invite a caller to hand it out unchecked; authorisation happens before any URL is minted.
    """

    id: str = Field(default_factory=new_id)
    sender_id: str
    recipient_id: str
    image: LoveNoteImageIdentity
    body: str
    # Privacy-safe preview shown to reviewers (``db_models.LoveNoteRecord.moderation_body``).
    # Optional: absent redaction renders an explicit unavailable state, never a ``body`` read.
    moderation_body: Optional[str] = None
    anonymous: bool = False
    status: str = "pending_review"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    read_at: Optional[datetime] = None

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)


class Match(BaseModel):
    """A match between two users from matchmaking."""
    id: str = Field(default_factory=new_id)
    user_1_regno: str
    user_2_regno: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

class Conversation(BaseModel):
    """A conversation request and its state between two matched users."""
    id: str = Field(default_factory=new_id)
    match_id: str
    initiator_regno: str
    receiver_regno: str
    status: str = "pending"  # 'pending', 'requested', 'accepted', 'rejected', or 'terminated' (moderator-closed; readable, not writable)
    requested_at: Optional[datetime] = None  # When initiator clicked "Send Message Request"
    accepted_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

class Message(BaseModel):
    """A single message within a conversation."""
    id: str = Field(default_factory=new_id)
    conversation_id: str
    sender_regno: str
    receiver_regno: str
    body: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_read: bool = False

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

class ConversationCreate(BaseModel):
    """Model for creating a conversation request."""
    match_id: str


class Notification(BaseModel):
    """A notification for a user."""
    id: str = Field(default_factory=new_id)
    recipient_regno: str
    heading: str
    body: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_read: bool = False

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)


# Update forward references to allow nested Pydantic models
Confession.model_rebuild()
SemanticSearchResponse.model_rebuild()
KeywordSearchSection.model_rebuild()
SemanticSearchSection.model_rebuild()
HybridSearchResponse.model_rebuild()


# --- Wordle configuration ------------------------------------------------------
#
# Admin-side only: a *puzzle* -- a day, a word, where the word came from -- and nothing
# about playing one (no guess/attempt/result exists yet).

# A ``Literal`` like ``Gender``: the schema hands the client the two legal modes (422
# otherwise). Mirrors the CHECK constraint on ``wordle_games.mode``.
WordleMode = Literal["custom", "random"]


class WordleGameCreate(BaseModel):
    """Configuring the answer for one day.

    ``game_date`` is optional (defaults to today UTC) and, not the request or row id, is what
    uniqueness is enforced on. ``word``/``token`` are optional in the type, required by the
    *mode*; word sanity is answered with a 400 naming the rule, not a 422. ``token`` is the
    proof of a draw: a re-draw at save time would publish an answer the admin never saw;
    ``extra="forbid"`` so a dropped key never answers 201.
    """

    model_config = ConfigDict(extra="forbid")

    mode: WordleMode
    word: Optional[str] = None
    token: Optional[str] = None
    game_date: Optional[date] = None


class WordleGame(BaseModel):
    """A Wordle session as stored: the day, the word, where the word came from.

    ``answer`` is returned deliberately: this is the admin's configuration screen, gated on
    the publish capability (no participant-facing read of this model exists). It is null
    while pending (manual mode, word not yet provided); ``period_*`` is the calendar period,
    which an Update shortens rather than renews.
    """

    id: str
    game_date: date
    mode: WordleMode
    answer: Optional[str] = None
    status: str = "active"
    period_start_date: Optional[date] = None
    period_end_date: Optional[date] = None
    created_at: datetime
    updated_at: datetime


class WordleDraw(BaseModel):
    """A word the server drew, plus the signature that lets it be saved unchanged.

    Only the server can produce either: ``token`` is an HMAC over word and date, so what the
    admin saw is what gets stored -- nothing the client sends can become the answer.
    """

    word: str
    token: str


# --- Anagram configuration -----------------------------------------------------
#
# Admin-side only, same boundary as Wordle: a puzzle and nothing about playing one. Also
# mirrors the CHECK constraint on ``anagram_games.mode`` -- payload, service and schema agree.

AnagramMode = Literal["custom", "random"]


class AnagramGameCreate(BaseModel):
    """Configuring the answer for one day's Anagram.

    ``word``/``token`` are optional in the type, required by the *mode*; word rules (non-empty,
    letters only, short enough) are answered in the service with a 400 naming the failed rule.
    ``mode="random"`` must carry the drawn word and its token -- the server re-derives the
    signature, so a wrong-day or invented token is a 400, not a second draw. ``extra="forbid"``.
    """

    model_config = ConfigDict(extra="forbid")

    mode: AnagramMode
    word: Optional[str] = None
    token: Optional[str] = None
    game_date: Optional[date] = None


class AnagramGame(BaseModel):
    """A configured puzzle as stored: the day, the word, who published it.

    ``answer`` is returned deliberately (admin-only configuration screen) and is always the
    canonical uppercase word -- each player's scramble is generated per user. ``created_by``
    is the publisher's ``regno``.
    """

    id: str
    game_date: date
    mode: AnagramMode
    answer: Optional[str] = None
    status: str = "active"
    period_start_date: Optional[date] = None
    period_end_date: Optional[date] = None
    created_by: str
    created_at: datetime
    updated_at: datetime


class AnagramDraw(BaseModel):
    """A word the server drew, plus the signature that lets it be saved unchanged.

    Only the server can produce either: ``token`` is an HMAC over word and date, so nothing
    the client sends can become an answer the server did not pick.
    """

    word: str
    token: str


# --- Couple configuration ------------------------------------------------------
#
# Admin-side only: a featured *date* and nothing else -- the 50-question bank is shared
# seeded content, so there is no mode, word or per-day choice to make.

class CoupleGameCreate(BaseModel):
    """Marking the Couple Prediction game featured for one day.

    ``game_date`` defaults to today UTC and -- not the request or row id -- identifies the
    featured instance, so it is what uniqueness is enforced on. ``extra="forbid"``.
    """

    model_config = ConfigDict(extra="forbid")

    game_date: Optional[date] = None


class CoupleGame(BaseModel):
    """A featured Couple game as stored: the day, and who marked it so.

    Nothing per-day to hide (the bank is shared and static); ``created_by`` is the
    publisher's ``regno``.
    """

    id: str
    game_date: date
    created_by: str
    created_at: datetime
    updated_at: datetime


# --- Couple question bank ------------------------------------------------------
#
# Read shapes for the bank seeded at startup: fifty templates naming ``X`` with four options
# each, and a participant's selected answer. No player workflow exists yet.

class CoupleGameQuestion(BaseModel):
    """One bank template as stored: the question naming ``X``, its options, its position.

    ``options`` is always four entries in display order; ``stable_key`` (``couple-q-01``,
    ``couple-q-02`` …) is the seed's check for "already exists".
    """

    id: str
    stable_key: str
    sequence_number: int
    question_text: str
    options: List[str]
    created_at: datetime
    updated_at: datetime


class CoupleGameAnswer(BaseModel):
    """The answer one participant selected for one bank question.

    ``selected_option_index`` is positional into the question's options rather than a copy
    of its text, so a wording correction can never re-point the stored answer.
    """

    id: str
    question_id: str
    user_id: str
    selected_option_index: int
    created_at: datetime
    updated_at: datetime


# --- Couple gameplay -----------------------------------------------------------
# One player's *place* in a featured day: partner choice, own questions, predictions of the
# partner, the derived score. The partner's originals never travel before the completed
# result -- hiding them is the point of the prediction phase.

# Where the caller stands; the CHECK on ``couple_game_sessions.status`` mirrors the three
# in-session states (the rest mean no session / no playable day).
CoupleStage = Literal[
    "no_game",
    "selecting_partner",
    "answering",
    "waiting_partner",
    "prediction",
    "completed",
]


class CouplePartnerCandidate(BaseModel):
    """One user the caller may choose as a partner: identity and display facts only."""

    id: str
    name: str
    gender: str
    year: int
    department: str


class CouplePartnerPage(BaseModel):
    """One page of partner candidates, with the total behind the current filters.

    ``total`` is what tells "load more" how many remain and to stop at the end.
    """

    partners: List[CouplePartnerCandidate]
    total: int
    skip: int
    limit: int


class CoupleSelectionRequest(BaseModel):
    """Choosing a partner: the one user this caller wants to play with.

    ``extra="forbid"`` -- unknown keys are refused, matching ``WordleGuessRequest``.
    """

    model_config = ConfigDict(extra="forbid")

    selected_user_id: str


class CoupleAnswerRequest(BaseModel):
    """Answering one of this caller's own session questions.

    ``session_question_id`` names one of the fifteen questions assigned to this caller; the
    session is derived from the caller, never the payload, so nobody can answer into someone
    else's game.
    """

    model_config = ConfigDict(extra="forbid")

    session_question_id: str
    selected_option_index: int = Field(ge=0, le=3)


class CouplePredictionRequest(BaseModel):
    """Predicting the partner's answer to one of the partner's session questions.

    The verdict is computed server-side against the stored original, so nothing the client
    sends can mark itself correct.
    """

    model_config = ConfigDict(extra="forbid")

    session_question_id: str
    predicted_option_index: int = Field(ge=0, le=3)


class CoupleQuestionView(BaseModel):
    """One of this caller's own questions, as the answering screen renders it.

    ``question_text`` is personalised on the read (the bank's ``X`` reads as "you"); the
    stored template is untouched.
    """

    session_question_id: str
    question_id: str
    question_text: str
    options: List[str]
    shared: bool
    selected_option_index: Optional[int] = None


class CouplePredictionView(BaseModel):
    """One of the partner's questions, as this caller predicts it.

    The partner's original answer never travels here -- it surfaces only in the completed
    review.
    """

    session_question_id: str
    question_id: str
    question_text: str
    options: List[str]
    shared: bool
    predicted_option_index: Optional[int] = None


class CoupleReviewItem(BaseModel):
    """One settled prediction, visible only once the session is completed.

    What the partner picked, what the caller guessed, whether they matched -- emitted only
    in the completed result, the single moment the originals are revealed.
    """

    session_question_id: str
    question_id: str
    question_text: str
    options: List[str]
    shared: bool
    original_option_index: int
    predicted_option_index: int
    correct: bool


class CoupleResultView(BaseModel):
    """The settled score, derived from persisted rows rather than trusted input.

    One point per prediction matching the partner's stored original; ``review`` replays the
    predictions against those originals.
    """

    my_score: int
    partner_score: int
    max_score: int
    my_correct_count: int
    my_prediction_count: int
    partner_correct_count: int
    partner_prediction_count: int
    completed_at: Optional[datetime] = None
    review: List[CoupleReviewItem] = []


class CoupleSelectionView(BaseModel):
    """This caller's pending partner selection, if they have made one."""

    selected_user_id: str
    selected_user_name: str


class CouplePartnerView(BaseModel):
    """The other half of this caller's session."""

    id: str
    name: str


class CoupleCurrentState(BaseModel):
    """Where this caller stands in the featured Couple Questionnaire, as the page opens.

    Reading never creates anything. ``questions`` holds this caller's own fifteen while
    answering, ``predictions`` the partner's fifteen in stable random order, ``result`` the
    settled score -- each present only in its phase, so the screen renders what it is given
    rather than deciding what the caller may see.
    """

    stage: CoupleStage
    game_id: Optional[str] = None
    game_date: Optional[date] = None
    my_selection: Optional[CoupleSelectionView] = None
    session_id: Optional[str] = None
    partner: Optional[CouplePartnerView] = None
    my_answers_submitted: int = 0
    my_answers_total: int = 0
    partner_answers_submitted: int = 0
    partner_answers_total: int = 0
    my_predictions_submitted: int = 0
    my_predictions_total: int = 0
    waiting_for: Optional[Literal["answers", "predictions"]] = None
    questions: List[CoupleQuestionView] = []
    predictions: List[CouplePredictionView] = []
    result: Optional[CoupleResultView] = None


# --- Anagram gameplay ----------------------------------------------------------
#
# One player's *place* in a puzzle: private scramble, guesses, time actually played. The
# answer travels only on a completed participation -- the single moment it may be revealed.

# Played vs settled; the CHECK constraint on ``anagram_user_games.status`` mirrors this set.
AnagramStatus = Literal["in_progress", "won", "lost"]


class AnagramParticipation(BaseModel):
    """One player's place in one day's puzzle: scramble, guesses, time actually played.

    ``scrambled_word`` is dealt once, never re-dealt. ``active_time_seconds`` sums finished
    segments; ``timer_running`` restates "an open, non-stale segment" so the screen ticks or
    offers RESUME without clock arithmetic. ``answer`` is shown on completion only for a loss
    (the winner typed it); the service decides when it stops being a secret.
    """

    anagram_game_id: str
    game_date: date
    status: AnagramStatus
    success: bool
    attempt_count: int
    max_attempts: int = ANAGRAM_MAX_ATTEMPTS
    guesses: list[str]
    scrambled_word: str
    active_time_seconds: int = 0
    timer_started_at: Optional[datetime] = None
    timer_running: bool = False
    started_at: datetime
    last_guess_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    solved_at: Optional[datetime] = None
    answer: Optional[str] = None


class AnagramCurrentGame(BaseModel):
    """Today's puzzle and this player's participation, as the game screen opens.

    Nullable ids because there may be no playable puzzle. ``participation`` is NULL until
    REVEAL WORD -- reading must not create a row, or opening twice would start two timers.
    ``max_attempts`` is carried so the screen draws its attempts line without a round trip.
    """

    game_id: Optional[str] = None
    game_date: Optional[date] = None
    participation: Optional[AnagramParticipation] = None
    session_status: Optional[str] = None
    message: Optional[str] = None
    max_attempts: int = ANAGRAM_MAX_ATTEMPTS


class AnagramActionRequest(BaseModel):
    """Starting, pausing or checkpointing one puzzle for this player.

    ``game_id`` is the puzzle the player was shown, so a stale screen cannot start a timer
    on yesterday's game after midnight. The three timer transitions share this shape;
    ``extra="forbid"`` as on ``WordleGuessRequest``.
    """

    model_config = ConfigDict(extra="forbid")

    game_id: str


class AnagramGuessRequest(BaseModel):
    """Guessing one arrangement against one puzzle.

    ``guess`` is submitted raw; normalisation and refusal reasons happen in the service so
    they reach the client as a readable 400, not a generic 422 about field shape.
    ``extra="forbid"`` as on ``WordleGuessRequest``.
    """

    model_config = ConfigDict(extra="forbid")

    game_id: str
    guess: str


class AnagramGuessResponse(AnagramParticipation):
    """A participation after one guess, plus the verdict the guess earned.

    ``correct`` is redundant with the tail of ``guesses``, but the toast needs the one
    outcome -- re-deriving it client-side is exactly the trust-the-client trap.
    """

    guess: str
    correct: bool


# --- Wordle gameplay --------------------------------------------------------------
#
# One participant's attempt: at most one per puzzle (unique pair). The answer travels only
# on a completed participation -- the single moment it may be revealed.


# A tile's colour for one guess letter. Closed like ``Gender``: the client renders exactly
# these three; anything else is a 422 rather than a guessed colour.
WordleTile = Literal["absent", "present", "correct"]

# Played vs settled; the CHECK constraint on ``wordle_user_games.status`` mirrors this set.
WordleStatus = Literal["in_progress", "won", "lost"]


class WordleParticipation(BaseModel):
    """One player's place in one day's puzzle: guesses and per-letter verdicts.

    ``results`` is re-derived on every read at the same order/length as ``guesses``, so a
    re-reading client cannot see a verdict disagreeing with the letters above it. ``answer``
    is owed only to a finished game (the loser reads it); ``success`` restates
    ``status = 'won'`` so a finished loss is not mistaken for a win.
    """

    wordle_game_id: str
    game_date: date
    status: WordleStatus
    success: bool
    attempt_count: int
    max_attempts: int = WORDLE_MAX_ATTEMPTS
    guesses: list[str]
    results: list[list[WordleTile]]
    started_at: datetime
    last_guess_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    solved_at: Optional[datetime] = None
    answer: Optional[str] = None


class WordleCurrentGame(BaseModel):
    """Today's puzzle and this player's participation, as the game screen opens.

    Nullable ids because there may be no playable puzzle. ``participation`` is NULL until
    the first guess commits -- reading must not create a row, or opening twice would count
    two attempts. ``session_status='pending'`` means today's word is not configured;
    ``max_attempts``/``word_length`` spare the screen a round trip.
    """

    game_id: Optional[str] = None
    game_date: Optional[date] = None
    participation: Optional[WordleParticipation] = None
    session_status: Optional[str] = None
    message: Optional[str] = None
    max_attempts: int = WORDLE_MAX_ATTEMPTS
    word_length: int = WORDLE_ANSWER_LENGTH


class WordleGuessRequest(BaseModel):
    """Guessing one word against one puzzle.

    ``guess`` is submitted raw; normalisation and refusal reasons (length, case, not a word)
    answer as a readable 400 from the service, not a generic 422 about field shape.
    ``extra="forbid"`` matching ``WordleGameCreate``.
    """

    model_config = ConfigDict(extra="forbid")

    game_id: str
    guess: str


class WordleGuessResponse(WordleParticipation):
    """A participation after one guess, plus the verdict the guess earned.

    ``result`` is redundant with the tail of ``results``, but the toast and keyboard need the
    one verdict that just happened -- munging the last row out of the history is off-by-one
    the client should not be asked to perform.
    """

    guess: str
    result: list[WordleTile]


# --- Game configuration, featured games, scheduling and selection ---------------
# Configuration owns availability/cadence, featured owns the one-spotlight-per-day schedule,
# scheduled words own future manual words; the selection response spares the screen
# re-deriving availability client-side.

GameResetCadence = Literal["daily", "every_2_days", "every_3_days", "every_5_days", "weekly"]
GameContentMode = Literal["random", "manual"]
# The difficulty the AI pipeline reads for Plot Twist / Case Closed; mirrors
# ``limits.PLOT_TWIST_DIFFICULTIES`` (the service re-validates; this shapes the contract).
GameDifficulty = Literal["easy", "medium", "hard"]
GameId = Literal["wordle", "anagram", "couple-questionnaire", "reaction-time", "connections", "trivia-drop", "two-key-escape", "plot-twist", "case-closed"]


class GameConfig(BaseModel):
    """One game's availability, content mode, reset cadence and grid size, as stored."""

    game_id: str
    enabled: bool = True
    is_hidden: bool = False
    content_mode: Optional[GameContentMode] = None
    reset_cadence: GameResetCadence = "daily"
    # Two-Key Escape maze edge length (None elsewhere): that game's own knob, not a
    # second content mode.
    grid_size: Optional[int] = None
    # Difficulty the generation pipeline uses for the next Plot Twist / Case Closed cycle.
    difficulty: Optional[GameDifficulty] = None


class GameConfigUpdate(BaseModel):
    """Patching one game's configuration; every field is optional."""

    model_config = ConfigDict(extra="forbid")

    enabled: Optional[bool] = None
    is_hidden: Optional[bool] = None
    content_mode: Optional[GameContentMode] = None
    reset_cadence: Optional[GameResetCadence] = None
    grid_size: Optional[int] = None
    difficulty: Optional[GameDifficulty] = None


class FeaturedGame(BaseModel):
    """One featured date: which game holds the spotlight on which UTC day."""

    id: str
    game_date: date
    game_id: str
    created_by: str
    created_at: datetime
    updated_at: datetime


class FeaturedGameCreate(BaseModel):
    """Pinning the spotlight; a date that already has one is a 409."""

    model_config = ConfigDict(extra="forbid")

    game_date: date
    game_id: GameId


class FeaturedGameRangeCreate(BaseModel):
    """Featuring one game for a whole span; free dates only, one-week chains."""

    model_config = ConfigDict(extra="forbid")

    game_id: GameId
    start_date: date
    end_date: date


class ScheduledWord(BaseModel):
    """A manual word waiting for its session period to begin."""

    id: str
    game_id: str
    target_date: date
    word: str
    created_by: str


class ScheduledWordCreate(BaseModel):
    """Scheduling a manual word inside the finite horizon."""

    model_config = ConfigDict(extra="forbid")

    target_date: date
    word: Optional[str] = None


class GameSelectEntry(BaseModel):
    """One game as the selection screen sees it: visibility and playability.

    Hidden games never appear at all; ``playable`` is false for disabled games and games
    with no configured session, with ``message`` the sentence to show for either.
    """

    game_id: str
    name: str
    enabled: bool
    playable: bool
    session_status: Optional[str] = None
    featured: bool = False
    message: Optional[str] = None


class GameSelectResponse(BaseModel):
    """The selection list plus today's featured game, if the day has a pick."""

    games: List[GameSelectEntry]
    featured_game_id: Optional[str] = None
    server_date: date


# --- Reaction Time configuration ------------------------------------------------
#
# Admin-side only: a featured date and nothing else -- mechanics are fixed by the app;
# sessions roll on the game's own cadence and featuring never touches a session.

class ReactionTimeGameCreate(BaseModel):
    """Featuring the Reaction Time game for one day (defaults to today UTC).

    One game holds the spotlight per date across all games, so an already-featured day
    is refused; ``extra="forbid"``.
    """

    model_config = ConfigDict(extra="forbid")

    game_date: Optional[date] = None


class ReactionTimeGame(BaseModel):
    """A featured Reaction Time date as it is stored: the day, and who marked it so."""

    id: str
    game_date: date
    created_by: str
    created_at: datetime
    updated_at: datetime


# --- Reaction Time gameplay -----------------------------------------------------
# One player's *place* in the current session: at most five submissions, each born pending
# on the blue-screen click and settled valid or too_early; a too_early elapsed time is
# never a score.

# Pending vs settled; mirrors the CHECK constraint on ``reaction_time_submissions.status``.
ReactionTimeStatus = Literal["pending", "valid", "too_early"]


class ReactionTimeAttempt(BaseModel):
    """One submission, as the player screen renders it.

    ``reaction_time_ms`` set only on ``valid``; ``elapsed_ms`` only on ``too_early``
    (informational, never a score).
    """

    submission_id: str
    attempt_number: int
    status: ReactionTimeStatus
    reaction_time_ms: Optional[int] = None
    elapsed_ms: Optional[int] = None
    started_at: datetime
    green_at: Optional[datetime] = None
    clicked_at: Optional[datetime] = None
    created_at: datetime


class ReactionTimeState(BaseModel):
    """Where this caller stands in the current Reaction Time session, as the page opens.

    Reading never creates anything; a ``pending`` submission left mid-attempt still counts
    as consumed. ``best_ms``/``average_ms`` cover valid attempts only and stay null until
    one exists; every number is derived server-side, never trusted from the client.
    """

    game_id: Optional[str] = None
    game_date: Optional[date] = None
    max_submissions: int = 5
    submissions_used: int = 0
    submissions_remaining: int = 5
    is_finished: bool = False
    attempts: List[ReactionTimeAttempt] = []
    valid_count: int = 0
    false_start_count: int = 0
    best_ms: Optional[float] = None
    average_ms: Optional[float] = None
    pending_submission: Optional[ReactionTimeAttempt] = None


class ReactionTimeStartRequest(BaseModel):
    """Starting one attempt: the blue-screen click that consumes a submission.

    ``game_id`` names the session the player was shown, so a stale screen cannot start on an
    ended one; attempt numbers are server-assigned, never sent by the client.
    """

    model_config = ConfigDict(extra="forbid")

    game_id: str


class ReactionTimeCompleteRequest(BaseModel):
    """Settling a pending attempt with its measured result.

    ``submission_id`` names the pending submission this caller owns; settled rows are
    immutable, so a second submit is refused. ``valid`` carries the client-measured
    ``reaction_time_ms``; ``too_early`` carries only ``elapsed_ms`` (informational, never a
    score). Bounds are a readable 400 in the service; column CHECKs guard non-API writes.
    """

    model_config = ConfigDict(extra="forbid")

    submission_id: str
    status: Literal["valid", "too_early"]
    reaction_time_ms: Optional[int] = None
    elapsed_ms: Optional[int] = None
    green_at: Optional[datetime] = None
    clicked_at: Optional[datetime] = None


# --- Plot Twist -------------------------------------------------------------------
#
# Admin models describe generation (config, approval, publication); player models describe
# one couple's place in the published story. Scores travel through the league service only.

PlotTwistStage = Literal[
    "no_game",
    "selecting_partner",
    "awaiting_you",
    "awaiting_partner",
    "completed",
]


class PlotTwistGenerationConfig(BaseModel):
    """Parameters for Stage A blueprint generation.

    Only ``difficulty`` is required (the admin UI's single knob); the rest exist so tests
    and power users can pin values.
    """

    model_config = ConfigDict(extra="forbid")

    difficulty: Literal["easy", "medium", "hard"] = "medium"
    theme: Optional[str] = None
    premise: Optional[str] = None
    genre: Optional[str] = None
    tone: Optional[str] = None
    decision_count: Optional[int] = None
    interchangeable_count: Optional[int] = None
    ending_types: Optional[List[str]] = None
    allow_treasure: Optional[bool] = None
    max_scenes: Optional[int] = None


class PlotTwistBlueprintView(BaseModel):
    id: str
    title: str
    difficulty: str
    config: dict = {}
    blueprint: dict = {}
    status: str
    validation_errors: list = []
    created_by: str
    created_at: datetime
    updated_at: datetime


class PlotTwistPackageView(BaseModel):
    id: str
    blueprint_id: str
    version: int
    title: str
    package: dict = {}
    status: str
    validation_errors: list = []
    published_at: Optional[datetime] = None
    created_by: str
    created_at: datetime
    updated_at: datetime


class PlotTwistSceneView(BaseModel):
    """One scene as the participant assigned to it sees it."""

    scene_id: str
    scene_type: str
    title: str
    narrative_text: str
    actor: str
    choices: List[dict] = []
    is_terminal: bool = False
    visited_count: int = 0


class PlotTwistChoiceView(BaseModel):
    choice_id: str
    text: str
    consequence_text: Optional[str] = None
    reward_tier: Optional[str] = None


class PlotTwistDecisionView(BaseModel):
    seq: int
    scene_id: str
    scene_title: Optional[str] = None
    actor_user_id: str
    actor_name: Optional[str] = None
    choice_id: str
    choice_text: Optional[str] = None
    consequence_text: Optional[str] = None
    created_at: datetime


class PlotTwistResultView(BaseModel):
    ending_id: Optional[str] = None
    ending_type: Optional[str] = None
    ending_title: Optional[str] = None
    narrative: Optional[str] = None
    success: bool = False
    treasures: List[str] = []
    decisions_made: int = 0
    completed_at: Optional[datetime] = None


class PlotTwistCurrentState(BaseModel):
    """This caller's place in Plot Twist, as the page opens.

    Reading never creates gameplay rows. Hidden future scenes and the partner's pending
    choice never travel here -- only the current scene, whose turn, and settled history.
    """

    stage: PlotTwistStage
    game_id: Optional[str] = None
    session_id: Optional[str] = None
    my_selection: Optional[CoupleSelectionView] = None
    partner: Optional[CouplePartnerView] = None
    scene: Optional[PlotTwistSceneView] = None
    state_version: int = 0
    decisions_made: int = 0
    history: List[PlotTwistDecisionView] = []
    result: Optional[PlotTwistResultView] = None


class PlotTwistChoiceSubmitRequest(BaseModel):
    """Submitting the assigned turn's choice.

    ``state_version`` is optimistic concurrency (must match the stored version); a repeat
    submit of a recorded turn resolves to the stored outcome rather than applying twice.
    """

    model_config = ConfigDict(extra="forbid")

    choice_id: str
    state_version: int


class PlotTwistSimulateRequest(BaseModel):
    """An admin preview walk: choice ids applied in order from the opening."""

    model_config = ConfigDict(extra="forbid")

    choice_ids: List[str] = Field(default_factory=list, max_length=24)


class PlotTwistSimulateResponse(BaseModel):
    steps: List[dict] = []
    ending_id: Optional[str] = None
    ending_title: Optional[str] = None
    final_state: dict = {}
    treasures: List[str] = []
    valid: bool = True
    error: Optional[str] = None


# --- Case Closed ---------------------------------------------------------------
#
# Admin-side generation views mirror Plot Twist; player views live in
# services.case_closed_schemas and are re-exported here for router typing.

class CaseClosedGenerationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    difficulty: Literal["easy", "medium", "hard"] = "medium"
    theme: Optional[str] = None
    premise: Optional[str] = None
    tone: Optional[str] = None
    suspect_count: Optional[int] = None
    target_lead_count: Optional[int] = None


class CaseClosedBlueprintView(BaseModel):
    id: str
    title: str
    difficulty: str
    config: dict = {}
    blueprint: dict = {}
    status: str
    validation_errors: list = []
    created_by: str
    created_at: datetime
    updated_at: datetime


class CaseClosedPackageView(BaseModel):
    id: str
    blueprint_id: str
    version: int
    title: str
    package: dict = {}
    status: str
    validation_errors: list = []
    published_at: Optional[datetime] = None
    created_by: str
    created_at: datetime
    updated_at: datetime


class CaseClosedInvestigationSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lead_id: str
    state_version: int


class CaseClosedProposeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suspect_id: str


class CaseClosedConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suspect_id: str


# --- Connections configuration --------------------------------------------------
#
# Admin-side only: a puzzle -- sixteen words in four canonical answer groups -- and nothing
# about playing one. No random mode: every session needs its own admin-supplied puzzle.

class ConnectionsPuzzleCreate(BaseModel):
    """Configuring one Connections puzzle: sixteen words in four groups of four.

    Words are submitted raw; the service trims, refuses empties/duplicates (case-insensitive)
    and stores both display and normalised spellings. ``groups`` omitted (update path)
    consumes the earliest scheduled puzzle in the new window, or opens the session pending.
    ``extra="forbid"``.
    """

    model_config = ConfigDict(extra="forbid")

    groups: Optional[List[List[str]]] = None


class ConnectionsGame(BaseModel):
    """A Connections session as stored: the day, its puzzle, its lifecycle.

    ``groups`` is returned deliberately (admin-only configuration screen; no participant-
    facing read). Empty while pending (manual content not yet provided); ``period_*`` the
    calendar period.
    """

    id: str
    game_date: date
    groups: List[List[str]] = []
    status: str = "active"
    period_start_date: Optional[date] = None
    period_end_date: Optional[date] = None
    created_by: str
    created_at: datetime
    updated_at: datetime


class ConnectionsScheduledPuzzle(BaseModel):
    """A manually configured Connections puzzle waiting for its session period."""

    id: str
    target_date: date
    groups: List[List[str]]
    created_by: str


class ConnectionsScheduledPuzzleCreate(BaseModel):
    """Scheduling a Connections puzzle inside the finite horizon."""

    model_config = ConfigDict(extra="forbid")

    target_date: date
    groups: List[List[str]]


# --- Connections gameplay -------------------------------------------------------
# One player's *place*: the tiles they were dealt (display spellings in their stable
# shuffled order -- the canonical groups never travel) and whether they submitted. Scores
# persist server-side but are never returned to the player.

class ConnectionsCurrentGame(BaseModel):
    """Today's puzzle and this player's submission state, as the screen opens.

    Nullable ids because there may be no playable puzzle. ``words`` is this player's stable
    shuffled order (empty once submitted); ``submitted`` marks the one submission consumed.
    ``session_status='pending'`` means the puzzle is not configured yet.
    """

    game_id: Optional[str] = None
    game_date: Optional[date] = None
    words: List[str] = []
    submitted: bool = False
    session_status: Optional[str] = None
    message: Optional[str] = None


class ConnectionsSubmitRequest(BaseModel):
    """Submitting one final arrangement against one puzzle.

    Words are submitted as displayed; the service normalises and validates against the
    stored puzzle (all sixteen distinct words exactly once, no unknowns). ``extra="forbid"``.
    """

    model_config = ConfigDict(extra="forbid")

    game_id: str
    groups: List[List[str]]


class ConnectionsSubmitResponse(BaseModel):
    """The confirmation a submitted player reads -- and all they read.

    No score, correctness or answer key here: those are persisted server-side for a future
    leaderboard and never returned.
    """

    game_id: str
    submitted: bool = True
    submitted_at: datetime
    message: str = "Your Connections puzzle has been submitted successfully."


# --- Trivia Drop configuration --------------------------------------------------
#
# Admin-side only: a question and its correct-option key, and nothing about answering one.
# The answer mode is derived from the key's size, never configured, so no mode can disagree.

TriviaDropAnswerMode = Literal["single", "multiple"]
TriviaDropReleaseMode = Literal["now", "scheduled", "draft"]
TriviaDropStatus = Literal["draft", "scheduled", "active", "ended", "cancelled"]


class TriviaDropOption(BaseModel):
    """One answer option as stored: its stable index, its text, its key membership."""

    option_index: int
    option_text: str
    is_correct: bool


class TriviaDropQuestionCreate(BaseModel):
    """Authoring one Trivia Drop question: text, options, the correct set.

    ``correct_indexes`` are 0-3 positions; the single/multiple answer mode is derived from
    how many are named. ``release_mode``: ``now`` deploys immediately (ending any live
    question), ``scheduled`` waits for the scheduler, ``draft`` stores for later. Raw
    options are validated in the service with a 400. ``extra="forbid"``.
    """

    model_config = ConfigDict(extra="forbid")

    question_text: str
    options: List[str]
    correct_indexes: List[int]
    release_mode: TriviaDropReleaseMode = "draft"
    scheduled_for: Optional[datetime] = None


class TriviaDropQuestion(BaseModel):
    """A Trivia Drop question as stored: text, options, key, lifecycle.

    ``correct_indexes`` returned deliberately (admin-only configuration screen); ``answer_
    mode`` restates one-vs-several correct so the console need not count. ``released_at`` is
    the authoritative activation instant, written once.
    """

    id: str
    question_text: str
    options: List[TriviaDropOption]
    correct_indexes: List[int]
    answer_mode: TriviaDropAnswerMode
    status: str
    scheduled_for: Optional[datetime] = None
    released_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    created_by: str
    created_at: datetime
    updated_at: datetime


class TriviaDropQuestionUpdate(BaseModel):
    """Editing a draft: fields supplied replace their stored counterpart wholesale.

    Only drafts with no submissions may be edited (live/answered are immutable; corrections
    arrive as a new question). ``extra="forbid"``.
    """

    model_config = ConfigDict(extra="forbid")

    question_text: Optional[str] = None
    options: Optional[List[str]] = None
    correct_indexes: Optional[List[int]] = None


class TriviaDropScheduleRequest(BaseModel):
    """Scheduling (or rescheduling) one question's release.

    Stored as UTC, and the admin screen labels the control UTC so conversion does not ride
    the operator's local clock. ``extra="forbid"``.
    """

    model_config = ConfigDict(extra="forbid")

    scheduled_for: datetime


# --- Trivia Drop gameplay -------------------------------------------------------
#
# One player's place in the live question. The correct-option set never travels: display
# options plus the derived mode only -- not the key, not even how many correct options exist.

class TriviaDropPublicOption(BaseModel):
    """One answer option as a player sees it: its stable index and its text."""

    option_index: int
    option_text: str


class TriviaDropCurrentQuestion(BaseModel):
    """The live question and this player's submission state, as the screen opens.

    ``question_id`` is null in the waiting state; ``answer_mode`` picks single vs
    multi-selection controls. ``submitted`` marks the one submission consumed.
    """

    question_id: Optional[str] = None
    question_text: Optional[str] = None
    options: List[TriviaDropPublicOption] = []
    answer_mode: Optional[TriviaDropAnswerMode] = None
    released_at: Optional[datetime] = None
    submitted: bool = False
    session_status: Optional[str] = None
    message: Optional[str] = None


class TriviaDropSubmitRequest(BaseModel):
    """Answering the live question: one complete selection in one request.

    ``selected_indexes`` is the whole selection (one index, or the full set for
    multiple-answer). Normalisation and key comparison happen in the service, so reasons
    reach the client as a readable 400. ``extra="forbid"`` keeps out any client-supplied
    correctness, score, timestamp or user id.
    """

    model_config = ConfigDict(extra="forbid")

    question_id: str
    selected_indexes: List[int]


class TriviaDropSubmitResponse(BaseModel):
    """The confirmation an answered player reads -- and all they read.

    No correctness or response time while the question is live: those would expose the key;
    they are persisted server-side and surface on the ended question's results.
    """

    question_id: str
    submitted: bool = True
    submitted_at: datetime
    message: str = "Your answer has been recorded."


class TriviaDropLeaderboardEntry(BaseModel):
    """One ranked submission on an ended question's results.

    Correct ranks above incorrect, faster above slower, with the submission instant breaking
    ties. ``selected_indexes`` is echoed so the historical result stays reconstructable
    without a second lookup.
    """

    rank: int
    user_id: str
    display_name: str
    is_correct: bool
    response_time_ms: int
    submitted_at: datetime
    selected_indexes: List[int]


class TriviaDropMySubmission(BaseModel):
    """This caller's own settled answer on one question: correct, response time, selection."""

    is_correct: bool
    response_time_ms: int
    submitted_at: datetime
    selected_indexes: List[int]


class TriviaDropPreviousQuestion(BaseModel):
    """The most recently ended question, with its answer key revealed.

    The key belongs to history once the session ends, so this is player-visible (unlike the
    live question, which never names it); null ids when nothing has ended yet.
    """

    question_id: Optional[str] = None
    question_text: Optional[str] = None
    options: List[TriviaDropPublicOption] = []
    correct_indexes: List[int] = []
    answer_mode: Optional[TriviaDropAnswerMode] = None
    released_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    my_submission: Optional[TriviaDropMySubmission] = None


class TriviaDropResults(BaseModel):
    """An ended question with its key revealed and leaderboard settled.

    Served only once ended: the key and per-submission correctness would expose a live key.
    ``leaderboard`` is every accepted submission in rank order.
    """

    question_id: str
    question_text: str
    options: List[TriviaDropPublicOption]
    correct_indexes: List[int]
    answer_mode: TriviaDropAnswerMode
    released_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    my_submission: Optional[TriviaDropLeaderboardEntry] = None
    leaderboard: List[TriviaDropLeaderboardEntry] = []

# --- Two-Key Escape ------------------------------------------------------------
# One global period owns one maze template; each couple owns a live overlay on it. Roles
# finalize only when both sides' choices are stored and distinct; clients submit actions,
# never state.

TwoKeyEscapeRole = Literal["navigator", "key_operator"]
TwoKeyEscapeDirection = Literal["up", "down", "left", "right"]


class TwoKeyEscapeRoleRequest(BaseModel):
    """Confirming one role choice: submit on first confirm, change before final."""

    model_config = ConfigDict(extra="forbid")

    role: TwoKeyEscapeRole


class TwoKeyEscapeMoveRequest(BaseModel):
    """One Navigator step: exactly one orthogonally adjacent cell."""

    model_config = ConfigDict(extra="forbid")

    direction: TwoKeyEscapeDirection


class TwoKeyEscapeKeyRequest(BaseModel):
    """Spending one key charge: ``key_index`` into the template locked list."""

    model_config = ConfigDict(extra="forbid")

    key_index: int = Field(ge=0)


class TwoKeyEscapeKeyView(BaseModel):
    """One key as the Key Operator sees it.

    Keys are reusable (cooldown plus a Navigator move per use), so there is no spent state
    and which lock each opens stays hidden until trial reveals the mapping.
    """

    key_index: int
    target_row: int
    target_col: int


class TwoKeyEscapeResultView(BaseModel):
    """The settled couple result, finalized exactly once at the exit.

    ``elapsed_seconds`` (first valid move to exit, time away included) is the whole
    ranking; move/key counts are progress facts, never tiebreaks. There is no score.
    """

    completed: bool
    total_moves: int = 0
    total_keys: int = 0
    elapsed_seconds: Optional[float] = None
    completed_at: Optional[datetime] = None


class TwoKeyEscapeState(BaseModel):
    """This caller's place in the couple's maze, as the page opens and after every write.

    Reading never creates an escape game; the first confirmed role submission does.
    ``stage`` is ``no_couple`` / ``selecting_roles`` / ``playing`` / ``completed``; the board
    is the same authoritative overlay for both participants, and ``my_role`` is the caller's
    own finalized role (null before finalization).
    """

    stage: Literal["no_couple", "selecting_roles", "playing", "completed"]
    game_id: Optional[str] = None
    session_id: Optional[str] = None
    # Pending questionnaire partner choice: choosing here and in the Couple Questionnaire
    # is the same act on the same row.
    my_selection: Optional[CoupleSelectionView] = None
    partner: Optional[CouplePartnerView] = None
    my_role: Optional[TwoKeyEscapeRole] = None
    my_submitted_role: Optional[TwoKeyEscapeRole] = None
    partner_submitted_role: Optional[TwoKeyEscapeRole] = None
    roles_finalized: bool = False
    conflict: Optional[str] = None
    grid_size: Optional[int] = None
    key_charges: int = 0
    player_row: Optional[int] = None
    player_col: Optional[int] = None
    exit_row: Optional[int] = None
    exit_col: Optional[int] = None
    walls: List[List[int]] = []
    locked_cells: List[List[int]] = []
    blocked_cells: List[List[int]] = []
    keys: List[TwoKeyEscapeKeyView] = []
    cooldown_seconds_remaining: float = 0
    total_moves: int = 0
    total_keys: int = 0
    state_version: int = 0
    result: Optional[TwoKeyEscapeResultView] = None


class TwoKeyEscapeLeaderboardEntry(BaseModel):
    """One couple's standing in the current period, in rank order.

    Completed couples rank above incomplete; among completed, the shorter start-to-finish
    time wins. No score: no other game carries one.
    """

    rank: int
    couple_names: List[str]
    completed: bool
    total_moves: int
    total_keys: int
    elapsed_seconds: Optional[float] = None


# --- League leaderboards ------------------------------------------------------
#
# Scores are server-calculated league points read from the persisted award ledger -- the
# frontend never computes them. ``native`` carries performance numbers only, never answers.

LeaguePeriod = Literal["daily", "weekly", "monthly", "all_time"]
LeagueCompetitionType = Literal["individual", "couple"]


class LeagueIndividualEntry(BaseModel):
    """One user's standing: rank, normalized score and native context."""

    rank: int
    user_id: str
    display_name: str
    score: float
    native: Dict[str, Any] = {}
    is_featured: bool = False
    opportunities_played: int = 0
    per_game: Optional[Dict[str, float]] = None


class LeagueCoupleEntry(BaseModel):
    """One canonical couple's standing: rank, normalized score and context."""

    rank: int
    member_a_id: str
    member_b_id: str
    member_a_name: str
    member_b_name: str
    score: float
    native: Dict[str, Any] = {}
    is_featured: bool = False
    opportunities_played: int = 0
    per_game: Optional[Dict[str, float]] = None


class LeagueIndividualBoard(BaseModel):
    """A ranked individual board with pagination totals."""

    competition_type: Literal["individual"] = "individual"
    period: str
    game_id: Optional[str] = None
    opportunity_count: Optional[int] = None
    total: int
    entries: List[LeagueIndividualEntry]


class LeagueCoupleBoard(BaseModel):
    """A ranked couple board with pagination totals."""

    competition_type: Literal["couple"] = "couple"
    period: str
    game_id: Optional[str] = None
    opportunity_count: Optional[int] = None
    total: int
    entries: List[LeagueCoupleEntry]


class LeagueMyStanding(BaseModel):
    """The caller's own entries on one board (their couples included)."""

    competition_type: str
    period: str
    entries: List[Any]


class GameCategoryInfo(BaseModel):
    """One selectable leaderboard view for one game."""

    id: str
    label: str
    description: str
    # 'period': aggregates the selected UTC window. 'record': all-time
    # personal record across all finalized history (rolling window ignored).
    scope: str
    is_default: bool = False


class GameCategoryEntry(BaseModel):
    """One ranked row on a category board: individual or canonical-couple identity.

    ``metrics`` carries the category's documented ranking and supporting values (never an
    ambiguous mixed "score"); ``records`` carries the underlying finalized detail rows.
    """

    rank: int
    user_id: Optional[str] = None
    display_name: Optional[str] = None
    member_a_id: Optional[str] = None
    member_b_id: Optional[str] = None
    member_a_name: Optional[str] = None
    member_b_name: Optional[str] = None
    metrics: Dict[str, Any] = {}
    records: List[Dict[str, Any]] = []


class GameCategoryBoard(BaseModel):
    """A ranked category board with pagination totals."""

    game_id: str
    category: str
    scope: str
    period: str
    anchor: Optional[str] = None
    total: int
    entries: List[GameCategoryEntry]


class GameCategoryMe(BaseModel):
    """The caller's own rows on one category board, with full-dataset ranks."""

    game_id: str
    category: str
    scope: str
    entries: List[GameCategoryEntry]


class GameRecordHighlight(BaseModel):
    """One game's headline record board and its current top row.

    Spares the admin overview opening every game's board; ``entry`` is null when the game
    has no finalized result yet.
    """

    game_id: str
    category: str
    label: str
    scope: str
    entry: Optional[GameCategoryEntry] = None


class GameRecordHighlights(BaseModel):
    """Every game's headline record, all-time, for the admin overview."""

    period: Literal["all_time"] = "all_time"
    highlights: List[GameRecordHighlight]


class LeagueFinalizeRequest(BaseModel):
    """Finalize one closed event's league awards (idempotent repair path)."""

    model_config = ConfigDict(extra="forbid")

    game_id: str
    opportunity_id: str


class LeagueSummary(BaseModel):
    """Finalized opportunities and live awards per game, for operators."""

    games: Dict[str, Dict[str, int]]


# --- Player activity summary ---------------------------------------------------
#
# Composed from native participation tables (services/player_stats.py) rather than the
# league ledger, so a round played today shows before finalization. Performance numbers only.


class PlayerRecentPlay(BaseModel):
    """One finished round in the caller's history, newest first."""

    game_id: str
    played_at: datetime
    summary: str
    score: Optional[float] = None
    score_label: Optional[str] = None


class PlayerGameSummary(BaseModel):
    """The caller's finished rounds, streaks and this week's activity."""

    games_played: int
    current_streak_days: int
    longest_streak_days: int
    plays_this_week: int
    #: Distinct game ids played this week, sorted; the weekly challenge's progress.
    games_this_week: List[str]
    recent_plays: List[PlayerRecentPlay]


# --- Games console telemetry ---------------------------------------------------
# Composed by the same collectors as the player summary (services/player_stats.py), so
# "a finished round" is defined once and the console cannot disagree with it. Only counts
# travel: no answers, prompts, words or identities.


class GamePlayTotals(BaseModel):
    """One game's finished-round counts over the console's three windows."""

    game_id: str
    plays_today: int
    plays_this_week: int
    plays_all_time: int


class GameStatsSummary(BaseModel):
    """Live play telemetry across every game, for the games console overview.

    ``players_*`` counts distinct people, not sessions (a couple round is one play, two
    players); ``games`` lists only titles with at least one finished round.
    """

    server_date: date
    plays_today: int
    plays_this_week: int
    plays_all_time: int
    players_today: int
    players_this_week: int
    games: List[GamePlayTotals]
