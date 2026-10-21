"""Normalized SQLAlchemy schema for PostgreSQL application data.

There are three account tables -- ``users`` for participants, ``admin`` and ``moderator``
for staff -- and an account's privileges are a function of which one its row is in, not of
any column it holds. Nothing in this schema stores a role.
"""
import uuid
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey, Index, Integer, LargeBinary,
    Numeric, SmallInteger, ARRAY, String, Text, UniqueConstraint, Uuid, event, func, select, text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

try:
    from pgvector.sqlalchemy import Vector as _PgVector
except ImportError:  # pragma: no cover - pgvector is a required dependency
    _PgVector = None  # type: ignore[assignment]

from .limits import (
    ANAGRAM_ANSWER_MAX,
    ANAGRAM_MAX_ATTEMPTS,
    COMMENT_BODY_MAX,
    CONFESSION_BODY_MAX,
    CONNECTIONS_GROUPS_COUNT,
    CONNECTIONS_WORD_MAX,
    CONNECTIONS_WORDS_PER_GROUP,
    CONNECTIONS_WORDS_TOTAL,
    COUPLE_OPTION_TEXT_MAX,
    COUPLE_OPTIONS_PER_QUESTION,
    COUPLE_QUESTION_TEXT_MAX,
    COUPLE_QUESTIONS_COUNT,
    COUPLE_SESSION_EXCLUSIVE_PER_PLAYER,
    COUPLE_SESSION_QUESTIONS,
    COUPLE_SESSION_SHARED_QUESTIONS,
    GAME_CONTENT_MODES,
    GAME_IDS,
    TWO_KEY_ESCAPE_GRID_SIZES,
    REACTION_TIME_MAX_ELAPSED_MS,
    REACTION_TIME_MAX_SUBMISSIONS,
    REACTION_TIME_MAX_VALID_MS,
    REACTION_TIME_MIN_VALID_MS,
    INTEREST_VALUES,
    LOVE_NOTE_BODY_MAX,
    MAX_FUTURE_SCHEDULE_DAYS,
    MESSAGE_BODY_MAX,
    REPORT_REASON_MAX,
    RESET_CADENCE_VALUES,
    SESSION_STATUS_VALUES,
    TRIVIA_OPTIONS_COUNT,
    TRIVIA_OPTION_TEXT_MAX,
    TRIVIA_QUESTION_TEXT_MAX,
    USER_DEPARTMENT_MAX,
    USER_GENDER_VALUES,
    USER_REGNO_MAX,
    USER_YEAR_MAX,
    USER_YEAR_MIN,
    WORDLE_ANSWER_LENGTH,
    WORDLE_MAX_ATTEMPTS,
)
from .accounts import (
    ACCOUNT_TYPE_VALUES,
    AccountType,
    is_valid_account_type,
    normalize_account_type,
)

# Rendered into the CHECK constraints below. The values are literals in trusted source, not
# user input, and quoting them here rather than passing them as bind parameters keeps each
# constraint a single self-contained DDL expression that survives the NOT VALID re-add in
# ``PostgresClient.apply_check_constraints``.
_ACCOUNT_TYPE_SQL_LIST = ", ".join(f"'{value}'" for value in ACCOUNT_TYPE_VALUES)
_GENDER_SQL_LIST = ", ".join(f"'{value}'" for value in USER_GENDER_VALUES)

# SQL array literal for the interests CHECK; cast to ``varchar[]`` to match the column type.
_INTEREST_SQL_ARRAY = (
    "ARRAY[" + ", ".join(f"'{value}'" for value in INTEREST_VALUES) + "]::varchar[]"
)

# Exclude NULL explicitly because PostgreSQL CHECK constraints accept unknown results.
_GENDER_CHECK_SQL = (
    f"gender IS NOT NULL AND gender IN ({_GENDER_SQL_LIST})"
)

# Years are bounded integers because matchmaking groups by numeric cohort; bounds come from limits.
_YEAR_RANGE_SQL = f"BETWEEN {USER_YEAR_MIN} AND {USER_YEAR_MAX}"
_YEAR_CHECK_SQL = f"year IS NOT NULL AND year {_YEAR_RANGE_SQL}"

def _non_character_code_points() -> list[int]:
    """Return Unicode non-characters: U+FDD0–U+FDEF and each plane's final two points."""
    return (
        list(range(0xFDD0, 0xFDEF + 1))
        + [code_point for plane in range(17) for code_point in (plane << 16 | 0xFFFE, plane << 16 | 0xFFFF)]
    )


# Build the regex from actual code points; PostgreSQL's ``\u`` escape cannot represent all planes.
_NON_CHARACTERS_PATTERN = "[" + "".join(chr(code_point) for code_point in _non_character_code_points()) + "]"


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    """An aware UTC default, so a row written outside a request still gets a real instant."""
    return datetime.now(timezone.utc)


class AccountColumns:
    """Columns shared by participant, admin, and moderator account tables.

    Participant-only profile and matchmaking fields live on ``UserRecord``. Shared identity
    fields, including required email, stay aligned through this mixin; uniqueness is per table.
    """

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    regno: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_login_ip: Mapped[Optional[str]] = mapped_column(INET)
    last_login_user_agent: Mapped[Optional[str]] = mapped_column(Text)
    # ``onupdate`` is applied by SQLAlchemy on the ORM side, and ``server_default`` covers
    # writes that bypass it. Both are set so ``updated_at`` cannot silently stay at the
    # creation instant for a row written by a script.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )


# ``ck_accounts_gender`` is declared on ``UserRecord`` alone rather than on a shared tuple
# attached to every account table: gender is a participant field now, and a CHECK on ``gender``
# attached to ``admin`` or ``moderator`` would fail to install the moment the column is gone
# from those tables.


class UserRecord(AccountColumns, Base):
    """Participant account and attribution target for user-generated content.

    Profile fields and report/removal tallies belong here, not on staff tables. Report counts
    include complaints; removal counts track upheld actions and drive automatic blocking. The
    shared counter service updates them atomically.
    """

    __tablename__ = "users"

    # Interests must be a subset of the canonical vocabulary; the explicit NULL check matters
    # because PostgreSQL CHECK constraints accept unknown results. The year constraint name
    # includes its upper bound so schema reconciliation replaces older versions.
    __table_args__ = (
        # Matchmaking filters by gender, so enforce the canonical non-null values.
        CheckConstraint(_GENDER_CHECK_SQL, name="ck_accounts_gender"),
        CheckConstraint(
            f"interests IS NOT NULL AND interests <@ {_INTEREST_SQL_ARRAY}",
            name="ck_users_interests_known",
        ),
        CheckConstraint(
            _YEAR_CHECK_SQL, name=f"ck_users_year_known_{USER_YEAR_MAX}"
        ),
    )

    bio: Mapped[Optional[str]] = mapped_column(String(500))

    # Store Cloudinary identity, not a URL; delivery URLs are derived on read. These
    # participant-only fields move together because staff cannot upload profile pictures.
    profile_picture_public_id: Mapped[Optional[str]] = mapped_column(Text)
    profile_picture_format: Mapped[Optional[str]] = mapped_column(String(16))
    # Nullable until the first upload; claimed with a conditional update to enforce cooldowns
    # under concurrent requests. This participant-only state belongs on ``users``.
    last_profile_picture_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # NOT NULL and closed to ``USER_GENDER_VALUES`` by ``ck_accounts_gender`` above. Staff rows
    # deliberately have none: a moderator is never a matchmaking candidate, so a gender on
    # ``admin`` or ``moderator`` was a value nothing read and a field every staff payload had
    # to invent an answer for.
    gender: Mapped[str] = mapped_column(String(24))

    # Matchmaking groups by indexed year and department. Year is a bounded integer;
    # department remains institution-defined free text. Both are required cohort fields.
    year: Mapped[int] = mapped_column(Integer, index=True)
    department: Mapped[str] = mapped_column(String(USER_DEPARTMENT_MAX), index=True)
    # The element width is the length of the longest entry in the vocabulary, and it is derived
    # from that rather than written as a bare 80 -- so a longer interest cannot be added to
    # ``limits.INTEREST_VALUES`` and then be rejected by the column as an unexplained 500. The
    # CHECK constraint is the real rule; this is just enough room to hold what it permits.
    interests: Mapped[list[str]] = mapped_column(
        ARRAY(String(max(len(value) for value in INTEREST_VALUES))),
        default=list,
        server_default=text("'{}'"),
    )
    matchmaking_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    love_notes_receive_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    love_notes_send_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    last_matchmaking_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Nullable until the first confession; conditional updates enforce the cooldown safely.
    last_confession_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    confession_report_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    confession_removal_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    love_note_report_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    love_note_removal_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    message_report_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    message_removal_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class AdminRecord(AccountColumns, Base):
    """A staff account with the account-lifecycle capabilities. No participant fields."""

    __tablename__ = "admin"

    # Enforce non-blockable admins at the database layer. ``IS NOT TRUE`` rejects true
    # while allowing legacy NULLs under PostgreSQL's CHECK semantics.
    __table_args__ = (
        CheckConstraint("is_blocked IS NOT TRUE", name="ck_admin_not_blockable"),
    )


class ModeratorRecord(AccountColumns, Base):
    """A staff account with the content-moderation capabilities. No participant fields."""

    __tablename__ = "moderator"


# The one place a table name becomes a model. ``AccountType`` is a plain string enum with no
# ORM knowledge, so the join lives here, next to the classes it joins, rather than in
# ``app.accounts`` (which ``db_models`` imports and so cannot import back).
ACCOUNT_MODELS: dict[str, type[Base]] = {
    AccountType.USERS.value: UserRecord,
    AccountType.ADMIN.value: AdminRecord,
    AccountType.MODERATOR.value: ModeratorRecord,
}


class RemovedContentColumns:
    """Soft-removal fields shared by content tables; rows remain for report audit history."""

    removed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    removed_by_regno: Mapped[Optional[str]] = mapped_column(String(100))

    @property
    def is_removed(self) -> bool:
        return self.removed_at is not None


class ConfessionRecord(RemovedContentColumns, Base):
    __tablename__ = "confessions"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    body: Mapped[str] = mapped_column(Text)
    recipient_name: Mapped[Optional[str]] = mapped_column(String(160))
    anonymous: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    comments_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    author_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    approved: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), index=True)
    report_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    reactions: Mapped[dict[str, list[str]]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    heart_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    haha_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    whoa_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    heartbreak_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    comment_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))

    __table_args__ = (
        CheckConstraint(
            f"length(body) <= {CONFESSION_BODY_MAX}",
            # Include the limit in the name so schema reconciliation replaces older ceilings.
            name=f"ck_confessions_body_length_{CONFESSION_BODY_MAX}",
        ),
        # Reject stored control characters and Unicode non-characters, including raw SQL writes.
        CheckConstraint(
            "body !~ '[\\u0000-\\u0008\\u000B\\u000C\\u000E-\\u001F\\u007F-\\u009F]'",
            name="ck_confessions_body_characters",
        ),
        # Its own constraint rather than a second arm on the one above, because
        # ``apply_check_constraints`` only adds what is missing. Editing the SQL of an
        # installed constraint would leave the running database on the old text forever with no
        # error anywhere, so a rule that grows has to arrive under a new name.
        CheckConstraint(
            f"body !~ '{_NON_CHARACTERS_PATTERN}'",
            # Named for the rule rather than for the first attempt at it, so that any database
            # which already installed an earlier version adds this one instead of keeping a
            # constraint whose pattern no longer matches what this module generates.
            name="ck_confessions_body_unicode_noncharacters",
        ),
    )


class ConfessionCommentRecord(RemovedContentColumns, Base):
    __tablename__ = "confession_comments"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    confession_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("confessions.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    likes: Mapped[list[str]] = mapped_column(ARRAY(Uuid(as_uuid=False)), default=list, server_default=text("'{}'"))
    dislikes: Mapped[list[str]] = mapped_column(ARRAY(Uuid(as_uuid=False)), default=list, server_default=text("'{}'"))
    like_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    dislike_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))

    __table_args__ = (
        CheckConstraint(
            f"length(body) <= {COMMENT_BODY_MAX}",
# Include the limit in the constraint name so schema reconciliation replaces older ceilings.
            name=f"ck_confession_comments_body_length_{COMMENT_BODY_MAX}",
        ),
    )
    report_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


# Fixed embedding contract for confession semantic search. The model and
# dimensions are invariants enforced in app.config and app.services.embedding_service:
# text-embedding-3-small with exactly 1536 dimensions, stored as vector(1536).
EMBEDDING_MODEL_ID = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536


