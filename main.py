import asyncio
import logging
from contextlib import AsyncExitStack, suppress

import uvicorn
from aiogram import Bot
from bot.telegram import create_dispatcher, send_match_notification
from config import settings
from core.backup_service import DatabaseBackupService
from core.client import TwinbyStealthClient
from core.proxy.manager import GeoProxyManager
from core.worker import SwipeWorker
from database.repository import DatabaseManager
from filters.engine import ProfileFilterEngine
from web.api import create_web_app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MAIN")


async def _cancel_task(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def main() -> None:
    async with AsyncExitStack() as cleanup:
        db = DatabaseManager(settings.database_url)
        cleanup.push_async_callback(db.engine.dispose)
        await db.init_db()
        filter_engine = ProfileFilterEngine()
        bot = Bot(token=settings.tg_bot_token.get_secret_value())
        cleanup.push_async_callback(bot.session.close)
        proxy_manager = GeoProxyManager([])
        if db.sqlite_path is not None:
            backup_service = DatabaseBackupService(
                bot=bot, db_path=db.sqlite_path, interval_hours=24
            )
            cleanup.push_async_callback(backup_service.stop)
            backup_service.start()
        else:
            logger.info("SQLite backup disabled: database is not a SQLite file.")

        client = await cleanup.enter_async_context(TwinbyStealthClient())
        worker = SwipeWorker(
            client=client,
            db=db,
            filter_engine=filter_engine,
            on_match=lambda p, s, r: send_match_notification(bot, p, s, r),
        )
        cleanup.push_async_callback(worker.stop)
        app = create_web_app(
            type("M", (), {"workers": [(client, worker)]})(),
            proxy_manager,
            db,
            filter_engine,
        )

        config = uvicorn.Config(
            app=app, host=settings.web_host, port=8080, log_level="warning"
        )
        server = uvicorn.Server(config)
        web_task = asyncio.create_task(server.serve())
        cleanup.push_async_callback(_cancel_task, web_task)

        dp = create_dispatcher(db=db, filter_engine=filter_engine, worker=worker)
        await bot.delete_webhook(drop_pending_updates=False)
        logger.info("Bot started via polling.")
        await dp.start_polling(bot, close_bot_session=False)


if __name__ == "__main__":
    asyncio.run(main())
