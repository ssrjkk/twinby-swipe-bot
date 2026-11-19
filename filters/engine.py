from dataclasses import dataclass
from config import settings
from core.base_client import CandidateProfile
from database.models import SwipeDecision


@dataclass
class FilterConfig:
    min_compatibility: int = settings.min_compatibility
    max_distance_km: int = settings.max_distance_km
    min_age: int = settings.min_age
    max_age: int = settings.max_age
    require_bio: bool = settings.require_bio


@dataclass
class EvaluationResult:
    decision: SwipeDecision
    score: float
    reason: str


class ProfileFilterEngine:
    def __init__(self, config: FilterConfig | None = None) -> None:
        self.config = config or FilterConfig()

    def evaluate(self, profile: CandidateProfile) -> EvaluationResult:
        if self.config.require_bio and not profile.bio.strip():
            return EvaluationResult(SwipeDecision.PASS, 0.0, "Описание обязательно")
        if profile.photos_count == 0:
            return EvaluationResult(SwipeDecision.PASS, 0.0, "Нет фотографий")
        if not (self.config.min_age <= profile.age <= self.config.max_age):
            return EvaluationResult(
                SwipeDecision.PASS, 0.0, f"Возраст {profile.age} вне диапазона"
            )
        if profile.distance_km > self.config.max_distance_km:
            return EvaluationResult(
                SwipeDecision.PASS, 0.0, f"Далеко: {profile.distance_km:.1f} км"
            )
        if profile.compatibility_score < self.config.min_compatibility:
            return EvaluationResult(
                SwipeDecision.PASS,
                0.0,
                f"Низкая совместимость: {profile.compatibility_score}%",
            )

        score = 50.0 + (profile.compatibility_score / 100) * 30.0
        return EvaluationResult(
            SwipeDecision.LIKE,
            score,
            f"Score {score} (совм. {profile.compatibility_score}%)",
        )