class ConfessionEmbeddingRecord(Base):
    """One validated embedding of a confession's public text for semantic search."""

    __tablename__ = "confession_embeddings"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    confession_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("confessions.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    # vector(1536) when pgvector is installed; falls back to JSONB only if the
    # optional import is unavailable (tests without the package). Production and
    # the pgvector-enabled test database always use the vector type.
    embedding: Mapped[Any] = mapped_column(
        _PgVector(EMBEDDING_DIMENSIONS) if _PgVector is not None else JSONB
    )
    model: Mapped[str] = mapped_column(
        String(64), default=EMBEDDING_MODEL_ID, server_default=text(f"'{EMBEDDING_MODEL_ID}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now(), index=True
    )


class ReportRecord(Base):
    """A participant's report that a confession, or a comment on one, breaks the rules.

    One table for both targets, discriminated by which of the two nullable foreign keys is
    set; ``ck_reports_one_target`` makes "both" and "neither" impossible at the database
    level rather than in application code.

    The three lifecycle columns exist so this queue can be closed out the same way the
    message-report and love-note-report queues are. They were added when the unified Reports
    Review screen was built, and every pre-existing row backfills to ``'pending'``: a report
    nobody had looked at is pending, which is what it was being treated as before the
    lifecycle was recorded at all.

    ``status`` carries the whole vocabulary, and it has exactly two closed outcomes:

    ``pending``
        Nobody has ruled on it yet.
    ``removed``
        A moderator ruled that the reported content broke the rules and took it down. The
        content row is marked removed rather than deleted, and both parties are notified.
    ``dismissed``
        A moderator looked and the content stays. The reporter is told it was reviewed.

    ``reviewed`` was the older word for "a human looked at this" and no longer has a button
    behind it; ``reviewed_at``/``reviewed_by_regno`` are what now record that a human looked,
    independently of which of the two outcomes they chose. Rows closed under the old
    vocabulary still read back as ``reviewed`` and are shown as such.

    The reported party is not stored. For a confession it is ``confessions.author_id`` and
    for a comment it is ``confession_comments.user_id`` -- both foreign keys that cascade, and
    both one join away in a table the queue already reads to show the content. A denormalised
    column could only ever disagree with its source.

    Automated moderation (Batch 1: Azure AI Content Safety for confessions) reuses this
    same table rather than a parallel system. ``source`` discriminates the actor:
    ``"user"`` for manual participant reports, ``"azure_content_safety"`` for automated
    confession reports. Automated rows carry ``reported_by_id IS NULL`` and
    ``reported_by_name`` of exactly ``"Azure Content Safety"`` -- there is no users row
    behind them, so they are never attributed to the author, the recipient or staff.
    ``reported_by_id`` is therefore nullable; pre-existing manual rows always carry a
    value. At most one automated report exists per confession, enforced by the partial
    unique index ``uq_reports_confession_automated`` (see PostgresClient).
    """

    __tablename__ = "reports"
    __table_args__ = (
        CheckConstraint("(confession_id IS NOT NULL) <> (comment_id IS NOT NULL)", name="ck_reports_one_target"),
        UniqueConstraint("confession_id", "reported_by_id", name="uq_reports_confession_reporter"),
        UniqueConstraint("comment_id", "reported_by_id", name="uq_reports_comment_reporter"),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    confession_id: Mapped[Optional[str]] = mapped_column(Uuid(as_uuid=False), ForeignKey("confessions.id", ondelete="CASCADE"), index=True)
    comment_id: Mapped[Optional[str]] = mapped_column(Uuid(as_uuid=False), ForeignKey("confession_comments.id", ondelete="CASCADE"), index=True)
    reported_by_id: Mapped[Optional[str]] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True)
    reported_by_name: Mapped[str] = mapped_column(String(160))
    reason: Mapped[str] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default=text("'pending'"), index=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # No foreign key: the value names an account in ``moderator`` or ``admin``, which a
    # foreign key to ``users`` could never point at.
    reviewed_by_regno: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    # Discriminates manual ("user") from automated ("azure_content_safety") reports.
    # Added for Batch 1 confession moderation; backfills to "user" for history.
    source: Mapped[str] = mapped_column(String(32), default="user", server_default=text("'user'"), index=True)


class LoginTokenRecord(Base):
    __tablename__ = "login_tokens"

    # Account ID is table-local, so ``account_type`` selects its table. Exclude NULL
    # explicitly because PostgreSQL CHECK constraints accept unknown results.
    __table_args__ = (
        CheckConstraint(
            f"account_type IS NOT NULL AND account_type IN ({_ACCOUNT_TYPE_SQL_LIST})",
            name="login_tokens_account_type_valid",
        ),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    # The server default exists only so the column can be added to a table that predates the
    # split, where the identity of the account is genuinely unknown. It backfills ``users``,
    # the least-privileged reading, so a token that outlives this change is refused rather
    # than resolved against a table nobody can confirm.
    account_type: Mapped[str] = mapped_column(String(16), index=True, server_default=text("'users'"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    request_ip: Mapped[str] = mapped_column(INET)
    request_user_agent: Mapped[str] = mapped_column(String(512))
    consume_ip: Mapped[Optional[str]] = mapped_column(INET)
    consume_user_agent: Mapped[Optional[str]] = mapped_column(String(512))
    # Plain Integer, not SmallInteger: the counter is incremented on every redemption
    # from an unrelated network and only bounded in SQL (see auth_service), so a
    # 16-bit column would overflow into a 500 under sustained probing. Existing
    # databases are widened at boot by PostgresClient.apply_widened_columns.
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    session_status: Mapped[str] = mapped_column(String(16), default="issued", server_default=text("'issued'"), index=True)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    device: Mapped[Optional[str]] = mapped_column(String(512))
    # One live session per account: when a newer login supersedes this one it records
    # which session replaced it and when, so the old session's next request can tell its
    # holder that they were signed out by a login on another device.
    superseded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    superseded_by: Mapped[Optional[str]] = mapped_column(Uuid(as_uuid=False))
    # Fixed session deadline set at redemption; refresh rotates credentials but cannot
    # extend the session. Startup backfills existing sessions.
    session_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Hash of the current refresh token; uniqueness lets one concurrent refresh win.
    refresh_token_hash: Mapped[Optional[str]] = mapped_column(String(64), unique=True, index=True)
    # Hash of the prior refresh token, used to identify replayed values after rotation.
    refresh_token_replaced_hash: Mapped[Optional[str]] = mapped_column(String(64))


class WebSocketTicketRecord(Base):
    """One-use, short-lived socket ticket stored as a hash and bound to its login session."""

    __tablename__ = "websocket_tickets"

    __table_args__ = (
        CheckConstraint(
            f"account_type IS NOT NULL AND account_type IN ({_ACCOUNT_TYPE_SQL_LIST})",
            name="websocket_tickets_account_type_valid",
        ),
    )

    ticket_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    account_type: Mapped[str] = mapped_column(String(16), index=True)
    session_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class LoginRateLimitRecord(Base):
    """Fixed-window login counter, atomically incremented and expired after its window."""

    __tablename__ = "login_rate_limits"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    hits: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class LoveNoteRecord(RemovedContentColumns, Base):
    __tablename__ = "love_notes"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    sender_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    recipient_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # Store Cloudinary object identity, not a signed URL; derive delivery URLs when needed.
    image_public_id: Mapped[str] = mapped_column(Text)
    image_format: Mapped[str] = mapped_column(String(16))
    # Use Text so the named CHECK constraint, rather than varchar truncation, enforces the limit.
    body: Mapped[str] = mapped_column(Text)
    # Redacted preview for moderation; nullable on failure, with no fallback to raw body.
    moderation_body: Mapped[Optional[str]] = mapped_column(Text)
    anonymous: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(String(24), default="pending_review", server_default=text("'pending_review'"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            f"length(body) <= {LOVE_NOTE_BODY_MAX}",
            # Include the limit in the name so startup replaces the previous ceiling.
            name=f"ck_love_notes_body_length_{LOVE_NOTE_BODY_MAX}",
        ),
    )


class LoveNoteReportRecord(Base):
    """A recipient's report that a love note they received breaks the rules.

    Kept in its own table rather than as a flag on ``love_notes`` for the same reason
    ``message_reports`` is separate from ``messages``: the report is a *review queue item*
    with a lifecycle of its own (pending -> reviewed/dismissed) and an audit trail naming
    who looked at it, and a note the recipient has reported is still a note staff must be
    able to read, approve or reject on its own merits.

    A note has exactly one recipient, so ``(love_note_id, reporter_regno)`` is unique in
    practice as well as on paper: the same person cannot file the same complaint twice, and
    nobody else is entitled to file one.

    The reported party is deliberately **not** denormalised into a second regno column.
    ``love_notes.sender_id`` is a foreign key that cascades, so a report can never outlive
    the note it is about, and the sender is one join away in a table that has to be read
    anyway to show the note. Storing it would add a column that can only ever disagree with
    its source.
    """

    __tablename__ = "love_note_reports"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    love_note_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("love_notes.id", ondelete="CASCADE"), index=True)
    # The reporter is always the note's recipient, and the regno rather than the id because
    # this is the column the staff queue groups and displays by.
    reporter_regno: Mapped[str] = mapped_column(String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    # Capped in the database as well as by Pydantic, so a write that does not come through
    # the API cannot store a reason longer than the application claims to accept.
    reason: Mapped[str] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default=text("'pending'"), index=True)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # No foreign key, for the same reason as message_reports.reviewed_by_regno: the reviewer
    # is a staff account whose regno lives in ``admin`` or ``moderator``, never in ``users``,
    # so a FK would reject every moderator's sign-off.
    reviewed_by_regno: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    __table_args__ = (
        UniqueConstraint("love_note_id", "reporter_regno"),
        CheckConstraint(f"length(reason) <= {REPORT_REASON_MAX}", name="ck_love_note_reports_reason_length"),
    )


class LoveNoteTemplateRecord(Base):
    """A pre-made illustration offered when composing a note, not a participant's content.

    ``image_url`` here is an ordinary public CDN address and stays one. It is not the column that
    used to hold a *sent* note's image address (``love_notes.image_public_id``): these are artwork
    the application ships, addressed like any other public asset and readable without a signature.
    Keeping them apart is what stops a change meant for private content from accidentally making
    these signed-and-private -- which would break every compose screen -- or, worse, the other
    way round.
    """

    __tablename__ = "love_note_templates"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    image_url: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class MatchRecord(Base):
    __tablename__ = "matches"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    user_1_regno: Mapped[str] = mapped_column(String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    user_2_regno: Mapped[str] = mapped_column(String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # Canonical unique pair key; expiration clears it so the pair can match again.
    pair_key: Mapped[Optional[str]] = mapped_column(String(201), unique=True, index=True)
    __table_args__ = (CheckConstraint("user_1_regno <> user_2_regno", name="ck_matches_distinct_users"),)


class ConversationRecord(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    match_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("matches.id", ondelete="CASCADE"), unique=True, index=True)
    initiator_regno: Mapped[str] = mapped_column(String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    receiver_regno: Mapped[str] = mapped_column(String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default=text("'pending'"), index=True)
    requested_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Encrypted latest-message preview; AAD binds it to the message and sender.
    last_message_preview_ciphertext: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    last_message_preview_nonce: Mapped[Optional[bytes]] = mapped_column(LargeBinary(length=12))
    last_message_preview_key_version: Mapped[Optional[str]] = mapped_column(String(16))
    last_message_preview_message_id: Mapped[Optional[str]] = mapped_column(Uuid(as_uuid=False))
    last_message_preview_sender_regno: Mapped[Optional[str]] = mapped_column(String(100))
    # The preview is deleted when the message it copies passes its retention
    # deadline, even if a cleanup run has not physically removed the message yet.
    last_message_preview_delete_after: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), index=True
    )
    # Sole participant allowed to reopen the conversation; array shape is legacy.
    blocked_by: Mapped[list[str]] = mapped_column(ARRAY(String(100)), default=list, server_default=text("'{}'"))
    terminated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class MessageRecord(RemovedContentColumns, Base):
    """One matchmaking chat message, encrypted at rest.

    The plaintext body never reaches this table. ``body_ciphertext`` is the
    AES-256-GCM output with its 16-byte authentication tag appended (the shape
    ``AESGCM.encrypt`` returns), ``body_nonce`` is the fresh 12-byte nonce used
    for this one row, and ``body_key_version`` selects the key for decryption.
    ``delete_after`` is the authoritative logical and physical retention
    deadline: match expiry + ``MATCHMAKING_CHAT_RETENTION_HOURS``.

    Body length and per-participant quotas are enforced by the service before
    encryption; a CHECK on ciphertext length would enforce nothing meaningful.
    """

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    sender_regno: Mapped[str] = mapped_column(String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    receiver_regno: Mapped[str] = mapped_column(String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    body_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    body_nonce: Mapped[bytes] = mapped_column(LargeBinary(length=12), nullable=False)
    body_key_version: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # The one retention timestamp. Logical reads require delete_after > now and
    # the scheduler deletes rows where delete_after <= now, so there is no second
    # clock that can drift away from it.
    delete_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    __table_args__ = (
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        Index("ix_messages_conversation_delete_after", "conversation_id", "delete_after"),
    )


class NotificationRecord(Base):
    """Participant notice with an optional target to open."""

    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    recipient_regno: Mapped[str] = mapped_column(String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    heading: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
# Target kind and ID identify the linked entity without per-kind foreign-key columns.
    target_kind: Mapped[Optional[str]] = mapped_column(String(40))
    target_id: Mapped[Optional[str]] = mapped_column(String(100))


class MatchmakingSlotRecord(Base):
    """A pair of cohorts belonging to a matchmaking slot.

    Rows permit only named pairs; overlapping rows do not imply transitive matches. The
    group key preserves editor boxes, while year and department remain separate cohort fields.
    """

    __tablename__ = "matchmaking_slots"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    year_a: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    department_a: Mapped[str] = mapped_column(String(USER_DEPARTMENT_MAX), index=True, nullable=False)
    year_b: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    department_b: Mapped[str] = mapped_column(String(USER_DEPARTMENT_MAX), index=True, nullable=False)
    # Identifies the editor box that grouped this pair; matchmaking does not use it.
    # Nullable for legacy rows, which retain the editor's fallback grouping.
    group_key: Mapped[Optional[str]] = mapped_column(Uuid(as_uuid=False), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

# Self-pairs represent single-cohort slots; bounded names replace the legacy year checks.
    __table_args__ = (
        CheckConstraint(
            f"year_a {_YEAR_RANGE_SQL}", name=f"ck_matchmaking_slot_year_a_known_{USER_YEAR_MAX}"
        ),
        CheckConstraint(
            f"year_b {_YEAR_RANGE_SQL}", name=f"ck_matchmaking_slot_year_b_known_{USER_YEAR_MAX}"
        ),
        CheckConstraint("department_a IS NOT NULL", name="ck_matchmaking_slot_department_a_present"),
        CheckConstraint("department_b IS NOT NULL", name="ck_matchmaking_slot_department_b_present"),
    )


class MessageReportRecord(Base):
    __tablename__ = "message_reports"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    # SET NULL rather than CASCADE: ordinary chat retention deletes the message,
    # but the report is the moderation audit record and must survive it. The
    # separately encrypted evidence row (``chat_message_evidence``) is keyed by
    # report id and keeps the individual reported message available for review.
    message_id: Mapped[Optional[str]] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("messages.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    # Nullable: an automated report (Azure AI Content Safety) has no participant
    # behind it -- the receiver is the only person who can *manually* report a
    # message, so a system report carries NULL here and names the sender in
    # ``reported_user_regno``. Manual reports always carry a reporter.
    reporter_regno: Mapped[Optional[str]] = mapped_column(String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True, nullable=True)
    reported_user_regno: Mapped[str] = mapped_column(String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    reason: Mapped[str] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default=text("'pending'"), index=True)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # No foreign key. The reviewer is a staff account, so its regno lives in ``admin`` or
    # ``moderator`` and never in ``users`` -- a FK to ``users.regno`` would reject every
    # moderator's sign-off. Kept as plain text so the audit trail survives the reviewer's
    # account being deleted or renamed, which is what ``ON DELETE SET NULL`` was doing.
    reviewed_by_regno: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    # Discriminates manual ("user") from automated ("azure_content_safety") reports.
    # Mirrors ``reports.source`` (see ReportRecord); backfills to "user" for history.
    source: Mapped[str] = mapped_column(String(32), default="user", server_default=text("'user'"), index=True)
    __table_args__ = (UniqueConstraint("message_id", "reporter_regno"),)


class ChatMessageEvidenceRecord(Base):
    """Separately encrypted report evidence retained after ordinary chat cleanup."""

    __tablename__ = "chat_message_evidence"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    report_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), unique=True, index=True)
    report_kind: Mapped[str] = mapped_column(String(20), index=True)
    # Kept as a plain UUID string rather than a FK: the referenced message is
    # expected to be gone at some point, and the id is useful metadata even then.
    message_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    chat_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    sender_ref: Mapped[str] = mapped_column(String(100), index=True)
    body_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    body_nonce: Mapped[bytes] = mapped_column(LargeBinary(length=12), nullable=False)
    body_key_version: Mapped[str] = mapped_column(String(16), nullable=False)
    message_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (
        Index("ix_chat_message_evidence_report", "report_kind", "report_id"),
    )


class ChatEvidenceAccessRecord(Base):
    """Append-only audit trail for reported-content review.

    Every time the report queue returns the decrypted body of a reported
    message, one row records which staff account read which report's evidence.
    No message text, key material or nonce is stored here.
    """

    __tablename__ = "chat_evidence_access"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    report_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    report_kind: Mapped[str] = mapped_column(String(20), index=True)
    evidence_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    accessed_by_regno: Mapped[str] = mapped_column(String(100), index=True)
    accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    action: Mapped[str] = mapped_column(String(40), default="report_queue_review", server_default=text("'report_queue_review'"))


class AnagramGameRecord(Base):
    """One day's Anagram puzzle, as an administrator configured it.

    The row is the *word* and nothing else. There is no scramble here on purpose: the
    answer stored below is the canonical, uppercase word, and every player will later
    receive their own private permutation of it -- so a scrambled copy in this table would
    be a second, conflicting spelling of the same puzzle. The per-player layer (their
    scramble, their guesses, their result) is a separate table keyed back to ``id`` when it
    exists; it is deliberately not imagined here, because a column for a feature that does
    not exist is a promise the rest of the application does not keep.

    ``game_date`` is unique for the same reason ``wordle_games.game_date`` is: one day, one
    puzzle, and two rows for one day would leave the player-facing page with two answers and
    no rule for choosing between them. The refusal lives in the schema rather than only in
    the service so that two administrators saving at the same moment cannot both be told 201.

    ``mode`` records how the answer came to be -- ``custom`` for a word the administrator
    typed, ``random`` for one the server drew from ``backend/assets/anagram_words.txt`` --
    and is provenance rather than behaviour: the row plays identically either way.

    ``created_by`` holds the creator's ``regno``, with no foreign key, for the same reason
    ``message_reports.reviewed_by_regno`` has none: staff live in ``admin`` and ``moderator``
    and never in ``users``, so a key into the participant table would reject every creator
    this route can admit, and plain text lets the audit trail survive the account being
    renamed or deleted.
    """

    __tablename__ = "anagram_games"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    # See ``wordle_games.game_date``: history shares days, so this is indexed rather
    # than unique and the single-active rule lives in a partial unique index.
    game_date: Mapped[date] = mapped_column(Date, index=True)
    mode: Mapped[str] = mapped_column(String(10))
    answer: Mapped[Optional[str]] = mapped_column(String(ANAGRAM_ANSWER_MAX), nullable=True)
    status: Mapped[str] = mapped_column(
        String(10), default="active", server_default=text("'ended'"), index=True
    )
    period_start_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    period_end_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(USER_REGNO_MAX), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("mode IN ('custom', 'random')", name="ck_anagram_games_mode_known"),
# Custom answers may be shorter than random draws; enforce only the valid stored range.
        CheckConstraint(
            f"length(answer) BETWEEN 1 AND {ANAGRAM_ANSWER_MAX}",
            name=f"ck_anagram_games_answer_length_{ANAGRAM_ANSWER_MAX}",
        ),
        CheckConstraint(
            "status IN ('active', 'pending', 'ended')",
            name="ck_anagram_games_status_known",
        ),
    )


class AnagramUserGameRecord(Base):
    """One player's attempt at one day's Anagram: the participation half of the game.

    The row pairs a *player* with a *puzzle*: everything below is an answer to "where is
    this user in today's game?", and nothing else. ``anagram_games`` still owns the answer
    itself, and this table holds exactly one derived copy of it -- ``scrambled_word``, the
    private permutation this player was dealt. That is the one deliberate exception to the
    Wordle rule of never storing answer-adjacent text here: an anagram cannot be played
    without the scrambled tiles on screen, the scramble is useless to anyone but its player,
    and it is generated once and never re-dealt, so there is no second spelling to drift.

    A player is bound to one puzzle by ``UniqueConstraint("anagram_game_id", "user_id")``,
    and the constraint is the rule rather than a service check: two starts at once each
    read "no participation yet" and each try to insert, and the unique index is what lets
    exactly one of them become the owner of the row.

    ``guesses`` is the only guess history stored, in play order. An anagram guess needs no
    per-letter verdict -- the guess either is the answer or it is not -- so unlike Wordle
    there is no ``results`` column and nothing to re-derive on read.

    ``status`` is written by the service at the moment a guess settles the game, and is
    the column the read decides by. ``success`` restates ``status = 'won'`` as a boolean so
    a leaderboard query can count wins without comparing strings; the service always writes
    the two together. ``solved_at`` is deliberately NULL on a loss, for the reason the
    Wordle table's is: a time the player *failed* is not a stat.

    The timer is authoritative here rather than on the client. ``active_time_seconds`` is
    finished timing segments, summed; ``timer_started_at`` opens the current segment, and
    is NULL while paused or finished. A segment is closed -- its seconds added to the total
    and the column cleared -- on pause, on heartbeat, on a guess that settles the game, and
    on resume-after-absence; a segment older than
    ``ANAGRAM_TIMER_STALE_AFTER_SECONDS`` is discarded rather than counted, because a
    browser that died mid-game never sends the close and the dead hours are not play.
    """

    __tablename__ = "anagram_user_games"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    anagram_game_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("anagram_games.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    scrambled_word: Mapped[str] = mapped_column(String(ANAGRAM_ANSWER_MAX))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    status: Mapped[str] = mapped_column(
        String(20), default="in_progress", server_default=text("'in_progress'")
    )
    success: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    guesses: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'{}'::jsonb")
    )
    active_time_seconds: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    timer_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now()
    )
    last_guess_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    solved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("anagram_game_id", "user_id"),
        # The suffix carries the limit, so tightening it and restarting supersedes the old
        # constraint (see ``_superseded_constraints``). ``attempt_count`` is NOT NULL, so a
        # plain BETWEEN cannot be satisfied by NULL.
        CheckConstraint(
            f"attempt_count BETWEEN 0 AND {ANAGRAM_MAX_ATTEMPTS}",
            name=f"ck_anagram_user_games_attempt_count_{ANAGRAM_MAX_ATTEMPTS}",
        ),
        CheckConstraint(
            "status IN ('in_progress', 'won', 'lost')",
            name="ck_anagram_user_games_status_known",
        ),
    )


class WordleGameRecord(Base):
    """One day's Wordle puzzle, as an administrator configured it.

    The row is the *configuration* of a puzzle -- which word that day runs on, and how that
    word was chosen -- and not a play of it. Nothing a participant does writes here: there is
    no attempt, no guess and no result column, because none of those exist yet and inventing
    the columns would be inventing the game.

    ``game_date`` is the identity that matters and is unique rather than merely indexed. A
    second row for the same day would leave the player-facing page with two answers and no
    rule for choosing between them, so the refusal belongs in the schema and not only in the
    service that happens to check first -- two administrators saving at the same moment would
    otherwise both be told 201.

    ``mode`` records how the answer came to be: ``custom`` for a word the administrator typed,
    ``random`` for one the server drew from the approved list. It is provenance rather than
    behaviour -- the row plays identically either way -- which is why it is a plain column
    with a CHECK rather than two nullable ones.
    """

    __tablename__ = "wordle_games"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    # The session's calendar identity. No longer unique: an Update ends the current
    # session and opens a new one, so one UTC day may hold several ended sessions.
    # The single-active rule is a partial unique index (see postgres.py), not this
    # column, because uniqueness here would forbid the history the lifecycle keeps.
    game_date: Mapped[date] = mapped_column(Date, index=True)
    mode: Mapped[str] = mapped_column(String(10))
    answer: Mapped[Optional[str]] = mapped_column(String(WORDLE_ANSWER_LENGTH), nullable=True)
    # Session lifecycle. ``active`` is playable, ``pending`` waits for a manual word,
    # ``ended`` is history. Only one active-or-pending row may exist at a time.
    status: Mapped[str] = mapped_column(
        String(10), default="active", server_default=text("'ended'"), index=True
    )
    # The calendar period this session covers. ``period_end_date`` is exclusive: a
    # daily session starting 2026-10-08 ends 2026-10-09. Derived from the start date
    # and the game's reset cadence by ``game_periods`` -- never by elapsed hours.
    period_start_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    period_end_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("mode IN ('custom', 'random')", name="ck_wordle_games_mode_known"),
        CheckConstraint(
            f"length(answer) = {WORDLE_ANSWER_LENGTH}", name="ck_wordle_games_answer_length"
        ),
        CheckConstraint(
            "status IN ('active', 'pending', 'ended')",
            name="ck_wordle_games_status_known",
        ),
    )


class WordleUserGameRecord(Base):
    """One player's attempt at one day's Wordle: the participation half of the game.

    The row pairs a *player* with a *puzzle*: everything below is an answer to "where is
    this user in today's game?", and nothing else. ``wordle_games`` still owns the word
    itself; this table never holds an answer, because the answer is the thing the game
    exists to keep hidden and a second copy of it is a second place it can leak. A player
    is bound to one puzzle by ``UniqueConstraint("wordle_game_id", "user_id")``, and the
    constraint is the rule rather than a service check: two guesses at once each read "no
    participation yet" and each try to insert, and the unique index is what lets exactly
    one of them become the owner of the row.

    ``guesses`` is the only history stored, in play order. Each guess's result is derived
    again on every read by the same deterministic evaluation the guess was scored with, so
    there is no ``results`` column to drift out of step with the letters above it.

    ``status`` is written by the service at the moment a guess settles the game, and is
    the column the read decides by. ``success`` restates ``status = 'won'`` as a boolean so
    a leaderboard query can count wins without comparing strings; the service always writes
    the two together.

    ``solved_at`` is deliberately NULL on a loss: a time the player *failed* is a record of
    how slowly they failed, which is not a stat. ``completed_at`` is set either way, so
    that a "games finished today" count is a field rather than a list-handling exercise.
    """

    __tablename__ = "wordle_user_games"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    wordle_game_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("wordle_games.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    status: Mapped[str] = mapped_column(
        String(20), default="in_progress", server_default=text("'in_progress'")
    )
    success: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    guesses: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'{}'::jsonb")
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now()
    )
    last_guess_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    solved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("wordle_game_id", "user_id"),
        # The suffix carries the limit, so tightening it and restarting supersedes the old
        # constraint (see ``_superseded_constraints``). ``attempt_count`` is NOT NULL, so a
        # plain BETWEEN cannot be satisfied by NULL.
        CheckConstraint(
            f"attempt_count BETWEEN 0 AND {WORDLE_MAX_ATTEMPTS}",
            name=f"ck_wordle_user_games_attempt_count_{WORDLE_MAX_ATTEMPTS}",
        ),
        CheckConstraint(
            "status IN ('in_progress', 'won', 'lost')",
            name="ck_wordle_user_games_status_known",
        ),
    )


class CoupleGameRecord(Base):
    """One featured Couple Prediction game, as an administrator configured it.

    The row is the *configuration* of a featured instance -- which day the game is
    active for, and who marked it so -- and not a play of it. There is deliberately
    no mode, no word, no answer and no puzzle-generation column here: unlike
    Wordle/Anagram there is nothing per-day for an administrator to type, draw or
    select. The 50-question bank is shared application content in
    ``couple_game_questions`` and is never duplicated per date; a featured date
    simply points at the game being active for that day.

    ``game_date`` is unique for the same reason ``wordle_games.game_date`` is: one
    day, one featured instance, and two rows for one day would leave any future
    player surface with two games and no rule for choosing between them. The
    refusal lives in the schema rather than only in the service so that two
    administrators saving at the same moment cannot both be told 201.

    ``created_by`` holds the creator's ``regno``, with no foreign key, for the same
    reason ``message_reports.reviewed_by_regno`` has none: staff live in ``admin``
    and ``moderator`` and never in ``users``, so a key into the participant table
    would reject every creator this route can admit, and plain text lets the audit
    trail survive the account being renamed or deleted.
    """

    __tablename__ = "couple_games"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    # A global questionnaire session period, not a featured date. Featured dates live
    # in ``featured_games`` now; this row is one lifecycle period shared by every
    # couple, so several ended rows may name one day and only one may be current.
    game_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(
        String(10), default="active", server_default=text("'ended'"), index=True
    )
    period_start_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    period_end_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(USER_REGNO_MAX), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'pending', 'ended')",
            name="ck_couple_games_status_known",
        ),
    )


