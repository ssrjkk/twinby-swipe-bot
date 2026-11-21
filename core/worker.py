import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from config import settings
from core.adapters_registry import AutomationDriverType, GLOBAL_DATING_REGISTRY
from core.base_client import BaseDatingClient, CandidateProfile
from core.humanizer import Humanizer
from core.image_utils import is_blank_or_secure_screen
from core.metrics import CV_INFERENCE_SECONDS, MATCHES_TOTAL, SWIPES_TOTAL, WORKER_ACTIVE
from core.ui_driver import MobileUIDriverFallback
from database.models import SwipeDecision
from database.repository import DatabaseManager
from filters.engine import ProfileFilterEngine
from filters.face_checker import FaceConsistencyChecker
from filters.synthetic_scorer import SyntheticCompatibilityScorer
from filters.vision_card_parser import VisionProfileParser
from web.events import event_bus

logger = logging.getLogger(__name__)
MatchCallback = Callable[[CandidateProfile, float, str], Awaitable[None]]
DecisionCallback = Callable[[CandidateProfile, SwipeDecision, float, str, bool], Awaitable[None]]


class SwipeWorker:
    def __init__(
        self,
        db: DatabaseManager,
        filter_engine: ProfileFilterEngine,
        client: BaseDatingClient | None = None,
        ui_driver: MobileUIDriverFallback | None = None,
        vision_parser: VisionProfileParser | None = None,
        synthetic_scorer: SyntheticCompatibilityScorer | None = None,
        face_checker: FaceConsistencyChecker | None = None,
        humanizer: Humanizer | None = None,
        mode: str = settings.mode,
        platform: str = settings.platform,
        dry_run: bool = settings.dry_run,
        capture_second_view: bool = settings.capture_second_view,
        user_preferences: str = settings.user_preferences,
        on_match: MatchCallback | None = None,
        on_decision: DecisionCallback | None = None,
    ) -> None:
        self.client = client
        self.db = db
        self.filter_engine = filter_engine
        self.synthetic_scorer = synthetic_scorer or SyntheticCompatibilityScorer()
        self.vision_parser = vision_parser or VisionProfileParser()
        self.face_checker = face_checker or FaceConsistencyChecker(
            enabled=settings.face_check_enabled,
            similarity_threshold=settings.face_similarity_threshold,
        )
        self.mode = mode
        self.platform = platform
        self.dry_run = dry_run
        self.capture_second_view = capture_second_view
        self.user_preferences = user_preferences
        self.humanizer = humanizer or Humanizer(
            min_delay=settings.min_swipe_delay,
            max_delay=settings.max_swipe_delay,
            max_per_hour=settings.max_swipes_per_hour,
            burst_size=settings.session_burst_size,
            break_min_minutes=settings.session_break_minutes_min,
            break_max_minutes=settings.session_break_minutes_max,
            active_start_hour=settings.active_hours_start,
            active_end_hour=settings.active_hours_end,
            enforce_active_hours=settings.enforce_active_hours,
        )
        self.on_match = on_match
        self.on_decision = on_decision
        self.ui_driver = ui_driver
        self._is_running = False
        self._task: asyncio.Task[None] | None = None
        self._last_seen_ui_id: str | None = None
        self.last_screenshot_bytes: bytes | None = None
        self.last_verdict: dict | None = None

    @property
    def is_running(self) -> bool:
        return self._is_running

    def start(self) -> bool:
        if self._is_running:
            return False
        self._is_running = True
        WORKER_ACTIVE.set(1)
        self._task = asyncio.create_task(self._run_loop())
        return True

    async def stop(self) -> bool:
        if not self._is_running:
            return False
        self._is_running = False
        WORKER_ACTIVE.set(0)
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        return True

    async def _evaluate_candidate(self, profile: CandidateProfile) -> tuple[SwipeDecision, float, str]:
        spec = GLOBAL_DATING_REGISTRY.get(profile.platform.lower())
        if (spec and not spec.has_native_compatibility) or profile.compatibility_score == 0:
            synth = await self.synthetic_scorer.compute_compatibility(
                profile=profile,
                user_preferences=self.user_preferences,
            )
            if synth.is_bot_or_ad:
                return SwipeDecision.PASS, 0.0, f"AI Антиспам: {synth.reasoning}"
            profile.compatibility_score = synth.vibe_score

        eval_res = self.filter_engine.evaluate(profile)
        return eval_res.decision, eval_res.score, eval_res.reason

    async def process_single_visual_screen(self) -> dict | None:
        if not self.ui_driver:
            raise RuntimeError(f"Визуальный драйвер не подключён (mode={self.mode}).")

        views = await self.ui_driver.capture_profile_views(include_second_view=self.capture_second_view)
        self.last_screenshot_bytes = views[0]

        if is_blank_or_secure_screen(views[0]):
            logger.warning("[Vision] Получен чёрный/пустой экран (возможно FLAG_SECURE в %s).", self.platform)
            self.last_verdict = {
                "screen_type": "secure_black_screen",
                "reason": "Экран чёрный или защищён FLAG_SECURE (для таких приложений используйте web_vision или Appium на рутованном/разрешённом устройстве).",
            }
            return self.last_verdict

        t0 = time.perf_counter()
        profile, parsed = await self.vision_parser.parse_screenshot(
            screenshot_bytes=views,
            platform=self.platform,
            user_preferences=self.user_preferences,
        )
        CV_INFERENCE_SECONDS.observe(time.perf_counter() - t0)

        if parsed.screen_type in ("match_popup", "promo_popup"):
            logger.info("[Vision] Обнаружен %s — %s", parsed.screen_type, "закрываю попап" if not self.dry_run else "DRY_RUN")
            await event_bus.publish(parsed.screen_type, {"platform": self.platform, "note": parsed.reasoning})
            if not self.dry_run:
                await self.ui_driver.dismiss_popup(parsed.dismiss_button)
            self.last_verdict = {"screen_type": parsed.screen_type, "reasoning": parsed.reasoning}
            return self.last_verdict

        if parsed.screen_type == "paywall":
            logger.warning("[Vision] Обнаружен paywall / лимит лайков — пауза.")
            await event_bus.publish("paywall", {"platform": self.platform})
            self.last_verdict = {"screen_type": "paywall"}
            return self.last_verdict

        if profile is None:
            self.last_verdict = {"screen_type": parsed.screen_type}
            return self.last_verdict

        if profile.user_id == self._last_seen_ui_id or await self.db.is_already_swiped(profile.user_id):
            return {"screen_type": "duplicate", "user_id": profile.user_id}

        self._last_seen_ui_id = profile.user_id

        face_verdict = await self.face_checker.verify_images(views, platform=self.platform)
        if not face_verdict.passed:
            decision, score, reason = SwipeDecision.PASS, 0.0, f"FaceCheck: {face_verdict.reason}"
        elif parsed.is_ad_or_bot:
            decision, score, reason = SwipeDecision.PASS, 0.0, f"VLM Антиспам: {parsed.reasoning}"
        else:
            eval_res = self.filter_engine.evaluate(profile)
            decision, score, reason = eval_res.decision, eval_res.score, f"{eval_res.reason} | {parsed.reasoning}"

        is_like = decision == SwipeDecision.LIKE
        action_used = "dry_run"
        if self.dry_run:
            logger.info("[DRY-RUN %s] %s, %d -> %s (%.1f): %s", self.mode, profile.name, profile.age, decision.value, score, reason)
        else:
            action_used = await self.ui_driver.execute_profile_action(
                is_like=is_like,
                like_button=parsed.like_button,
                pass_button=parsed.pass_button,
            )
            self.humanizer.record_action()

        await self._record_and_emit(
            profile=profile,
            decision=decision,
            score=score,
            reason=reason,
            is_match=False,
            visual_hooks=parsed.visual_hooks,
        )
        self.last_verdict = {
            "screen_type": "profile",
            "name": profile.name,
            "age": profile.age,
            "decision": decision.value,
            "score": score,
            "reason": reason,
            "visual_hooks": parsed.visual_hooks,
            "action_used": action_used,
            "dry_run": self.dry_run,
        }
        return self.last_verdict

    async def _record_and_emit(
        self,
        profile: CandidateProfile,
        decision: SwipeDecision,
        score: float,
        reason: str,
        is_match: bool,
        visual_hooks: list[str] | None = None,
    ) -> None:
        SWIPES_TOTAL.labels(decision=decision.value).inc()
        if is_match:
            MATCHES_TOTAL.inc()

        await self.db.save_swipe(
            profile=profile,
            decision=decision,
            reason=reason,
            match_score=score,
            is_match=is_match,
            is_dry_run=self.dry_run,
            visual_hooks=visual_hooks,
        )
        await event_bus.publish(
            "dry_run" if self.dry_run else "swipe",
            {
                "platform": profile.platform,
                "name": profile.name,
                "age": profile.age,
                "decision": decision.value,
                "score": score,
                "reason": reason,
                "is_match": is_match,
                "visual_hooks": visual_hooks or [],
            },
        )
        if self.on_decision:
            try:
                await self.on_decision(profile, decision, score, reason, self.dry_run)
            except Exception as exc:
                logger.warning("Ошибка callback on_decision (не критично): %s", exc)
        if is_match and self.on_match:
            try:
                await self.on_match(profile, score, reason)
            except Exception as exc:
                logger.warning("Ошибка callback on_match (не критично): %s", exc)

    async def _run_loop(self) -> None:
        while self._is_running:
            try:
                if not self.humanizer.is_within_active_hours():
                    logger.info("[Humanizer] Вне активных часов — пауза 10 мин.")
                    await asyncio.sleep(600)
                    continue

                if not self.dry_run:
                    if self.humanizer.hourly_cap_reached():
                        logger.info("[Humanizer] Достигнут часовой лимит (%d/ч) — пауза 5 мин.", self.humanizer.max_per_hour)
                        await asyncio.sleep(300)
                        continue
                    if self.humanizer.should_take_session_break():
                        await self.humanizer.sleep_session_break()
                        continue
                    today_likes = await self.db.get_today_likes_count()
                    if today_likes >= settings.daily_like_limit:
                        await asyncio.sleep(1800)
                        continue

                if self.mode in ("ui_vision", "web_vision"):
                    res = await self.process_single_visual_screen()
                    if not res or res.get("screen_type") in ("other", "duplicate", "match_popup", "promo_popup", "secure_black_screen"):
                        await asyncio.sleep(5)
                    elif res.get("screen_type") == "paywall":
                        await asyncio.sleep(600)
                    else:
                        await self.humanizer.sleep_between_swipes()
                    continue

                if not self.client:
                    raise RuntimeError("API-клиент не передан в SwipeWorker (mode=api).")

                candidates = await self.client.get_recommendations(limit=15)
                if not candidates:
                    await asyncio.sleep(300)
                    continue

                for profile in candidates:
                    if not self._is_running:
                        break
                    if await self.db.is_already_swiped(profile.user_id):
                        continue

                    decision, score, reason = await self._evaluate_candidate(profile)
                    is_like = decision == SwipeDecision.LIKE
                    if not self.dry_run and is_like:
                        if await self.db.get_today_likes_count() >= settings.daily_like_limit:
                            logger.info("[Worker] Достигнут дневной лимит лайков (%d) внутри батча.", settings.daily_like_limit)
                            break
                    await self.humanizer.sleep_between_swipes(has_long_bio=len(profile.bio) > 80)

                    if self.dry_run:
                        is_match = False
                    else:
                        spec = GLOBAL_DATING_REGISTRY.get(profile.platform.lower())
                        if spec and spec.driver == AutomationDriverType.UI_DRIVER_ONLY and self.ui_driver:
                            await self.ui_driver.swipe_with_bezier_curve(is_like=is_like)
                            is_match = False
                        else:
                            api_res = await self.client.swipe(profile.user_id, is_like=is_like)
                            is_match = bool(api_res.get("is_match", False))
                        self.humanizer.record_action()

                    await self._record_and_emit(profile, decision, score, reason, is_match=is_match)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Ошибка в цикле SwipeWorker: %s", exc)
                await asyncio.sleep(30)
