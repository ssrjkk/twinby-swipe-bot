import logging
import numpy as np
import redis.asyncio as redis

logger = logging.getLogger(__name__)


class PromptThompsonBandit:
    def __init__(
        self, redis_client: redis.Redis, experiment_name: str = "icebreaker_variants"
    ) -> None:
        self.redis = redis_client
        self.prefix = f"bandit:{experiment_name}"

    async def select_arm(self, arms: list[str]) -> str:
        best_arm = arms[0]
        max_sample = -1.0
        for arm in arms:
            h_key = f"{self.prefix}:{arm}"
            alpha = float(await self.redis.hget(h_key, "alpha") or 1.0)
            beta = float(await self.redis.hget(h_key, "beta") or 1.0)
            sample = np.random.beta(alpha, beta)
            if sample > max_sample:
                max_sample = sample
                best_arm = arm
        return best_arm

    async def record_reward(self, arm: str, success: bool) -> None:
        key = f"{self.prefix}:{arm}"
        # hincrby по отсутствующему ключу стартует с 0 и съедает априорную Beta(1,1):
        # первое наблюдение не меняло постериор. Фиксируем prior перед инкрементом.
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hsetnx(key, "alpha", 1)
            pipe.hsetnx(key, "beta", 1)
            pipe.hincrby(key, "alpha" if success else "beta", 1)
            await pipe.execute()