class CoupleGameQuestionRecord(Base):
    """One static Couple Prediction question template, seeded at application startup.

    The row is shared application content, not per-day configuration: the bank is
    seeded once from the committed list in ``couple_service`` and a featured date
    never copies it. ``stable_key`` (``couple-q-01`` … ``couple-q-50``) is the
    deterministic identity the seed uses to decide whether a predefined question
    already exists, so restarting the application never duplicates a row and never
    needs to overwrite one.

    The question text names ``X`` rather than a person -- ``What would X prefer
    for a perfect date?`` -- and the application substitutes the answering
    participant's name at presentation time. The stored template is never rewritten
    per viewer; the same row serves both directions of a relationship.

    The four options live in individual columns rather than a JSON array so that
    each one is length-bounded by the schema like every other stored string here.
    ``selected_option_index`` is intentionally absent: which option a participant
    picked is a fact about that participant, stored per answer in
    ``couple_game_answers``, and a column for it here would claim one shared answer
    for every player.
    """

    __tablename__ = "couple_game_questions"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    stable_key: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    sequence_number: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    question_text: Mapped[str] = mapped_column(Text)
    option_a: Mapped[str] = mapped_column(String(COUPLE_OPTION_TEXT_MAX))
    option_b: Mapped[str] = mapped_column(String(COUPLE_OPTION_TEXT_MAX))
    option_c: Mapped[str] = mapped_column(String(COUPLE_OPTION_TEXT_MAX))
    option_d: Mapped[str] = mapped_column(String(COUPLE_OPTION_TEXT_MAX))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            f"sequence_number BETWEEN 1 AND {COUPLE_QUESTIONS_COUNT}",
            name=f"ck_couple_game_questions_sequence_{COUPLE_QUESTIONS_COUNT}",
        ),
        CheckConstraint(
            f"length(question_text) BETWEEN 1 AND {COUPLE_QUESTION_TEXT_MAX}",
            name=f"ck_couple_game_questions_text_length_{COUPLE_QUESTION_TEXT_MAX}",
        ),
        CheckConstraint(
            f"length(option_a) BETWEEN 1 AND {COUPLE_OPTION_TEXT_MAX}",
            name=f"ck_couple_game_questions_option_a_length_{COUPLE_OPTION_TEXT_MAX}",
        ),
        CheckConstraint(
            f"length(option_b) BETWEEN 1 AND {COUPLE_OPTION_TEXT_MAX}",
            name=f"ck_couple_game_questions_option_b_length_{COUPLE_OPTION_TEXT_MAX}",
        ),
        CheckConstraint(
            f"length(option_c) BETWEEN 1 AND {COUPLE_OPTION_TEXT_MAX}",
            name=f"ck_couple_game_questions_option_c_length_{COUPLE_OPTION_TEXT_MAX}",
        ),
        CheckConstraint(
            f"length(option_d) BETWEEN 1 AND {COUPLE_OPTION_TEXT_MAX}",
            name=f"ck_couple_game_questions_option_d_length_{COUPLE_OPTION_TEXT_MAX}",
        ),
        # The placeholder mechanic, enforced where it cannot be bypassed. Every
        # template names ``X`` so the player surface can substitute the answering
        # participant's name at read time; a template without it is a row no screen
        # can personalise.
        CheckConstraint(
            "question_text LIKE '%X%'",
            name="ck_couple_game_questions_has_placeholder",
        ),
    )


