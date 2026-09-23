# app/models.py

from pydantic import BaseModel, ConfigDict, EmailStr, Field, computed_field, field_validator, model_validator, validator
from typing import Annotated, Literal, Optional, List, Dict, Any
from datetime import date, datetime, timezone
from .postgres import new_id
from .limits import (
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

# The closed gender vocabulary, as a Pydantic type rather than a validator.
#
# A ``Literal`` rather than a ``field_validator`` over a plain ``str`` so the constraint is
# part of the field's declared type: the generated OpenAPI schema becomes an enum, so the
# client is handed the three legal values instead of a free-text string it has to guess the
# contents of, and a value outside the set is a 422 at validation rather than a check some
# other layer might forget to run.
#
# Mirrors ``limits.USER_GENDER_VALUES``, which is what the CHECK constraint and the frontend
# Select are generated from. All three must agree.
Gender = Literal["male", "female", "other"]

# Same treatment for a participant's year of study, and closed for the same reason: matchmaking
# groups participants by it, so "2" and "02" would be two cohorts that never meet and no screen
# could tell them apart.
#
# A bounded ``int`` rather than a ``Literal`` of names, because the bounds have to hold a school as
# well as an undergraduate programme: 1-12 covers Class 1 through Class 12, where a list written
# out would have had to be extended -- and re-extended in three mirrored places -- for a deployment
# that differs only in how it spells the same field. The CHECK constraint and the frontend select
# are generated from the same ``limits`` bounds. All three must agree.
#
# ``ge``/``le`` rather than ``Field(..., multiple_of=1)`` or a hand-written membership test because
# the error they produce names the field and the offending value, which is the whole reason
# ``Gender`` above is a type and not a validator.
#
# Unlike ``department``, which stays a plain string: the departments that exist belong to the
# institution rather than to this codebase, so enumerating them here would be a guess.
Year = Annotated[int, Field(ge=USER_YEAR_MIN, le=USER_YEAR_MAX)]

# Same treatment for interests: an enum on the array's members rather than a hand-written
# membership test, so "not one of ours" is a validation error naming the offending value.
Interest = Literal[
    "Music", "Movies", "Reading", "Gaming", "Traveling", "Cooking",
    "Sports", "Art", "Photography", "Dancing", "Writing", "Coding",
]

class Cohort(BaseModel):
    """A ``(year, department)`` pair, as returned by ``/love-notes/cohorts``.

    Modelled rather than left as ``Dict[str, str]`` because that annotation is what hid the type
    change: Pydantic v2 does not coerce ``int`` into ``str``, so a heterogeneous pair returned under
    a homogeneous annotation is a ``ResponseValidationError`` -- a 500 on a screen whose only job is
    to list cohorts. Naming the halves also stops the next caller from re-deriving "which field is
    the number" from a dictionary.

    ``year`` is a plain ``int`` and not the bounded ``Year`` for the reason ``UserDetails.year`` is:
    this is a response, and a row that predates the CHECK constraint being validated would turn a
    legacy out-of-range value into a 500 on a read. Refusing to *write* such a row is the
    constraint's job; describing one that already exists must not be able to take the endpoint down.
    """
    year: int
    department: str

class Token(BaseModel):
    """
    Pydantic model for the JWT access token response sent to the client.
    """
    access_token: str
    token_type: str


def _is_non_character(codepoint: int) -> bool:
    """Unicode non-characters: reserved forever, guaranteed never to be assigned a meaning.

    There are two families and the second is the one that gets forgotten. Sixty-six of them sit
    in a contiguous block at U+FDD0-U+FDEF; the rest are the last two code points of every
    plane, so U+FFFE and U+FFFF in the BMP but also U+1FFFE, U+2FFFE and so on up to
    U+10FFFE and U+10FFFF. Testing only the BMP pair would accept a text that a reader on a
    strict implementation is entitled to replace with U+FFFD.
    """
    if 0xFDD0 <= codepoint <= 0xFDEF:
        return True
    return (codepoint & 0xFFFF) >= 0xFFFE


def _reject_unchangeable_characters(value: Optional[str], field_name: str) -> Optional[str]:
    """Reject text that cannot survive the trip to the database.

    The length cap is only half of "is this storable". A string can be well within the limit
    and still be impossible to persist:

    * a NUL byte is rejected by PostgreSQL outright -- ``text`` cannot hold 0x00, and the
      failure surfaces as an unhandled driver error rather than a validation message;
    * an unpaired surrogate (U+D800-U+DFFF) cannot be encoded to UTF-8 at all, so the
      driver raises ``UnicodeEncodeError`` while binding the parameter;
    * the remaining C0 controls, DEL and the C1 block are storable but have no meaning in a
      confession, and they are what terminal-escape and log-injection payloads are built from.

    Everything else is allowed on purpose. Emoji, Devanagari, right-to-left text and its
    directional marks are ordinary content, and a filter that rejected them would be
    rejecting the people most likely to be confessing in a language that is not English.

    Only *unchangeable* characters are refused here. Content that is merely unwelcome is a
    moderation question, and moderation already has a screen for it.
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
    """Normalise a submitted interest list, or refuse it.

    Shared by every payload that accepts interests -- the admin create and update forms and the
    participant's own profile update -- because a rule about what a participant may hold has to
    hold identically no matter which door the write came through, and three copies of the same
    three checks is three chances for one of them to drift.

    Membership is already settled by the ``List[Interest]`` annotation on each field; what is
    left is what a *list of* them has to mean:

    * a repeat is collapsed rather than stored twice. ``["Music", "Music"]`` is one interest
      the client sent twice, and storing it verbatim puts two identical badges on somebody's
      profile and makes an interest count wrong for matchmaking scoring. First occurrence wins,
      so the stored order stays the order the vocabulary declares.
    * a list longer than the vocabulary is refused. ``INTEREST_MAX_SELECTED`` is the size of
      ``INTEREST_VALUES``, so this cannot reject a real selection once the repeats above are
      gone; it exists so a client posting the same twelve values four times over gets a
      validation error rather than an array nobody has a sane way to render.

    ``None`` passes through untouched so a caller can distinguish "no interests supplied" from
    "the empty set"; which of the two reaches the column is decided by the caller.
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
        _reject_unchangeable_characters(value, "recipient_name")
        # A field the client left blank and a field it left absent mean the same thing,
        # so both land in the column as NULL rather than as "".
        normalized = value.strip()
        return normalized or None

    @validator("body")
    def _validate_body_characters(cls, value: str) -> str:
        return _reject_unchangeable_characters(value, "body")

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
    # Derived on read from the stored Cloudinary identity, never stored on the post. It is
    # withheld for an anonymous confession so the author's photo cannot unmask them; the
    # render layer hides the name on the same condition.
    profile_picture_url: Optional[str] = None


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

    ``email`` is required of *every* account type and is declared unconditionally rather than
    through the validator below: it is a non-null column on all three tables, which is what makes
    it a property of being an account at all rather than of being a particular kind of one; a
    moderator nobody can be contacted about is an account that cannot be recovered when its
    holder leaves.

    ``gender`` is participant-only, and follows the same rule ``year`` and ``department`` do
    below: optional in the type, required by the validator only when ``account_type`` is
    ``users``. It is a matchmaking filter on ``users`` and nothing else, so a staff table has no
    such column -- and a payload required to answer a question about a row that cannot hold the
    answer is a required field that can only ever be dropped on arrival.

    **Everything an account owns about itself is absent, and the model refuses it by name.**
    There is no ``profile_picture_url``, no ``bio``, no ``interests``, and none of
    the four preference flags (``notifications_enabled``, ``matchmaking_enabled``,
    ``love_notes_receive_enabled``, ``love_notes_send_enabled``). An admin mints the identity
    of an account -- who it is, which cohort it belongs to, how to reach it -- and the account
    then fills in the rest itself through ``PUT /profile/update``. Anything an admin does not
    belong in, therefore, is not merely ignored here: ``extra="forbid"`` turns it into a 422.

    That is the part worth being explicit about. Pydantic's default is ``extra="ignore"``, so a
    payload carrying ``bio`` would be accepted, the key dropped, and the caller told ``201``.
    The write would not happen -- so the column would be unreachable either way -- but the
    response would be a lie about what the request asked for, and an admin watching the list
    refresh would conclude the bio saved. Forbidding the key is what makes a refused request
    look refused.

    ``is_blocked`` is gone for the same reason, and blocking keeps its own route
    (``PUT /admin/users/{id}/block``) with its own confirmation. Block state is not a property
    you fill in when the account is born; it is a later act, on an existing account, and
    "create this participant already blocked" is not a thing that needs to exist.

    Every removed column still has a value on the new row, because the database declares the
    default rather than the payload: ``notifications_enabled``, ``matchmaking_enabled`` and
    ``love_notes_receive_enabled`` default to true, ``love_notes_send_enabled`` to false, and
    ``interests`` to the empty set -- see ``db_models.UserRecord``. ``bio`` and
    ``profile_picture_url`` are nullable and start NULL. The defaults live in one place and are
    stated identically in Pydantic's ``UserDetails``, so a participant and an admin-created
    account begin life in the same state.

    The participant-only fields are optional here and required by the validator below only
    when ``account_type`` is ``users``. Declaring them conditionally in the type system is
    not something Pydantic can express without a discriminated union, and a union would
    make the OpenAPI schema unreadable for a payload that is, in practice, one form.
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
        # Gender joins the cohort pair below: it is NOT NULL on ``users`` and closed to three
        # values, so letting it through unset would be a 500 from the insert rather than a
        # validation message.
        if not self.gender:
            raise ValueError("gender is required for a participant account.")
        # Both halves of what used to be one ``class_name``. Storing "CSE 2nd Year" in a single
        # column meant the value could only be grouped by whatever a person typed, so two
        # spellings of the same cohort were two cohorts; requiring them separately is what makes
        # them independently addressable. The database holds both as NOT NULL, so letting one
        # through here would be a 500 from the insert rather than a validation message.
        if not self.year:
            raise ValueError("year is required for a participant account.")
        if not self.department:
            raise ValueError("department is required for a participant account.")
        return self


class ConfessionCooldown(BaseModel):
    """How long this participant has to wait before their next confession.

    ``status`` is ``cooldown`` or ``eligible``, which is the same two-word vocabulary
    ``GET /matchmaking/check`` uses, so a client reading either answer checks one field.

    The seconds are in the response as well as the formatted sentence. The message is what a
    user reads; the number is what a countdown needs, and a countdown cannot subtract
    "1 hour and 58 minutes" out of a string that gets re-rounded on every render.
    """

    status: str
    message: str
    cooldown_hours: int
    remaining_minutes: int = 0
    remaining_seconds: int = 0


class AdminUserUpdate(BaseModel):
    """Payload for editing an account that already exists.

    Every field is optional, and the account is found by id across all three tables, so one
    payload shape edits a participant or a staff account. The editable set is exactly the
    identity of the account: ``regno``, ``name``, ``email``, and ``gender``, ``year`` and
    ``department`` for a participant -- the router drops those three for a staff row, for the
    same reason it drops them from any request that names a moderator: the staff tables have no
    such columns, and an edit that tried to write one would be a 500 rather than a save.

    There is deliberately no ``account_type`` here. Moving a row between tables is not a
    field edit: eleven other tables hold foreign keys into ``users`` by both id and regno, so
    relocating a row would either orphan them or require rewriting every reference under the
    hood. Which table an account lives in is therefore fixed when it is created; minting a
    staff account is a create, and demoting one is a matter of not using it.

    For the same reasons as :class:`AdminUserCreate`, there is no ``profile_picture_url``,
    no ``bio``, no ``interests``, no ``is_blocked``, and none of the four preference
    flags. A picture is bytes through a participant-only upload route rather than a string
    named in a JSON body; the profile fields belong to the participant; and blocking is its own
    route. ``extra="forbid"`` refuses any of them by name rather than accepting the request and
    dropping the key, so a caller that still sends one is told 422 instead of being answered
    ``200`` for an edit that never happened.

    ``gender`` is narrowed to the closed vocabulary, so an edit cannot write a value the create
    payload would have refused.
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
        """Treat an empty string as "not supplied" rather than as a value to write.

        A form that submits every field sends ``""`` for the ones the admin left untouched.
        Without this, editing a staff account's name through the participant-shaped form
        would blank out fields that were never offered on that form at all.

        It also keeps a cleared input from becoming a NULL in a column that cannot hold one.
        ``name``, ``regno``, ``gender``, ``year``, ``department`` and ``email`` are NOT NULL, so
        writing the ``None`` this validator produces would be a 500 from PostgreSQL rather than a
        validation message. Dropping the key is the honest reading: the admin left it empty,
        which is not the same as asking to erase it. See ``update_user_details``, which drops
        these keys outright as a second line of defence.

        ``year`` needs this more than any of the others. The participant-shaped editor submits a
        ``<select>`` with no blank option, so the only way an empty string arrives is a client
        that has not caught up with the split; letting it through would fail the year CHECK
        constraint as a 500 rather than being read as "leave it alone".
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value


class AccountDetails(BaseModel):
    """The fields every account has, whatever table it lives in.

    This is the shape every authenticated caller is resolved to, because the three tables
    share exactly these columns and nothing more. Participant-only fields are deliberately
    absent rather than null: an admin has no ``year`` or ``department``, and returning ``null``
    for them would describe a value the row does not have.

    ``email`` is shared and *required*, like the column underneath it. It used to be optional
    for a staff account, on the reasoning that creating one is a deliberate act by somebody who
    already knows the regno and nothing in sign-in needs an address. That turned out to be the
    wrong trade: a staff row with no address is an account nobody can be contacted about when its
    holder leaves or loses access, and the fix for that later is far harder than requiring an
    address at the moment an operator is already typing one. It is NOT NULL on all three tables
    for the same reason.

    ``gender`` and ``profile_picture_url`` are deliberately absent rather than null, for the
    same reason ``year`` and ``department`` are: the staff tables have no gender column and no
    picture columns, so ``null`` here would describe a value the row does not have rather than a
    value it holds as empty. Both belong to ``UserDetails`` below.
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

    ``gender`` and ``profile_picture_url`` are declared here rather than on ``AccountDetails``
    because only ``users`` has the columns underneath them: gender is a matchmaking filter and a
    picture is uploaded through a participant-only route, so on a staff row both would be
    answers about columns that do not exist. ``gender`` is required, like the column -- NOT NULL
    and closed to ``Gender`` by ``ck_accounts_gender``.

    ``interests`` stays a plain list of strings rather than a list of the closed vocabulary,
    deliberately. A CHECK constraint added by ``apply_check_constraints`` lands ``NOT VALID``,
    so rows written before it existed keep whatever they held; if this response model refused
    them, reading one such participant's profile would be a 500 on every request, which is the
    worst possible place to discover a legacy value. The write paths -- every payload that
    accepts interests -- enforce the vocabulary, which is where enforcing it does any good.
    """
    gender: Gender
    # Derived on read from the stored Cloudinary identity (``profile_picture_public_id`` plus
    # ``profile_picture_format``), never stored as a URL. Nullable because a participant who has
    # never uploaded has no picture, which is a real state and not a missing answer.
    profile_picture_url: Optional[str] = None
    bio: Optional[str] = None
    # Both NOT NULL on ``users``, so both are required here. ``year`` is a plain ``int`` rather
    # than narrowed to ``Year`` for the reason ``interests`` is: a CHECK constraint added by
    # ``apply_check_constraints`` lands ``NOT VALID``, so a row written before it existed can hold a
    # year outside the range, and refusing it here would turn reading that participant's profile
    # into a 500 on every request. The write paths enforce the range, which is where enforcing it
    # does any good.
    year: int
    department: str
    interests: List[str] = Field(default_factory=list)
    matchmaking_enabled: bool = True
    love_notes_receive_enabled: bool = True
    love_notes_send_enabled: bool = False
    last_matchmaking_at: Optional[datetime] = None
    last_confession_at: Optional[datetime] = None
    # When the participant last changed their profile picture. Read by the profile screen to
    # show the wait before anybody picks a file; the refusal itself is the conditional UPDATE
    # on the column, which answers for itself. Nullable because never having uploaded is a
    # real state -- see ``db_models.UserRecord``.
    last_profile_picture_at: Optional[datetime] = None


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
    superseded_at: Optional[datetime] = None
    superseded_by: Optional[str] = None

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

class LoveNoteImageIdentity(BaseModel):
    """The durable identity of a love note's image, and nothing derived from it.

    A love note image is an ``authenticated`` Cloudinary object, so the only thing worth keeping
    about it is *which object it is*. A usable address for it is short-lived, signed by
    Cloudinary against a token this application mints per viewer, and therefore cannot be stored:
    it would be a credential in a database column, and in every response that ever read it back.
    The delivery URL is derived at the moment it is handed out, after the viewer has been
    authorised -- see ``storage_service.love_note_delivery_url``.
    """

    public_id: str
    format: str


class LoveNote(BaseModel):
    """A love note as it is stored: identity for the image, no address for it.

    ``image`` replaces the ``image_url`` this used to carry. Nothing on the read path takes a
    stored note and needs an address without first deciding the reader is entitled to one, so
    holding one here would only invite a caller to hand it out unchecked.
    """

    id: str = Field(default_factory=new_id)
    sender_id: str
    recipient_id: str
    image: LoveNoteImageIdentity
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
    status: str = "pending"  # Can be 'pending' (not requested yet), 'requested' (waiting for response), 'accepted', 'rejected', or 'terminated' (closed by a moderator; readable, not writable)
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


# --- Wordle configuration ------------------------------------------------------
#
# Admin-side only. These describe a *puzzle* -- a day, a word, and how the word was chosen --
# and nothing about playing one: no guess, no attempt, no result, because none of those exist
# yet and a field here would be a promise the rest of the application does not keep.

# Same treatment as ``Gender`` above: a ``Literal`` so the generated schema hands the client
# the two legal modes instead of a free-text string, and a value outside the set is a 422 at
# validation rather than a check some other layer might forget to run. Mirrors the CHECK
# constraint on ``wordle_games.mode`` -- all three must agree.
WordleMode = Literal["custom", "random"]


class WordleGameCreate(BaseModel):
    """Configuring the answer for one day.

    ``game_date`` is optional and defaults to the current day in UTC, because the usual
    request *is* "today's puzzle" and making every caller restate the date would be a field
    that is never anything else. Supplying it is for scheduling a day ahead or correcting a
    save, and the date -- not the request, and not the row id -- is what identifies the
    puzzle, so it is the field uniqueness is enforced on.

    ``word`` and ``token`` are both optional in the type and required by the *mode*, which is
    why neither is a ``model_validator`` here. Whether a word is five letters and on the
    approved list is a question about the server's own list rather than about the shape of the
    request, so it is answered in the service with a 400 that says which rule failed; a 422
    from a length constraint would answer a question this payload is not asking.

    ``token`` is the proof of a draw: the server signed it for exactly this word and date, and
    ``mode="random"`` without one is refused rather than re-drawn. Re-drawing at save time
    would mean the answer the admin was shown is not the answer that was published, which is
    the one outcome this whole flow exists to prevent.

    ``extra="forbid"`` so a key the payload does not carry is refused outright rather than
    accepted and dropped -- a caller told 201 for a field that never landed is a caller who
    believes something false about the stored row.
    """

    model_config = ConfigDict(extra="forbid")

    mode: WordleMode
    word: Optional[str] = None
    token: Optional[str] = None
    game_date: Optional[date] = None


class WordleGame(BaseModel):
    """A configured puzzle as it is stored: the day, the word, and where the word came from.

    ``answer`` is returned deliberately. This is the administrator's own configuration screen
    -- the whole point of which is that the admin can see what will be published -- and the
    endpoint is gated on the capability that may publish it. There is no participant-facing
    read of this model; when one exists it will not be this one.
    """

    id: str
    game_date: date
    mode: WordleMode
    answer: str
    created_at: datetime
    updated_at: datetime


class WordleDraw(BaseModel):
    """A word the server drew, plus the signature that lets it be saved unchanged.

    The two halves travel together and only the server can produce either: ``word`` is chosen
    from the approved list here, and ``token`` is an HMAC over that word and the date it was
    drawn for. The client shows the word, sends both back, and the service re-derives the
    signature -- so what the admin saw is what gets stored, and nothing the client sends can
    become an answer the server did not pick.
    """

    word: str
    token: str


# --- Wordle gameplay --------------------------------------------------------------
#
# The participant side of the game. Everything these models describe is derived from two
# tables -- ``wordle_games`` and ``wordle_user_games`` -- and the one rule that binds them:
# a participant has at most one attempt at a puzzle, enforced by the unique pair. ``answer``
# never travels on a row that is still being played: it is present only on a completed
# participation, which is the single moment the feature exists to reveal it.


# A tile's colour accounts for one letter of one guess, rated against the day's answer.
# Closed like the genders above: the client renders exactly these three, and a value outside
# the set is a 422 rather than a colour some screen has to guess at.
WordleTile = Literal["absent", "present", "correct"]

# Whether a participation is still being played or has been settled. The database's CHECK
# constraint on ``wordle_user_games.status`` mirrors this set; all three must agree.
WordleStatus = Literal["in_progress", "won", "lost"]


class WordleParticipation(BaseModel):
    """One player's place in one day's puzzle: how far they got, and what got them there.

    ``guesses`` is the played history in order and ``results`` its per-letter verdicts in
    the same order and at the same length, so the screen renders the board straight from
    the pair. Results are re-derived on every read rather than stored, which is why there
    is exactly one history and why a re-reading client cannot receive a verdict that
    disagrees with the letters above it.

    ``answer`` is the reveal. It is NULL on a participation still in progress and set only
    once the game is over -- a completed puzzle has nothing left to hide, and the answer is
    the one field a player who lost the game is owed. The service is the only place that
    decides when it is no longer a secret, and that decision is what the GET and POST
    handlers share rather than restating. ``success`` restates ``status = 'won'`` as a
    boolean so a loss -- which also finishes the game -- is not mistaken for a win.
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

    ``game_id`` and ``game_date`` are nullable because there may be *no* playable puzzle:
    an administrator has not published anything, or published one ahead and the day has not
    arrived. ``participation`` is NULL until the player commits a first guess -- reading the
    board must not create a row, or opening the screen twice would count as two attempts.

    ``max_attempts`` and ``word_length`` are carried so the screen can draw its empty board
    without a second round trip and without hardcoding the shape of the game in the client.
    """

    game_id: Optional[str] = None
    game_date: Optional[date] = None
    participation: Optional[WordleParticipation] = None
    max_attempts: int = WORDLE_MAX_ATTEMPTS
    word_length: int = WORDLE_ANSWER_LENGTH


class WordleGuessRequest(BaseModel):
    """Guessing one word against one puzzle.

    ``game_id`` names the puzzle the guess is for -- the one the player was shown -- and
    ``guess`` is submitted raw. The five-letter lowercase normalisation happens in the
    service rather than on the model, because the reason a guess is refused (length, case,
    not a word at all) is meant to reach the client as a readable 400 rather than a generic
    422 about the field's shape -- see the message on ``WordleGameCreate`` for the same
    choice.

    ``extra="forbid"`` so a key the payload does not carry is refused outright rather than
    accepted and dropped, matching ``WordleGameCreate``.
    """

    model_config = ConfigDict(extra="forbid")

    game_id: str
    guess: str


class WordleGuessResponse(WordleParticipation):
    """A participation after one guess, plus the verdict the guess earned.

    The extra pair is redundant with the tail of ``results`` -- the new guess is always its
    own last row -- but both the toast and the keyboard need the *one* verdict that just
    happened, and munging the last row back out of the history is the kind of off-by-one
    the server should not make the client do. ``guess`` and ``result`` restate the last
    entry of ``guesses`` and ``results`` explicitly.
    """

    guess: str
    result: list[WordleTile]
