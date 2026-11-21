"""Тесты доработок «Б» (выбор и переключение коннектов) и «В» (ключи любой формы от любой нейросети).
Панель поднимается через FastAPI TestClient, живые устройства и сеть не используются: визуальные
драйверы и API-клиенты подменяются двойниками, поэтому тесты одинаково зелёные локально и на CI.
"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ai.bandits import PromptThompsonBandit
from ai.chat_agent import AutonomousChatAgent
from ai.universal_llm import PROVIDER_PRESETS, UniversalAIGateway, list_provider_presets, mask_api_key
from config import settings, upsert_env_values
from core.proxy.manager import GeoProxyManager
from core.visual_chat_worker import VisualChatAssistant
from core.worker import SwipeWorker
from database.repository import DatabaseManager
from filters.engine import ProfileFilterEngine
from filters.synthetic_scorer import SyntheticCompatibilityScorer
from filters.vision_card_parser import VisionProfileParser
from web.api import MODE_DRIVERS, create_web_app

# PATCH /ai/config намеренно обновляет настройки процесса, а коннектор — часть настроек устройства.
MUTATED_SETTINGS = (
    "ai_provider", "llm_base_url", "llm_model", "llm_api_key", "custom_auth_header",
    "llm_auth_prefix", "llm_api_key_query_param", "web_api_token", "device_driver", "appium_url", "web_headless",
)

SECRET = "sk-live-secret-9977"
GROQ_KEY = "gsk_live_abcdefghijkl"


@pytest.fixture(autouse=True)
def _restore_global_settings() -> Any:
    before = {key: getattr(settings, key) for key in MUTATED_SETTINGS}
    yield
    for key, value in before.items():
        setattr(settings, key, value)


class FakeDriver:
    """Двойник MobileUIDriverFallback: те же поля, что у настоящего драйвера, плюс записанные вызовы.

    Поля объявлены со значениями по умолчанию, чтобы фейк, созданный в тесте напрямую
    (`FakeDriver(backend="adb")`), оставался совместим с кодом панели, читающим `drv.device_udid` и др.
    """

    instances: list["FakeDriver"] = []
    raise_on_connect = False

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.device_udid = kwargs.get("device_udid", "")
        self.platform_package = kwargs.get("platform_package", "com.twinby.app")
        self.appium_url = kwargs.get("appium_url", "http://localhost:4723")
        self.backend = kwargs.get("backend", "adb")
        self.device_os = kwargs.get("device_os", "Android")
        self.platform_id = kwargs.get("platform_id", "twinby")
        self.web_target_url = kwargs.get("web_target_url", "")
        self.web_profile_dir = kwargs.get("web_profile_dir", "./data/browser_profile")
        self.web_headless = kwargs.get("web_headless", False)
        self.connected = False
        self.disconnected = False
        self.switched: list[tuple[str, str, str]] = []
        FakeDriver.instances.append(self)

    async def connect(self) -> None:
        if FakeDriver.raise_on_connect:
            raise RuntimeError("устройство не видно по adb")
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def switch_platform(self, platform_id: str, package_or_bundle: str, web_url: str) -> None:
        self.switched.append((platform_id, package_or_bundle, web_url))


class FakeApiClient:
    """Двойник REST-клиента: считает вызовы __aenter__/__aexit__."""

    instances: list["FakeApiClient"] = []
    raise_on_enter = False

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.entered = 0
        self.exited = 0
        FakeApiClient.instances.append(self)

    async def __aenter__(self) -> "FakeApiClient":
        if FakeApiClient.raise_on_enter:
            raise RuntimeError("401 от платформы")
        self.entered += 1
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        self.exited += 1
        return False


def _gateway(provider: str, key: str) -> UniversalAIGateway:
    url, model = PROVIDER_PRESETS[provider]
    return UniversalAIGateway(provider=provider, base_url=url, api_key=key, model=model)


class Harness:
    """Панель с четырьмя настоящими шлюзами ИИ — чтобы проверить их горячую замену по-настоящему."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        for holder in (FakeDriver, FakeApiClient):
            holder.instances.clear()
            holder.raise_on_connect = False
            holder.raise_on_enter = False
        monkeypatch.setattr(settings, "web_api_token", None)
        monkeypatch.setattr("web.api.MobileUIDriverFallback", FakeDriver)
        monkeypatch.setattr("web.api.create_dating_client", lambda **kw: FakeApiClient(**kw))

        self.db = DatabaseManager(db_url=f"sqlite+aiosqlite:///{tmp_path / 'panel.db'}")
        asyncio.run(self.db.init_db())
        self.filter_engine = ProfileFilterEngine()
        self.gateways = {
            "worker.vision_parser": _gateway("openai", SECRET),
            "worker.synthetic_scorer": _gateway("openai", SECRET),
            "chat_agent.engine": _gateway("openai", SECRET),
            "visual_chat.vision_parser": _gateway("openai", SECRET),
        }
        self.worker = SwipeWorker(
            db=self.db,
            filter_engine=self.filter_engine,
            mode="ui_vision",
            platform="twinby",
            dry_run=True,
            vision_parser=VisionProfileParser(gateway=self.gateways["worker.vision_parser"]),
            synthetic_scorer=SyntheticCompatibilityScorer(gateway=self.gateways["worker.synthetic_scorer"]),
        )
        self.agent = AutonomousChatAgent(
            gateway=self.gateways["chat_agent.engine"],
            bandit=PromptThompsonBandit(redis_client=None, state_file=str(tmp_path / "bandit.json")),
        )
        self.vchat = VisualChatAssistant(
            ui_driver=None,
            db=self.db,
            chat_agent=self.agent,
            vision_parser=VisionProfileParser(gateway=self.gateways["visual_chat.vision_parser"]),
        )
        self.app = create_web_app(
            worker=self.worker,
            proxy_manager=GeoProxyManager([]),
            db=self.db,
            filter_engine=self.filter_engine,
            chat_agent=self.agent,
            visual_chat=self.vchat,
        )
        self.client = TestClient(self.app)

    def close(self) -> None:
        asyncio.run(self.db.close())