class CoupleGameAnswerRecord(Base):
    """The answer one participant selected for one bank question.

    The foundation the later two-person participation flow builds on, and nothing
    more: which option this participant picked for this question. There is
    deliberately no pairing, no attempt count, no scoring, no completion state, no
    timer and no session column here -- those belong to the player workflow that
    does not exist yet, and a column for any of them would be a promise the rest
    of the application does not keep.

    One row per participant per question, enforced by the unique pair rather than
    a service check: two submissions at once each read "no answer yet" and each
    try to insert, and the unique index is what lets exactly one of them own the
    row. ``selected_option_index`` is positional into the question's four options
    (0 for ``option_a`` … 3 for ``option_d``) rather than a copy of the option
    text, so a bank edit cannot leave an answer pointing at words that moved.
    """

    __tablename__ = "couple_game_answers"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    question_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("couple_game_questions.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    selected_option_index: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("question_id", "user_id"),
        CheckConstraint(
            f"selected_option_index BETWEEN 0 AND {COUPLE_OPTIONS_PER_QUESTION - 1}",
            name=f"ck_couple_game_answers_selected_{COUPLE_OPTIONS_PER_QUESTION - 1}",
        ),
    )


class CoupleGameSelectionRecord(Base):
    """One participant's current partner selection: who they want to play with.

    The row is a *pending* selection and nothing more: choosing someone sends no
    notification, creates no session, assigns no questions and reveals nothing to
    the chosen user. It exists so that two independent choices can later be
    recognised as mutual -- the only event that starts a game.

    One row per participant, enforced by the unique ``user_id`` rather than a
    service check: changing a selection (``A → B`` then ``A → C``) updates the
    row, so the previous choice simply stops being the active one instead of
    lingering as a second pending request. A participant cannot select themselves,
    held by the schema rather than the screen that happens to hide the option.
    """

    __tablename__ = "couple_game_selections"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    selected_user_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "user_id != selected_user_id",
            name="ck_couple_game_selections_not_self",
        ),
    )


class CoupleGameSessionRecord(Base):
    """One Couple Questionnaire game between two mutually selected participants.

    The row exists only for an explicit mutual selection: ``A → B`` followed by
    ``B → A`` creates exactly one session holding both users and the featured
    game instance it belongs to. A one-sided choice never creates one, and two
    users who each chose the same third person get nothing until that person
    chooses one of them back.

    ``user_a_id`` and ``user_b_id`` are stored in canonical (sorted) order so
    that the pair ``(game, A, B)`` names one session regardless of who completed
    the mutual selection. Uniqueness is enforced by the schema -- one pair, one
    session per featured game -- and the service additionally refuses a session
    for a participant who already has one for the same game, so ``A ↔ B`` plus
    ``A → C`` plus ``C → A`` still leaves A with exactly one game.

    ``status`` is written by the service at the moment a phase completes:
    ``active`` while original answers are still being collected, ``prediction``
    once both participants have answered all of their questions, ``completed``
    once both have predicted all of their partner's. There is deliberately no
    timer, no score column and no attempt budget here: the score is derived from
    the stored answers and predictions whenever it is read, so there is no second
    number to drift out of step with them.
    """

    __tablename__ = "couple_game_sessions"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    couple_game_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("couple_games.id", ondelete="CASCADE"), index=True
    )
    user_a_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    user_b_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), default="active", server_default=text("'active'")
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("couple_game_id", "user_a_id", "user_b_id"),
        CheckConstraint(
            "user_a_id != user_b_id",
            name="ck_couple_game_sessions_distinct_users",
        ),
        CheckConstraint(
            "status IN ('active', 'prediction', 'completed')",
            name="ck_couple_game_sessions_status_known",
        ),
    )


class CoupleGameSessionQuestionRecord(Base):
    """One of the 25 bank questions drawn for one session, and who answers it.

    The draw happens once, when the mutual selection creates the session, and
    never again: these rows are the source of truth for the session's question
    set, so a refresh or a return tomorrow reads the same 25. ``assignment``
    splits them into the five shared questions both participants answer
    independently plus ten exclusive to each player -- 5 + 10 + 10 is the whole
    session, and 15 per participant is what each one is asked.

    A shared question lives here exactly once even though two people answer it:
    the two original answers are separate rows in ``couple_game_session_answers``
    pointing at this same row, because sharing the *question* is not sharing the
    *answer*.
    """

    __tablename__ = "couple_game_session_questions"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("couple_game_sessions.id", ondelete="CASCADE"), index=True
    )
    question_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("couple_game_questions.id", ondelete="CASCADE"), index=True
    )
    assignment: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("session_id", "question_id"),
        CheckConstraint(
            "assignment IN ('shared', 'user_a', 'user_b')",
            name="ck_couple_game_session_questions_assignment_known",
        ),
    )


class CoupleGameSessionAnswerRecord(Base):
    """What one participant actually selected for one of their session questions.

    The canonical correct answer the partner will later have to predict: it is
    written once, insert-only, and never overwritten by a prediction, so the
    original and the guess about it cannot collapse into one row. A shared
    question therefore holds two rows here -- one per participant -- each
    pointing at the same session question.

    Scoped to the session rather than to the bank: the same participant may play
    again on another featured date, and an answer given then is a different fact
    about a different game. (``couple_game_answers`` predates the session flow
    and holds no session; session play reads and writes only this table.)
    """

    __tablename__ = "couple_game_session_answers"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("couple_game_sessions.id", ondelete="CASCADE"), index=True
    )
    session_question_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("couple_game_session_questions.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    selected_option_index: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("session_question_id", "user_id"),
        CheckConstraint(
            f"selected_option_index BETWEEN 0 AND {COUPLE_OPTIONS_PER_QUESTION - 1}",
            name=f"ck_couple_game_session_answers_selected_{COUPLE_OPTIONS_PER_QUESTION - 1}",
        ),
    )


