import html
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject

from ai.calendar_tool import book_dating_event, list_busy_slots
from ai.chat_agent import AutonomousChatAgent
from config import settings
from core.backup_service import DatabaseBackupService
from core.base_client import CandidateProfile
from core.chat_worker import ChatWorker
from core.visual_chat_worker import VisualChatAssistant
from core.worker import SwipeWorker
from database.models import SwipeDecision
from database.repository import DatabaseManager
from filters.engine import ProfileFilterEngine

router = Router()

# Меню команд по языкам интерфейса Telegram. Клиент показывает скоуп своего языка, а при его отсутствии
# откатывается на default-скоуп — поэтому его тоже заполняем, иначе у пользователя без ru/en в меню пусто.
BOT_COMMANDS: dict[str, list[BotCommand]] = {
    "ru": [
        BotCommand(command="start", description="Панель управления"),
        BotCommand(command="menu", description="Вернуться к панели"),
        BotCommand(command="step", description="Свайп по текущему экрану"),
        BotCommand(command="chatstep", description="Ответ по открытому чату"),
        BotCommand(command="slots", description="Свободные слоты календаря"),
        BotCommand(command="book", description="Забронировать свидание"),
        BotCommand(command="reply", description="Черновик ответа мэтчу"),
        BotCommand(command="backup", description="Бэкап базы SQLite"),
    ],
    "en": [
        BotCommand(command="start", description="Control panel"),
        BotCommand(command="menu", description="Back to the panel"),
        BotCommand(command="step", description="Swipe the current screen"),
        BotCommand(command="chatstep", description="Reply in the open chat"),
        BotCommand(command="slots", description="Free calendar slots"),
        BotCommand(command="book", description="Book a date"),
        BotCommand(command="reply", description="Draft a reply to a match"),
        BotCommand(command="backup", description="Back up the SQLite database"),
    ],
}


async def register_bot_commands(bot: Bot) -> dict[str, int]:
    """Загружает меню команд в Telegram: по скоупу на язык плюс копия в default-скоуп."""
    registered: dict[str, int] = {}
    for language, commands in BOT_COMMANDS.items():
        await bot.set_my_commands(commands, language_code=language)
        registered[language] = len(commands)
    await bot.set_my_commands(BOT_COMMANDS["ru"])
    registered["default"] = len(BOT_COMMANDS["ru"])
    return registered


class AdminOnlyMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user and user.id != settings.tg_admin_id:
            return None
        return await handler(event, data)


def get_main_keyboard(is_running: bool, dry_run: bool) -> InlineKeyboardMarkup:
    toggle_btn = (
        InlineKeyboardButton(text="⏸ Остановить", callback_data="worker:stop")
        if is_running
        else InlineKeyboardButton(text="▶️ Запустить", callback_data="worker:start")
    )
    dry_btn = InlineKeyboardButton(
        text=f"🧪 DRY_RUN: {'ВКЛ' if dry_run else 'ВЫКЛ'}",
        callback_data="worker:toggle_dry_run",
    )
    step_btn = InlineKeyboardButton(text="📸 1 шаг (VLM)", callback_data="worker:step")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [toggle_btn, InlineKeyboardButton(text="📊 Статистика", callback_data="stats:show")],
            [dry_btn, step_btn],
        ]
    )


def _panel_text(worker: SwipeWorker) -> str:
    status = "🟢 Работает" if worker.is_running else "🔴 На паузе"
    dry = "🧪 DRY_RUN (без реальных свайпов)" if worker.dry_run else "⚡ БОЕВОЙ РЕЖИМ"
    return (
        "<b>&lt;3 Control Panel</b>\n\n"
        f"Режим: <code>{html.escape(worker.mode)}</code> ({html.escape(worker.platform)})\n"
        f"Статус: {status}\n"
        f"Свайпы: {dry}\n\n"
        "<i>Команды: /step (анкета), /chatstep (чат с экрана), /reply Имя | текст, /backup</i>"
    )


@router.message(Command("start", "menu"))
async def cmd_start(message: Message, worker: SwipeWorker) -> None:
    await message.answer(
        _panel_text(worker),
        parse_mode="HTML",
        reply_markup=get_main_keyboard(worker.is_running, worker.dry_run),
    )


