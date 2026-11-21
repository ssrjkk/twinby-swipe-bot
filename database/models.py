from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SwipeDecision(str, Enum):
    LIKE = "like"
    PASS = "pass"


class SwipeRecord(Base):
    __tablename__ = "swipe_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(32), default="twinby", index=True)
    target_user_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    age: Mapped[int] = mapped_column(Integer)
    compatibility: Mapped[int] = mapped_column(Integer)
    distance_km: Mapped[float] = mapped_column(Float)
    bio: Mapped[str] = mapped_column(Text, default="")
    visual_hooks_json: Mapped[str] = mapped_column(Text, default="[]")
    decision: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(512))
    match_score: Mapped[float] = mapped_column(Float, default=0.0)
    is_match: Mapped[bool] = mapped_column(Boolean, default=False)
    is_dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DialogueThread(Base):
    """Персистентная карточка диалога (работает и в UI-режиме через Telegram/Dashboard, и в API-режиме)."""

    __tablename__ = "dialogue_threads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    platform: Mapped[str] = mapped_column(String(32), default="twinby")
    girl_name: Mapped[str] = mapped_column(String(128))
    profile_context: Mapped[str] = mapped_column(Text, default="")
    visual_hooks_json: Mapped[str] = mapped_column(Text, default="[]")
    stage: Mapped[str] = mapped_column(String(32), default="icebreaker")
    bandit_arm: Mapped[str] = mapped_column(String(64), default="")
    is_handed_off: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class ChatMessageRecord(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[int] = mapped_column(Integer, ForeignKey("dialogue_threads.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
