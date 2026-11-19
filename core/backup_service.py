import asyncio
import logging
from contextlib import closing, suppress
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from aiogram import Bot
from aiogram.types import BufferedInputFile
from config import settings

logger = logging.getLogger(__name__)


class DatabaseBackupService:
    def __init__(self, bot: Bot, db_path: str | Path, interval_hours: int = 24) -> None:
        self.bot = bot
        self.db_path = Path(db_path)
        self.interval_seconds = interval_hours * 3600
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
                with suppress(asyncio.CancelledError):
                    await self._task
            finally:
                self._task = None

    def _create_safe_backup_sync(self) -> Path:
        source_path = self.db_path.resolve()
        if not source_path.is_file():
            raise FileNotFoundError(f"Backup source does not exist: {source_path}")
        # mode=ro also prevents accidental creation if the source disappears
        # between the existence check and opening the connection.
        with closing(
            sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True)
        ) as src_conn:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            backup_dir = source_path.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"heart3_backup_{timestamp}.db"
            with closing(sqlite3.connect(backup_path)) as dst_conn:
                with dst_conn:
                    src_conn.backup(dst_conn)
        return backup_path

    async def _loop(self) -> None:
        await asyncio.sleep(60)
        while self._is_running:
            try:
                backup_task = asyncio.create_task(
                    asyncio.to_thread(self._create_safe_backup_sync)
                )
                try:
                    backup_file = await asyncio.shield(backup_task)
                except asyncio.CancelledError:
                    # A thread cannot be cancelled: wait for it to close both
                    # SQLite connections before allowing stop() to finish.
                    try:
                        await backup_task
                    except Exception:
                        logger.exception("Backup failed during shutdown")
                    raise
                file_bytes = backup_file.read_bytes()
                document = BufferedInputFile(file_bytes, filename=backup_file.name)
                await self.bot.send_document(
                    chat_id=settings.tg_admin_id,
                    document=document,
                    caption=f"📦 Database Backup ({datetime.now(timezone.utc).isoformat()})",
                    parse_mode="HTML",
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Backup error: {e}")
            await asyncio.sleep(self.interval_seconds)
