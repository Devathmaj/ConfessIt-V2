# app/models.py

from pydantic import BaseModel, ConfigDict, EmailStr, Field, computed_field, field_validator, model_validator, validator
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
    """A Wordle session as it is stored: the day, the word, and where the word came from.

    ``answer`` is returned deliberately. This is the administrator's own configuration screen
    -- the whole point of which is that the admin can see what will be published -- and the
    endpoint is gated on the capability that may publish it. There is no participant-facing
    read of this model; when one exists it will not be this one.

    ``answer`` is null while the session is pending (manual mode, word not yet
    provided). ``status`` is the lifecycle state; ``period_*`` is the calendar
    period the session covers, which an Update shortens rather than renews.
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

    The two halves travel together and only the server can produce either: ``word`` is chosen
    from the approved list here, and ``token`` is an HMAC over that word and the date it was
    drawn for. The client shows the word, sends both back, and the service re-derives the
    signature -- so what the admin saw is what gets stored, and nothing the client sends can
    become an answer the server did not pick.
    """

    word: str
    token: str


# --- Anagram configuration -----------------------------------------------------
#
# Admin-side only, and the same boundary the Wordle models above hold: these describe a
# puzzle -- a day, a word, and where the word came from -- and nothing about playing one.
# No scramble, no guess, no attempt, no result, because each of those belongs to a
# *player's* relationship with the puzzle and there is no player-side table yet.
#
# ``AnagramMode`` is a ``Literal`` for the reason ``WordleMode`` is: the generated schema
# hands the client the two legal modes instead of a free-text string, a value outside the
# set is a 422 at validation, and all three of the payload, the service and the CHECK
# constraint on ``anagram_games.mode`` agree on the same two words.

AnagramMode = Literal["custom", "random"]


class AnagramGameCreate(BaseModel):
    """Configuring the answer for one day's Anagram.

    ``word`` and ``token`` are both optional here and required by the *mode*, which is why
    neither is validated above: whether a supplied word is non-empty, letters only and short
    enough is a rule about the answer rather than about the shape of the request, so it is
    answered in the service with a 400 that says which rule failed. The minimum length a
    *random* draw obeys is likewise a rule about the word list, not about this payload.

    ``mode="random"`` carries the word a preview drew together with that draw's token. The
    server re-derives the signature, so a token minted for another day -- or one a client
    made up -- is a 400 rather than a second chance to choose the answer.

    ``extra="forbid"`` for the same reason ``WordleGameCreate`` sets it: a key the payload
    does not carry is refused outright rather than accepted and dropped.
    """

    model_config = ConfigDict(extra="forbid")

    mode: AnagramMode
    word: Optional[str] = None
    token: Optional[str] = None
    game_date: Optional[date] = None


class AnagramGame(BaseModel):
    """A configured puzzle as it is stored: the day, the word, and who published it.

    ``answer`` is returned deliberately, for the reason the Wordle model returns its own:
    this is the administrator's configuration screen, and the endpoint it comes from is
    closed to everyone who may not publish. It is always the canonical, uppercase word --
    never a scramble, because each player's arrangement is generated per user and does not
    exist yet.

    ``created_by`` is the publishing account's ``regno``, so the list can show who set each
    day's word without a second lookup.
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

    The two halves travel together and only the server can produce either: ``word`` is chosen
    from the eligible pool here, and ``token`` is an HMAC over that word and the date it was
    drawn for. The client shows the word, sends both back, and the service re-derives the
    signature -- so what the admin saw is what gets stored, and nothing the client sends can
    become an answer the server did not pick.
    """

    word: str
    token: str


# --- Couple configuration ------------------------------------------------------
#
# Admin-side only, and a smaller surface than either puzzle above: these describe a
# *featured date* -- which day the game is active for -- and nothing else. There is
# no mode, no word, no answer and no per-day content selection, because there is
# nothing per-day for an administrator to choose: the 50-question bank is shared
# application content seeded at startup, and a featured date simply marks the game
# active for that day.