@router.message(Command("step"))
async def cmd_step(message: Message, worker: SwipeWorker) -> None:
    if worker.mode not in ("ui_vision", "web_vision"):
        await message.answer("Команда /step работает в режимах ui_vision и web_vision.")
        return
    try:
        res = await worker.process_single_visual_screen()
        await message.answer(f"📸 <b>Результат шага:</b>\n<code>{html.escape(str(res))}</code>", parse_mode="HTML")
    except Exception as exc:
        await message.answer(f"❌ Ошибка /step: <code>{html.escape(str(exc))}</code>", parse_mode="HTML")


@router.message(Command("chatstep"))
async def cmd_chatstep(message: Message, visual_chat: VisualChatAssistant | None = None) -> None:
    if not visual_chat or not visual_chat.ui_driver:
        await message.answer("Команда /chatstep работает в режимах ui_vision и web_vision.")
        return
    try:
        res = await visual_chat.capture_and_draft(auto_send=False)
        if not res.get("ok"):
            await message.answer(f"⚠️ {html.escape(str(res.get('error', 'Чат не распознан')))}")
            return
        draft_id = res["draft_id"]
        chunks_txt = "\n".join(f"— {html.escape(c)}" for c in res["chunks"])
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🚀 Отправить в чат", callback_data=f"vchat:send:{draft_id}"),
                    InlineKeyboardButton(text="❌ Отмена", callback_data=f"vchat:skip:{draft_id}"),
                ]
            ]
        )
        await message.answer(
            f"💬 <b>Чат с {html.escape(res['girl_name'])}</b> (стадия: <code>{html.escape(res['stage'])}</code>)\n\n{chunks_txt}",
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception as exc:
        await message.answer(f"❌ Ошибка /chatstep: <code>{html.escape(str(exc))}</code>", parse_mode="HTML")


@router.callback_query(F.data.startswith("vchat:send:"))
async def cb_vchat_send(callback: CallbackQuery, visual_chat: VisualChatAssistant | None = None) -> None:
    draft_id = (callback.data or "").split(":")[-1]
    if not visual_chat:
        await callback.answer("VisualChatAssistant не активен.", show_alert=True)
        return
    try:
        res = await visual_chat.send_draft(draft_id)
        if callback.message:
            await callback.message.edit_text(f"✅ Отправлено в чат для {html.escape(res['girl_name'])}", parse_mode="HTML")
    except Exception as exc:
        if callback.message:
            await callback.message.edit_text(f"❌ Ошибка отправки: <code>{html.escape(str(exc))}</code>", parse_mode="HTML")


@router.callback_query(F.data.startswith("vchat:skip:"))
async def cb_vchat_skip(callback: CallbackQuery, visual_chat: VisualChatAssistant | None = None) -> None:
    draft_id = (callback.data or "").split(":")[-1]
    if visual_chat:
        visual_chat.pending_drafts.pop(draft_id, None)
    if callback.message:
        await callback.message.edit_text("❌ Отправка в чат отменена.")


@router.message(Command("slots"))
async def cmd_slots(message: Message) -> None:
    res = await list_busy_slots(days_ahead=3)
    await message.answer(f"📅 <b>Календарь (3 дня):</b>\n{html.escape(res)}", parse_mode="HTML")


@router.message(Command("book"))
async def cmd_book(message: Message) -> None:
    """Формат: /book 2026-11-21T19:00:00 | Кофейня | Соня"""
    raw = (message.text or "").replace("/book", "", 1).strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 3:
        await message.answer(
            "Использование: <code>/book 2026-11-21T19:00:00 | Surf Coffee | Соня</code>",
            parse_mode="HTML",
        )
        return
    res = await book_dating_event(slot_iso_string=parts[0], venue_name=parts[1], girl_name=parts[2])
    await message.answer(f"📅 {html.escape(res)}", parse_mode="HTML")


@router.message(Command("backup"))
async def cmd_backup(message: Message, backup_service: DatabaseBackupService | None = None) -> None:
    if not backup_service:
        await message.answer("Сервис бэкапа SQLite не активен.")
        return
    path = backup_service.create_safe_backup_sync()
    if not path:
        await message.answer("Файл БД пока не создан.")
        return
    doc = BufferedInputFile(path.read_bytes(), filename=path.name)
    await message.answer_document(doc, caption="📦 Ручной бэкап БД &lt;3", parse_mode="HTML")


@router.message(Command("reply"))
async def cmd_reply(message: Message, db: DatabaseManager, chat_agent: AutonomousChatAgent) -> None:
    raw = (message.text or "").replace("/reply", "", 1).strip()
    if not raw:
        await message.answer(
            "Использование:\n<code>/reply Соня | Привет! Тоже люблю фильтр-кофе</code>\nили просто <code>/reply текст</code>",
            parse_mode="HTML",
        )
        return
    if "|" in raw:
        girl_name, girl_msg = [p.strip() for p in raw.split("|", 1)]
    else:
        girl_name, girl_msg = "Собеседница", raw

    try:
        thread = await db.get_or_create_thread(
            thread_key=f"tg:{girl_name.lower()}",
            girl_name=girl_name,
            platform=settings.platform,
            profile_context=settings.user_preferences,
        )
        await db.append_message(thread.id, "user", girl_msg)
        history = await db.get_thread_history(thread.id)

        res = await chat_agent.generate_reply(
            history=history,
            profile_context=thread.profile_context or settings.user_preferences,
        )
        for chunk in res["chunks"]:
            await db.append_message(thread.id, "assistant", chunk)
        await db.update_thread_meta(thread.id, stage=res["stage"], bandit_arm=res.get("bandit_arm"))

        bubbles = "\n".join(f"• {html.escape(c)}" for c in res["chunks"])
        await message.answer(
            f"💬 <b>Ответ для {html.escape(girl_name)}</b> "
            f"(стадия: <code>{html.escape(res['stage'])}</code>, история: {len(history)})\n\n{bubbles}",
            parse_mode="HTML",
        )
    except Exception as exc:
        await message.answer(f"❌ Ошибка генерации ответа: <code>{html.escape(str(exc))}</code>", parse_mode="HTML")


@router.callback_query(F.data == "worker:start")
async def cb_start(callback: CallbackQuery, worker: SwipeWorker) -> None:
    worker.start()
    if callback.message:
        await callback.message.edit_text(
            _panel_text(worker),
            parse_mode="HTML",
            reply_markup=get_main_keyboard(True, worker.dry_run),
        )


@router.callback_query(F.data == "worker:stop")
async def cb_stop(callback: CallbackQuery, worker: SwipeWorker) -> None:
    await worker.stop()
    if callback.message:
        await callback.message.edit_text(
            _panel_text(worker),
            parse_mode="HTML",
            reply_markup=get_main_keyboard(False, worker.dry_run),
        )


@router.callback_query(F.data == "worker:toggle_dry_run")
async def cb_toggle_dry(callback: CallbackQuery, worker: SwipeWorker) -> None:
    worker.dry_run = not worker.dry_run
    if callback.message:
        await callback.message.edit_text(
            _panel_text(worker),
            parse_mode="HTML",
            reply_markup=get_main_keyboard(worker.is_running, worker.dry_run),
        )


@router.callback_query(F.data == "worker:step")
async def cb_step(callback: CallbackQuery, worker: SwipeWorker) -> None:
    if worker.mode not in ("ui_vision", "web_vision"):
        await callback.answer("Доступно только в ui_vision / web_vision.", show_alert=True)
        return
    await callback.answer("Снимаю экран...")
    try:
        res = await worker.process_single_visual_screen()
        if callback.message:
            await callback.message.answer(f"📸 <code>{html.escape(str(res))}</code>", parse_mode="HTML")
    except Exception as exc:
        if callback.message:
            await callback.message.answer(f"❌ Ошибка шага: <code>{html.escape(str(exc))}</code>", parse_mode="HTML")


@router.callback_query(F.data == "stats:show")
async def cb_stats(callback: CallbackQuery, db: DatabaseManager, worker: SwipeWorker) -> None:
    stats = await db.get_stats()
    text = (
        "📊 <b>Статистика &lt;3</b>\n"
        f"Обработано: {stats['total_processed']}\n"
        f"Лайков: {stats['total_likes']} (сегодня: {stats['today_likes']})\n"
        f"Мэтчей: {stats['total_matches']} ({stats['match_rate']}%)"
    )
    if callback.message:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=get_main_keyboard(worker.is_running, worker.dry_run),
        )


