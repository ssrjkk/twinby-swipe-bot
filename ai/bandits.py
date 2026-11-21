import json
import logging
import random
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class PromptThompsonBandit:
    """Multi-Armed Bandit (Thompson Sampling) для A/B-оптимизации стратегий общения.
    Работает через Redis, если передан redis_client, или автоматически сохраняет
    статистику в локальный JSON-файл (без необходимости поднимать Redis).
    """

    DEFAULT_ARMS = [
        "visual_hook_playful",
        "short_witty_observation",
        "warm_curious_question",
        "bold_tease",
    ]

    def __init__(
        self,
        redis_client: Any | None = None,
        experiment_name: str = "icebreaker_variants",
        state_file: str = "./data/bandit_state.json",
    ) -> None:
        self.redis = redis_client
        self.prefix = f"bandit:{experiment_name}"
        self.state_path = Path(state_file)
        self._local_state: dict[str, dict[str, float]] = self._load_local() if redis_client is None else {}

    def _load_local(self) -> dict[str, dict[str, float]]:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_local(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self._local_state, ensure_ascii=False, indent=2), encoding="utf-8")

    async def _get_params(self, arm: str) -> tuple[float, float]:
        if self.redis is not None:
            h_key = f"{self.prefix}:{arm}"
            alpha = float(await self.redis.hget(h_key, "alpha") or 1.0)
            beta = float(await self.redis.hget(h_key, "beta") or 1.0)
            return alpha, beta

        entry = self._local_state.get(arm, {"alpha": 1.0, "beta": 1.0})
        return float(entry.get("alpha", 1.0)), float(entry.get("beta", 1.0))

    async def select_arm(self, arms: list[str] | None = None) -> str:
        candidates = arms or self.DEFAULT_ARMS
        best_arm = candidates[0]
        max_sample = -1.0
        for arm in candidates:
            alpha, beta = await self._get_params(arm)
            sample = random.betavariate(alpha, beta)
            if sample > max_sample:
                max_sample = sample
                best_arm = arm
        return best_arm

    async def record_reward(self, arm: str, success: bool) -> None:
        field = "alpha" if success else "beta"
        if self.redis is not None:
            await self.redis.hincrby(f"{self.prefix}:{arm}", field, 1)
            return

        entry = self._local_state.setdefault(arm, {"alpha": 1.0, "beta": 1.0})
        entry[field] = float(entry.get(field, 1.0)) + 1.0
        self._save_local()

    async def get_summary(self, arms: list[str] | None = None) -> dict[str, dict[str, float]]:
        result: dict[str, dict[str, float]] = {}
        for arm in (arms or self.DEFAULT_ARMS):
            a, b = await self._get_params(arm)
            result[arm] = {"alpha": a, "beta": b, "expected_rate": round(a / (a + b), 3)}
        return result