class CoupleGameSessionPredictionRecord(Base):
    """What one participant thinks their partner selected for one session question.

    Always a statement *about the partner's answer*, never a second answer of
    the predictor's own: the row names the session question and the predictor,
    and the verdict is computed against the partner's stored original at submit
    time. ``is_correct`` restates ``predicted_option_index = original`` as a
    boolean so scoring counts rows without comparing across tables; the service
    always writes the two together, exactly like the win flag on the puzzle
    participation rows.

    One prediction per predictor per question, insert-only like the originals: a
    submitted prediction is final, so there is no resubmission that could fish
    for the right answer one option at a time.
    """

    __tablename__ = "couple_game_session_predictions"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("couple_game_sessions.id", ondelete="CASCADE"), index=True
    )
    session_question_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("couple_game_session_questions.id", ondelete="CASCADE"), index=True
    )
    predictor_user_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    predicted_option_index: Mapped[int] = mapped_column(Integer)
    is_correct: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("session_question_id", "predictor_user_id"),
        CheckConstraint(
            f"predicted_option_index BETWEEN 0 AND {COUPLE_OPTIONS_PER_QUESTION - 1}",
            name=f"ck_couple_game_session_predictions_selected_{COUPLE_OPTIONS_PER_QUESTION - 1}",
        ),
    )


class TwoKeyEscapeGameRecord(Base):
    """One Two-Key Escape session: the calendar period the maze runs on.

    The row is a *session* in the same lifecycle the puzzle games share --
    ``active`` while playable, ``ended`` as history -- and it owns the one
    maze template every couple plays on this period: the edge length, the
    deterministic seed it was generated from, and the full layout (start,
    exit, permanent walls, initially locked cells). The template is dealt
    once, when the reset loop opens the period, and never again: a refresh
    or a reconnect reads the same walls, so progress stays comparable and
    the per-couple live state in ``two_key_escape_sessions`` can never
    rewrite what fairness depends on.

    ``created_by`` holds the creator's ``regno``, with no foreign key, for
    the same reason every other game table does: staff live in ``admin``
    and ``moderator`` and never in ``users``. The scheduler writes
    ``"scheduler"`` here, like every other game.
    """

    __tablename__ = "two_key_escape_games"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    game_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(
        String(10), default="active", server_default=text("'ended'"), index=True
    )
    period_start_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    period_end_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(USER_REGNO_MAX), index=True)
    # The configured edge length this period's maze was built at, and the
    # seed its layout was generated from, so the template is reproducible.
    grid_size: Mapped[int] = mapped_column(Integer, default=12, server_default=text("12"))
    maze_seed: Mapped[str] = mapped_column(Text)
    # The template itself, as ``[row, col]`` pairs: the start and exit cells,
    # the permanent walls, and the cells that begin locked (one key each).
    start_row: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    start_col: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    exit_row: Mapped[int] = mapped_column(Integer, default=11, server_default=text("11"))
    exit_col: Mapped[int] = mapped_column(Integer, default=11, server_default=text("11"))
    walls: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    locked_cells: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
# Server-generated key-to-lock derangement, never exposed to clients.
    key_mapping: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    # Which entries of ``locked_cells`` are chamber gates (as opposed to
    # flavor locks): the doors progress must pass through. Read by the
    # block proof, which treats gates as the operator's future job.
    gate_indexes: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'pending', 'ended')",
            name="ck_two_key_escape_games_status_known",
        ),
        CheckConstraint(
            "grid_size IN (12, 13, 14, 15)",
            # Numeric suffix, so this supersedes the 7-10 membership on the
            # next boot -- see ``PostgresClient._superseded_constraints``.
            name="ck_two_key_escape_games_grid_size_known_15",
        ),
    )


class TwoKeyEscapeSessionRecord(Base):
    """One couple's live game inside a Two-Key Escape session period.

    The row pairs a *couple* with a *period*: ``couple_session_id`` names
    the mutual-selection session in ``couple_game_sessions`` that resolved
    the two participants, so the pairing logic lives in exactly one place
    and this table never re-decides who forms a couple. One escape game per
    couple session, enforced by the unique pair rather than a service check.

    Roles are submitted then finalized: each side's confirmed choice is
    stored in ``user_a_submitted_role`` / ``user_b_submitted_role``, and
    the assignment becomes real only when both are stored and distinct, at
    which point ``navigator_user_id`` / ``key_operator_user_id`` are set
    together and never rewritten. The maze itself is never copied here --
    position, unlocked cells, blocked cells, consumed keys, cooldown and
    movement counters are the live overlay on the period's shared template.

    Clients submit actions, never state: movement and key use are validated
    against this row under ``FOR UPDATE`` and committed atomically, with
    ``state_version`` bumped on every accepted action so concurrent requests
    cannot silently overwrite one another.
    """

    __tablename__ = "two_key_escape_sessions"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    two_key_escape_game_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("two_key_escape_games.id", ondelete="CASCADE"), index=True
    )
    couple_session_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("couple_game_sessions.id", ondelete="CASCADE"), index=True
    )
    user_a_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    user_b_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Each side's confirmed role choice (``navigator`` / ``key_operator``),
    # NULL until that side confirms. Finalization reads both, never a default.
    user_a_submitted_role: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    user_b_submitted_role: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # The finalized assignment: set together exactly once, then immutable.
    navigator_user_id: Mapped[Optional[str]] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True
    )
    key_operator_user_id: Mapped[Optional[str]] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True
    )
    player_row: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    player_col: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # When the Navigator made the first valid move. The start of the only
    # clock this game keeps: completion is stamped against it, and logouts,
    # tab switches or hours away change nothing -- the elapsed time simply
    # includes them.
    first_move_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Live overlay on the template: unlocked (formerly locked) and blocked
    # (formerly traversable) cells, as ``[row, col]`` pairs.
    unlocked_cells: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    blocked_cells: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    # No consumed-charges column: keys are reusable, so there is nothing to
    # record per key. (An earlier revision kept ``used_key_indexes`` here;
    # it is retired in ``postgres.RETIRED_COLUMNS``.)
    last_key_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # No movement-gate columns: keys never required a Navigator move between
    # uses. (An earlier revision kept ``moves_since_last_key`` and
    # ``has_used_key`` here; both are retired in
    # ``postgres.RETIRED_COLUMNS``.)
    total_moves: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    total_keys: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
# Elapsed completion time used for ranking; NULL until the exit is reached.
    elapsed_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    state_version: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("two_key_escape_game_id", "couple_session_id"),
        UniqueConstraint("two_key_escape_game_id", "user_a_id", "user_b_id"),
        CheckConstraint(
            "user_a_id != user_b_id",
            name="ck_two_key_escape_sessions_distinct_users",
        ),
    )


class ReactionTimeGameRecord(Base):
    """One Reaction Time session: the calendar period the game runs on.

    The row is a *session* in the same lifecycle the puzzle games share --
    ``active`` while playable, ``ended`` as history -- and not a play of it.
    Sessions are opened by the reset loop under the game's own cadence and
    are independent of the featured spotlight: a session exists whether or
    not Reaction Time is featured today, exactly like the Couple
    Questionnaire. There is deliberately no mode, no word, no answer, no
    timing range, no difficulty and no attempt budget here: the mechanics are
    fixed by the application (five submissions per user per session, a random
    red wait, a client-measured reaction after green).

    ``game_date`` is indexed rather than unique: an Update ends the current
    session and opens a new one, so history shares days and the single-active
    rule lives in a partial unique index (see
    ``PostgresClient.apply_game_session_convergence``), not in this column --
    the same shape ``wordle_games`` converged to.

    ``created_by`` holds the creator's ``regno``, with no foreign key, for the
    same reason ``message_reports.reviewed_by_regno`` has none: staff live in
    ``admin`` and ``moderator`` and never in ``users``, so a key into the
    participant table would reject every creator this route can admit, and
    plain text lets the audit trail survive the account being renamed or
    deleted. The scheduler writes ``"scheduler"`` here, like every other
    game.
    """

    __tablename__ = "reaction_time_games"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    # The session's calendar identity. No longer unique: history shares days,
    # so one UTC day may hold several ended sessions and only one may be
    # current.
    game_date: Mapped[date] = mapped_column(Date, index=True)
    # Session lifecycle. ``active`` is playable, ``pending`` is reserved for
    # symmetry with the other session tables (the loop never opens a pending
    # Reaction Time session -- there is no manual word to wait for),
    # ``ended`` is history. Only one active-or-pending row may exist.
    status: Mapped[str] = mapped_column(
        String(10), default="active", server_default=text("'ended'"), index=True
    )
    # The calendar period this session covers. ``period_end_date`` is
    # exclusive: a daily session starting 2026-10-08 ends 2026-10-09. Derived
    # from the start date and the game's reset cadence by ``game_periods`` --
    # never by elapsed hours.
    period_start_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    period_end_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(USER_REGNO_MAX), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'pending', 'ended')",
            name="ck_reaction_time_games_status_known",
        ),
    )


class ReactionTimeSubmissionRecord(Base):
    """One user's individual attempt within one Reaction Time session.

    The row pairs a *player* with a *session*: everything below answers "what
    happened on this attempt?", and nothing else. The start click that creates
    the row already consumes one of the five submissions -- the row is born
    ``pending`` the moment the blue screen is clicked -- and the result later
    settles it to ``valid`` (a click after green, carrying the client-measured
    reaction time) or ``too_early`` (a click before green, carrying only
    informational elapsed time that is never a score). A new session means a
    fresh budget of five: submissions are scoped to the session, never to the
    date alone.

    A user holds attempts numbered 1..5 per session, enforced by
    ``UniqueConstraint(game, user, attempt_number)`` plus the
    ``attempt_number BETWEEN 1 AND 5`` check: the number is assigned
    server-side from the stored count, never trusted from the client, and two
    concurrent starts race the unique pair so exactly one of them owns the next
    number. Completed rows are immutable -- the service never updates a
    ``valid`` or ``too_early`` row -- so a score cannot be rewritten after the
    fact.
    """

    __tablename__ = "reaction_time_submissions"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    reaction_time_game_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("reaction_time_games.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(10), default="pending", server_default=text("'pending'")
    )
    # A valid reaction time in milliseconds, measured client-side between the
    # green transition and the click. Set only on ``valid`` rows; never a
    # score on ``too_early`` rows.
    reaction_time_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # How quickly the user clicked after the red/waiting state began, on a
    # ``too_early`` row. Informational only -- never a reaction-time score.
    elapsed_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # When the attempt started (the blue-screen click that consumed it).
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    # When the screen turned green, and when the user clicked, where known.
    green_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    clicked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("reaction_time_game_id", "user_id", "attempt_number"),
        CheckConstraint(
            "status IN ('pending', 'valid', 'too_early')",
            name="ck_reaction_time_submissions_status_known",
        ),
        CheckConstraint(
            f"attempt_number BETWEEN 1 AND {REACTION_TIME_MAX_SUBMISSIONS}",
            name=f"ck_reaction_time_submissions_attempt_{REACTION_TIME_MAX_SUBMISSIONS}",
        ),
        CheckConstraint(
            f"reaction_time_ms IS NULL OR reaction_time_ms BETWEEN "
            f"{REACTION_TIME_MIN_VALID_MS} AND {REACTION_TIME_MAX_VALID_MS}",
            name=f"ck_reaction_time_submissions_reaction_{REACTION_TIME_MAX_VALID_MS}",
        ),
        CheckConstraint(
            f"elapsed_ms IS NULL OR elapsed_ms BETWEEN 0 AND {REACTION_TIME_MAX_ELAPSED_MS}",
            name=f"ck_reaction_time_submissions_elapsed_{REACTION_TIME_MAX_ELAPSED_MS}",
        ),
    )


class ConnectionsGameRecord(Base):
    """One Connections session: the calendar period the puzzle runs on.

    The row is a *session* in the same lifecycle the puzzle games share --
    ``active`` while playable, ``pending`` while a manual puzzle is still
    missing, ``ended`` as history -- and not a play of it. There is
    deliberately no mode, no answer and no word column here: the 16 words and
    their four canonical groups live in ``connections_words``, one row per
    word, so a later administrative Update can never silently rewrite the
    answer key an earlier submission was scored against -- the old rows stay
    attached to the ended session.

    ``game_date`` is indexed rather than unique: an Update ends the current
    session and opens a new one, so history shares days and the single-active
    rule lives in a partial unique index (see
    ``PostgresClient.apply_game_session_convergence``), not in this column --
    the same shape ``wordle_games`` converged to.

    ``created_by`` holds the creator's ``regno``, with no foreign key, for the
    same reason ``message_reports.reviewed_by_regno`` has none: staff live in
    ``admin`` and ``moderator`` and never in ``users``. The scheduler writes
    ``"scheduler"`` here, like every other game.
    """

    __tablename__ = "connections_games"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    game_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(
        String(10), default="active", server_default=text("'ended'"), index=True
    )
    period_start_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    period_end_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(USER_REGNO_MAX), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'pending', 'ended')",
            name="ck_connections_games_status_known",
        ),
    )