class CoupleGameCreate(BaseModel):
    """Marking the Couple Prediction game featured for one day.

    ``game_date`` is optional and defaults to the current day in UTC, because the
    usual request *is* "feature it today" and making every caller restate the date
    would be a field that is never anything else. Supplying it is for scheduling a
    day ahead or correcting a save, and the date -- not the request, and not the
    row id -- is what identifies the featured instance, so it is the field
    uniqueness is enforced on.

    ``extra="forbid"`` so a key the payload does not carry is refused outright
    rather than accepted and dropped -- a caller told 201 for a field that never
    landed is a caller who believes something false about the stored row.
    """

    model_config = ConfigDict(extra="forbid")

    game_date: Optional[date] = None


class CoupleGame(BaseModel):
    """A featured Couple game as it is stored: the day, and who marked it so.

    There is no answer to return here, because there is nothing per-day to hide:
    the question bank is shared and static, and the player surface does not exist
    yet. ``created_by`` is the publishing account's ``regno``, so the list can show
    who featured each day without a second lookup.
    """

    id: str
    game_date: date
    created_by: str
    created_at: datetime
    updated_at: datetime


# --- Couple question bank ------------------------------------------------------
#
# The static, shared content the featured game runs on. These are read shapes for
# the bank seeded at startup -- fifty templates naming ``X`` with exactly four
# options each -- and the answer a participant selected for one of them. There is
# no player workflow here yet: no pairing, no prediction submission, no scoring,
# no completion state, no timer. The answer model below is the storage foundation
# that workflow will later build on.

class CoupleGameQuestion(BaseModel):
    """One bank template as it is stored: the question naming ``X``, its options,
    and where it sits in the sequence.

    ``options`` is always four entries in display order. ``stable_key`` is the
    deterministic identity (``couple-q-01`` … ``couple-q-50``) the startup seed
    uses to decide whether the predefined question already exists.
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

    ``selected_option_index`` is positional into the question's four options (0 for
    the first … 3 for the fourth) rather than a copy of the option text, so the
    stored answer keeps pointing at the right option even if bank wording is
    ever corrected.
    """

    id: str
    question_id: str
    user_id: str
    selected_option_index: int
    created_at: datetime
    updated_at: datetime


# --- Couple gameplay -----------------------------------------------------------
#
# The participant side of the Couple Questionnaire. The admin models above mark a
# day as featured and the bank holds the fifty shared templates; everything here
# describes one player's *place* in that day's game: who they chose, the session
# a mutual choice created, their own questions, their predictions of their
# partner, and the score derived from the two. The partner's original answers
# never travel on a read that is not the completed result -- they are the thing
# the prediction phase exists to keep hidden.

# Where the caller stands in the day's game. The database's CHECK constraint on
# ``couple_game_sessions.status`` mirrors the three in-session states; the other
# three describe the absence of a session or of a playable day.
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


class CoupleSelectionRequest(BaseModel):
    """Choosing a partner: the one user this caller wants to play with.

    ``extra="forbid"`` so a key the payload does not carry is refused outright
    rather than accepted and dropped, matching ``WordleGuessRequest``.
    """

    model_config = ConfigDict(extra="forbid")

    selected_user_id: str


class CoupleAnswerRequest(BaseModel):
    """Answering one of this caller's own session questions.

    ``session_question_id`` names the session question -- one of the fifteen this
    caller was assigned -- and ``selected_option_index`` is positional into its
    four options. Which session the question belongs to is derived from the
    caller, never from the payload, so a caller cannot answer into someone
    else's game by naming its ids.
    """

    model_config = ConfigDict(extra="forbid")

    session_question_id: str
    selected_option_index: int = Field(ge=0, le=3)


class CouplePredictionRequest(BaseModel):
    """Predicting the partner's answer to one of the partner's session questions.

    ``session_question_id`` names one of the fifteen questions the *partner*
    answered, and ``predicted_option_index`` is the caller's guess at which
    option the partner picked. The verdict is computed server-side against the
    stored original, so nothing the client sends can mark itself correct.
    """

    model_config = ConfigDict(extra="forbid")

    session_question_id: str
    predicted_option_index: int = Field(ge=0, le=3)


