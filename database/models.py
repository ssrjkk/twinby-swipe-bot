from datetime import datetime
from enum import StrEnum
from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SwipeDecision(StrEnum):
    LIKE = "like"
    PASS = "pass"


class SwipeRecord(Base):
    __tablename__ = "swipe_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_user_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    age: Mapped[int] = mapped_column(Integer)
    compatibility: Mapped[int] = mapped_column(Integer)
    distance_km: Mapped[float] = mapped_column(Float)
    bio: Mapped[str] = mapped_column(Text, default="")
    decision: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(255))
    match_score: Mapped[float] = mapped_column(Float, default=0.0)
    is_match: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