class ConnectionsWordRecord(Base):
    """One word of one Connections session's puzzle, and the group it belongs to.

    Sixteen rows per configured session, four per ``group_index``. ``word_display``
    is the spelling the tiles show (trimmed, with sensible capitalization kept);
    ``word_normalized`` is the lowercased spelling duplicate detection and answer
    comparison run on, so ``Apple`` and `` apple `` are one word rather than two.
    ``group_index`` (0-3) is an organizational identifier only -- the groups are
    never named, described or hinted at on the player surface.

    The puzzle belongs to its session: a later Update attaches fresh rows to the
    new session and leaves these untouched, so historical submissions keep the
    answer key they were scored against.
    """

    __tablename__ = "connections_words"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    connections_game_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("connections_games.id", ondelete="CASCADE"), index=True
    )
    word_display: Mapped[str] = mapped_column(String(CONNECTIONS_WORD_MAX))
    word_normalized: Mapped[str] = mapped_column(String(CONNECTIONS_WORD_MAX), index=True)
    group_index: Mapped[int] = mapped_column(Integer, index=True)
    # The word's slot inside its group (0-3), as the administrator typed it.
    # The admin read echoes groups in this order, so what was saved is what
    # the console shows back; scoring never depends on it.
    position_in_group: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("connections_game_id", "word_normalized"),
        UniqueConstraint("connections_game_id", "group_index", "position_in_group"),
        CheckConstraint(
            "group_index BETWEEN 0 AND 3",
            name="ck_connections_words_group_index_0_3",
        ),
        CheckConstraint(
            "position_in_group BETWEEN 0 AND 3",
            name="ck_connections_words_position_0_3",
        ),
        CheckConstraint(
            f"length(word_display) BETWEEN 1 AND {CONNECTIONS_WORD_MAX}",
            name=f"ck_connections_words_display_length_{CONNECTIONS_WORD_MAX}",
        ),
        CheckConstraint(
            f"length(word_normalized) BETWEEN 1 AND {CONNECTIONS_WORD_MAX}",
            name=f"ck_connections_words_normalized_length_{CONNECTIONS_WORD_MAX}",
        ),
    )


class ConnectionsSubmissionRecord(Base):
    """One player's single final arrangement for one Connections session.

    The row pairs a *player* with a *session*: everything below answers "what
    did this user submit for this puzzle?", and nothing else. ``groups`` is the
    submitted assignment as four lists of four display spellings each, in the
    player's own group order; ``correct_groups`` (0-4) and ``correct_words``
    (0-16) are computed server-side from the stored canonical groups at submit
    time and never trusted from the client.

    One row per player per session, enforced by the unique pair rather than a
    service check: two concurrent submissions each read "no submission yet" and
    each try to insert, and the unique index is what lets exactly one of them
    own the row. Rows are insert-only -- a submitted arrangement is final, so
    there is no resubmission that could fish for the answer key one group at a
    time.
    """

    __tablename__ = "connections_submissions"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    connections_game_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("connections_games.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    groups: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    correct_groups: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    correct_words: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("connections_game_id", "user_id"),
        CheckConstraint(
            "correct_groups BETWEEN 0 AND 4",
            name="ck_connections_submissions_groups_0_4",
        ),
        CheckConstraint(
            "correct_words BETWEEN 0 AND 16",
            name="ck_connections_submissions_words_0_16",
        ),
    )


class ConnectionsScheduledPuzzleRecord(Base):
    """A manually configured Connections puzzle waiting for its session period.

    One row per period-start date: when the scheduler opens the period
    beginning ``target_date`` it attaches this row's groups to the new session
    instead of leaving the session pending. ``groups`` holds the four canonical
    answer groups as four lists of four display spellings each; the normalized
    spellings are derived on consumption, so this row stores exactly what the
    administrator typed.

    Rows are bounded by ``limits.MAX_FUTURE_SCHEDULE_DAYS`` at write time, and
    a scheduled puzzle never activates its session early -- the row waits until
    its date arrives. Consumed rows are deleted by the scheduler, like the
    scheduled words for Wordle and Anagram.
    """

    __tablename__ = "connections_scheduled_puzzles"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    target_date: Mapped[date] = mapped_column(Date, unique=True, index=True)
    groups: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    created_by: Mapped[str] = mapped_column(String(USER_REGNO_MAX), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )


class TriviaDropQuestionRecord(Base):
    """One Trivia Drop question, which is also its own session when released.

    Unlike the period games, Trivia Drop has no reset cadence: each question
    carries its own release schedule and becomes an individual live session.
    ``status`` is the lifecycle -- ``draft`` (being authored, invisible),
    ``scheduled`` (waiting for its release time), ``active`` (the one live
    question), ``ended`` (history with its submissions attached), ``cancelled``
    (withdrawn before release). At most one row may be ``active``; the partial
    unique index installed by
    ``PostgresClient.apply_trivia_drop_convergence`` is the schema's rule, not
    a service check, so an admin Deploy racing a scheduled release lands one
    winner and one integrity error.

    The answer key lives on ``trivia_drop_options`` (one row per option, with
    ``is_correct``), never here: the single-answer vs multiple-answer
    distinction is derived from the correct count, so there is no mode column
    that could disagree with the key. ``scheduled_for`` is the intended
    release; ``released_at`` is the authoritative activation instant response
    times are measured from, and it is written once and never rewritten.
    """

    __tablename__ = "trivia_drop_questions"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    question_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(16), default="draft", server_default=text("'draft'"), index=True
    )
    scheduled_for: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    released_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(USER_REGNO_MAX), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            f"length(question_text) BETWEEN 1 AND {TRIVIA_QUESTION_TEXT_MAX}",
            name=f"ck_trivia_drop_questions_text_length_{TRIVIA_QUESTION_TEXT_MAX}",
        ),
        CheckConstraint(
            "status IN ('draft', 'scheduled', 'active', 'ended', 'cancelled')",
            name="ck_trivia_drop_questions_status_known",
        ),
    )


class TriviaDropOptionRecord(Base):
    """One of the four answer options of one Trivia Drop question.

    ``option_index`` (0-3) is the stable identifier the answer key and the
    submissions name: options are positional into the question's four, so a
    wording correction cannot leave an answer pointing at words that moved.
    ``is_correct`` marks the option as part of the answer key -- exactly the
    collection the single/multiple distinction is derived from, so there is no
    redundant mode to drift.

    Options belong to their question: a later question is new rows, never an
    edit of these, so historical submissions keep the key they were scored
    against.
    """

    __tablename__ = "trivia_drop_options"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    question_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("trivia_drop_questions.id", ondelete="CASCADE"), index=True
    )
    option_index: Mapped[int] = mapped_column(Integer)
    option_text: Mapped[str] = mapped_column(String(TRIVIA_OPTION_TEXT_MAX))
    is_correct: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("question_id", "option_index"),
        CheckConstraint(
            f"option_index BETWEEN 0 AND {TRIVIA_OPTIONS_COUNT - 1}",
            name=f"ck_trivia_drop_options_index_{TRIVIA_OPTIONS_COUNT - 1}",
        ),
        CheckConstraint(
            f"length(option_text) BETWEEN 1 AND {TRIVIA_OPTION_TEXT_MAX}",
            name=f"ck_trivia_drop_options_text_length_{TRIVIA_OPTION_TEXT_MAX}",
        ),
    )


class TriviaDropSubmissionRecord(Base):
    """One player's single accepted answer for one Trivia Drop question.

    The row pairs a *player* with a *question*: everything below answers "what
    did this user submit for this question?", and nothing else.
    ``selected_options`` is the complete submitted set as option indexes, in
    ascending order -- the whole selection, never reduced to one option, so a
    historical result stays reconstructable. ``is_correct`` is exact set
    equality against the stored key, computed server-side at submit time;
    ``response_time_ms`` is ``submitted_at - released_at`` in milliseconds,
    from server timestamps only.

    One row per player per question, enforced by the unique pair rather than a
    service check: two concurrent submissions each read "no submission yet"
    and each try to insert, and the unique index is what lets exactly one of
    them own the row. Rows are insert-only -- an accepted answer, right or
    wrong, is final.
    """

    __tablename__ = "trivia_drop_submissions"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    question_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("trivia_drop_questions.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    selected_options: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    is_correct: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now()
    )
    response_time_ms: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("question_id", "user_id"),
        CheckConstraint(
            "response_time_ms >= 0",
            name="ck_trivia_drop_submissions_response_nonnegative",
        ),
    )


# ---------------------------------------------------------------------------
# Game configuration, featured games and scheduled content
# ---------------------------------------------------------------------------
# Configuration, featured dates, scheduled content, and active sessions use separate tables.


class GameConfigRecord(Base):
    """One row per game: availability, content mode and reset cadence.

    ``game_id`` is one of ``limits.GAME_IDS``. ``enabled`` and ``is_hidden`` are
    independent: disabled-but-visible games keep their session lifecycle while
    refusing play, hidden games vanish from selection, refuse direct play, and
    stop the scheduler from creating sessions. ``content_mode`` applies only to
    Wordle/Anagram (``random``/``manual``); the questionnaire stores NULL.
    ``reset_cadence`` is one of ``limits.RESET_CADENCE_VALUES`` and is read by
    the centralized ``game_periods`` calculation -- never by elapsed hours.
    """

    __tablename__ = "game_configs"

    __table_args__ = (
        CheckConstraint(
            "content_mode IS NULL OR content_mode IN ('random', 'manual')",
            name="ck_game_configs_content_mode_known",
        ),
        CheckConstraint(
            "reset_cadence IN ('daily', 'every_2_days', 'every_3_days', 'every_5_days', 'weekly')",
            name="ck_game_configs_reset_cadence_known",
        ),
        CheckConstraint(
            "grid_size IS NULL OR grid_size IN (12, 13, 14, 15)",
            # Numeric suffix, so this supersedes ``ck_game_configs_grid_size_known``
            # (the 7-10 membership) on the next boot -- see
            # ``PostgresClient._superseded_constraints``.
            name="ck_game_configs_grid_size_known_15",
        ),
        CheckConstraint(
            "difficulty IS NULL OR difficulty IN ('easy', 'medium', 'hard')",
            name="ck_game_configs_difficulty_known",
        ),
    )

    game_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    content_mode: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    reset_cadence: Mapped[str] = mapped_column(
        String(16), default="daily", server_default=text("'daily'")
    )
    # Two-Key Escape grid size; NULL for other games and defaulted on first read.
    grid_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Snapshotted into the next AI generation job; existing jobs are unchanged.
    difficulty: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )


class FeaturedGameRecord(Base):
    """The spotlight: at most one featured game per UTC calendar date.

    Independent of sessions by design -- featuring Wordle on a date never
    creates, expires or otherwise touches any session. ``game_date`` is unique
    in the schema (not only in the service) so two administrators pinning two
    games on one day race the constraint and one of them is refused. The same
    game may appear on many dates; many games may never share one date.
    """

    __tablename__ = "featured_games"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    game_date: Mapped[date] = mapped_column(Date, unique=True, index=True)
    game_id: Mapped[str] = mapped_column(String(64), index=True)
    created_by: Mapped[str] = mapped_column(String(USER_REGNO_MAX), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )


class ScheduledGameContentRecord(Base):
    """A manual word waiting for its session period to begin.

    One row per game per period-start date: when the scheduler opens the period
    beginning ``target_date`` it consumes this row's word (ending any pending
    session and opening a configured one) instead of leaving the session
    pending. Rows are bounded by ``limits.MAX_FUTURE_SCHEDULE_DAYS`` at write
    time, and a scheduled word never activates its session early -- the row
    waits until its date arrives.
    """

    __tablename__ = "scheduled_game_contents"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    game_id: Mapped[str] = mapped_column(String(64), index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    word: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(USER_REGNO_MAX), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("game_id", "target_date"),
    )


# League ledger: snapshot each scoring opportunity and persist immutable awards.
# Corrections supersede prior rows; featured status is captured at activation.

class GameScoreOpportunityRecord(Base):
    """One official scoring opportunity, snapshotted at activation.

    ``opportunity_id`` is the native event identifier -- a session id for the
    period games, a question id for Trivia Drop. ``activation_date`` is the UTC
    day the featured lookup ran against (the period start for sessions, the
    release day for questions); ``competition_at`` is the canonical UTC instant
    period boundaries are attributed to (period-start midnight for sessions,
    the release instant for questions).
    """

    __tablename__ = "game_score_opportunities"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    game_id: Mapped[str] = mapped_column(String(64), index=True)
    opportunity_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    competition_type: Mapped[str] = mapped_column(String(16), index=True)
    activation_date: Mapped[date] = mapped_column(Date, index=True)
    competition_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    is_featured_snapshot: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    scoring_version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    # Set when the opportunity has been through finalization, even when it
    # had no eligible results: the due scan skips processed rows instead of
    # re-examining empty events on every run.
    finalized_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("game_id", "opportunity_id", name="uq_score_opportunity_game_event"),
        CheckConstraint(
            "competition_type IN ('individual', 'couple')",
            name="ck_score_opportunity_competition_type",
        ),
    )


