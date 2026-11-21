from dataclasses import dataclass, field

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
    min_score_threshold: float = settings.min_score_threshold
    stop_words: list[str] = field(
        default_factory=lambda: [
            "inst:",
            "инст:",
            "tg:",
            "тг:",
            "подпишись",
            "onlyfans",
            "эскорт",
            "крипта",
            "инвестиции",
        ]
    )
    priority_keywords: list[str] = field(
        default_factory=lambda: [
            "кофе",
            "it",
            "разработка",
            "путешествия",
            "психология",
            "кино",
            "музыка",
            "книги",
        ]
    )


@dataclass
class EvaluationResult:
    decision: SwipeDecision
    score: float
    reason: str
    matched_keywords: list[str] = field(default_factory=list)


class ProfileFilterEngine:
    def __init__(self, config: FilterConfig | None = None) -> None:
        self.config = config or FilterConfig()

    def evaluate(self, profile: CandidateProfile) -> EvaluationResult:
        # 1. Жёсткие правила (Hard Rules)
        if profile.photos_count == 0:
            return EvaluationResult(SwipeDecision.PASS, 0.0, "Нет фотографий")

        if not (self.config.min_age <= profile.age <= self.config.max_age):
            return EvaluationResult(SwipeDecision.PASS, 0.0, f"Возраст {profile.age} вне диапазона")

        if profile.distance_km > 0 and profile.distance_km > self.config.max_distance_km:
            return EvaluationResult(SwipeDecision.PASS, 0.0, f"Далеко: {profile.distance_km:.1f} км")

        bio_clean = profile.bio.strip()
        if self.config.require_bio and len(bio_clean) < 5:
            return EvaluationResult(SwipeDecision.PASS, 0.0, "Пустое био (require_bio=True)")

        searchable_text = f"{profile.name} {bio_clean} {' '.join(profile.tags)}".lower()
        for stop_word in self.config.stop_words:
            if stop_word.lower() in searchable_text:
                return EvaluationResult(SwipeDecision.PASS, 0.0, f"Стоп-слово: '{stop_word}'")

        if profile.compatibility_score < self.config.min_compatibility:
            return EvaluationResult(
                SwipeDecision.PASS,
                0.0,
                f"Низкая совместимость/вайб: {profile.compatibility_score}% < {self.config.min_compatibility}%",
            )

        # 2. Взвешенный скоринг (Weighted Scoring)
        score = 35.0 + (profile.compatibility_score / 100.0) * 45.0

        matched = [kw for kw in self.config.priority_keywords if kw.lower() in searchable_text]
        score += min(len(matched) * 6.0, 18.0)

        if profile.is_verified:
            score += 5.0
        if len(bio_clean) >= 40:
            score += 4.0

        score = round(min(score, 100.0), 1)

        if score < self.config.min_score_threshold:
            return EvaluationResult(
                SwipeDecision.PASS,
                score,
                f"Итоговый скор {score} ниже порога {self.config.min_score_threshold}",
                matched_keywords=matched,
            )

        kw_note = f", ключи: {', '.join(matched)}" if matched else ""
        return EvaluationResult(
            SwipeDecision.LIKE,
            score,
            f"Score {score} (совм. {profile.compatibility_score}%{kw_note})",
            matched_keywords=matched,
        )