class CoupleQuestionView(BaseModel):
    """One of this caller's own questions, as the answering screen renders it.

    ``question_text`` is already personalised: the bank's ``X`` reads as "you"
    here, because the person answering is the one being asked about. The stored
    template is untouched -- this rendering exists only on the read.
    ``selected_option_index`` is this caller's submitted answer, or null when
    unanswered.
    """

    session_question_id: str
    question_id: str
    question_text: str
    options: List[str]
    shared: bool
    selected_option_index: Optional[int] = None


class CouplePredictionView(BaseModel):
    """One of the partner's questions, as this caller predicts it.

    ``question_text`` names the partner, because the caller is guessing what the
    partner chose. ``predicted_option_index`` is this caller's submitted
    prediction, or null when unpredicted. The partner's original answer is never
    carried here -- not while predicting -- it surfaces only in the completed
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

    The three halves of the verdict together: what the partner actually picked,
    what this caller guessed, and whether the two matched. Emitted only in the
    completed result, which is the single moment the feature exists to reveal the
    partner's originals.
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

    Each side is scored out of the fifteen predictions they owed: one point per
    prediction matching the partner's stored original. ``review`` replays this
    caller's fifteen predictions against those originals.
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

    Reading this never creates anything but answers nothing twice: a fresh caller
    reads ``selecting_partner`` with no selection, a choosing caller reads their
    pending pick, and a paired caller reads their questions, their predictions or
    their score depending on how far both sides have played. ``questions`` holds
    this caller's own fifteen while answering, ``predictions`` the partner's
    fifteen in stable random order while predicting, and ``result`` the settled
    score once both sides are done -- each present only in its phase, so a screen
    renders what it is given rather than deciding what the caller may see.
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
# The player-facing half of Anagram. The admin models above describe a puzzle -- a day, a
# word, where the word came from -- and these describe one player's *place* in that
# puzzle: their private scramble, their guesses, whether they are done, and how long they
# have actually played. ``anagram_games.answer`` never travels on a participation that is
# still in progress; it is present only on a completed one, which is the single moment the
# feature exists to reveal it.

# Whether a participation is still being played or has been settled. The database's CHECK
# constraint on ``anagram_user_games.status`` mirrors this set; all three must agree.
AnagramStatus = Literal["in_progress", "won", "lost"]


class AnagramParticipation(BaseModel):
    """One player's place in one day's puzzle: their scramble, their guesses, their time.

    ``scrambled_word`` is the private permutation this player was dealt at reveal: the
    tiles on their screen, in the order they arrived. It is dealt once and never re-dealt,
    so a returning player -- refresh, navigation, logout and back -- always sees the same
    tiles.

    ``active_time_seconds`` is finished timing segments, summed; ``timer_started_at`` opens
    the current segment and is NULL while paused or finished. ``timer_running`` restates
    "there is an open, non-stale segment" as one boolean so the screen knows whether to
    tick or to offer RESUME without doing clock arithmetic of its own. The client still
    owns the visible ticking; the server owns every number that persists.

    ``answer`` is the reveal. It is NULL on a participation still in progress and set only
    once the game is over -- and then only a loss needs it shown, since a winner already
    typed it. The service is the only place that decides when it is no longer a secret.
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

    ``game_id`` and ``game_date`` are nullable because there may be *no* playable puzzle:
    an administrator has not published anything, or published one ahead and the day has not
    arrived. ``participation`` is NULL until the player presses REVEAL WORD -- reading the
    screen must not create a row, or opening it twice would start two timers' worth of
    confusion. ``max_attempts`` is carried so the screen can draw its attempts line without
    a second round trip and without hardcoding the shape of the game in the client.
    """

    game_id: Optional[str] = None
    game_date: Optional[date] = None
    participation: Optional[AnagramParticipation] = None
    session_status: Optional[str] = None
    message: Optional[str] = None
    max_attempts: int = ANAGRAM_MAX_ATTEMPTS


class AnagramActionRequest(BaseModel):
    """Starting, pausing or checkpointing one puzzle for this player.

    ``game_id`` names the puzzle -- the one the player was shown -- so a stale screen
    cannot start a timer on yesterday's game after midnight has moved the current one on.
    The three timer transitions share this one shape because they differ only in which
    segment edge they move; the guess, which also scores, carries its own payload below.

    ``extra="forbid"`` so a key the payload does not carry is refused outright rather than
    accepted and dropped, matching ``WordleGuessRequest``.
    """

    model_config = ConfigDict(extra="forbid")

    game_id: str


class AnagramGuessRequest(BaseModel):
    """Guessing one arrangement against one puzzle.

    ``game_id`` names the puzzle the guess is for -- the one the player was shown -- and
    ``guess`` is submitted raw. The uppercase letters-only normalisation happens in the
    service rather than on the model, because the reason a guess is refused (not letters,
    wrong length, not a rearrangement of the puzzle at all) is meant to reach the client as
    a readable 400 rather than a generic 422 about the field's shape.

    ``extra="forbid"`` so a key the payload does not carry is refused outright rather than
    accepted and dropped, matching ``WordleGuessRequest``.
    """

    model_config = ConfigDict(extra="forbid")

    game_id: str
    guess: str


class AnagramGuessResponse(AnagramParticipation):
    """A participation after one guess, plus the verdict the guess earned.

    The extra pair is redundant -- a correct guess is always the tail of ``guesses`` -- but
    the toast needs the *one* outcome that just happened, and re-deriving "was the last
    guess right" client-side is exactly the trust-the-client the server exists to avoid.
    ``guess`` restates the submitted arrangement and ``correct`` whether it matched.
    """

    guess: str
    correct: bool


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

    ``session_status`` mirrors the session lifecycle: ``pending`` means today's
    word has not been configured yet and the board is not playable, in which
    case ``message`` carries the sentence the screen shows.

    ``max_attempts`` and ``word_length`` are carried so the screen can draw its empty board
    without a second round trip and without hardcoding the shape of the game in the client.
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


# --- Game configuration, featured games, scheduling and selection ---------------
#
# The admin and player surfaces of the separated model. Configuration owns
# per-game availability and cadence, featured owns the one-spotlight-per-day
# schedule, scheduled content owns future manual words inside the finite
# horizon, and the selection response is what ``GET /games/select`` serves so a
# screen never has to re-derive availability client-side.

GameResetCadence = Literal["daily", "every_2_days", "every_3_days", "every_5_days", "weekly"]
GameContentMode = Literal["random", "manual"]
GameId = Literal["wordle", "anagram", "couple-questionnaire", "reaction-time", "connections", "trivia-drop", "two-key-escape"]


class GameConfig(BaseModel):
    """One game's availability, content mode, reset cadence and grid size, as stored."""

    game_id: str
    enabled: bool = True
    is_hidden: bool = False
    content_mode: Optional[GameContentMode] = None
    reset_cadence: GameResetCadence = "daily"
    # The Two-Key Escape maze edge length. None for every other game: it is
    # that game's own knob, not a second content mode, and no other screen
    # may read or write it.
    grid_size: Optional[int] = None