@pytest.fixture
def panel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    harness = Harness(tmp_path, monkeypatch)
    yield harness
    harness.close()


# ================================ Б: коннекты ================================


def test_connection_endpoint_describes_modes_drivers_and_28_platforms(panel: Harness) -> None:
    body = panel.client.get("/api/v1/connection").json()
    assert body["mode"] == "ui_vision" and body["platform"] == "twinby"
    assert body["platform_name"] == "Twinby" and body["region"] == "CIS"
    assert body["driver"] == "none" and body["connected"] is False
    assert {m["mode"] for m in body["options"]["modes"]} == {"ui_vision", "web_vision", "api"}
    assert len(body["options"]["platforms"]) == 28
    twinby = next(p for p in body["options"]["platforms"] if p["id"] == "twinby")
    assert twinby["android_package"] == "com.twinby.app" and twinby["web_url"].startswith("https://")
    assert set(body["api"]) == {"client_active", "har_profiles", "registry_base_url", "token_from_env"}
    assert SECRET not in json.dumps(body)


def test_connection_apply_validates_mode_platform_and_driver(panel: Harness) -> None:
    bad_mode = panel.client.post("/api/v1/connection", json={"mode": "telepathy", "platform": "twinby"})
    assert bad_mode.status_code == 400 and "telepathy" in bad_mode.json()["detail"]
    bad_platform = panel.client.post("/api/v1/connection", json={"mode": "api", "platform": "crushio"})
    assert bad_platform.status_code == 404 and "crushio" in bad_platform.json()["detail"]
    bad_driver = panel.client.post(
        "/api/v1/connection", json={"mode": "ui_vision", "platform": "twinby", "driver": "playwright"}
    )
    assert bad_driver.status_code == 400 and "playwright" in bad_driver.json()["detail"]
    assert MODE_DRIVERS["ui_vision"] == ("adb", "appium")
    assert FakeDriver.instances == []
    assert panel.client.get("/api/v1/status").status_code == 200


