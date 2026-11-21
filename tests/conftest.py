import pytest
import pytest_asyncio
from database.repository import DatabaseManager

@pytest_asyncio.fixture
async def in_memory_db() -> DatabaseManager:
    db = DatabaseManager(db_url="sqlite+aiosqlite:///:memory:")
    await db.init_db()
    return db