class GameConfigUpdate(BaseModel):
    """Patching one game's configuration; every field is optional."""

    model_config = ConfigDict(extra="forbid")

    enabled: Optional[bool] = None
    is_hidden: Optional[bool] = None
    content_mode: Optional[GameContentMode] = None
    reset_cadence: Optional[GameResetCadence] = None
    grid_size: Optional[int] = None


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

    Hidden games never appear here at all -- the list carries only what the
    caller may discover. ``playable`` is false for disabled games and for
    games with no configured session, and ``message`` is the sentence the
    screen shows for either case.
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
# Admin-side only, and the smallest surface of any game: these describe a
# *featured date* -- which day holds the spotlight -- and nothing else. There
# is no mode, no word, no answer, no timing range, no difficulty and no
# attempt budget, because the mechanics are fixed by the application and the
# sessions roll on the game's own cadence with a fresh five-submission budget
# per player per session. Featuring never touches a session.

class ReactionTimeGameCreate(BaseModel):
    """Featuring the Reaction Time game for one day.

    ``game_date`` is optional and defaults to the current day in UTC, because
    the usual request *is* "feature it today". One game holds the spotlight
    per date across all games, so a day that already features anything is
    refused.

    ``extra="forbid"`` so a key the payload does not carry is refused outright
    rather than accepted and dropped.
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
#
# The participant side of the game. Sessions roll on the game's own cadence
# whether or not the day is featured; everything here describes one player's
# *place* in the current session: their five-or-fewer submissions, each born
# pending on the blue-screen click and settled later as valid (a click after
# green, carrying the client-measured reaction time) or too early (a click
# before green, carrying only informational elapsed time that is never a
# score).

# Whether a submission is still waiting for its result or has been settled.
# Mirrors the CHECK constraint on ``reaction_time_submissions.status``.
ReactionTimeStatus = Literal["pending", "valid", "too_early"]


class ReactionTimeAttempt(BaseModel):
    """One submission, as the player screen renders it.

    ``reaction_time_ms`` is set only on ``valid`` rows; ``elapsed_ms`` only on
    ``too_early`` rows, where it says how quickly the user clicked after the
    red/waiting state began rather than any kind of score.
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

    Reading this never creates anything: a fresh caller reads zero submissions
    and a blue start screen, a returning caller reads the submissions they
    left -- including a ``pending`` one when they navigated away mid-attempt,
    which still counts as consumed. A new session means a fresh budget of
    five. ``best_ms`` is the minimum valid reaction time and ``average_ms``
    the mean of valid reaction times only; both are null when the caller has
    no valid attempt yet. Every number here is derived server-side from the
    stored submissions, never trusted from the client.
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

    ``game_id`` names the session -- the one the player was shown -- so a
    stale screen cannot start an attempt on an ended session. The attempt
    number is assigned server-side from the stored count, never from the
    payload, because there is no payload field that could honestly know it.
    """

    model_config = ConfigDict(extra="forbid")

    game_id: str


class ReactionTimeCompleteRequest(BaseModel):
    """Settling a pending attempt with its measured result.

    ``submission_id`` names the pending submission this caller owns; settled
    rows are immutable, so a second submit for the same id is refused rather
    than overwritten. ``status="valid"`` carries the client-measured
    ``reaction_time_ms`` (green transition until click); ``status="too_early"``
    carries only the informational ``elapsed_ms`` (red start until the early
    click), which is never a score. Bounds are enforced in the service with a
    readable 400; the column CHECKs guard writes that never went through the
    API.
    """

    model_config = ConfigDict(extra="forbid")

    submission_id: str
    status: Literal["valid", "too_early"]
    reaction_time_ms: Optional[int] = None
    elapsed_ms: Optional[int] = None
    green_at: Optional[datetime] = None
    clicked_at: Optional[datetime] = None


# --- Connections configuration --------------------------------------------------
#
# Admin-side only, and the same boundary the Wordle models hold: these describe a
# puzzle -- sixteen words in four canonical answer groups -- and nothing about
# playing one. No scores, no scrambles, no arrangements, because each of those
# belongs to a *player's* relationship with the puzzle. There is no random mode:
# every session needs an administrator to supply its own sixteen words, and a
# session with no valid puzzle stays pending and unplayable rather than drawing
# anything.

class ConnectionsPuzzleCreate(BaseModel):
    """Configuring one Connections puzzle: sixteen words in four groups of four.

    ``groups`` holds the four canonical answer groups in order; each group is
    exactly four words. Words are submitted raw -- the service trims them,
    refuses empties and duplicates (case-insensitively), and stores both the
    display spelling and the normalized spelling comparison runs on. Omitted
    entirely, the update instead consumes the earliest scheduled puzzle inside
    the new window, or opens the session pending when the window holds none.

    ``extra="forbid"`` so a key the payload does not carry is refused outright
    rather than accepted and dropped.
    """

    model_config = ConfigDict(extra="forbid")

    groups: Optional[List[List[str]]] = None


class ConnectionsGame(BaseModel):
    """A Connections session as it is stored: the day, its puzzle, its lifecycle.

    ``groups`` is returned deliberately -- this is the administrator's own
    configuration screen, and the endpoint it comes from is closed to everyone
    who may not publish. There is no participant-facing read of this model.
    ``groups`` is empty while the session is pending (manual content not yet
    provided). ``status`` is the lifecycle state; ``period_*`` is the calendar
    period the session covers.
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
#
# The participant side of the game. The admin models above describe a puzzle --
# sixteen words and where each belongs -- and these describe one player's
# *place* in that puzzle: the shuffled tiles they were dealt and whether they
# have submitted. The canonical groups never travel on any of these reads;
# ``words`` is display spellings only, in this player's own stable shuffled
# order. Scores are computed and persisted server-side but never returned to
# the player -- the submit response is a confirmation and nothing more.

