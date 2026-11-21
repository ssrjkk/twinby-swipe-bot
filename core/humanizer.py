import asyncio
import logging
import random
import time
from datetime import datetime

logger = logging.getLogger(__name__)


class Humanizer:
    """Антибан-ритм: гауссовы задержки, часовой лимит, перерывы между сессиями и окно активных часов."""

    def __init__(
        self,
        min_delay: float = 2.5,
        max_delay: float = 7.5,
        max_per_hour: int = 45,
        burst_size: int = 25,
        break_min_minutes: int = 8,
        break_max_minutes: int = 20,
        active_start_hour: int = 8,
        active_end_hour: int = 23,
        enforce_active_hours: bool = False,
    ) -> None:
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_per_hour = max_per_hour
        self.burst_size = burst_size
        self.break_min_minutes = break_min_minutes
        self.break_max_minutes = break_max_minutes
        self.active_start_hour = active_start_hour
        self.active_end_hour = active_end_hour
        self.enforce_active_hours = enforce_active_hours
        self._recent_timestamps: list[float] = []
        self._burst_counter = 0

    def is_within_active_hours(self, now: datetime | None = None) -> bool:
        if not self.enforce_active_hours:
            return True
        current_hour = (now or datetime.now()).hour
        if self.active_start_hour <= self.active_end_hour:
            return self.active_start_hour <= current_hour <= self.active_end_hour
        return current_hour >= self.active_start_hour or current_hour <= self.active_end_hour

    def record_action(self, now_ts: float | None = None) -> None:
        ts = now_ts if now_ts is not None else time.monotonic()
        self._recent_timestamps.append(ts)
        cutoff = ts - 3600.0
        self._recent_timestamps = [t for t in self._recent_timestamps if t >= cutoff]
        self._burst_counter += 1

    def hourly_cap_reached(self, now_ts: float | None = None) -> bool:
        ts = now_ts if now_ts is not None else time.monotonic()
        cutoff = ts - 3600.0
        self._recent_timestamps = [t for t in self._recent_timestamps if t >= cutoff]
        return len(self._recent_timestamps) >= self.max_per_hour

    def should_take_session_break(self) -> bool:
        return self._burst_counter >= self.burst_size

    def reset_burst(self) -> None:
        self._burst_counter = 0

    async def sleep_between_swipes(self, has_long_bio: bool = False) -> float:
        mean = (self.min_delay + self.max_delay) / 2.0
        std_dev = max((self.max_delay - self.min_delay) / 4.0, 0.2)
        delay = random.gauss(mean, std_dev)
        delay = max(self.min_delay, min(self.max_delay, delay))
        if has_long_bio:
            delay += random.uniform(1.5, 4.0)
        if random.random() < 0.10:
            delay += random.uniform(8.0, 18.0)
        await asyncio.sleep(delay)
        return round(delay, 2)

    async def sleep_session_break(self) -> float:
        minutes = random.uniform(self.break_min_minutes, self.break_max_minutes)
        logger.info("[Humanizer] Короткий перерыв после серии анкет: %.1f мин.", minutes)
        self.reset_burst()
        await asyncio.sleep(minutes * 60.0)
        return round(minutes, 1)