class GameScoreAwardRecord(Base):
    """Immutable league award with separate game/global scores and version stamps.

    Corrections supersede rows rather than overwrite them. Legacy score columns remain for
    compatibility; couple subjects use a canonical member order.
    """

    __tablename__ = "game_score_awards"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    game_id: Mapped[str] = mapped_column(String(64), index=True)
    opportunity_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    competition_type: Mapped[str] = mapped_column(String(16), index=True)
    user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), index=True)
    member_a_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), index=True)
    member_b_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id"), index=True)
    raw_metrics: Mapped[Optional[Any]] = mapped_column(JSONB)
    rank: Mapped[int] = mapped_column(Integer)
    participant_count: Mapped[int] = mapped_column(Integer)
    base_points: Mapped[Any] = mapped_column(Numeric(8, 2))
    featured_multiplier: Mapped[int] = mapped_column(SmallInteger, default=1, server_default=text("1"))
    awarded_points: Mapped[Any] = mapped_column(Numeric(8, 2))
    global_base_points: Mapped[Optional[Any]] = mapped_column(Numeric(8, 2))
    game_base_points: Mapped[Optional[Any]] = mapped_column(Numeric(8, 2))
    global_points: Mapped[Optional[Any]] = mapped_column(Numeric(8, 2))
    game_points: Mapped[Optional[Any]] = mapped_column(Numeric(8, 2))
    global_scoring_version: Mapped[Optional[int]] = mapped_column(Integer)
    game_scoring_version: Mapped[Optional[int]] = mapped_column(Integer)
    is_featured_snapshot: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    competition_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    scoring_version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    finalized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    superseded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "competition_type IN ('individual', 'couple')",
            name="ck_score_award_competition_type",
        ),
        CheckConstraint(
            "featured_multiplier IN (1, 2)",
            name="ck_score_award_multiplier",
        ),
        CheckConstraint(
            "(competition_type = 'individual' AND user_id IS NOT NULL "
            "AND member_a_id IS NULL AND member_b_id IS NULL) "
            "OR (competition_type = 'couple' AND user_id IS NULL "
            "AND member_a_id IS NOT NULL AND member_b_id IS NOT NULL)",
            name="ck_score_award_subject_shape",
        ),
        CheckConstraint(
            "member_a_id IS NULL OR member_b_id IS NULL OR member_a_id <= member_b_id",
            name="ck_score_award_couple_order",
        ),
        Index(
            "uq_score_award_individual",
            "game_id", "opportunity_id", "user_id",
            unique=True,
            postgresql_where=text(
                "competition_type = 'individual' AND superseded_at IS NULL"
            ),
        ),
        Index(
            "uq_score_award_couple",
            "game_id", "opportunity_id", "member_a_id", "member_b_id",
            unique=True,
            postgresql_where=text(
                "competition_type = 'couple' AND superseded_at IS NULL"
            ),
        ),
        Index("ix_score_award_game_competition", "game_id", "competition_at"),
        Index("ix_score_award_type_competition", "competition_type", "competition_at"),
    )


# Staff notices target roles and use report, love-note, or game categories.
# Read state and email preferences are per account; preferences do not filter the feed.


class StaffNotificationRecord(Base):
    """One stored staff notice, addressed to a role rather than a person."""

    __tablename__ = "staff_notifications"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    # Who this is news to: ``admin``, ``moderator`` or ``all`` (both roles).
    # Report and love-note events are ``all``; the automatic block at the removal
    # threshold and every game event are ``admin`` (only an admin can act on
    # them).
    audience: Mapped[str] = mapped_column(String(16), index=True)
    # One of ``report``, ``love_note`` or ``game``. The automatic block notice
    # shares ``report`` with the filed-report notices; game notices are stored
    # by the scheduler and start-up checks in staff_notification_service.
    category: Mapped[str] = mapped_column(String(16), index=True)
    heading: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    target_kind: Mapped[Optional[str]] = mapped_column(String(40))
    target_id: Mapped[Optional[str]] = mapped_column(String(100))
    # Idempotency key (e.g. ``report:<report_id>``, ``love-note:<note_id>``).
    # Unique when present so a retried filing cannot double-notify; NULL when
    # the event has no natural identity.
    dedup_key: Mapped[Optional[str]] = mapped_column(String(160), unique=True, index=True)

    __table_args__ = (
        CheckConstraint(
            "audience IN ('admin', 'moderator', 'all')",
            name="ck_staff_notifications_audience_known",
        ),
        CheckConstraint(
            "category IN ('report', 'love_note', 'game')",
            name="ck_staff_notifications_category_known",
        ),
    )


class StaffNotificationReadRecord(Base):
    """Read state for a notice, keyed by the staff account table and primary key."""

    __tablename__ = "staff_notification_reads"

    notification_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("staff_notifications.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    account_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    account_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            f"account_type IN ({_ACCOUNT_TYPE_SQL_LIST})",
            name="ck_staff_notification_reads_account_type_known",
        ),
    )


class StaffNotificationPreferenceRecord(Base):
    """Per-account email preferences; role checks remain the authorization boundary."""

    __tablename__ = "staff_notification_preferences"

    account_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    account_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    reports_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    love_notes_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    games_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            f"account_type IN ({_ACCOUNT_TYPE_SQL_LIST})",
            name="ck_staff_notification_prefs_account_type_known",
        ),
    )


# Staff preference rows are created with ORM staff accounts; startup backfills
# legacy and raw-SQL-created accounts.
def _staff_notification_preferences_after_insert(account_type: str):
    def _listener(mapper, connection, target) -> None:
        connection.execute(
            pg_insert(StaffNotificationPreferenceRecord)
            .values(
                account_type=account_type,
                account_id=str(target.id),
                reports_enabled=True,
                love_notes_enabled=True,
                games_enabled=True,
            )
            .on_conflict_do_nothing(index_elements=["account_type", "account_id"])
        )

    return _listener


event.listen(
    AdminRecord,
    "after_insert",
    _staff_notification_preferences_after_insert(AccountType.ADMIN.value),
)
event.listen(
    ModeratorRecord,
    "after_insert",
    _staff_notification_preferences_after_insert(AccountType.MODERATOR.value),
)


# Account identifiers are table-local. Login resolves only the selected account table;
# cross-table lookup helpers are reserved for display and duplicate checks.

def find_account_in_table(session, account_type: Optional[str], regno: Optional[str]) -> Optional[tuple[str, Any]]:
    """Resolve a regno only in the named account table; invalid types fail closed."""
    if not regno or not is_valid_account_type(account_type):
        return None
    model = ACCOUNT_MODELS[normalize_account_type(account_type)]
    row = session.scalar(select(model).where(model.regno == regno))
    if row is None:
        return None
    return normalize_account_type(account_type), row


def resolve_account_by_regno(session, regno: Optional[str]) -> Optional[tuple[str, Any]]:
    """Find the account holding ``regno``, as ``(account_type, row)`` or None."""
    if not regno:
        return None
    for account_type, model in ACCOUNT_MODELS.items():
        row = session.scalar(select(model).where(model.regno == regno))
        if row is not None:
            return account_type, row
    return None


class PlotTwistBlueprintRecord(Base):
    """One AI-generated story blueprint awaiting approval.

    The blueprint is the approved contract every later scene request must
    follow: generation prompts carry it explicitly rather than depending on
    hidden conversation history. ``blueprint`` holds the validated
    BlueprintDef; ``status`` moves draft -> generating -> ready_for_review ->
    approved (or generation_failed). Approval never edits history in place:
    publishing always snapshots a new package version.
    """

    __tablename__ = "plot_twist_blueprints"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(200), default="", server_default=text("''"))
    difficulty: Mapped[str] = mapped_column(String(16), default="medium", server_default=text("'medium'"))
    config: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    blueprint: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(20), default="draft", server_default=text("'draft'"), index=True)
    validation_errors: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    created_by: Mapped[str] = mapped_column(String(USER_REGNO_MAX), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'generating', 'ready_for_review', 'approved', 'generation_failed')",
            name="ck_plot_twist_blueprints_status_known",
        ),
    )


class PlotTwistPackageRecord(Base):
    """One versioned, validated story package generated from a blueprint.

    ``package`` holds the validated StoryPackageDef: scenes, choices,
    endings and rewards. ``status`` moves draft -> generating ->
    validation_failed -> ready_for_review -> published (or
    generation_failed). Publishing is append-only: edits and regeneration
    create a new row with a bumped version, so sessions already assigned to
    a published version keep playing exactly what they started.
    """

    __tablename__ = "plot_twist_packages"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    blueprint_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("plot_twist_blueprints.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    title: Mapped[str] = mapped_column(String(200), default="", server_default=text("''"))
    package: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(20), default="draft", server_default=text("'draft'"), index=True)
    validation_errors: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(USER_REGNO_MAX), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("blueprint_id", "version"),
        CheckConstraint(
            "status IN ('draft', 'generating', 'validation_failed', 'ready_for_review', 'published', 'generation_failed')",
            name="ck_plot_twist_packages_status_known",
        ),
    )


class PlotTwistGameRecord(Base):
    """One Plot Twist global period: the calendar window couples play in.

    The row pins the published story package version in force when the
    period opened (``story_package_id``), so every couple in the period
    plays the same version. Publishing a package rotates the event
    (terminate-then-create, like a Wordle/Anagram Update): the live row ends
    and a fresh row pinned to the new package opens. The reset loop also
    opens periods under the game's own cadence; with no published package
    the loop opens nothing.
    """

    __tablename__ = "plot_twist_games"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    game_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(
        String(10), default="active", server_default=text("'ended'"), index=True
    )
    period_start_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    period_end_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    story_package_id: Mapped[Optional[str]] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("plot_twist_packages.id", ondelete="SET NULL"), index=True, nullable=True
    )
    seed: Mapped[str] = mapped_column(String(64), default="", server_default=text("''"))
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(USER_REGNO_MAX), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'pending', 'ended')",
            name="ck_plot_twist_games_status_known",
        ),
    )


class PlotTwistSessionRecord(Base):
    """One couple's playthrough of a pinned story package version.

    ``story_package_id`` is denormalized from the global game at creation so
    the session's history and scoring stay bound to the version it played:
    publishing ends the live period and opens a fresh one, so couples start
    the new story from scratch while unfinished sessions of the ended period
    stay unfinished and score nothing. ``selected_scenes`` is the deterministic module selection for
    this couple (ordered scene ids plus the seed), persisted before the first
    decision and never reshuffled. ``state`` is the current variable snapshot;
    ``current_scene_id`` plus ``assigned_actor`` name whose turn it is.
    ``state_version`` is optimistic concurrency: every accepted choice bumps
    it, and a stale submit is refused.
    """

    __tablename__ = "plot_twist_sessions"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    plot_twist_game_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("plot_twist_games.id", ondelete="CASCADE"), index=True
    )
    story_package_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("plot_twist_packages.id", ondelete="CASCADE"), index=True
    )
    user_a_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    user_b_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    current_scene_id: Mapped[str] = mapped_column(String(64), default="", server_default=text("''"))
    state: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    tags: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    assigned_actor: Mapped[str] = mapped_column(String(16), default="either", server_default=text("'either'"))
    status: Mapped[str] = mapped_column(String(20), default="awaiting_user_a", server_default=text("'awaiting_user_a'"), index=True)
    selected_scenes: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    visited_scenes: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    ending_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    treasures: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    state_version: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    score_awarded: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("plot_twist_game_id", "user_a_id", "user_b_id"),
        CheckConstraint("user_a_id != user_b_id", name="ck_plot_twist_sessions_distinct_users"),
        CheckConstraint(
            "status IN ('awaiting_user_a', 'awaiting_user_b', 'completed')",
            name="ck_plot_twist_sessions_status_known",
        ),
    )


class PlotTwistDecisionRecord(Base):
    """One recorded decision: who chose what, and what it changed.

    Insert-only and seq-numbered server-side from the stored count, so a
    retry or duplicate submit for the same turn resolves to the stored row
    instead of applying consequences twice.
    """

    __tablename__ = "plot_twist_decisions"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("plot_twist_sessions.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    scene_id: Mapped[str] = mapped_column(String(64), index=True)
    actor_user_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    choice_id: Mapped[str] = mapped_column(String(64))
    next_scene_id: Mapped[str] = mapped_column(String(64))
    state_before: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    state_after: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("session_id", "seq"),
    )


class CaseClosedBlueprintRecord(Base):
    """One AI-generated mystery blueprint awaiting approval.

    Hidden authoritative truth (Stage A). ``blueprint`` holds the validated
    CaseClosedBlueprint; ``status`` moves draft -> generating ->
    ready_for_review -> approved (or generation_failed). Publishing never
    mutates this row; packages snapshot new versions.
    """

    __tablename__ = "case_closed_blueprints"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(200), default="", server_default=text("''"))
    difficulty: Mapped[str] = mapped_column(String(16), default="medium", server_default=text("'medium'"))
    config: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    blueprint: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(20), default="draft", server_default=text("'draft'"), index=True)
    validation_errors: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    created_by: Mapped[str] = mapped_column(String(USER_REGNO_MAX), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'generating', 'ready_for_review', 'approved', 'generation_failed')",
            name="ck_case_closed_blueprints_status_known",
        ),
    )