@router.callback_query(F.data.startswith("draft:send:"))
async def cb_draft_send(callback: CallbackQuery, chat_worker: ChatWorker | None = None) -> None:
    draft_id = (callback.data or "").split(":")[-1]
    if not chat_worker:
        await callback.answer("ChatWorker не активен в текущем режиме.", show_alert=True)
        return
    result_text = await chat_worker.approve_draft(draft_id)
    if callback.message:
        await callback.message.edit_text(html.escape(result_text), parse_mode="HTML")


@router.callback_query(F.data.startswith("draft:regen:"))
async def cb_draft_regen(callback: CallbackQuery, chat_worker: ChatWorker | None = None) -> None:
    draft_id = (callback.data or "").split(":")[-1]
    if not chat_worker:
        await callback.answer("ChatWorker не активен.", show_alert=True)
        return
    draft = await chat_worker.regenerate_draft(draft_id)
    if not draft:
        if callback.message:
            await callback.message.edit_text("Черновик устарел.")
        return
    chunks_txt = "\n".join(f"— {html.escape(c)}" for c in draft.chunks)
    if callback.message:
        await callback.message.edit_text(
            f"💬 <b>Новый черновик для {html.escape(draft.girl_name)}</b> "
            f"(стадия: <code>{html.escape(draft.stage)}</code>)\n\n{chunks_txt}",
            parse_mode="HTML",
            reply_markup=callback.message.reply_markup,
        )


