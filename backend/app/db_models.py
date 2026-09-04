"""Relational SQLAlchemy schema for application data."""
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UserRecord(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    Regno: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    Name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    emoji: Mapped[Optional[str]] = mapped_column(String(32))
    bio: Mapped[Optional[str]] = mapped_column(Text)
    which_class: Mapped[str] = mapped_column(String(120), index=True)
    profile_picture_id: Mapped[Optional[str]] = mapped_column(Text)
    gender: Mapped[str] = mapped_column(String(40))
    interests: Mapped[list] = mapped_column(JSONB, default=list)
    isMatchmaking: Mapped[bool] = mapped_column(Boolean, default=True)
    isNotifications: Mapped[bool] = mapped_column(Boolean, default=True)
    isLovenotesRecieve: Mapped[bool] = mapped_column(Boolean, default=True)
    isLovenotesSend: Mapped[bool] = mapped_column(Boolean, default=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    reported_count: Mapped[int] = mapped_column(Integer, default=0)
    last_login_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_login_ip: Mapped[Optional[str]] = mapped_column(String(64))
    last_login_user_agent: Mapped[Optional[str]] = mapped_column(Text)
    user_role: Mapped[str] = mapped_column(String(24), default="user", index=True)
    last_matchmaking_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class ConfessionRecord(Base):
    __tablename__ = "confessions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    confession: Mapped[str] = mapped_column(Text)
    confessing_to: Mapped[Optional[str]] = mapped_column(Text)
    is_anonymous: Mapped[bool] = mapped_column(Boolean, default=False)
    is_comment: Mapped[bool] = mapped_column(Boolean, default=False)
    timestamp: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    user_info: Mapped[Optional[dict]] = mapped_column(JSONB)
    is_approved: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    report_count: Mapped[int] = mapped_column(Integer, default=0)
    reported_by: Mapped[list] = mapped_column(JSONB, default=list)
    reactions: Mapped[dict] = mapped_column(JSONB, default=dict)
    comments: Mapped[list] = mapped_column(JSONB, default=list)
    heart_count: Mapped[int] = mapped_column(Integer, default=0)
    haha_count: Mapped[int] = mapped_column(Integer, default=0)
    whoa_count: Mapped[int] = mapped_column(Integer, default=0)
    heartbreak_count: Mapped[int] = mapped_column(Integer, default=0)
    comment_count: Mapped[int] = mapped_column(Integer, default=0)


class ConfessionCommentRecord(Base):
    __tablename__ = "confession_comments"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    confession_id: Mapped[str] = mapped_column(ForeignKey("confessions.id", ondelete="CASCADE"), index=True)
    user_info: Mapped[dict] = mapped_column(JSONB)
    message: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    likes: Mapped[list] = mapped_column(JSONB, default=list)
    dislikes: Mapped[list] = mapped_column(JSONB, default=list)
    like_count: Mapped[int] = mapped_column(Integer, default=0)
    dislike_count: Mapped[int] = mapped_column(Integer, default=0)
    report_count: Mapped[int] = mapped_column(Integer, default=0)
    reported_by: Mapped[list] = mapped_column(JSONB, default=list)


class ReportRecord(Base):
    __tablename__ = "reports"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    content_id: Mapped[str] = mapped_column(String(32), index=True)
    content_type: Mapped[str] = mapped_column(String(24))
    reported_by_id: Mapped[str] = mapped_column(String(32), index=True)
    reported_by_name: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginTokenRecord(Base):
    __tablename__ = "login_tokens"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    request_ip: Mapped[str] = mapped_column(String(64))
    request_user_agent: Mapped[str] = mapped_column(Text)
    consume_ip: Mapped[Optional[str]] = mapped_column(String(64))
    consume_user_agent: Mapped[Optional[str]] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[Optional[dict]] = mapped_column("metadata", JSONB)


class LoveNoteRecord(Base):
    __tablename__ = "love_notes"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    sender_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    recipient_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    image_base64: Mapped[str] = mapped_column(Text)
    message_text: Mapped[str] = mapped_column(Text)
    is_anonymous: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="pending_review", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class LoveNoteTemplateRecord(Base):
    __tablename__ = "love_note_templates"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    preview_image: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MatchRecord(Base):
    __tablename__ = "matches"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_1_regno: Mapped[str] = mapped_column(ForeignKey("users.Regno"), index=True)
    user_2_regno: Mapped[str] = mapped_column(ForeignKey("users.Regno"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ConversationRecord(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    matchId: Mapped[str] = mapped_column(ForeignKey("matches.id"), unique=True, index=True)
    initiatorId: Mapped[str] = mapped_column(ForeignKey("users.Regno"), index=True)
    receiverId: Mapped[str] = mapped_column(ForeignKey("users.Regno"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    requestedAt: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    acceptedAt: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_message_text: Mapped[Optional[str]] = mapped_column(Text)
    participants: Mapped[list] = mapped_column(JSONB, default=list)
    blocked_by: Mapped[list] = mapped_column(JSONB, default=list)
    terminatedAt: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class MessageRecord(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    matchId: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    sender_id: Mapped[str] = mapped_column(ForeignKey("users.Regno"), index=True)
    receiver_id: Mapped[str] = mapped_column(ForeignKey("users.Regno"), index=True)
    senderId: Mapped[Optional[str]] = mapped_column(String(100))
    receiverId: Mapped[Optional[str]] = mapped_column(String(100))
    text: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False)


class NotificationRecord(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.Regno"), index=True)
    content: Mapped[dict] = mapped_column(JSONB)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False)


class MessageReportRecord(Base):
    __tablename__ = "message_reports"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    reporter_id: Mapped[str] = mapped_column(ForeignKey("users.Regno"), index=True)
    reported_user_id: Mapped[str] = mapped_column(ForeignKey("users.Regno"), index=True)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(100))
    __table_args__ = (UniqueConstraint("message_id", "reporter_id"),)
