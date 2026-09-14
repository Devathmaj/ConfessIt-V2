"""Normalized SQLAlchemy schema for PostgreSQL application data.

There are three account tables -- ``users`` for participants, ``admin`` and ``moderator``
for staff -- and an account's privileges are a function of which one its row is in, not of
any column it holds. Nothing in this schema stores a role.
"""
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, SmallInteger,
    ARRAY, String, Text, UniqueConstraint, Uuid, func, select, text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .limits import (
    COMMENT_BODY_MAX,
    CONFESSION_BODY_MAX,
    MESSAGE_BODY_MAX,
)
from .accounts import (
    ACCOUNT_TYPE_VALUES,
    AccountType,
    is_valid_account_type,
    normalize_account_type,
)

# Rendered into the CHECK below. The values are literals in trusted source, not user input,
# and quoting them here rather than passing them as bind parameters keeps the constraint a
# single self-contained DDL expression that survives the NOT VALID re-add in
# ``PostgresClient.apply_check_constraints``.
_ACCOUNT_TYPE_SQL_LIST = ", ".join(f"'{value}'" for value in ACCOUNT_TYPE_VALUES)


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    """An aware UTC default, so a row written outside a request still gets a real instant."""
    return datetime.now(timezone.utc)


class AccountColumns:
    """The columns every account table shares.

    Kept as one mixin rather than copied three times so a field that is genuinely common
    cannot drift between the tables -- ``last_login_ip`` being present for participants and
    absent for staff would be a bug no type checker would catch, and the whole point of
    splitting the tables is that the shared shape stays honest.

    Deliberately *not* here: emoji, bio, class_name, interests, email, matchmaking_enabled
    and the love-note toggles. Those describe a participant, and a staff account is not a
    participant, so storing them would only create columns nothing writes and nothing reads.
    """

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    regno: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    profile_picture_url: Mapped[Optional[str]] = mapped_column(Text)
    gender: Mapped[str] = mapped_column(String(24))
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


class UserRecord(AccountColumns, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    emoji: Mapped[Optional[str]] = mapped_column(String(16))
    bio: Mapped[Optional[str]] = mapped_column(String(500))
    class_name: Mapped[str] = mapped_column(String(80), index=True)
    interests: Mapped[list[str]] = mapped_column(ARRAY(String(80)), default=list, server_default=text("'{}'"))
    matchmaking_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    love_notes_receive_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    love_notes_send_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    last_matchmaking_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class AdminRecord(AccountColumns, Base):
    """A staff account with the account-lifecycle capabilities. No participant fields."""

    __tablename__ = "admin"


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


class ConfessionRecord(Base):
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
            name="ck_confessions_body_length",
        ),
    )


class ConfessionCommentRecord(Base):
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
            name="ck_confession_comments_body_length",
        ),
    )
    report_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class ReportRecord(Base):
    __tablename__ = "reports"
    __table_args__ = (
        CheckConstraint("(confession_id IS NOT NULL) <> (comment_id IS NOT NULL)", name="ck_reports_one_target"),
        UniqueConstraint("confession_id", "reported_by_id", name="uq_reports_confession_reporter"),
        UniqueConstraint("comment_id", "reported_by_id", name="uq_reports_comment_reporter"),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    confession_id: Mapped[Optional[str]] = mapped_column(Uuid(as_uuid=False), ForeignKey("confessions.id", ondelete="CASCADE"), index=True)
    comment_id: Mapped[Optional[str]] = mapped_column(Uuid(as_uuid=False), ForeignKey("confession_comments.id", ondelete="CASCADE"), index=True)
    reported_by_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    reported_by_name: Mapped[str] = mapped_column(String(160))
    reason: Mapped[str] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class LoginTokenRecord(Base):
    __tablename__ = "login_tokens"

    # A magic link is minted for an account in whichever of the three tables that account
    # lives in, so there is no single table this id can point at and the old ``users.id``
    # foreign key had to go. The pair below replaces it: the id identifies the row, and
    # ``account_type`` says which table to read it from. The CHECK keeps the two in step --
    # a row id without a resolvable table would be a login that cannot be redeemed.
    #
    # NULL is excluded explicitly for the same reason as in a bare ``IN``: NULL would
    # evaluate to NULL, which a CHECK constraint treats as passing.
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
    attempt_count: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"))
    session_status: Mapped[str] = mapped_column(String(16), default="issued", server_default=text("'issued'"), index=True)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    device: Mapped[Optional[str]] = mapped_column(String(512))


class LoveNoteRecord(Base):
    __tablename__ = "love_notes"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    sender_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    recipient_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    image_url: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(String(2000))
    anonymous: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(String(24), default="pending_review", server_default=text("'pending_review'"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class LoveNoteTemplateRecord(Base):
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
    # Canonical "lower|upper" form of the two regnos, so a pair is identified the same way
    # regardless of who initiated. Unique, which is what stops two concurrent requests
    # from matching the same two people. Cleared to NULL when the match lapses, and NULLs
    # are not compared by a unique index, so an expired match frees the pair for good
    # without needing a background job to sweep it.
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
    last_message_preview: Mapped[Optional[str]] = mapped_column(String(160))
    blocked_by: Mapped[list[str]] = mapped_column(ARRAY(String(100)), default=list, server_default=text("'{}'"))
    terminated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class MessageRecord(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    sender_regno: Mapped[str] = mapped_column(String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    receiver_regno: Mapped[str] = mapped_column(String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    __table_args__ = (
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        CheckConstraint(
            f"length(body) <= {MESSAGE_BODY_MAX}",
            name="ck_messages_body_length",
        ),
    )


class NotificationRecord(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    recipient_regno: Mapped[str] = mapped_column(String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    heading: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class MessageReportRecord(Base):
    __tablename__ = "message_reports"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    message_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    reporter_regno: Mapped[str] = mapped_column(String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
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
    __table_args__ = (UniqueConstraint("message_id", "reporter_regno"),)


# ---------------------------------------------------------------------------
# Cross-table account lookup
# ---------------------------------------------------------------------------
# A regno or an id is unique *within* one account table, not across all three. Login says
# which table it means, so the resolution that matters at request time is
# :func:`find_account_in_table`, which reads exactly one named table and never has to break
# a tie.
#
# :func:`resolve_account_by_regno` and :func:`resolve_account_by_id` still exist and still
# search all three, but they are only for the places where no table is known and none is
# needed to be right: showing an admin which conversations a regno is attached to, and
# refusing a create that would reuse a regno on a table where it would be ambiguous to a
# human. Nothing that decides *who is calling* uses them -- a caller is always identified by
# the table and primary key their credential recorded, never by a search.

def find_account_in_table(session, account_type: Optional[str], regno: Optional[str]) -> Optional[tuple[str, Any]]:
    """Find the account holding ``regno`` in one named table, as ``(account_type, row)``.

    The lookup behind login. The table is supplied by the caller -- by the page the person
    chose to sign in through -- so a regno that exists in two tables resolves to the one
    that page was for, and a regno that exists only in a different table is simply not
    found rather than quietly resolving to somebody else's account.

    An unrecognised ``account_type`` returns None instead of falling back to a default
    table. :func:`normalize_account_type` would answer such a value with ``users``, which is
    the right fail-closed answer for a *stored* discriminator whose privileges are being
    read, and exactly the wrong one here: a request that named no real account table must
    not be answered out of the participant table.
    """
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