@router.callback_query(F.data.startswith("draft:skip:"))
async def cb_draft_skip(callback: CallbackQuery, chat_worker: ChatWorker | None = None) -> None:
    draft_id = (callback.data or "").split(":")[-1]
    if chat_worker:
        chat_worker.skip_draft(draft_id)
    if callback.message:
        await callback.message.edit_text("❌ Черновик пропущен.")


def create_dispatcher(
    db: DatabaseManager,
    filter_engine: ProfileFilterEngine,
    worker: SwipeWorker,
    chat_worker: ChatWorker | None = None,
    chat_agent: AutonomousChatAgent | None = None,
    backup_service: DatabaseBackupService | None = None,
    visual_chat: VisualChatAssistant | None = None,
) -> Dispatcher:
    dp = Dispatcher()
    dp.message.middleware(AdminOnlyMiddleware())
    dp.callback_query.middleware(AdminOnlyMiddleware())
    dp["db"] = db
    dp["filter_engine"] = filter_engine
    dp["worker"] = worker
    dp["chat_worker"] = chat_worker
    dp["chat_agent"] = chat_agent or AutonomousChatAgent()
    dp["backup_service"] = backup_service
    dp["visual_chat"] = visual_chat
    dp.include_router(router)
    return dp


async def send_match_notification(bot: Bot, profile: CandidateProfile, score: float, reason: str) -> None:
    text = (
        f"🔥 <b>Мэтч ({html.escape(profile.platform)})!</b> "
        f"{html.escape(profile.name)}, {profile.age} | Score {score}\n{html.escape(reason)}"
    )
    await bot.send_message(chat_id=settings.tg_admin_id, text=text, parse_mode="HTML")


async def send_dry_run_preview(
    bot: Bot,
    profile: CandidateProfile,
    decision: SwipeDecision,
    score: float,
    reason: str,
    dry_run: bool,
) -> None:
    if not dry_run or settings.tg_admin_id == 0:
        return
    icon = "❤️ LIKE" if decision == SwipeDecision.LIKE else "⏭ PASS"
    await bot.send_message(
        chat_id=settings.tg_admin_id,
        text=(
            f"🧪 <b>[DRY-RUN] {icon}</b> | {html.escape(profile.name)}, {profile.age} "
            f"({html.escape(profile.platform)})\nScore: {score}\nПричина: {html.escape(reason)}"
        ),
        parse_mode="HTML",
    )