def test_connection_apply_swaps_visual_driver_and_rewires_chat(panel: Harness) -> None:
    old = FakeDriver(backend="adb", platform_id="twinby")
    old.connected = True
    panel.worker.ui_driver = old
    panel.vchat.ui_driver = old
    panel.worker._is_running = True
    panel.worker.stop = AsyncMock(return_value=True)

    res = panel.client.post(
        "/api/v1/connection",
        json={"mode": "ui_vision", "platform": "mamba", "driver": "appium", "device_udid": "emulator-5554"},
    )
    assert res.status_code == 200, res.text
    assert res.json() == {
        "ok": True, "mode": "ui_vision", "platform": "mamba", "driver": "appium",
        "package": "ru.mamba.client", "web_url": "https://www.mamba.ru/rating",
        "connected": True, "worker_was_running": True, "env_saved": None,
    }
    panel.worker.stop.assert_awaited_once()
    new = FakeDriver.instances[-1]
    assert new is not old and new.connected is True and new.disconnected is False
    assert old.disconnected is True
    assert new.kwargs["device_udid"] == "emulator-5554"
    assert new.kwargs["platform_id"] == "mamba" and new.kwargs["backend"] == "appium"
    assert new.kwargs["platform_package"] == "ru.mamba.client"
    assert new.switched == [("mamba", "ru.mamba.client", "https://www.mamba.ru/rating")]
    assert panel.worker.ui_driver is new and panel.worker.platform == "mamba"
    assert panel.vchat.ui_driver is new and panel.vchat.platform == "mamba"
    assert panel.client.get("/api/v1/connection").json()["driver"] == "appium"


def test_connection_apply_web_vision_forces_playwright_backend(panel: Harness) -> None:
    res = panel.client.post(
        "/api/v1/connection",
        json={
            "mode": "web_vision", "platform": "pure", "web_headless": True,
            "web_target_url": "https://pure.example/app", "launch": False,
        },
    )
    assert res.status_code == 200, res.text
    new = FakeDriver.instances[-1]
    assert new.kwargs["backend"] == "playwright" and new.kwargs["web_headless"] is True
    assert new.kwargs["web_target_url"] == "https://pure.example/app"
    assert new.switched == []
    assert panel.client.get("/api/v1/connection").json()["mode"] == "web_vision"


def test_connection_apply_failure_keeps_previous_connection(panel: Harness) -> None:
    old = FakeDriver(backend="adb", platform_id="twinby")
    old.connected = True
    panel.worker.ui_driver = old
    FakeDriver.raise_on_connect = True

    res = panel.client.post("/api/v1/connection", json={"mode": "ui_vision", "platform": "tinder", "driver": "adb"})
    assert res.status_code == 502
    detail = res.json()["detail"]
    assert "Не удалось подключить ui_vision/adb" in detail and "Прежнее подключение сохранено" in detail
    assert old.disconnected is False and old.connected is True
    assert panel.worker.ui_driver is old and panel.worker.platform == "twinby"
    assert FakeDriver.instances[-1].disconnected is True  # сорванный новый драйвер закрыт, а не оставлен висеть
    # Сервер жив: неудачная кнопка не роняет Control Panel.
    assert panel.client.get("/api/v1/status").status_code == 200
    assert panel.client.get("/api/v1/connection").json()["connected"] is True


def test_connection_apply_api_mode_builds_rest_client_and_drops_driver(panel: Harness) -> None:
    old = FakeDriver(backend="adb", platform_id="twinby")
    old.connected = True
    panel.worker.ui_driver = old
    panel.vchat.ui_driver = old

    res = panel.client.post("/api/v1/connection", json={"mode": "api", "platform": "mamba", "api_token": "tok-1"})
    assert res.status_code == 200, res.text
    rest = FakeApiClient.instances[-1]
    assert rest.entered == 1 and rest.exited == 0
    assert rest.kwargs["platform"] == "mamba" and rest.kwargs["credentials"] == {"token": "tok-1"}
    assert panel.worker.client is rest and panel.worker.ui_driver is None
    assert panel.worker.mode == "api" and panel.vchat.ui_driver is None
    assert old.disconnected is True
    conn = panel.client.get("/api/v1/connection").json()
    assert conn["driver"] == "rest_api" and conn["api"]["client_active"] is True


def test_connection_apply_api_failure_returns_502_and_keeps_state(panel: Harness) -> None:
    FakeApiClient.raise_on_enter = True
    res = panel.client.post("/api/v1/connection", json={"mode": "api", "platform": "pure"})
    assert res.status_code == 502 and "401" in res.json()["detail"]
    assert panel.worker.client is None and panel.worker.mode == "ui_vision"
    assert panel.client.get("/api/v1/health").json()["status"] == "ok"


