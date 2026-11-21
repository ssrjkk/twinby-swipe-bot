import os
from collections.abc import Iterator

import pytest
import pytest_asyncio
from database.repository import DatabaseManager
from tests._net_env import PROXY_VARS, hermetic_proxy


@pytest.fixture(autouse=True, scope="session")
def hermetic_proxy_environment() -> Iterator[None]:
    saved = {name: os.environ.get(name) for name in PROXY_VARS + ("NO_PROXY",)}
    hermetic_proxy()
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest_asyncio.fixture
async def in_memory_db() -> DatabaseManager:
    db = DatabaseManager(db_url="sqlite+aiosqlite:///:memory:")
    await db.init_db()
    return db

