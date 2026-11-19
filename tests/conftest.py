import curl_cffi
import httpx
import pytest
import pytest_asyncio
from pydantic_settings.sources import DotEnvSettingsSource

SESSION_PATCH = pytest.MonkeyPatch()
TEST_ENV = {
    "PYTHON_DOTENV_DISABLED": "1",
    "TWINBY_BASE_URL": "https://api.twinby.com/v1",
    "TWINBY_ACCESS_TOKEN": "test-only-twinby-token",
    "TWINBY_REFRESH_TOKEN": "",
    "PROXY_URL": "",
    "TG_BOT_TOKEN": "123456:test-only-bot-token",
    "TG_ADMIN_ID": "1",
    "WEB_ADMIN_TOKEN": "test-only-web-token",
    "WEB_HOST": "127.0.0.1",
    "WEB_ALLOWED_ORIGINS": "http://localhost:8080,http://127.0.0.1:8080",
    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
    "AI_PROVIDER": "openai",
    "LLM_BASE_URL": "https://api.openai.com/v1",
    "LLM_API_KEY": "sk-dummy",
    "LLM_MODEL": "test-model",
    "CUSTOM_AUTH_HEADER": "",
    "DAILY_LIKE_LIMIT": "150",
    "MIN_SWIPE_DELAY": "2.5",
    "MAX_SWIPE_DELAY": "7.5",
    "MIN_COMPATIBILITY": "75",
    "MAX_DISTANCE_KM": "25",
    "MIN_AGE": "18",
    "MAX_AGE": "30",
    "REQUIRE_BIO": "false",
    "GOOGLE_ACCESS_TOKEN": "test-calendar-token",
    "GOOGLE_REFRESH_TOKEN": "",
    "GOOGLE_CLIENT_ID": "",
    "GOOGLE_CLIENT_SECRET": "",
    "ANTHROPIC_API_KEY": "",
    "OPENAI_API_KEY": "",
    "GEMINI_API_KEY": "",
    "AGENT": "claude",
    "LIKE_THRESHOLD": "75",
    "MAX_SWIPES": "50",
    "DELAY_MIN": "3",
    "DELAY_MAX": "7",
    "NO_PROXY": "",
}
for key, value in TEST_ENV.items():
    SESSION_PATCH.setenv(key, value)
# pydantic-settings reads dotenv independently of PYTHON_DOTENV_DISABLED.
SESSION_PATCH.setattr(DotEnvSettingsSource, "_read_env_files", lambda self: {})


def block_native_network(*args, **kwargs):
    raise RuntimeError("Native curl network is disabled in tests")


# Native curl bypasses Python socket guards, including during test collection.
SESSION_PATCH.setattr(curl_cffi.Curl, "perform", block_native_network)
SESSION_PATCH.setattr(curl_cffi.AsyncCurl, "add_handle", block_native_network)
SESSION_PATCH.setattr(curl_cffi.Curl, "ws_send", block_native_network)
SESSION_PATCH.setattr(curl_cffi.Curl, "ws_recv", block_native_network)

from database.repository import DatabaseManager  # noqa: E402


def pytest_unconfigure(config):
    SESSION_PATCH.undo()


@pytest_asyncio.fixture
async def in_memory_db():
    db = DatabaseManager(db_url="sqlite+aiosqlite:///:memory:")
    await db.init_db()
    try:
        yield db
    finally:
        await db.engine.dispose()


class HttpxBackedSession:
    def __init__(
        self, impersonate=None, proxy=None, headers=None, timeout=None, **kwargs
    ):
        self.impersonate = impersonate
        self.proxy = proxy
        self._client = httpx.AsyncClient(
            headers=headers or {}, timeout=timeout or 15, trust_env=False
        )

    @property
    def headers(self):
        return self._client.headers

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def request(self, method, url, **kwargs):
        return await self._client.request(method, url, **kwargs)

    async def get(self, url, **kwargs):
        return await self.request("GET", url, **kwargs)

    async def post(self, url, **kwargs):
        return await self.request("POST", url, **kwargs)

    async def close(self):
        await self._client.aclose()


@pytest.fixture(autouse=True)
def curl_cffi_via_httpx(monkeypatch):
    import core.adapters.pure_and_cis as pure_and_cis
    import core.client as stealth_client
    import core.proxy.manager as proxy_manager

    for module in (pure_and_cis, stealth_client, proxy_manager):
        monkeypatch.setattr(module, "AsyncSession", HttpxBackedSession)
    return HttpxBackedSession
