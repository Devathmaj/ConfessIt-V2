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


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class UserRecord(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    Regno: Mapped[str] = mapped_column("regno", String(100), unique=True, index=True)
    Name: Mapped[str] = mapped_column("name", String(160))
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    emoji: Mapped[Optional[str]] = mapped_column(String(16))
    bio: Mapped[Optional[str]] = mapped_column(String(500))
    which_class: Mapped[str] = mapped_column("class_name", String(80), index=True)
    profile_picture_id: Mapped[Optional[str]] = mapped_column("profile_picture_url", Text)
    gender: Mapped[str] = mapped_column(String(24))
    interests: Mapped[list[str]] = mapped_column(ARRAY(String(80)), default=list, server_default=text("'{}'"))
    isMatchmaking: Mapped[bool] = mapped_column("matchmaking_enabled", Boolean, default=True, server_default=text("true"))
    isNotifications: Mapped[bool] = mapped_column("notifications_enabled", Boolean, default=True, server_default=text("true"))
    isLovenotesRecieve: Mapped[bool] = mapped_column("love_notes_receive_enabled", Boolean, default=True, server_default=text("true"))
    isLovenotesSend: Mapped[bool] = mapped_column("love_notes_send_enabled", Boolean, default=False, server_default=text("false"))
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    reported_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    last_login_time: Mapped[Optional[datetime]] = mapped_column("last_login_at", DateTime(timezone=True))
    last_login_ip: Mapped[Optional[str]] = mapped_column(INET)
    last_login_user_agent: Mapped[Optional[str]] = mapped_column(Text)
    user_role: Mapped[str] = mapped_column("role", String(20), default="user", server_default=text("'user'"), index=True)
    last_matchmaking_time: Mapped[Optional[datetime]] = mapped_column("last_matchmaking_at", DateTime(timezone=True))


class ConfessionRecord(Base):
    __tablename__ = "confessions"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    confession: Mapped[str] = mapped_column("body", Text)
    confessing_to: Mapped[Optional[str]] = mapped_column("recipient_name", String(160))
    is_anonymous: Mapped[bool] = mapped_column("anonymous", Boolean, default=False, server_default=text("false"))
    is_comment: Mapped[bool] = mapped_column("comments_enabled", Boolean, default=False, server_default=text("false"))
    timestamp: Mapped[datetime] = mapped_column("created_at", DateTime(timezone=True), index=True)
    user_id: Mapped[str] = mapped_column("author_id", Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    is_approved: Mapped[bool] = mapped_column("approved", Boolean, default=True, server_default=text("true"), index=True)
    report_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    reactions: Mapped[dict[str, list[str]]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    heart_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    haha_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    whoa_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    heartbreak_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    comment_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class ConfessionCommentRecord(Base):
    __tablename__ = "confession_comments"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    confession_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("confessions.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    message: Mapped[str] = mapped_column("body", Text)
    timestamp: Mapped[datetime] = mapped_column("created_at", DateTime(timezone=True), index=True)
    likes: Mapped[list[str]] = mapped_column(ARRAY(Uuid(as_uuid=False)), default=list, server_default=text("'{}'"))
    dislikes: Mapped[list[str]] = mapped_column(ARRAY(Uuid(as_uuid=False)), default=list, server_default=text("'{}'"))
    like_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    dislike_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
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
    timestamp: Mapped[datetime] = mapped_column("created_at", DateTime(timezone=True), index=True)


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
    message_text: Mapped[str] = mapped_column("body", String(2000))
    is_anonymous: Mapped[bool] = mapped_column("anonymous", Boolean, default=False, server_default=text("false"))
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
    __table_args__ = (CheckConstraint("user_1_regno <> user_2_regno", name="ck_matches_distinct_users"),)


class ConversationRecord(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    matchId: Mapped[str] = mapped_column("match_id", Uuid(as_uuid=False), ForeignKey("matches.id", ondelete="CASCADE"), unique=True, index=True)
    initiatorId: Mapped[str] = mapped_column("initiator_regno", String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    receiverId: Mapped[str] = mapped_column("receiver_regno", String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default=text("'pending'"), index=True)
    requestedAt: Mapped[Optional[datetime]] = mapped_column("requested_at", DateTime(timezone=True))
    acceptedAt: Mapped[Optional[datetime]] = mapped_column("accepted_at", DateTime(timezone=True))
    createdAt: Mapped[datetime] = mapped_column("created_at", DateTime(timezone=True), index=True)
    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_message_text: Mapped[Optional[str]] = mapped_column(String(160))
    blocked_by: Mapped[list[str]] = mapped_column(ARRAY(String(100)), default=list, server_default=text("'{}'"))
    terminatedAt: Mapped[Optional[datetime]] = mapped_column("terminated_at", DateTime(timezone=True))


class MessageRecord(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    sender_id: Mapped[str] = mapped_column("sender_regno", String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    receiver_id: Mapped[str] = mapped_column("receiver_regno", String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column("body", Text)
    timestamp: Mapped[datetime] = mapped_column("created_at", DateTime(timezone=True), index=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    __table_args__ = (Index("ix_messages_conversation_created", "conversation_id", "created_at"),)


class NotificationRecord(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column("recipient_regno", String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    heading: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(String(500))
    timestamp: Mapped[datetime] = mapped_column("created_at", DateTime(timezone=True), index=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class MessageReportRecord(Base):
    __tablename__ = "message_reports"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    message_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    reporter_id: Mapped[str] = mapped_column("reporter_regno", String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    reported_user_id: Mapped[str] = mapped_column("reported_user_regno", String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    reason: Mapped[str] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default=text("'pending'"), index=True)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[Optional[str]] = mapped_column("reviewed_by_regno", String(100), ForeignKey("users.regno", ondelete="SET NULL"))
    __table_args__ = (UniqueConstraint("message_id", "reporter_regno"),)
