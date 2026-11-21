import hashlib
import logging
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ai.universal_llm import UniversalAIGateway
from core.base_client import CandidateProfile
from core.image_utils import prepare_image_for_vlm

logger = logging.getLogger(__name__)


def _normalize_coord_pair(val):
    if val is None:
        return None
    if isinstance(val, (list, tuple)) and len(val) == 2:
        try:
            x, y = int(round(float(val[0]))), int(round(float(val[1])))
            if 0 <= x <= 1000 and 0 <= y <= 1000:
                return (x, y)
        except (TypeError, ValueError):
            return None
    return None


class ParsedCardSchema(BaseModel):
    screen_type: Literal["profile", "match_popup", "promo_popup", "paywall", "chat", "other"] = Field(default="profile")
    is_profile_screen: bool = Field(default=True)
    name: str = Field(default="Без имени")
    age: int = Field(default=22, ge=18, le=99)
    bio: str = Field(default="")
    distance_km: float = Field(default=0.0, ge=0.0)
    native_compatibility: int | None = Field(default=None, ge=0, le=100)
    vibe_score: int = Field(default=50, ge=0, le=100)
    is_verified: bool = Field(default=False)
    is_ad_or_bot: bool = Field(default=False)
    visual_hooks: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    reasoning: str = Field(default="")
    # Нормализованные координаты (0..1000) кнопок на экране для точных кликов в любом приложении/сайте
    like_button: tuple[int, int] | None = Field(default=None)
    pass_button: tuple[int, int] | None = Field(default=None)
    dismiss_button: tuple[int, int] | None = Field(default=None)

    @field_validator("like_button", "pass_button", "dismiss_button", mode="before")
    @classmethod
    def _validate_coords(cls, v):
        return _normalize_coord_pair(v)


class ParsedChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ParsedChatScreenSchema(BaseModel):
    is_chat_screen: bool = Field(default=False)
    girl_name: str = Field(default="Собеседница")
    messages: list[ParsedChatMessage] = Field(default_factory=list)
    input_box: tuple[int, int] | None = Field(default=None)
    send_button: tuple[int, int] | None = Field(default=None)
    notes: str = Field(default="")

    @field_validator("input_box", "send_button", mode="before")
    @classmethod
    def _validate_chat_coords(cls, v):
        return _normalize_coord_pair(v)


class VisionProfileParser:
    """Универсальный VLM-парсер экранов:
    1. Разбирает анкеты кандидатов + находит координаты кнопок Like/Pass/Dismiss (0..1000).
    2. Разбирает открытые экраны переписки (история сообщений + поле ввода + кнопка отправки).
    Работает с любыми дейтинг-приложениями и сайтами без жёсткой привязки к CSS/XPath.
    """

    def __init__(self, gateway: UniversalAIGateway | None = None, compress_images: bool = True) -> None:
        self.gateway = gateway or UniversalAIGateway()
        self.compress_images = compress_images

    def _prepare(self, screenshot_bytes: bytes | list[bytes]) -> list[bytes]:
        raw_list = screenshot_bytes if isinstance(screenshot_bytes, list) else [screenshot_bytes]
        if not self.compress_images:
            return raw_list
        return [prepare_image_for_vlm(b) for b in raw_list]

    async def parse_screenshot(
        self,
        screenshot_bytes: bytes | list[bytes],
        platform: str,
        user_preferences: str,
    ) -> tuple[CandidateProfile | None, ParsedCardSchema]:
        images = self._prepare(screenshot_bytes)
        prompt = (
            f"Ты анализируешь скриншот(ы) дейтинг-сервиса ({platform}).\n"
            f"Критерии пользователя: {user_preferences}\n\n"
            "Определи тип экрана (screen_type):\n"
            "- 'profile' — видна карточка кандидата (is_profile_screen=true)\n"
            "- 'match_popup' — всплывающее окно взаимного мэтча (is_profile_screen=false)\n"
            "- 'promo_popup' — реклама/куки/уведомление с кнопкой закрытия (is_profile_screen=false)\n"
            "- 'paywall' — окно лимита лайков/подписки (is_profile_screen=false)\n"
            "- 'chat' — экран переписки (is_profile_screen=false)\n"
            "- 'other' — другой экран (is_profile_screen=false)\n\n"
            "Также определи нормализованные координаты [x, y] (целые числа от 0 до 1000 от левого верхнего угла) "
            "для видимых кнопок на первом скриншоте (или null, если кнопки нет):\n"
            "- like_button: кнопка лайка/сердечка\n"
            "- pass_button: кнопка пропуска/крестика\n"
            "- dismiss_button: кнопка закрытия попапа/баннера (X / Продолжить / Позже)\n\n"
            "Если видна анкета, извлеки имя, возраст, био, расстояние в км, нативный % совместимости "
            "(если он есть на экране, как в Twinby — иначе null), теги, 2-3 визуальные детали с фото (visual_hooks), "
            "проверь на спам/бота (is_ad_or_bot) и поставь vibe_score (0..100) относительно критериев.\n"
            "Верни СТРОГО JSON с полями: "
            "screen_type, is_profile_screen, name, age, bio, distance_km, native_compatibility, vibe_score, "
            "is_verified, is_ad_or_bot, visual_hooks, tags, reasoning, like_button, pass_button, dismiss_button."
        )

        data = await self.gateway.complete_json(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            images=images,
        )
        parsed = ParsedCardSchema.model_validate(data)
        if not parsed.is_profile_screen or parsed.screen_type != "profile":
            return None, parsed

        card_hash = hashlib.sha256(
            f"{platform}:{parsed.name}:{parsed.age}:{parsed.bio[:80]}".encode("utf-8")
        ).hexdigest()[:16]
        score = parsed.native_compatibility if parsed.native_compatibility is not None else parsed.vibe_score

        profile = CandidateProfile(
            platform=platform,
            user_id=f"ui_{card_hash}",
            name=parsed.name,
            age=parsed.age,
            bio=parsed.bio,
            distance_km=parsed.distance_km,
            compatibility_score=score,
            photo_urls=[f"screen://view_{i}" for i in range(len(images))],
            tags=parsed.tags,
            is_verified=parsed.is_verified,
        )
        return profile, parsed

    async def parse_chat_screenshot(
        self,
        screenshot_bytes: bytes,
        platform: str,
    ) -> ParsedChatScreenSchema:
        """Считывает историю переписки и координаты поля ввода/отправки прямо со скриншота чата."""
        images = self._prepare(screenshot_bytes)
        prompt = (
            f"Ты анализируешь скриншот экрана переписки в дейтинг-сервисе ({platform}).\n"
            "Определи, открыт ли чат (is_chat_screen).\n"
            "Извлеки имя собеседницы (girl_name) и список видимых сообщений сверху вниз (messages):\n"
            "- role='user' для входящих сообщений собеседницы (обычно слева)\n"
            "- role='assistant' для наших исходящих сообщений (обычно справа)\n"
            "Также укажи нормализованные координаты [x, y] (0..1000) для:\n"
            "- input_box: поле ввода текста сообщения\n"
            "- send_button: кнопка отправки сообщения (или null, если скрыта до ввода текста)\n"
            "Верни СТРОГО JSON: is_chat_screen, girl_name, messages, input_box, send_button, notes."
        )
        data = await self.gateway.complete_json(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            images=images,
        )
        return ParsedChatScreenSchema.model_validate(data)
