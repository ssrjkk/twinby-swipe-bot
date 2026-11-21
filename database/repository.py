import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import settings
from core.base_client import CandidateProfile
from database.models import Base, ChatMessageRecord, DialogueThread, SwipeDecision, SwipeRecord


class DatabaseManager:
    def __init__(self, db_url: str = settings.database_url) -> None:
        if db_url.startswith("sqlite") and ":memory:" not in db_url:
            db_path = Path(db_url.split("///")[-1])
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine(db_url, echo=False)
        self.session_factory = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def init_db(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def is_already_swiped(self, target_user_id: str) -> bool:
        async with self.session_factory() as session:
            stmt = select(SwipeRecord.id).where(SwipeRecord.target_user_id == target_user_id)
            res = await session.execute(stmt)
            return res.scalar_one_or_none() is not None

    async def save_swipe(
        self,
        profile: CandidateProfile,
        decision: SwipeDecision,
        reason: str,
        match_score: float,
        is_match: bool = False,
        is_dry_run: bool = False,
        visual_hooks: list[str] | None = None,
    ) -> None:
        async with self.session_factory() as session:
            record = SwipeRecord(
                platform=profile.platform,
                target_user_id=profile.user_id,
                name=profile.name,
                age=profile.age,
                compatibility=profile.compatibility_score,
                distance_km=profile.distance_km,
                bio=profile.bio,
                visual_hooks_json=json.dumps(visual_hooks or [], ensure_ascii=False),
                decision=decision.value,
                reason=reason[:510],
                match_score=match_score,
                is_match=is_match,
                is_dry_run=is_dry_run,
            )
            session.add(record)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()

    async def get_today_likes_count(self) -> int:
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
        async with self.session_factory() as session:
            stmt = select(func.count(SwipeRecord.id)).where(
                SwipeRecord.decision == SwipeDecision.LIKE.value,
                SwipeRecord.is_dry_run.is_(False),
                SwipeRecord.created_at >= today_start,
            )
            res = await session.execute(stmt)
            return res.scalar_one() or 0

    async def get_stats(self) -> dict[str, int | float]:
        async with self.session_factory() as session:
            total = (await session.execute(select(func.count(SwipeRecord.id)))).scalar_one() or 0
            likes = (
                await session.execute(
                    select(func.count(SwipeRecord.id)).where(SwipeRecord.decision == SwipeDecision.LIKE.value)
                )
            ).scalar_one() or 0
            matches = (
                await session.execute(select(func.count(SwipeRecord.id)).where(SwipeRecord.is_match.is_(True)))
            ).scalar_one() or 0
            today_likes = await self.get_today_likes_count()
            conversion = round((matches / likes * 100), 1) if likes > 0 else 0.0
            return {
                "total_processed": total,
                "total_likes": likes,
                "total_passes": total - likes,
                "total_matches": matches,
                "today_likes": today_likes,
                "match_rate": conversion,
            }

    async def get_recent_swipes(self, limit: int = 30) -> list[dict]:
        async with self.session_factory() as session:
            stmt = select(SwipeRecord).order_by(SwipeRecord.created_at.desc()).limit(limit)
            res = await session.execute(stmt)
            records = res.scalars().all()
            return [
                {
                    "platform": r.platform,
                    "target_user_id": r.target_user_id,
                    "name": r.name,
                    "age": r.age,
                    "compatibility": r.compatibility,
                    "bio": r.bio,
                    "visual_hooks": json.loads(r.visual_hooks_json or "[]"),
                    "decision": r.decision,
                    "reason": r.reason,
                    "match_score": r.match_score,
                    "is_match": r.is_match,
                    "is_dry_run": r.is_dry_run,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in records
            ]

    async def get_recent_matches(self, limit: int = 25) -> list[dict]:
        async with self.session_factory() as session:
            stmt = (
                select(SwipeRecord)
                .where(SwipeRecord.is_match.is_(True))
                .order_by(SwipeRecord.created_at.desc())
                .limit(limit)
            )
            res = await session.execute(stmt)
            records = res.scalars().all()
            return [
                {
                    "platform": r.platform,
                    "target_user_id": r.target_user_id,
                    "name": r.name,
                    "age": r.age,
                    "compatibility": r.compatibility,
                    "bio": r.bio,
                    "reason": r.reason,
                    "match_score": r.match_score,
                }
                for r in records
            ]

    async def get_or_create_thread(
        self,
        thread_key: str,
        girl_name: str,
        platform: str = "twinby",
        profile_context: str = "",
        visual_hooks: list[str] | None = None,
    ) -> DialogueThread:
        async with self.session_factory() as session:
            stmt = select(DialogueThread).where(DialogueThread.thread_key == thread_key)
            thread = (await session.execute(stmt)).scalar_one_or_none()
            if thread is None:
                thread = DialogueThread(
                    thread_key=thread_key,
                    platform=platform,
                    girl_name=girl_name,
                    profile_context=profile_context,
                    visual_hooks_json=json.dumps(visual_hooks or [], ensure_ascii=False),
                )
                session.add(thread)
                await session.commit()
                await session.refresh(thread)
            return thread

    async def append_message(self, thread_id: int, role: str, content: str) -> None:
        async with self.session_factory() as session:
            session.add(ChatMessageRecord(thread_id=thread_id, role=role, content=content))
            await session.commit()

    async def update_thread_meta(
        self, thread_id: int, stage: str, bandit_arm: str | None = None, is_handed_off: bool = False
    ) -> None:
        async with self.session_factory() as session:
            thread = await session.get(DialogueThread, thread_id)
            if not thread:
                return
            thread.stage = stage
            if bandit_arm and not thread.bandit_arm:
                thread.bandit_arm = bandit_arm
            if is_handed_off:
                thread.is_handed_off = True
            await session.commit()

    async def get_thread_history(self, thread_id: int, limit: int = 16) -> list[dict[str, str]]:
        async with self.session_factory() as session:
            stmt = (
                select(ChatMessageRecord)
                .where(ChatMessageRecord.thread_id == thread_id)
                .order_by(ChatMessageRecord.id.desc())
                .limit(limit)
            )
            rows = list(reversed((await session.execute(stmt)).scalars().all()))
            return [{"role": r.role, "content": r.content} for r in rows]

    async def list_threads(self, limit: int = 25) -> list[dict]:
        async with self.session_factory() as session:
            stmt = select(DialogueThread).order_by(DialogueThread.updated_at.desc()).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
            return [
                {
                    "id": t.id,
                    "thread_key": t.thread_key,
                    "platform": t.platform,
                    "girl_name": t.girl_name,
                    "stage": t.stage,
                    "bandit_arm": t.bandit_arm,
                    "is_handed_off": t.is_handed_off,
                }
                for t in rows
            ]

    async def delete_thread(self, thread_id: int) -> bool:
        async with self.session_factory() as session:
            await session.execute(delete(ChatMessageRecord).where(ChatMessageRecord.thread_id == thread_id))
            res = await session.execute(delete(DialogueThread).where(DialogueThread.id == thread_id))
            await session.commit()
            return (res.rowcount or 0) > 0

    async def close(self) -> None:
        await self.engine.dispose()
