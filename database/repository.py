import asyncio
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import url2pathname

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from config import settings
from core.base_client import CandidateProfile
from database.models import Base, SwipeDecision, SwipeRecord


class DatabaseManager:
    def __init__(self, db_url: str = settings.database_url) -> None:
        self.engine = create_async_engine(db_url, echo=False)
        # Shared by workers for the current account in this process only.
        self.swipe_lock = asyncio.Lock()
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    @property
    def sqlite_path(self) -> Path | None:
        url = self.engine.url
        if url.get_backend_name() != "sqlite" or not url.database:
            return None
        database = url.database
        if str(url.query.get("uri", "false")).lower() in {"true", "1"}:
            if url.query.get("mode") == "memory" or database == "file::memory:":
                return None
            if database.startswith("file:"):
                database = url2pathname(database[5:])
        if not database or database == ":memory:":
            return None
        return Path(database).resolve()

    async def init_db(self) -> None:
        if self.sqlite_path is not None:
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def is_already_swiped(self, target_user_id: str) -> bool:
        async with self.session_factory() as session:
            stmt = select(SwipeRecord.id).where(
                SwipeRecord.target_user_id == target_user_id
            )
            res = await session.execute(stmt)
            return res.scalar_one_or_none() is not None

    async def save_swipe(
        self,
        profile: CandidateProfile,
        decision: SwipeDecision,
        reason: str,
        match_score: float,
        is_match: bool = False,
    ) -> int:
        async with self.session_factory() as session:
            record = SwipeRecord(
                target_user_id=profile.user_id,
                name=profile.name,
                age=profile.age,
                compatibility=profile.compatibility_score,
                distance_km=profile.distance_km,
                bio=profile.bio,
                decision=decision.value,
                reason=reason,
                match_score=match_score,
                is_match=is_match,
            )
            session.add(record)
            await session.commit()
            return record.id

    async def update_swipe_result(
        self, record_id: int, reason: str, match_score: float, is_match: bool
    ) -> None:
        """Acknowledge an existing intent without inserting another attempt."""
        async with self.session_factory() as session:
            record = await session.get(SwipeRecord, record_id)
            if record is None:
                raise LookupError(f"Swipe intent {record_id} does not exist")
            record.reason = reason
            record.match_score = match_score
            record.is_match = is_match
            await session.commit()

    async def get_today_likes_count(self) -> int:
        """Count LIKE attempts, including pending or uncertain outcomes."""
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        async with self.session_factory() as session:
            stmt = select(func.count(SwipeRecord.id)).where(
                SwipeRecord.decision == SwipeDecision.LIKE.value,
                SwipeRecord.created_at >= today_start,
            )
            res = await session.execute(stmt)
            return res.scalar_one() or 0

    async def get_stats(self) -> dict[str, int | float]:
        async with self.session_factory() as session:
            total = (
                await session.execute(select(func.count(SwipeRecord.id)))
            ).scalar_one() or 0
            likes = (
                await session.execute(
                    select(func.count(SwipeRecord.id)).where(
                        SwipeRecord.decision == SwipeDecision.LIKE.value
                    )
                )
            ).scalar_one() or 0
            matches = (
                await session.execute(
                    select(func.count(SwipeRecord.id)).where(
                        SwipeRecord.is_match.is_(True)
                    )
                )
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
