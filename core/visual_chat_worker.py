import asyncio
import logging
import uuid
from dataclasses import dataclass

from ai.chat_agent import AutonomousChatAgent
from core.image_utils import is_blank_or_secure_screen
from core.ui_driver import MobileUIDriverFallback
from database.repository import DatabaseManager
from filters.vision_card_parser import VisionProfileParser

logger = logging.getLogger(__name__)


@dataclass
class VisualChatDraft:
    draft_id: str
    thread_id: int
    girl_name: str
    stage: str
    chunks: list[str]
    bandit_arm: str | None
    should_handoff: bool
    handoff_summary: str
    input_box: tuple[int, int] | None
    send_button: tuple[int, int] | None
    messages_seen: int


class VisualChatAssistant:
    """Работает поверх ui_vision (ADB/Appium) и web_vision (Playwright):
    снимает скриншот открытого чата, считывает сообщения через VLM, сохраняет историю в SQLite,
    генерирует ответ через AutonomousChatAgent (LangGraph + Bandit) и по подтверждению
    печатает и отправляет его прямо в приложение или браузер.
    """

    def __init__(
        self,
        ui_driver: MobileUIDriverFallback | None,
        db: DatabaseManager,
        chat_agent: AutonomousChatAgent,
        vision_parser: VisionProfileParser | None = None,
        platform: str = "twinby",
        user_preferences: str = "",
    ) -> None:
        self.ui_driver = ui_driver
        self.db = db
        self.chat_agent = chat_agent
        self.vision_parser = vision_parser or VisionProfileParser()
        self.platform = platform
        self.user_preferences = user_preferences
        self.pending_drafts: dict[str, VisualChatDraft] = {}

    async def capture_and_draft(self, auto_send: bool = False) -> dict:
        if not self.ui_driver:
            raise RuntimeError("Визуальный драйвер не подключён (требуется MODE=ui_vision или web_vision).")

        shot = await self.ui_driver.capture_screenshot()
        if is_blank_or_secure_screen(shot):
            return {
                "ok": False,
                "error": "Чёрный/защищённый экран (FLAG_SECURE). Скриншот заблокирован приложением.",
            }

        parsed = await self.vision_parser.parse_chat_screenshot(shot, platform=self.platform)
        if not parsed.is_chat_screen:
            return {"ok": False, "error": f"Открыт не экран переписки ({parsed.notes or 'чат не распознан'})."}

        tkey = f"visual:{self.platform}:{parsed.girl_name.strip().lower()}"
        thread = await self.db.get_or_create_thread(
            thread_key=tkey,
            girl_name=parsed.girl_name.strip(),
            platform=self.platform,
            profile_context=self.user_preferences,
        )

        # Дозаписываем в БД новые сообщения с экрана без дублирования уже сохранённого хвоста
        existing = await self.db.get_thread_history(thread.id, limit=30)
        existing_set = {(m["role"], m["content"].strip()) for m in existing[-12:]}
        for msg in parsed.messages:
            key = (msg.role, msg.content.strip())
            if msg.content.strip() and key not in existing_set:
                await self.db.append_message(thread.id, msg.role, msg.content.strip())
                existing_set.add(key)

        history = await self.db.get_thread_history(thread.id, limit=16)
        reply = await self.chat_agent.generate_reply(
            history=history,
            profile_context=thread.profile_context or self.user_preferences,
        )

        draft_id = uuid.uuid4().hex[:8]
        draft = VisualChatDraft(
            draft_id=draft_id,
            thread_id=thread.id,
            girl_name=parsed.girl_name,
            stage=reply["stage"],
            chunks=reply["chunks"],
            bandit_arm=reply.get("bandit_arm"),
            should_handoff=bool(reply.get("should_handoff", False)),
            handoff_summary=str(reply.get("handoff_summary", "")),
            input_box=parsed.input_box,
            send_button=parsed.send_button,
            messages_seen=len(history),
        )
        self.pending_drafts[draft_id] = draft

        sent = False
        if auto_send and not draft.should_handoff:
            await self.send_draft(draft_id)
            sent = True

        return {
            "ok": True,
            "draft_id": draft_id,
            "girl_name": draft.girl_name,
            "stage": draft.stage,
            "chunks": draft.chunks,
            "bandit_arm": draft.bandit_arm,
            "should_handoff": draft.should_handoff,
            "handoff_summary": draft.handoff_summary,
            "messages_seen": draft.messages_seen,
            "has_input_coords": draft.input_box is not None,
            "sent": sent,
        }

    async def send_draft(self, draft_id: str) -> dict:
        if not self.ui_driver:
            raise RuntimeError("Визуальный драйвер не подключён.")
        draft = self.pending_drafts.pop(draft_id, None)
        if not draft:
            raise KeyError("Черновик не найден или уже отправлен.")

        for chunk in draft.chunks:
            await self.ui_driver.type_and_send_text(
                text=chunk,
                input_box=draft.input_box,
                send_button=draft.send_button,
            )
            await self.db.append_message(draft.thread_id, "assistant", chunk)
            await asyncio.sleep(min(max(len(chunk) * 0.04, 0.7), 2.5))

        await self.db.update_thread_meta(
            draft.thread_id,
            stage=draft.stage,
            bandit_arm=draft.bandit_arm,
            is_handed_off=draft.should_handoff,
        )
        return {"sent": True, "girl_name": draft.girl_name, "chunks": draft.chunks}
