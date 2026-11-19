import html
from typing import Any, Awaitable, Callable
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from config import settings
from core.base_client import CandidateProfile
from core.worker import SwipeWorker
from database.repository import DatabaseManager
from filters.engine import ProfileFilterEngine

router = Router()


class AdminOnlyMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id != settings.tg_admin_id:
            if isinstance(event, CallbackQuery):
                await event.answer()
            return None
        return await handler(event, data)


def get_main_keyboard(is_running: bool) -> InlineKeyboardMarkup:
    toggle_btn = (
        InlineKeyboardButton(text="⏸ Остановить", callback_data="worker:stop")
        if is_running
        else InlineKeyboardButton(text="▶️ Запустить", callback_data="worker:start")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                toggle_btn,
                InlineKeyboardButton(text="📊 Статистика", callback_data="stats:show"),
            ]
        ]
    )


@router.message(Command("start", "menu"))
async def cmd_start(message: Message, worker: SwipeWorker) -> None:
    status = "🟢 Работает" if worker.is_running else "🔴 На паузе"
    await message.answer(
        f"<b>&lt;3 · Панель управления</b>\n\nСтатус: {status}",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(worker.is_running),
    )


@router.callback_query(F.data == "worker:start")
async def cb_start(callback: CallbackQuery, worker: SwipeWorker) -> None:
    await callback.answer()
    worker.start()
    await callback.message.edit_text(
        "<b>&lt;3 · Панель управления</b>\n\nСтатус: 🟢 Работает",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(True),
    )


@router.callback_query(F.data == "worker:stop")
async def cb_stop(callback: CallbackQuery, worker: SwipeWorker) -> None:
    await callback.answer()
    await worker.stop()
    await callback.message.edit_text(
        "<b>&lt;3 · Панель управления</b>\n\nСтатус: 🔴 На паузе",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(False),
    )


@router.callback_query(F.data == "stats:show")
async def cb_stats(
    callback: CallbackQuery, db: DatabaseManager, worker: SwipeWorker
) -> None:
    await callback.answer()
    stats = await db.get_stats()
    text = f"📊 <b>Статистика</b>\nОбработано: {stats['total_processed']}\nМэтчей: {stats['total_matches']}"
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=get_main_keyboard(worker.is_running)
    )


def create_dispatcher(
    db: DatabaseManager, filter_engine: ProfileFilterEngine, worker: SwipeWorker
) -> Dispatcher:
    dp = Dispatcher()
    dp.message.middleware(AdminOnlyMiddleware())
    dp.callback_query.middleware(AdminOnlyMiddleware())
    dp["db"] = db
    dp["filter_engine"] = filter_engine
    dp["worker"] = worker
    dp.include_router(router)
    return dp


async def send_match_notification(
    bot: Bot, profile: CandidateProfile, score: float, reason: str
) -> None:
    text = f"🔥 <b>Мэтч!</b> {html.escape(profile.name)}, {profile.age} ({profile.compatibility_score}%)"
    await bot.send_message(chat_id=settings.tg_admin_id, text=text, parse_mode="HTML")
