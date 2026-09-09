"""Normalized SQLAlchemy schema for PostgreSQL application data."""
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, SmallInteger,
    ARRAY, String, Text, UniqueConstraint, Uuid, text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .limits import COMMENT_BODY_MAX, CONFESSION_BODY_MAX, MESSAGE_BODY_MAX


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class UserRecord(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    regno: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    emoji: Mapped[Optional[str]] = mapped_column(String(16))
    bio: Mapped[Optional[str]] = mapped_column(String(500))
    class_name: Mapped[str] = mapped_column(String(80), index=True)
    profile_picture_url: Mapped[Optional[str]] = mapped_column(Text)
    gender: Mapped[str] = mapped_column(String(24))
    interests: Mapped[list[str]] = mapped_column(ARRAY(String(80)), default=list, server_default=text("'{}'"))
    matchmaking_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    love_notes_receive_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    love_notes_send_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    reported_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_login_ip: Mapped[Optional[str]] = mapped_column(INET)
    last_login_user_agent: Mapped[Optional[str]] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), default="user", server_default=text("'user'"), index=True)
    last_matchmaking_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


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

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True)
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
    reviewed_by_regno: Mapped[Optional[str]] = mapped_column(String(100), ForeignKey("users.regno", ondelete="SET NULL"))
    __table_args__ = (UniqueConstraint("message_id", "reporter_regno"),)
