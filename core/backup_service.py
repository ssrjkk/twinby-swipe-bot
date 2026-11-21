import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot
from aiogram.types import BufferedInputFile

from config import settings

logger = logging.getLogger(__name__)


class DatabaseBackupService:
    def __init__(
        self,
        bot: Bot | None,
        db_path: str = "./data/heart3.db",
        interval_hours: int = 24,
        retention_count: int = 7,
    ) -> None:
        self.bot = bot
        self.db_path = Path(db_path)
        self.interval_seconds = interval_hours * 3600
        self.retention_count = retention_count
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

    def _rotate_old_backups(self, backup_dir: Path) -> None:
        backups = sorted(backup_dir.glob("heart3_backup_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in backups[self.retention_count :]:
            try:
                stale.unlink()
            except OSError:
                pass

    def create_safe_backup_sync(self) -> Path | None:
        if not self.db_path.exists():
            logger.warning("Файл БД %s ещё не создан — пропуск бэкапа.", self.db_path)
            return None

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_dir = self.db_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"heart3_backup_{timestamp}.db"

        src_conn = sqlite3.connect(self.db_path)
        dst_conn = sqlite3.connect(backup_path)
        try:
            with dst_conn:
                src_conn.backup(dst_conn)
        finally:
            src_conn.close()
            dst_conn.close()

        self._rotate_old_backups(backup_dir)
        return backup_path

    async def _loop(self) -> None:
        await asyncio.sleep(60)
        while self._is_running:
            try:
                backup_file = await asyncio.to_thread(self.create_safe_backup_sync)
                if backup_file and self.bot and settings.tg_admin_id:
                    file_bytes = backup_file.read_bytes()
                    document = BufferedInputFile(file_bytes, filename=backup_file.name)
                    await self.bot.send_document(
                        chat_id=settings.tg_admin_id,
                        document=document,
                        caption=f"📦 <b>&lt;3 Backup</b> ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})",
                        parse_mode="HTML",
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Ошибка бэкапа БД: %s", exc)
            await asyncio.sleep(self.interval_seconds)
