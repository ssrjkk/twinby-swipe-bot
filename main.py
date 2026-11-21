import asyncio
import logging

import uvicorn
from aiogram import Bot

from ai.bandits import PromptThompsonBandit
from ai.chat_agent import AutonomousChatAgent
from bot.telegram import create_dispatcher, send_dry_run_preview, send_match_notification
from config import configure_utf8_console, settings
from core.backup_service import DatabaseBackupService
from core.chat_worker import ChatWorker
from core.adapters_registry import resolve_platform_defaults
from core.factory import create_dating_client
from core.metrics import start_metrics_server
from core.proxy.manager import GeoProxyManager
from core.ui_driver import MobileUIDriverFallback
from core.visual_chat_worker import VisualChatAssistant
from core.worker import SwipeWorker
from database.repository import DatabaseManager
from filters.engine import ProfileFilterEngine
from web.api import create_web_app

# Before basicConfig: the StreamHandler binds sys.stderr at construction, so the console has to be
# switched to UTF-8 first for the emoji in log lines to survive a cp1251 Windows console.
configure_utf8_console()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("HEART3")


async def main() -> None:
    db = DatabaseManager()
    await db.init_db()
    filter_engine = ProfileFilterEngine()
    redis_client = None
    if settings.redis_url:
        try:
            import redis.asyncio as aioredis
            redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
            logger.info("Redis подключён для PromptThompsonBandit.")
        except Exception as exc:
            logger.warning("Redis недоступен (%s) — Bandit работает через локальный JSON.", exc)

    chat_agent = AutonomousChatAgent(bandit=PromptThompsonBandit(redis_client=redis_client))
    proxy_manager = GeoProxyManager.from_file(settings.proxies_file)

    if settings.metrics_port > 0:
        try:
            start_metrics_server(settings.metrics_port)
            logger.info("Prometheus метрики запущены на порту %d", settings.metrics_port)
        except OSError as exc:
            logger.warning("Порт метрик %d занят: %s", settings.metrics_port, exc)

    tg_token = settings.tg_bot_token.get_secret_value()
    tg_enabled = tg_token not in ("", "000000:dummy") and settings.tg_admin_id > 0
    bot = Bot(token=tg_token) if tg_enabled else None

    backup_service: DatabaseBackupService | None = None
    if settings.database_url.startswith("sqlite") and ":memory:" not in settings.database_url:
        db_file = settings.database_url.split("///")[-1]
        backup_service = DatabaseBackupService(
            bot=bot,
            db_path=db_file,
            interval_hours=settings.backup_interval_hours,
            retention_count=settings.backup_retention_count,
        )
        backup_service.start()

    proxy_task: asyncio.Task[None] | None = None
    if proxy_manager.proxies_count > 0:
        proxy_task = asyncio.create_task(proxy_manager.health_check_loop(300))

    ui_driver: MobileUIDriverFallback | None = None
    client = None
    chat_worker: ChatWorker | None = None

    if settings.mode in ("ui_vision", "web_vision"):
        backend = "playwright" if settings.mode == "web_vision" else settings.device_driver
        default_pkg, default_url = resolve_platform_defaults(settings.platform, settings.device_os)
        ui_driver = MobileUIDriverFallback(
            device_udid=settings.device_udid,
            platform_package=settings.app_package or default_pkg,
            appium_url=settings.appium_url,
            backend=backend,
            device_os=settings.device_os,
            platform_id=settings.platform,
            web_target_url=settings.web_target_url or default_url,
            web_profile_dir=settings.web_profile_dir,
            web_headless=settings.web_headless,
        )
        await ui_driver.connect()
    else:
        token = (
            settings.twinby_access_token.get_secret_value()
            if settings.platform == "twinby" and settings.twinby_access_token
            else (settings.api_platform_token.get_secret_value() if settings.api_platform_token else "")
        )
        refresh = settings.twinby_refresh_token.get_secret_value() if settings.twinby_refresh_token else ""
        client = create_dating_client(
            platform=settings.platform,
            session_id=f"main_{settings.platform}",
            credentials={"token": token, "refresh_token": refresh},
            proxy_manager=proxy_manager,
        )
        await client.__aenter__()
        if bot is not None:
            chat_worker = ChatWorker(client=client, bot=bot, chat_agent=chat_agent, auto_send=settings.auto_send_replies)
            chat_worker.start()

    worker = SwipeWorker(
        db=db,
        filter_engine=filter_engine,
        client=client,
        ui_driver=ui_driver,
        mode=settings.mode,
        platform=settings.platform,
        dry_run=settings.dry_run,
        capture_second_view=settings.capture_second_view,
        user_preferences=settings.user_preferences,
        on_match=(lambda p, s, r: send_match_notification(bot, p, s, r)) if bot else None,
        on_decision=(lambda p, d, s, r, dr: send_dry_run_preview(bot, p, d, s, r, dr)) if bot else None,
    )

    visual_chat = VisualChatAssistant(
        ui_driver=ui_driver,
        db=db,
        chat_agent=chat_agent,
        platform=settings.platform,
        user_preferences=settings.user_preferences,
    )

    app = create_web_app(
        worker=worker,
        proxy_manager=proxy_manager,
        db=db,
        filter_engine=filter_engine,
        chat_agent=chat_agent,
        backup_service=backup_service,
        visual_chat=visual_chat,
    )
    uvicorn_cfg = uvicorn.Config(app=app, host=settings.web_host, port=settings.web_port, log_level="warning")
    server = uvicorn.Server(uvicorn_cfg)

    logger.info(
        "<3 запущен | mode=%s | platform=%s | dry_run=%s | Control Panel: http://%s:%d/dashboard",
        settings.mode,
        settings.platform,
        settings.dry_run,
        settings.web_host,
        settings.web_port,
    )

    try:
        if bot is not None:
            web_task = asyncio.create_task(server.serve())
            dp = create_dispatcher(
                db=db,
                filter_engine=filter_engine,
                worker=worker,
                chat_worker=chat_worker,
                chat_agent=chat_agent,
                backup_service=backup_service,
                visual_chat=visual_chat,
            )
            await bot.delete_webhook(drop_pending_updates=True)
            try:
                await dp.start_polling(bot)
            finally:
                web_task.cancel()
                try:
                    await web_task
                except asyncio.CancelledError:
                    pass
        else:
            logger.info("TG_BOT_TOKEN не задан — запуск в режиме Web Control Panel без Telegram-бота.")
            await server.serve()
    finally:
        if proxy_task:
            proxy_task.cancel()
            try:
                await proxy_task
            except asyncio.CancelledError:
                pass
        if backup_service:
            await backup_service.stop()
        if chat_worker:
            await chat_worker.stop()
        await worker.stop()
        if client:
            await client.__aexit__(None, None, None)
        if ui_driver:
            await ui_driver.disconnect()
        if redis_client:
            await redis_client.aclose()
        if bot:
            await bot.session.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
