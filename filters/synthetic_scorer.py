import logging
from pydantic import BaseModel, Field

from ai.universal_llm import UniversalAIGateway
from config import settings
from core.base_client import CandidateProfile

logger = logging.getLogger(__name__)


class SyntheticScoreResult(BaseModel):
    vibe_score: int = Field(ge=0, le=100, description="Синтетический процент совместимости 0-100")
    is_bot_or_ad: bool = Field(default=False)
    reasoning: str = Field(description="Краткое объяснение оценки")


class SyntheticCompatibilityScorer:
    """Вычисляет синтетический процент совместимости для платформ без нативного скоринга
    (Pure, Tinder, VK Знакомства, ОК, Mamba, Tabor, LovePlanet и др.) через UniversalAIGateway.
    """

    PLATFORMS_WITH_NATIVE_SCORE = {"twinby", "teamo", "rusdate", "okcupid"}

    def __init__(self, gateway: UniversalAIGateway | None = None) -> None:
        self.gateway = gateway or UniversalAIGateway()

    async def compute_compatibility(
        self,
        profile: CandidateProfile,
        user_preferences: str,
        visual_hooks: list[str] | None = None,
    ) -> SyntheticScoreResult:
        plat = profile.platform.lower()
        if plat in self.PLATFORMS_WITH_NATIVE_SCORE and profile.compatibility_score > 0:
            return SyntheticScoreResult(
                vibe_score=profile.compatibility_score,
                is_bot_or_ad=False,
                reasoning=f"Нативный скоринг платформы {profile.platform}: {profile.compatibility_score}%",
            )

        if not settings.llm_enabled:
            fallback_score = 74 if len(profile.bio.strip()) > 15 else 48
            return SyntheticScoreResult(
                vibe_score=fallback_score,
                is_bot_or_ad=False,
                reasoning="LLM_ENABLED=false — использован эвристический скоринг по био",
            )

        is_pure = plat == "pure"
        mode_instruction = (
            "Это анонимная анкета из приложения Pure. Здесь часто нет лица на фото и мало текста, "
            "главное — совпадение вайба, остроумие, открытость и отсутствие эскорта/скама."
            if is_pure
            else "Оцени анкету на искренность, совпадение ценностей и интересов с критериями пользователя."
        )

        prompt = "\n".join(
            [
                mode_instruction,
                f"Критерии пользователя: {user_preferences}",
                f"Платформа: {profile.platform}",
                f"Имя/Возраст: {profile.name}, {profile.age}",
                f"Текст анкеты: {profile.bio or '[Пустое описание]'}",
                f"Теги: {', '.join(profile.tags) if profile.tags else 'нет'}",
                f"Детали с фото (VLM): {', '.join(visual_hooks) if visual_hooks else 'не извлечены'}",
                'Верни СТРОГО JSON: {"vibe_score": 0..100, "is_bot_or_ad": bool, "reasoning": "почему"}',
            ]
        )

        try:
            data = await self.gateway.complete_json(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )
            return SyntheticScoreResult.model_validate(data)
        except Exception as exc:
            logger.warning("Fallback скоринга для %s: %s", profile.platform, exc)
            fallback_score = 72 if len(profile.bio.strip()) > 15 else 45
            return SyntheticScoreResult(
                vibe_score=fallback_score,
                is_bot_or_ad=False,
                reasoning="Эвристический fallback по длине био",
            )