class ConnectionsCurrentGame(BaseModel):
    """Today's puzzle and this player's submission state, as the screen opens.

    ``game_id`` and ``game_date`` are nullable because there may be *no*
    playable puzzle: no session, or a pending session still waiting for its
    puzzle. ``words`` is this player's stable shuffled tile order -- empty
    when there is nothing playable or once they have submitted. ``submitted``
    is whether this player already consumed their one submission for this
    session; a submitted player reads a confirmation rather than a board.

    ``session_status`` mirrors the session lifecycle: ``pending`` means the
    puzzle has not been configured yet, in which case ``message`` carries the
    sentence the screen shows.
    """

    game_id: Optional[str] = None
    game_date: Optional[date] = None
    words: List[str] = []
    submitted: bool = False
    session_status: Optional[str] = None
    message: Optional[str] = None


class ConnectionsSubmitRequest(BaseModel):
    """Submitting one final arrangement against one puzzle.

    ``game_id`` names the puzzle the arrangement is for -- the one the player
    was shown -- and ``groups`` is the player's final assignment as four lists
    of four words each. Words are submitted as displayed; the service
    normalizes and validates them against the stored puzzle (all sixteen
    distinct puzzle words exactly once, no unknown words).

    ``extra="forbid"`` so a key the payload does not carry is refused outright
    rather than accepted and dropped.
    """

    model_config = ConfigDict(extra="forbid")

    game_id: str
    groups: List[List[str]]


