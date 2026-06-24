"""
Базовый класс агента.
Чтобы добавить нового агента — унаследуй BaseAgent и реализуй analyze().
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Decision:
    decision: str     # "like" | "skip"
    confidence: int   # 0–100
    reason: str

    def __post_init__(self):
        if self.decision not in ("like", "skip"):
            raise ValueError(f"decision must be 'like' or 'skip', got {self.decision!r}")
        if not (0 <= self.confidence <= 100):
            raise ValueError(f"confidence must be 0–100, got {self.confidence}")


class BaseAgent(ABC):
    """Интерфейс AI-агента для анализа анкет."""

    name: str = "base"

    SYSTEM_PROMPT = """
Ты — AI-агент для отбора анкет в приложении знакомств Twinby.

Критерии:
1. Внешность: естественная, ухоженная, аккуратный стиль, эстетичные фото, уверенность.
2. Качество профиля: живые эмоции, описание, интересы, реальный человек.
   Минусы: плохое качество, агрессивные фильтры, пустой профиль, фейк, нет лица.
3. Вайб: спокойный, интеллектуальный, творческий, стильный, доброжелательный.
   Избегать: токсичный, агрессивный, демонстративная роскошь, провокационный.

Правила: лайк только при совпадении > 75%, при сомнении — пропуск.

Ответь СТРОГО в JSON (без markdown):
{"decision": "like" | "skip", "confidence": 0-100, "reason": "кратко на русском"}
"""

    @abstractmethod
    def analyze(self, screenshots: list[bytes], bio: str) -> Decision:
        """
        Анализирует анкету и возвращает Decision.
        screenshots — список PNG-байтов (до 3 фото)
        bio         — текст описания профиля
        """
        ...

    def _parse_json(self, raw: str) -> Decision:
        """Хелпер: парсит JSON-ответ агента в Decision."""
        import json
        clean = raw.strip().replace("```json", "").replace("```", "").strip()
        data = json.loads(clean)
        return Decision(
            decision=data.get("decision", "skip"),
            confidence=int(data.get("confidence", 0)),
            reason=data.get("reason", "—"),
        )
