import asyncio
import html
import logging
import uuid
from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ai.chat_agent import AutonomousChatAgent
from config import settings
from core.base_client import BaseDatingClient
from core.metrics import HANDOFFS_TOTAL

logger = logging.getLogger(__name__)


@dataclass
class PendingDraft:
    draft_id: str
    match_id: str
    girl_name: str
    profile_context: str
    history: list[dict[str, str]]
    chunks: list[str]
    stage: str
    bandit_arm: str | None = None


class ChatWorker:
    """Опрашивает непрочитанные мэтчи, прогоняет через AutonomousChatAgent (LangGraph + Bandit)
    и либо отправляет черновик админу в Telegram на подтверждение, либо отвечает автоматически.
    """

    def __init__(
        self,
        client: BaseDatingClient,
        bot: Bot,
        chat_agent: AutonomousChatAgent | None = None,
        auto_send: bool = settings.auto_send_replies,
        poll_interval_seconds: int = 45,
    ) -> None:
        self.client = client
        self.bot = bot
        self.chat_agent = chat_agent or AutonomousChatAgent()
        self.auto_send = auto_send
        self.poll_interval_seconds = poll_interval_seconds
        self.pending_drafts: dict[str, PendingDraft] = {}
        self._handed_off_matches: set[str] = set()
        self._match_arms: dict[str, str] = {}
        self._is_running = False
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._is_running:
            return
        self._is_running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def send_chunks(self, match_id: str, chunks: list[str]) -> None:
        for chunk in chunks:
            await asyncio.sleep(min(max(len(chunk) * 0.06, 0.8), 4.0))
            await self.client.send_message(match_id, chunk)

    async def approve_draft(self, draft_id: str) -> str:
        draft = self.pending_drafts.pop(draft_id, None)
        if not draft:
            return "Черновик уже обработан или устарел."
        await self.send_chunks(draft.match_id, draft.chunks)
        if draft.bandit_arm:
            self._match_arms[draft.match_id] = draft.bandit_arm
        return f"✅ Отправлено для {draft.girl_name}: {' / '.join(draft.chunks)}"

    async def regenerate_draft(self, draft_id: str) -> PendingDraft | None:
        old = self.pending_drafts.get(draft_id)
        if not old:
            return None
        res = await self.chat_agent.generate_reply(old.history, old.profile_context)
        old.chunks = res["chunks"]
        old.stage = res["stage"]
        old.bandit_arm = res.get("bandit_arm")
        return old

    def skip_draft(self, draft_id: str) -> bool:
        return self.pending_drafts.pop(draft_id, None) is not None

    async def _loop(self) -> None:
        while self._is_running:
            try:
                unread_matches = await self.client.get_matches_with_unread()
                for match in unread_matches:
                    match_id = str(match.get("id") or match.get("match_id"))
                    if match_id in self._handed_off_matches:
                        continue
                    if any(d.match_id == match_id for d in self.pending_drafts.values()):
                        continue

                    girl_name = str(match.get("name") or "Собеседница")
                    profile_ctx = str(match.get("bio") or "")
                    history = await self.client.get_chat_history(match_id, limit=15)

                    # Если собеседница ответила на наш стартовый icebreaker — автоматически награждаем Bandit!
                    if match_id in self._match_arms and any(m.get("role") == "user" for m in history[-2:]):
                        arm = self._match_arms.pop(match_id)
                        await self.chat_agent.bandit.record_reward(arm, success=True)

                    decision = await self.chat_agent.generate_reply(history, profile_ctx)
                    safe_name = html.escape(girl_name)

                    if decision["should_handoff"]:
                        self._handed_off_matches.add(match_id)
                        HANDOFFS_TOTAL.inc()
                        safe_summary = html.escape(str(decision["handoff_summary"]))
                        safe_reply = html.escape(" / ".join(decision["chunks"]))
                        await self.bot.send_message(
                            chat_id=settings.tg_admin_id,
                            text=(
                                f"🎯 <b>HANDOFF: {safe_name}</b>\n\n"
                                f"Сводка: {safe_summary}\n"
                                f"Предложенный ответ: {safe_reply}"
                            ),
                            parse_mode="HTML",
                        )
                        continue

                    if self.auto_send:
                        await self.send_chunks(match_id, decision["chunks"])
                        if decision.get("bandit_arm"):
                            self._match_arms[match_id] = decision["bandit_arm"]
                    else:
                        draft_id = uuid.uuid4().hex[:8]
                        draft = PendingDraft(
                            draft_id=draft_id,
                            match_id=match_id,
                            girl_name=girl_name,
                            profile_context=profile_ctx,
                            history=history,
                            chunks=decision["chunks"],
                            stage=decision["stage"],
                            bandit_arm=decision.get("bandit_arm"),
                        )
                        self.pending_drafts[draft_id] = draft
                        kb = InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(text="✅ Отправить", callback_data=f"draft:send:{draft_id}"),
                                    InlineKeyboardButton(text="🔄 Перегенерировать", callback_data=f"draft:regen:{draft_id}"),
                                    InlineKeyboardButton(text="❌ Пропустить", callback_data=f"draft:skip:{draft_id}"),
                                ]
                            ]
                        )
                        formatted_chunks = "\n".join(f"— {html.escape(c)}" for c in draft.chunks)
                        await self.bot.send_message(
                            chat_id=settings.tg_admin_id,
                            text=(
                                f"💬 <b>Черновик ответа для {safe_name}</b> (стадия: <code>{html.escape(draft.stage)}</code>)\n\n"
                                f"{formatted_chunks}"
                            ),
                            parse_mode="HTML",
                            reply_markup=kb,
                        )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Ошибка в ChatWorker: %s", exc)
            await asyncio.sleep(self.poll_interval_seconds)