def test_connection_apply_save_to_env_updates_only_relevant_keys(panel: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text(
        f"# мой локальный конфиг\nMODE=ui_vision\nPLATFORM=twinby\nDRY_RUN=true\nLLM_API_KEY={SECRET}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    res = panel.client.post(
        "/api/v1/connection",
        json={
            "mode": "ui_vision", "platform": "vk_dating", "driver": "appium",
            "device_udid": "R58M12", "appium_url": "http://127.0.0.1:4723", "save_to_env": True,
        },
    )
    assert res.status_code == 200, res.text
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert env.startswith("# мой локальный конфиг\nMODE=ui_vision\nPLATFORM=vk_dating\n"), "порядок и комментарий .env должны выжить"
    assert "DEVICE_DRIVER=appium" in env and "DEVICE_UDID=R58M12" in env
    assert "APPIUM_URL=http://127.0.0.1:4723" in env
    assert "DRY_RUN=true" in env and f"LLM_API_KEY={SECRET}" in env
    assert "APP_PACKAGE=" not in env and "WEB_TARGET_URL=" not in env
    assert res.json()["env_saved"].endswith(".env")


def test_har_import_writes_declarative_profile(panel: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    def entry(method: str, path: str, body: str) -> dict[str, Any]:
        return {
            "request": {
                "method": method,
                "url": f"https://api.mamba.ru{path}",
                "headers": [{"name": "Authorization", "value": "Bearer zzz"}] if method == "GET" else [],
            },
            "response": {"status": 200, "content": {"mimeType": "application/json", "text": body}},
        }

    har = {
        "log": {
            "entries": [
                entry("GET", "/v1/feed?limit=20", '{"items": [{"id": "u1"}]}'),
                entry("POST", "/v1/swipe/like", '{"ok": true}'),
                entry("POST", "/v1/swipe/pass", '{"ok": true}'),
            ]
        }
    }
    res = panel.client.post(
        "/api/v1/connection/har-import",
        files={"file": ("dump.har", json.dumps(har).encode("utf-8"), "application/json")},
        data={"platform": "mamba", "host": ""},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["apply"] == {"mode": "api", "platform": "mamba", "driver": "rest_api"}
    assert body["har_profiles"] == ["mamba"]
    profile_file = Path(body["profile_path"])
    assert profile_file.name == "mamba.json"
    assert profile_file.parent.as_posix().endswith("data/api_profiles")
    profile = json.loads(profile_file.read_text(encoding="utf-8"))
    assert profile["platform"] == "mamba" and profile["base_url"] == "https://api.mamba.ru"
    assert profile["feed"]["path"] == "/v1/feed" and profile["like"]["path"] == "/v1/swipe/like"
    assert profile["pass_action"]["path"] == "/v1/swipe/pass"
    assert panel.client.get("/api/v1/connection").json()["api"]["har_profiles"] == ["mamba"]


def test_har_import_rejects_unknown_platform_empty_and_junk(panel: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    known = {
        "неизвестная платформа": ("crushio", b'{"log":{"entries":[]}}', 404),
        "пустой файл": ("mamba", b"", 400),
        "мусор вместо HAR": ("mamba", b"not a har at all", None),
    }
    for label, (platform, payload, expected) in known.items():
        res = panel.client.post(
            "/api/v1/connection/har-import",
            files={"file": ("a.har", payload, "application/json")},
            data={"platform": platform},
        )
        if expected is not None:
            assert res.status_code == expected, f"{label}: {res.status_code} {res.text[:120]}"
        else:
            assert res.status_code in (400, 502), label
    assert panel.client.get("/api/v1/health").json()["status"] == "ok"
    assert not (tmp_path / "data" / "api_profiles").exists()


# ============================= В: ключи и шлюзы =============================


def test_gateway_never_doubles_an_auth_scheme() -> None:
    for already_schemed in ("Bearer sk-1", "Token sk-1", "Api-Key sk-1", "apikey sk-1", "Basic sk-1", "Key sk-1"):
        gw = UniversalAIGateway(provider="openai", base_url="https://x/v1", api_key=already_schemed, model="m")
        assert gw._build_headers()["Authorization"] == already_schemed
        assert gw.auth_scheme() == "как задан в ключе"


def test_gateway_supports_custom_header_raw_prefix_and_query_key() -> None:
    custom = UniversalAIGateway(
        provider="custom", base_url="https://x/v1", api_key="abc", model="m", custom_header="X-llama-api-key"
    )
    headers = custom._build_headers()
    assert headers["X-llama-api-key"] == "abc" and "Authorization" not in headers
    assert custom.auth_scheme() == "header: X-llama-api-key"

    raw = UniversalAIGateway(provider="openai", base_url="https://x/v1", api_key="abc", model="m", auth_prefix="")
    assert raw._build_headers()["Authorization"] == "abc"
    assert raw.auth_scheme() == "Authorization (raw)"

    token_prefixed = UniversalAIGateway(
        provider="openai", base_url="https://x/v1", api_key="abc", model="m", auth_prefix="Token "
    )
    assert token_prefixed._build_headers()["Authorization"] == "Token abc"
    assert token_prefixed.auth_scheme() == "Authorization: Token"

    queried = UniversalAIGateway(
        provider="custom", base_url="https://x/v1", api_key="abc", model="m", api_key_query_param="key"
    )
    assert "Authorization" not in queried._build_headers()
    assert queried._with_query_key("https://x/v1/chat/completions") == "https://x/v1/chat/completions?key=abc"
    assert queried._with_query_key("https://x/v1/models?alt=1") == "https://x/v1/models?alt=1&key=abc"
    assert queried.auth_scheme() == "query: key"


def test_gateway_is_keyless_for_local_providers() -> None:
    by_name = {preset["provider"]: preset for preset in list_provider_presets()}
    for provider in ("ollama", "vllm", "lmstudio"):
        gw = UniversalAIGateway(provider=provider, api_key="", model="m")
        assert gw._build_headers() == {"Content-Type": "application/json"}
        assert gw.auth_scheme() == "none"
        assert gw.describe()["keyless_ok"] is True
        assert by_name[provider]["requires_key"] is False
    assert by_name["groq"]["base_url"] == "https://api.groq.com/openai/v1"
    assert by_name["groq"]["requires_key"] is True


def test_mask_api_key_hides_the_secret_but_keeps_the_tail() -> None:
    assert mask_api_key(SECRET) == "*" * 12 + "et-9977"
    assert mask_api_key(GROQ_KEY) == "*" * 14 + "fghijkl"
    assert mask_api_key("short") == "*****"
    assert mask_api_key("") == "" and mask_api_key("sk-dummy") == ""
    described = UniversalAIGateway(provider="groq", api_key="gsk_SUPERSECRETVALUE123", model="m").describe()
    assert "SUPERSECRET" not in json.dumps(described)
    assert described["api_key_masked"] == "*" * 16 + "ALUE123"


def test_ai_config_get_returns_masked_key_and_every_preset(panel: Harness) -> None:
    body = panel.client.get("/api/v1/ai/config").json()
    assert SECRET not in json.dumps(body)
    assert body["current"]["api_key_masked"] == "*" * 12 + "et-9977"
    assert body["current"]["key_configured"] is True
    assert body["current"]["auth"] == "Authorization: Bearer"
    assert body["gateways_in_sync"] is True
    assert body["gateway_paths"] == [
        "worker.vision_parser", "worker.synthetic_scorer", "chat_agent.engine", "visual_chat.vision_parser",
    ]
    presets = {p["provider"]: p for p in body["presets"]}
    assert presets["groq"]["base_url"] == "https://api.groq.com/openai/v1"
    assert presets["ollama"]["requires_key"] is False
    assert {"anthropic", "gemini", "openrouter", "deepseek", "mistral", "together", "xai", "custom"} <= set(presets)


def test_ai_config_patch_hot_swaps_every_live_gateway(panel: Harness) -> None:
    res = panel.client.patch("/api/v1/ai/config", json={"provider": "groq", "api_key": GROQ_KEY})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["rewired"] == [
        "worker.vision_parser", "worker.synthetic_scorer", "chat_agent.engine", "visual_chat.vision_parser",
    ]
    assert body["config"]["provider"] == "groq"
    assert body["config"]["base_url"] == "https://api.groq.com/openai/v1"
    assert body["config"]["model"] == PROVIDER_PRESETS["groq"][1]
    assert body["config"]["auth"] == "Authorization: Bearer"
    assert body["config"]["api_key_masked"] == "*" * 14 + "fghijkl"
    assert GROQ_KEY not in json.dumps(body)
    for name, gw in panel.gateways.items():
        assert (gw.provider, gw.base_url, gw.model) == ("groq", "https://api.groq.com/openai/v1", PROVIDER_PRESETS["groq"][1]), name
        assert gw.api_key == GROQ_KEY, name
    assert settings.ai_provider == "groq"
    assert settings.llm_api_key.get_secret_value() == GROQ_KEY
    assert panel.client.get("/api/v1/ai/config").json()["gateways_in_sync"] is True


def test_ai_config_patch_accepts_arbitrary_key_shapes_and_overrides(panel: Harness) -> None:
    res = panel.client.patch(
        "/api/v1/ai/config",
        json={
            "provider": "custom",
            "base_url": "https://gateway.example.com/v1/",
            "model": "mixtral-8x7b",
            "api_key": "RAWKEY123456",
            "custom_auth_header": "X-Access-Token",
            "auth_prefix": "",
            "api_key_query_param": "api_key",
        },
    )
    assert res.status_code == 200, res.text
    cfg = res.json()["config"]
    assert cfg["base_url"] == "https://gateway.example.com/v1"
    assert cfg["auth"] == "header: X-Access-Token"
    assert cfg["auth_prefix"] == ""
    gw = panel.gateways["worker.vision_parser"]
    assert gw._build_headers()["X-Access-Token"] == "RAWKEY123456"
    assert gw._with_query_key("https://gateway.example.com/v1/chat/completions").endswith("api_key=RAWKEY123456")

    # «Не прислали поле» != «прислали пусто»: пустой префикс сохраняется, пока его не переопределили.
    again = panel.client.patch("/api/v1/ai/config", json={"model": "mixtral-8x22b"}).json()
    assert again["config"]["auth"] == "header: X-Access-Token"
    assert again["config"]["model"] == "mixtral-8x22b"
    assert again["config"]["auth_prefix"] == ""


def test_ai_config_patch_keeps_key_unless_explicitly_sent(panel: Harness) -> None:
    panel.client.patch("/api/v1/ai/config", json={"provider": "deepseek", "api_key": "sk-keepme-1234567890"})
    body = panel.client.patch("/api/v1/ai/config", json={"model": "deepseek-reasoner"}).json()
    assert body["config"]["model"] == "deepseek-reasoner"
    assert panel.gateways["chat_agent.engine"].api_key == "sk-keepme-1234567890"
    assert body["config"]["api_key_masked"] == "*" * 13 + "4567890"

    cleared = panel.client.patch("/api/v1/ai/config", json={"api_key": ""}).json()
    assert cleared["config"]["key_configured"] is False
    assert cleared["config"]["api_key_masked"] == ""
    assert all(gw.api_key == "" for gw in panel.gateways.values())


def test_ai_config_patch_requires_urls_for_unknown_provider(panel: Harness) -> None:
    res = panel.client.patch("/api/v1/ai/config", json={"provider": "yandexgpt"})
    assert res.status_code == 400 and "yandexgpt" in res.json()["detail"]
    ok = panel.client.patch(
        "/api/v1/ai/config",
        json={
            "provider": "yandexgpt",
            "base_url": "https://llm.api.cloud.yandex.net/foundationModels/v1",
            "model": "yandexgpt",
        },
    )
    assert ok.status_code == 200
    assert ok.json()["config"]["provider"] == "yandexgpt"
    assert panel.gateways["visual_chat.vision_parser"].base_url.endswith("/foundationModels/v1")


def test_ai_config_patch_saves_to_env_with_the_key(panel: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("MODE=ui_vision\nAI_PROVIDER=openai\nLLM_MODEL=gpt-4o-mini\nDRY_RUN=true\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    res = panel.client.patch(
        "/api/v1/ai/config",
        json={"provider": "groq", "api_key": GROQ_KEY, "auth_prefix": "Bearer ", "save_to_env": True},
    )
    assert res.status_code == 200, res.text
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "AI_PROVIDER=groq" in env and f"LLM_API_KEY={GROQ_KEY}" in env
    assert "LLM_BASE_URL=https://api.groq.com/openai/v1" in env
    assert "LLM_AUTH_PREFIX=Bearer " in env and "LLM_API_KEY_QUERY_PARAM=\n" in env
    assert "MODE=ui_vision" in env and "DRY_RUN=true" in env
    assert res.json()["env_saved"].endswith(".env")


def test_ai_config_patch_can_run_a_live_check_after_applying(panel: Harness) -> None:
    panel.gateways["worker.vision_parser"].health_check = AsyncMock(return_value=(True, "groq ответил OK"))
    res = panel.client.patch("/api/v1/ai/config", json={"provider": "groq", "api_key": "gsk_x", "test_after": True})
    assert res.status_code == 200
    assert res.json()["test"] == {"ok": True, "message": "groq ответил OK"}
    panel.gateways["worker.vision_parser"].health_check.assert_awaited_once()


def test_ai_test_endpoint_uses_the_live_gateway(panel: Harness) -> None:
    panel.gateways["worker.vision_parser"].health_check = AsyncMock(return_value=(True, "живой шлюз ответил OK"))
    res = panel.client.post("/api/v1/ai/test")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True and body["message"] == "живой шлюз ответил OK"
    assert body["config"]["provider"] == "openai" and body["config"]["api_key_masked"] == "*" * 12 + "et-9977"
    assert SECRET not in json.dumps(body)
    panel.gateways["worker.vision_parser"].health_check.assert_awaited_once()


class _MockLLMServer:
    """Настоящий loopback-сервер OpenAI-совместимого API: проверяет пресет целиком, без внешнего ключа."""

    def __init__(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0))
                outer.seen.append({"path": self.path, "headers": dict(self.headers), "payload": body})
                reply = json.dumps(
                    {"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.seen: list[dict[str, Any]] = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/openai/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def mock_llm(monkeypatch: pytest.MonkeyPatch) -> Any:
    # Запрос идёт на loopback, поэтому на время теста убираем переменные прокси:
    # httpx читает окружение при создании клиента, а системный NO_PROXY вида `[::1]` он не разбирает.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    server = _MockLLMServer()
    yield server
    server.close()


def test_groq_preset_answers_through_a_real_http_round_trip(
    panel: Harness, mock_llm: _MockLLMServer
) -> None:
    """Пресет groq + ключ вида `gsk_...`: панель переключает шлюзы, а запрос реально уходит в сеть."""
    res = panel.client.patch(
        "/api/v1/ai/config",
        json={"provider": "groq", "base_url": mock_llm.base_url, "api_key": GROQ_KEY, "test_after": True},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["test"]["ok"] is True and "OK" in body["test"]["message"]
    assert body["config"]["model"] == PROVIDER_PRESETS["groq"][1]

    assert len(mock_llm.seen) == 1
    call = mock_llm.seen[0]
    assert call["path"] == "/openai/v1/chat/completions"
    assert call["headers"]["Authorization"] == f"Bearer {GROQ_KEY}"
    assert call["payload"]["model"] == PROVIDER_PRESETS["groq"][1]
    assert GROQ_KEY not in json.dumps(call["payload"])


def test_connection_and_ai_config_routes_are_token_protected(panel: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "web_api_token", SecretStr("panel-token"))
    calls = [
        panel.client.get("/api/v1/connection"),
        panel.client.post("/api/v1/connection", json={"mode": "api", "platform": "mamba"}),
        panel.client.get("/api/v1/ai/config"),
        panel.client.patch("/api/v1/ai/config", json={"provider": "groq"}),
    ]
    assert [c.status_code for c in calls] == [401, 401, 401, 401]
    allowed = panel.client.get("/api/v1/connection", headers={"X-API-Token": "panel-token"})
    assert allowed.status_code == 200 and allowed.json()["mode"] == "ui_vision"
    assert panel.client.get("/api/v1/ai/config?token=panel-token").status_code == 200


def test_dashboard_and_landing_expose_the_new_controls() -> None:
    root = Path(__file__).resolve().parent.parent
    dash = (root / "web/static/dashboard.html").read_text(encoding="utf-8")
    for route in ("/api/v1/connection", "/api/v1/connection/har-import", "/api/v1/ai/config"):
        assert f"'{route}'" in dash, f"панель не вызывает {route}"
    for control_id in ("connMode", "connDriver", "connPlatform", "connHarFile", "aiProvider", "aiKey", "aiPrefix"):
        assert f'id="{control_id}"' in dash, f"в панели нет элемента {control_id}"
    assert "escapeHtml(" in dash


def test_upsert_env_values_creates_and_flattens(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    upsert_env_values({"A": "1", "B": "multi\r\nline"}, path=target)
    assert target.read_text(encoding="utf-8") == "A=1\nB=multiline\n"
    upsert_env_values({"A": "2", "C": "3"}, path=target)
    assert target.read_text(encoding="utf-8") == "A=2\nB=multiline\nC=3\n"