class CaseClosedPackageRecord(Base):
    """One versioned, validated investigation package from a blueprint.

    ``package`` holds the validated CaseClosedPackage (DAG + evidence +
    hidden solution). Immutable once published and referenced by an event.
    """

    __tablename__ = "case_closed_packages"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    blueprint_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("case_closed_blueprints.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    title: Mapped[str] = mapped_column(String(200), default="", server_default=text("''"))
    package: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(20), default="draft", server_default=text("'draft'"), index=True)
    validation_errors: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(USER_REGNO_MAX), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("blueprint_id", "version"),
        CheckConstraint(
            "status IN ('draft', 'generating', 'validation_failed', 'ready_for_review', 'published', 'generation_failed')",
            name="ck_case_closed_packages_status_known",
        ),
    )


class CaseClosedGameRecord(Base):
    """One Case Closed global event: pins one published package version.

    Every couple in the event plays the same case, scoring rules, and
    deadline. Publishing a new package does not mutate this row; the
    scheduler opens a fresh event pinned to the new package.
    """

    __tablename__ = "case_closed_games"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    game_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(
        String(10), default="active", server_default=text("'ended'"), index=True
    )
    period_start_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    period_end_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    case_package_id: Mapped[Optional[str]] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("case_closed_packages.id", ondelete="SET NULL"), index=True, nullable=True
    )
    seed: Mapped[str] = mapped_column(String(64), default="", server_default=text("''"))
    scoring_half_life_seconds: Mapped[int] = mapped_column(Integer, default=43200, server_default=text("43200"))
    scoring_max_points: Mapped[int] = mapped_column(Integer, default=100, server_default=text("100"))
    finalized: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    finalized_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(USER_REGNO_MAX), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'pending', 'ended')",
            name="ck_case_closed_games_status_known",
        ),
    )


class CaseClosedSessionRecord(Base):
    """One couple's session for a pinned case event.

    ``case_package_id`` is denormalized from the game at creation so the
    session stays bound to the version it started even if a newer package
    is published. Budgets/paths/proposals live here; append-only history
    lives in investigations + evidence tables. ``state_version`` is
    optimistic concurrency for investigations and accusation locks.
    """

    __tablename__ = "case_closed_sessions"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    case_closed_game_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("case_closed_games.id", ondelete="CASCADE"), index=True
    )
    case_package_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("case_closed_packages.id", ondelete="CASCADE"), index=True
    )
    user_a_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    user_b_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="investigating", server_default=text("'investigating'"), index=True)
    actions_used_a: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    actions_used_b: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    path_a: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    path_b: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    proposal_a: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    proposal_b: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    confirmed_a: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    confirmed_b: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    locked_culprit_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    first_investigation_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    accusation_locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
# Copied event boundary; investigation chat expires with the case, not activity.
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    elapsed_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_correct: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    score: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    finalized: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    finalized_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    state_version: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("case_closed_game_id", "user_a_id", "user_b_id"),
        CheckConstraint("user_a_id != user_b_id", name="ck_case_closed_sessions_distinct_users"),
        CheckConstraint(
            "status IN ('investigating', 'accusation_locked', 'completed', 'expired')",
            name="ck_case_closed_sessions_status_known",
        ),
    )


class CaseClosedInvestigationRecord(Base):
    """Append-only per-actor investigation history (audit + idempotency).

    One row per (session, actor, lead) first touch; retries resolve to the
    stored row instead of charging twice. ``already_known`` marks a visit
    to a lead the couple already discovered via the partner (no budget
    charge, navigation only).
    """

    __tablename__ = "case_closed_investigations"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("case_closed_sessions.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    lead_id: Mapped[str] = mapped_column(String(64), index=True)
    actor_user_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    already_known: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("session_id", "seq"),
        UniqueConstraint("session_id", "actor_user_id", "lead_id"),
    )


class CaseClosedEvidenceRecord(Base):
    """Shared evidence board: one row per first discovery per session.

    Records who discovered what and when; the partner's later visit to the
    same lead replays this row instead of creating a duplicate.
    """

    __tablename__ = "case_closed_evidence"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("case_closed_sessions.id", ondelete="CASCADE"), index=True
    )
    evidence_id: Mapped[str] = mapped_column(String(64), index=True)
    lead_id: Mapped[str] = mapped_column(String(64), index=True)
    discoverer_user_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("session_id", "evidence_id"),
    )


class CaseClosedMessageRecord(RemovedContentColumns, Base):
    """One chat turn in a couple's Case Closed detective thread, encrypted at rest.

    Scoped to the couple's game session (one thread per session), not to a
    matchmaking match: the thread is found via the caller's current Case
    Closed session, so the game id and session id travel together. Quotas
    (400 per thread, 200 per detective) are enforced in
    ``case_closed_messaging``; the columns here only persist the transcript.

    ``delete_after`` is the session's own ``expires_at`` at send time. Chat
    locking (an accusation confirmation) closes writes but never changes this
    deadline; only the game session's expiry makes the messages eligible for
    the next scheduled purge.

    Carries ``removed_at``/``removed_by_regno`` like every other content table
    so an Azure Content Safety removal can hide a turn without deleting it: the
    flagged turn is stored born hidden (never delivered, never in the
    transcript) but still counts against the sender's allowance, which is why
    the quota reads count removed rows while the transcript reads filter them.
    """

    __tablename__ = "case_closed_messages"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("case_closed_sessions.id", ondelete="CASCADE"), index=True
    )
    sender_user_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    body_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    body_nonce: Mapped[bytes] = mapped_column(LargeBinary(length=12), nullable=False)
    body_key_version: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now(), index=True)
    delete_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))

    __table_args__ = (
        Index("ix_case_closed_messages_session_created", "session_id", "created_at"),
        Index("ix_case_closed_messages_session_delete_after", "session_id", "delete_after"),
    )


class CaseClosedMessageReportRecord(Base):
    """One report filed against a Case Closed detective-thread turn.

    Mirrors ``MessageReportRecord``: the partner (the only one who may report a
    turn, since a sender cannot report their own) files against a turn they
    received, and Azure Content Safety rows are written when a turn is removed
    (``reporter_regno`` NULL, ``source`` = ``azure_content_safety``, sender named
    in ``reported_user_regno``). Kept as its own table rather than reusing
    ``message_reports`` because that table's FK points at matchmaking
    ``messages``, not at ``case_closed_messages``. A manual report keeps the turn
    visible until a moderator rules on it from the admin report queue.
    """

    __tablename__ = "case_closed_message_reports"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    # SET NULL for the same reason as ``message_reports.message_id``: ordinary
    # chat retention removes the turn, while the moderation report and its
    # separately encrypted evidence follow their own retention window.
    message_id: Mapped[Optional[str]] = mapped_column(
        Uuid(as_uuid=False),
        ForeignKey("case_closed_messages.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    # Nullable for the same reason as ``message_reports.reporter_regno``: an
    # automated report has no participant behind it.
    reporter_regno: Mapped[Optional[str]] = mapped_column(
        String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True, nullable=True
    )
    reported_user_regno: Mapped[str] = mapped_column(
        String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True
    )
    reason: Mapped[str] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default=text("'pending'"), index=True)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    reviewed_by_regno: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    source: Mapped[str] = mapped_column(String(32), default="user", server_default=text("'user'"), index=True)
    __table_args__ = (UniqueConstraint("message_id", "reporter_regno"),)


def resolve_account_by_id(session, account_id: Optional[str]) -> Optional[tuple[str, Any]]:
    """Find the account with this primary key, as ``(account_type, row)`` or None."""
    if not _is_account_id(account_id):
        return None
    for account_type, model in ACCOUNT_MODELS.items():
        row = session.get(model, str(account_id))
        if row is not None:
            return account_type, row
    return None


def resolve_account(session, account_type: Optional[str], account_id: Optional[str]) -> Optional[Any]:
    """Fetch one account from one named table, or None if that table has no such row.

    Used wherever the table is already known -- a magic-link token carries the account kind
    it was minted for, so redemption reads exactly one table instead of searching three.
    """
    if not _is_account_id(account_id):
        return None
    return session.get(ACCOUNT_MODELS[normalize_account_type(account_type)], str(account_id))


def _is_account_id(value: Optional[str]) -> bool:
    """Whether ``value`` is a well-formed account primary key.

    Duplicated rather than imported from ``app.postgres`` because that module imports this
    one; the check is four lines and importing it would close the cycle.
    """
    if not value:
        return False
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


class GameGenerationJobRecord(Base):
    """One scheduled AI generation cycle for Plot Twist or Case Closed.

    The scheduler never generates content inline: at a cycle boundary (or at
    boot / the manual run-resets trigger) it writes one row here and returns.
    A background worker claims at most one job at a time under a single
    advisory lock shared by both games, runs the existing blueprint ->
    approve -> package -> validate pipeline, and only opens the playable
    session when the validated package is published -- all in one transaction
    together with the job's own terminal update, so a crash can never leave a
    published session without its job or a completed job without its session.

    ``cycle_start_date``/``cycle_end_date`` are the calendar window the cycle
    was scheduled for. They are fixed at enqueue time, so re-running the
    scheduler for the same day finds the same job (the unique key) instead of
    starting a second one, and generation that runs long never shifts the
    game's configured frequency. ``difficulty`` is likewise a snapshot: the
    admin's pick at enqueue time, not whatever it is when a worker runs.

    ``status`` is the lifecycle: ``queued`` -> ``running`` -> (``published`` |
    ``failed`` | ``cancelled``), with ``retry_wait`` between bounded attempts.
    """
    __tablename__ = "game_generation_jobs"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    game_id: Mapped[str] = mapped_column(String(64), index=True)
    cycle_start_date: Mapped[date] = mapped_column(Date, index=True)
    cycle_end_date: Mapped[date] = mapped_column(Date, index=True)
    difficulty: Mapped[str] = mapped_column(String(16), default="medium", server_default=text("'medium'"))
    status: Mapped[str] = mapped_column(
        String(16), default="queued", server_default=text("'queued'"), index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, server_default=text("3"))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Which worker claimed the job. Diagnostic only; empty when unclaimed.
    worker_id: Mapped[str] = mapped_column(String(128), default="", server_default=text("''"))
    blueprint_id: Mapped[Optional[str]] = mapped_column(Uuid(as_uuid=False), nullable=True)
    package_id: Mapped[Optional[str]] = mapped_column(Uuid(as_uuid=False), nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(USER_REGNO_MAX), default="scheduler", server_default=text("'scheduler'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    __table_args__ = (
        # One cycle per game per start date, enforced in the schema: a second
        # scheduler run for the same boundary races this key and loses, so a
        # duplicate generation cycle can never exist.
        UniqueConstraint("game_id", "cycle_start_date", name="uq_generation_job_game_cycle"),
        Index("ix_generation_jobs_status_requested", "status", "requested_at"),
        CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'published', 'failed', 'cancelled')",
            name="ck_game_generation_jobs_status_known",
        ),
    )


class GameResetHeartbeatRecord(Base):
    """The last outcome of the daily game-reset tick.

    The tick runs in the scheduler sidecar: a separate process that starts, does
    one pass and exits. Nothing it does outlives it, so a tick that stopped
    running -- a broken cron entry, a sidecar that would not start, a pass that
    died before it could say anything -- left no trace anywhere. The games simply
    stopped rolling over, and the only symptom was a stale leaderboard days
    later.

    This row is that trace. One row per job, written by the tick after every
    pass and read by the backend, which publishes it as a metric so that "the
    daily job has not succeeded since <time>" is a thing an alert can see.

    ``last_success_at`` is the timestamp the alert cares about, and it moves only
    on a pass that finished without a failure: a failed attempt records
    ``last_attempt_at`` and ``last_error`` and leaves the last success alone, so
    one bad night does not erase the run before it, and the age of the last
    success is what says how long the job has really been broken.
    """
    __tablename__ = "game_reset_heartbeats"

    # The job this heartbeat is about. The primary key, so a second job gets its
    # own row rather than overwriting this one, and so the write is an upsert
    # that cannot produce a duplicate.
    job: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Diagnostic only: the failure text of the last failed attempt, cleared on the
    # next success. It is short on purpose -- a whole traceback would not fit a
    # column, and the traceback is in the tick's own log.
    last_error: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )


class GameSessionRevocationRecord(Base):
    """One admin revocation of an AI-game session, kept as an audit trail.

    Revoking ends the live session through the existing ``ended`` state and
    suppresses replacement generation until ``revoked_until`` -- the revoked
    session's own period end, or the cancelled cycle's boundary when there was
    no live session. The scheduler consults only whether any row for the game
    carries a ``revoked_until`` later than today, so a revocation expires by
    the calendar exactly like every other boundary in the games system; the
    rows themselves are never deleted, so history can show what was revoked
    and when. ``session_id`` is NULL when the revocation cancelled a queued or
    running generation cycle that had not opened a session yet.
    """
    __tablename__ = "game_session_revocations"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    game_id: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[Optional[str]] = mapped_column(Uuid(as_uuid=False), nullable=True)
    revoked_until: Mapped[date] = mapped_column(Date, index=True)
    revoked_by: Mapped[str] = mapped_column(String(USER_REGNO_MAX), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())

    __table_args__ = (
        Index("ix_game_revocations_game_until", "game_id", "revoked_until"),
    )