class ConnectionsSubmitResponse(BaseModel):
    """The confirmation a submitted player reads -- and all they read.

    Deliberately carries no score, no correctness, and no answer key: not how
    many groups or words were correct, not which groups matched, not the
    canonical memberships. Those values are persisted server-side for a future
    leaderboard, never returned here.
    """

    game_id: str
    submitted: bool = True
    submitted_at: datetime
    message: str = "Your Connections puzzle has been submitted successfully."


# --- Trivia Drop configuration --------------------------------------------------
#
# Admin-side only, and the same boundary the other puzzle models hold: these
# describe a question -- its text, its four options, its correct-option set --
# and nothing about answering one. There is no reset cadence and no
# random-content mode: each question carries its own release schedule and
# becomes an individual live session.
#
# The answer mode is derived, never configured: exactly one correct option is
# a single-answer question, more than one is multiple-answer. No payload here
# carries a mode, so no mode can disagree with the key.

TriviaDropAnswerMode = Literal["single", "multiple"]
TriviaDropReleaseMode = Literal["now", "scheduled", "draft"]
TriviaDropStatus = Literal["draft", "scheduled", "active", "ended", "cancelled"]


class TriviaDropOption(BaseModel):
    """One answer option as stored: its stable index, its text, its key membership."""

    option_index: int
    option_text: str
    is_correct: bool


class TriviaDropQuestionCreate(BaseModel):
    """Authoring one Trivia Drop question: text, four options, the correct set.

    ``correct_indexes`` names the correct options by their 0-3 positions into
    ``options`` -- one or more, with at least one required. Whether the
    question plays single- or multiple-answer is derived from how many are
    named, so no mode is configured. ``release_mode="now"`` deploys
    immediately (ending any live question); ``"scheduled"`` needs
    ``scheduled_for`` and waits for the scheduler; ``"draft"`` stores it for
    later. Options are submitted raw; trimming, emptiness, duplicates and
    key-membership are all answered in the service with a 400.

    ``extra="forbid"`` so a key the payload does not carry is refused outright
    rather than accepted and dropped.
    """

    model_config = ConfigDict(extra="forbid")

    question_text: str
    options: List[str]
    correct_indexes: List[int]
    release_mode: TriviaDropReleaseMode = "draft"
    scheduled_for: Optional[datetime] = None


