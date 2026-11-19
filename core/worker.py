import asyncio
import logging
from collections.abc import Awaitable, Callable

from config import settings
from core.adapters_registry import AutomationDriverType, GLOBAL_DATING_REGISTRY
from core.base_client import BaseDatingClient, CandidateProfile
from core.humanizer import Humanizer
from core.ui_driver import MobileUIDriverFallback
from database.models import SwipeDecision
from database.repository import DatabaseManager
from filters.engine import ProfileFilterEngine
from filters.synthetic_scorer import SyntheticCompatibilityScorer

logger = logging.getLogger(__name__)
MatchCallback = Callable[[CandidateProfile, float, str], Awaitable[None]]


class SwipeWorker:
    def __init__(
        self,
        client: BaseDatingClient,
        db: DatabaseManager,
        filter_engine: ProfileFilterEngine,
        user_preferences: str = "интеллект, юмор, путешествия, кофе, искренность",
        on_match: MatchCallback | None = None,
        ui_driver: MobileUIDriverFallback | None = None,
    ) -> None:
        self.client = client
        self.db = db
        self.filter_engine = filter_engine
        self.synthetic_scorer = SyntheticCompatibilityScorer()
        self.user_preferences = user_preferences
        self.humanizer = Humanizer(settings.min_swipe_delay, settings.max_swipe_delay)
        self.on_match = on_match
        self.ui_driver = ui_driver
        self._is_running = False
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._is_running

    def start(self) -> bool:
        if self._is_running:
            return False
        self._is_running = True
        self._task = asyncio.create_task(self._run_loop())
        return True

    async def stop(self) -> bool:
        if not self._is_running:
            return False
        self._is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        return True

    async def _evaluate_candidate(
        self, profile: CandidateProfile
    ) -> tuple[SwipeDecision, float, str]:
        spec = GLOBAL_DATING_REGISTRY.get(profile.platform.lower())

        if (
            spec and not spec.has_native_compatibility
        ) or profile.compatibility_score == 0:
            synth = await self.synthetic_scorer.compute_compatibility(
                profile=profile,
                user_preferences=self.user_preferences,
            )
            if synth.scoring_failed:
                return SwipeDecision.PASS, 0.0, "AI scoring unavailable"
            if synth.is_bot_or_ad:
                return SwipeDecision.PASS, 0.0, f"AI Антиспам: {synth.reasoning}"
            profile.compatibility_score = synth.vibe_score

        eval_res = self.filter_engine.evaluate(profile)
        return eval_res.decision, eval_res.score, eval_res.reason

    async def _execute_swipe(self, profile: CandidateProfile, is_like: bool) -> bool:
        spec = GLOBAL_DATING_REGISTRY.get(profile.platform.lower())
        if spec and spec.driver == AutomationDriverType.UI_DRIVER_ONLY:
            if self.ui_driver is None:
                raise RuntimeError("UI-only platform requires a connected UI driver")
            if (
                isinstance(self.ui_driver, MobileUIDriverFallback)
                and not self.ui_driver._session_id
            ):
                raise RuntimeError("UI driver is not connected")
            await self.ui_driver.swipe_with_bezier_curve(is_like=is_like)
            return False

        api_res = await self.client.swipe(profile.user_id, is_like=is_like)
        return bool(api_res.get("is_match", False))

    async def _run_loop(self) -> None:
        while self._is_running:
            try:
                today_likes = await self.db.get_today_likes_count()
                if today_likes >= settings.daily_like_limit:
                    await asyncio.sleep(1800)
                    continue

                candidates = await self.client.get_recommendations(limit=15)
                if not candidates:
                    await asyncio.sleep(300)
                    continue

                for profile in candidates:
                    if not self._is_running:
                        break
                    if not await self._process_candidate(profile):
                        break

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Ошибка в цикле SwipeWorker: %s", exc)
                await asyncio.sleep(30)

    async def _process_candidate(self, profile: CandidateProfile) -> bool:
        """Return False to stop the batch when the LIKE quota is exhausted."""
        async with self.db.swipe_lock:
            if await self.db.is_already_swiped(profile.user_id):
                return True

            decision, score, reason = await self._evaluate_candidate(profile)
            is_like = decision == SwipeDecision.LIKE

            await self.humanizer.sleep_between_swipes(
                has_long_bio=len(profile.bio) > 80
            )
            if (
                is_like
                and await self.db.get_today_likes_count() >= settings.daily_like_limit
            ):
                return False

            # Commit before any external action. Failure/cancellation leaves this
            # intent consuming quota and preventing replay, even after a restart.
            record_id = await self.db.save_swipe(
                profile=profile,
                decision=decision,
                reason=f"Pending/uncertain external swipe; do not replay. {reason}"[
                    :255
                ],
                match_score=score,
            )
            is_match = await self._execute_swipe(profile, is_like=is_like)
            await self.db.update_swipe_result(
                record_id=record_id,
                reason=reason,
                match_score=score,
                is_match=is_match,
            )
            if is_match and self.on_match:
                await self.on_match(profile, score, reason)
            return True