class TriviaDropQuestion(BaseModel):
    """A Trivia Drop question as it is stored: text, options, key, lifecycle.

    ``correct_indexes`` is returned deliberately -- this is the
    administrator's own configuration screen, and the endpoint it comes from
    is closed to everyone who may not publish. ``answer_mode`` restates
    "one correct" vs "several correct" so the console can label the question
    without counting. ``scheduled_for`` is the intended release;
    ``released_at`` the authoritative activation instant, written once.
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
    """Editing a draft: text, options and the correct set, all replaceable.

    Only drafts with no submissions may be edited -- anything live or answered
    is immutable for its session, and corrections arrive as a new question.
    Every field supplied replaces its stored counterpart wholesale, so the
    four-option invariant and the key-membership rule are re-validated as one.

    ``extra="forbid"`` for the same reason the create payload sets it.
    """

    model_config = ConfigDict(extra="forbid")

    question_text: Optional[str] = None
    options: Optional[List[str]] = None
    correct_indexes: Optional[List[int]] = None


class TriviaDropScheduleRequest(BaseModel):
    """Scheduling (or rescheduling) one question's release.

    The instant is stored as UTC; the admin screen labels the control as UTC
    so an entered time converts consistently rather than by the operator's
    local clock.

    ``extra="forbid"`` for the same reason the create payload sets it.
    """

    model_config = ConfigDict(extra="forbid")

    scheduled_for: datetime


# --- Trivia Drop gameplay -------------------------------------------------------
#
# The participant side of the game. The admin models above describe questions;
# these describe one player's *place* in the live one. The correct-option set
# never travels on any of these reads: the live question carries display
# options plus the derived mode (single vs multiple) the screen needs to
# render its instruction and its controls, and nothing that names the key --
# not the correct indexes, not even how many of them there are.

class TriviaDropPublicOption(BaseModel):
    """One answer option as a player sees it: its stable index and its text."""

    option_index: int
    option_text: str


class TriviaDropCurrentQuestion(BaseModel):
    """The live question and this player's submission state, as the screen opens.

    ``question_id`` is null when no question is live -- the waiting state, not
    a puzzle to invent. ``options`` are display only; ``answer_mode`` tells the
    screen whether to render "Select one answer" with single-selection or
    "Select all that apply" with multi-selection. ``submitted`` is whether
    this player already consumed their one submission for this question; a
    submitted player reads a confirmation rather than a board.
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

    ``question_id`` names the question -- the one the player was shown -- and
    ``selected_indexes`` is the whole selection: one index for a
    single-answer question, the complete set for a multiple-answer one.
    Several checked boxes never become several requests; the one request is
    the one submission. Normalization and key comparison happen in the
    service, so the reason a selection is refused reaches the client as a
    readable 400 rather than a generic 422.

    ``extra="forbid"`` so a key the payload does not carry is refused outright
    rather than accepted and dropped -- in particular no client-supplied
    correctness, score, timestamp or user id is accepted here.
    """

    model_config = ConfigDict(extra="forbid")

    question_id: str
    selected_indexes: List[int]


class TriviaDropSubmitResponse(BaseModel):
    """The confirmation an answered player reads -- and all they read.

    Deliberately carries no correctness and no response time while the
    question is live: those would expose the key to other users. They are
    persisted server-side and surface on the ended question's results.
    """

    question_id: str
    submitted: bool = True
    submitted_at: datetime
    message: str = "Your answer has been recorded."


class TriviaDropLeaderboardEntry(BaseModel):
    """One ranked submission on an ended question's results.

    Correct answers rank above incorrect ones, faster correct responses above
    slower ones, with the authoritative submission instant breaking ties.
    ``selected_indexes`` is the player's own complete selection, echoed so a
    historical result stays reconstructable without a second lookup.
    """

    rank: int
    user_id: str
    display_name: str
    is_correct: bool
    response_time_ms: int
    submitted_at: datetime
    selected_indexes: List[int]


class TriviaDropMySubmission(BaseModel):
    """This caller's own settled answer on one question.

    A slice of the leaderboard entry without rank or identity: whether it was
    correct, how long after release it landed, and the complete selection it
    carried.
    """

    is_correct: bool
    response_time_ms: int
    submitted_at: datetime
    selected_indexes: List[int]


class TriviaDropPreviousQuestion(BaseModel):
    """The most recently ended question, with its answer key revealed.

    The key belongs to history once the session ends, so this is public to
    players -- unlike the live question, which never names it. Empty
    (``question_id`` null) when no question has ended yet, in which case the
    screen shows nothing at all.
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
    """An ended question with its key revealed and its leaderboard settled.

    Served only once the session has ended: the correct indexes and the
    per-submission correctness would expose the live key, so a live question
    is never answered here. ``my_submission`` is this caller's own entry, or
    null when they never answered; ``leaderboard`` is every accepted
    submission in rank order.
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
#
# The participant side of the cooperative maze. One global session period owns
# a single maze template (edge length, seed, walls, locked cells); each
# mutually selected couple owns one live overlay on it. Roles are submitted
# then finalized: each side confirms one of ``navigator`` / ``key_operator``,
# and the assignment becomes real only when both are stored and distinct.
# Clients submit actions (move, key), never state; the service validates and
# commits each one atomically under row lock.

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
    """One key as the Key Operator sees it: the lock it is spent on.

    Keys are reusable -- every use costs the cooldown and a Navigator move
    -- so there is no spent state. Which lock each key *opens* stays hidden:
    only trial reveals the mapping.
    """

    key_index: int
    target_row: int
    target_col: int


class TwoKeyEscapeResultView(BaseModel):
    """The settled couple result, finalized exactly once at the exit.

    ``elapsed_seconds`` is the whole ranking: seconds from the first valid
    move to the exit, including any time away. ``total_moves`` and
    ``total_keys`` are progress facts, never tiebreaks. There is no score:
    no other game carries one.
    """

    completed: bool
    total_moves: int = 0
    total_keys: int = 0
    elapsed_seconds: Optional[float] = None
    completed_at: Optional[datetime] = None


class TwoKeyEscapeState(BaseModel):
    """This caller`s place in the couple`s maze, as the page opens and after
    every write. Reading this never creates an escape game for a caller with
    no couple session; the first confirmed role submission creates it.

    ``stage`` is ``no_couple`` (no mutual-selection session yet),
    ``selecting_roles`` (roles unfinalized, with both sides` confirmed
    choices carried so a conflict names itself), ``playing`` or
    ``completed``. The board (walls, locked, blocked, player, exit, keys,
    cooldown, movement gate) is the same authoritative overlay for both
    participants; ``my_role`` is the caller`s own finalized role, or null
    before finalization.
    """

    stage: Literal["no_couple", "selecting_roles", "playing", "completed"]
    game_id: Optional[str] = None
    session_id: Optional[str] = None
    # This caller's pending questionnaire partner choice, if they made one.
    # Choosing here and choosing in the Couple Questionnaire are the same
    # act on the same row, so a pair formed in either game opens the other.
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
    """One couple`s standing in the current period, in rank order.

    Completed couples rank above incomplete ones; among completed, the
    shorter start-to-finish time wins. There is no score: no other game
    carries one.
    """

    rank: int
    couple_names: List[str]
    completed: bool
    total_moves: int
    total_keys: int
    elapsed_seconds: Optional[float] = None


# --- League leaderboards ------------------------------------------------------
#
# The unified scoring layer across all games. Individual boards rank users;
# couple boards rank canonical pairs; per-game boards rank one game's
# normalized contribution. Scores are server-calculated league points read
# from the persisted award ledger -- the frontend never computes them.
# ``native`` carries performance numbers only (attempts, times, correctness):
# answers, words, prompts and selections never travel through these models.

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


class LeagueFinalizeRequest(BaseModel):
    """Finalize one closed event's league awards (idempotent repair path)."""

    model_config = ConfigDict(extra="forbid")

    game_id: str
    opportunity_id: str


class LeagueSummary(BaseModel):
    """Finalized opportunities and live awards per game, for operators."""

    games: Dict[str, Dict[str, int]]
