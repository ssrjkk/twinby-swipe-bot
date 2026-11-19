"""Fail-closed configuration, real ASGI authentication, and Telegram safety."""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.types import CallbackQuery, TelegramObject, User
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from bot import telegram
from config import Settings
from core.base_client import CandidateProfile
from web import api
from web.events import RealtimeEventBus

WEB_TOKEN = "test-only-independent-web-secret"
TG_TOKEN = "123456:test-only-telegram-secret"
TWINBY_TOKEN = "test-only-twinby-secret"
REQUIRED_SETTINGS = {
    "tg_bot_token": TG_TOKEN,
    "twinby_access_token": TWINBY_TOKEN,
    "tg_admin_id": 42,
}
GOOGLE_FIELDS = (
    "google_access_token",
    "google_refresh_token",
    "google_client_id",
    "google_client_secret",
)
INVALID_AUTHORIZATION = (
    None,
    "",
    "Bearer",
    "Bearer ",
    "Bearer wrong",
    f"Basic {WEB_TOKEN}",
    f"Bearer\t{WEB_TOKEN}",
    f"Bearer  {WEB_TOKEN}",
    f"Bearer {WEB_TOKEN} extra",
    f"Bearer {TG_TOKEN}",
)


@pytest.fixture(autouse=True)
def clean_settings_environment(monkeypatch):
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)


def make_settings(**overrides):
    values = {"_env_file": None, **REQUIRED_SETTINGS, **overrides}
    return Settings(**values)


@pytest.fixture
def app_settings(monkeypatch):
    configured = make_settings(
        web_admin_token=WEB_TOKEN,
        web_allowed_origins="http://localhost:8080, https://trusted.example , ",
    )
    monkeypatch.setattr(api, "settings", configured)
    monkeypatch.setattr(telegram, "settings", configured)
    return configured


@pytest.fixture
def database():
    return SimpleNamespace(
        get_stats=AsyncMock(return_value={"total_processed": 3, "total_matches": 1}),
        get_recent_matches=AsyncMock(return_value=[]),
    )


@pytest.fixture
def event_bus(monkeypatch):
    bus = RealtimeEventBus()
    monkeypatch.setattr(api, "event_bus", bus)
    return bus


@pytest.fixture
def client(app_settings, database, event_bus):
    app = api.create_web_app(None, None, database, None)
    with TestClient(app) as test_client:
        yield test_client
    assert not event_bus._connections


def auth_headers(authorization):
    return {} if authorization is None else {"Authorization": authorization}


@pytest.mark.parametrize("field", ["tg_bot_token", "twinby_access_token"])
def test_required_tokens_cannot_be_missing(field):
    values = REQUIRED_SETTINGS.copy()
    del values[field]
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, **values)
    assert error.value.errors()[0]["loc"] == (field,)
    assert TG_TOKEN not in str(error.value)
    assert TWINBY_TOKEN not in str(error.value)


@pytest.mark.parametrize("field", ["tg_bot_token", "twinby_access_token"])
@pytest.mark.parametrize("value", ["", "   ", "\t\r\n", None])
def test_required_tokens_reject_blank_values(field, value):
    with pytest.raises(ValidationError) as error:
        make_settings(**{field: value})
    assert error.value.errors()[0]["loc"] == (field,)
    assert TG_TOKEN not in str(error.value)
    assert TWINBY_TOKEN not in str(error.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"tg_admin_id": 0},
        {"tg_admin_id": -1},
        {"min_age": 17},
        {"min_age": 100},
        {"max_age": 17},
        {"max_age": 100},
        {"min_age": 31, "max_age": 30},
        {"min_swipe_delay": 0.4},
        {"max_swipe_delay": 0.9},
        {"min_swipe_delay": 8, "max_swipe_delay": 7},
        {"min_swipe_delay": float("nan")},
        {"max_swipe_delay": float("inf")},
    ],
)
def test_settings_reject_invalid_bounds_without_printing_secrets(overrides):
    with pytest.raises(ValidationError) as error:
        make_settings(**overrides)
    for secret in (TG_TOKEN, TWINBY_TOKEN):
        assert secret not in str(error.value)
        assert secret not in repr(error.value)


@pytest.mark.parametrize("age", [18, 99])
def test_settings_allow_equal_age_and_delay_bounds(age):
    configured = make_settings(
        min_age=age, max_age=age, min_swipe_delay=1, max_swipe_delay=1
    )
    assert configured.min_age == configured.max_age == age
    assert configured.min_swipe_delay == configured.max_swipe_delay == 1


@pytest.mark.parametrize("field", ["proxy_url", "custom_auth_header"])
@pytest.mark.parametrize("value", [None, "", " \t "])
def test_empty_optional_settings_become_none(field, value):
    assert getattr(make_settings(**{field: value}), field) is None


def test_nonempty_optional_settings_are_preserved():
    configured = make_settings(
        proxy_url="http://proxy.example:8080", custom_auth_header="x-api-key"
    )
    assert configured.proxy_url == "http://proxy.example:8080"
    assert configured.custom_auth_header == "x-api-key"


def test_web_and_google_secrets_default_to_empty():
    configured = make_settings()
    for field in ("web_admin_token", *GOOGLE_FIELDS):
        value = getattr(configured, field)
        assert isinstance(value, SecretStr)
        assert value.get_secret_value() == ""


def run_dotenv_probe(tmp_path, env_file, assertions):
    # Shared fixtures disable dotenv reads; isolate real loading to a test-only cwd.
    env = {
        key: value
        for key, value in os.environ.items()
        if key.lower() not in Settings.model_fields
    }
    env.update({key.upper(): str(value) for key, value in REQUIRED_SETTINGS.items()})
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    env["PYTHON_DOTENV_DISABLED"] = "0"
    code = (
        "import sys\n"
        "from config import Settings\n"
        "from pydantic import SecretStr\n"
        "configured = Settings(_env_file=sys.argv[1])\n" + assertions
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(env_file)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_google_credentials_and_web_secret_load_from_dotenv(tmp_path):
    values = {
        field: f"test-only-{field}" for field in (*GOOGLE_FIELDS, "web_admin_token")
    }
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(f"{field.upper()}={value}" for field, value in values.items()),
        encoding="utf-8",
    )
    run_dotenv_probe(
        tmp_path,
        env_file,
        f"expected = {values!r}\n"
        "for field, value in expected.items():\n"
        "    secret = getattr(configured, field)\n"
        "    assert isinstance(secret, SecretStr)\n"
        "    assert secret.get_secret_value() == value\n"
        "    assert value not in repr(configured)\n"
        "    assert value not in configured.model_dump_json()\n",
    )


def test_env_example_keeps_web_disabled_when_required_settings_are_supplied(tmp_path):
    env_example = Path(__file__).resolve().parents[1] / ".env.example"
    run_dotenv_probe(
        tmp_path,
        env_example,
        "assert configured.web_admin_token.get_secret_value() == ''\n"
        "assert configured.proxy_url is None\n"
        "assert configured.custom_auth_header is None\n",
    )


@pytest.mark.parametrize("candidate", [None, "", " \t "])
def test_token_match_rejects_empty_candidates(app_settings, candidate):
    assert not api._token_matches(candidate)


def test_token_comparison_is_constant_time_on_utf8_bytes(app_settings, monkeypatch):
    app_settings.web_admin_token = SecretStr("ключ-для-теста")
    compare = Mock(wraps=api.hmac.compare_digest)
    monkeypatch.setattr(api.hmac, "compare_digest", compare)
    assert api._token_matches("ключ-для-теста")
    compare.assert_called_once_with(
        "ключ-для-теста".encode("utf-8"), "ключ-для-теста".encode("utf-8")
    )
    assert not api._token_matches("другой-ключ")


@pytest.mark.parametrize("path", ["/api/v1/overview", "/api/v1/matches"])
@pytest.mark.parametrize("authorization", INVALID_AUTHORIZATION)
def test_rest_requires_web_bearer_header(client, database, path, authorization):
    response = client.get(path, headers=auth_headers(authorization))
    assert response.status_code == 401
    database.get_stats.assert_not_awaited()
    database.get_recent_matches.assert_not_awaited()


@pytest.mark.parametrize("authorization", INVALID_AUTHORIZATION)
def test_websocket_requires_web_bearer_header_before_accept(
    client, event_bus, authorization
):
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect("/ws/live", headers=auth_headers(authorization)):
            pytest.fail("Unauthorized WebSocket was accepted")
    assert error.value.code == 1008
    assert not event_bus._connections


@pytest.mark.parametrize("query_token", ["bogus", WEB_TOKEN, TG_TOKEN, ""])
def test_query_tokens_never_authenticate(client, query_token):
    for path in ("/api/v1/overview", "/api/v1/matches"):
        assert client.get(path, params={"token": query_token}).status_code == 401
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(f"/ws/live?token={query_token}"):
            pytest.fail("Query token authenticated a WebSocket")
    assert error.value.code == 1008


@pytest.mark.parametrize("expected", ["", " \t "])
@pytest.mark.parametrize("candidate", ["", WEB_TOKEN])
def test_blank_web_secret_disables_rest_and_websocket(
    client, app_settings, expected, candidate
):
    app_settings.web_admin_token = SecretStr(expected)
    assert not api._token_matches(expected)
    headers = {"Authorization": f"Bearer {candidate}"}
    assert client.get("/api/v1/overview", headers=headers).status_code == 401
    assert client.get("/api/v1/matches", headers=headers).status_code == 401
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect("/ws/live", headers=headers):
            pytest.fail("Blank web secret enabled a WebSocket")
    assert error.value.code == 1008


@pytest.mark.parametrize("scheme", ["Bearer", "bEaReR"])
@pytest.mark.parametrize("suffix", ["", "?token=bogus"])
def test_correct_header_accepts_rest_and_websocket_and_cleans_up(
    client, database, event_bus, scheme, suffix
):
    headers = {"Authorization": f"{scheme} {WEB_TOKEN}"}
    response = client.get(f"/api/v1/overview{suffix}", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"stats": {"total_processed": 3, "total_matches": 1}}
    matches = client.get(f"/api/v1/matches{suffix}", headers=headers)
    assert matches.status_code == 200
    assert matches.json() == []
    database.get_stats.assert_awaited_once()
    database.get_recent_matches.assert_awaited_once()
    with client.websocket_connect(f"/ws/live{suffix}", headers=headers) as websocket:
        websocket.send_text("ready")
        client.portal.call(event_bus.publish, "test", {"ok": True})
        assert websocket.receive_json() == {"type": "test", "data": {"ok": True}}
    assert not event_bus._connections


@pytest.mark.parametrize("origin", ["https://evil.example", "null", ""])
def test_websocket_rejects_unconfigured_origin_even_with_valid_token(client, origin):
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(
            "/ws/live",
            headers={"Authorization": f"Bearer {WEB_TOKEN}", "Origin": origin},
        ):
            pytest.fail("Unconfigured Origin was accepted")
    assert error.value.code == 1008


@pytest.mark.parametrize("origin", ["http://localhost:8080", "https://trusted.example"])
def test_websocket_accepts_configured_origin(client, origin):
    with client.websocket_connect(
        "/ws/live", headers={"Authorization": f"Bearer {WEB_TOKEN}", "Origin": origin}
    ):
        pass


def test_rest_cors_retains_explicit_origin_allowlist(client):
    for origin, expected_status in (
        ("https://trusted.example", 200),
        ("https://evil.example", 400),
    ):
        response = client.options(
            "/api/v1/overview",
            headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
        )
        assert response.status_code == expected_status
        assert response.headers.get("access-control-allow-origin") == (
            origin if expected_status == 200 else None
        )


def test_websocket_cleans_up_on_unexpected_receive_error(
    client, event_bus, monkeypatch
):
    receive = AsyncMock(side_effect=RuntimeError("receive failed"))
    disconnect = Mock(wraps=event_bus.disconnect)
    monkeypatch.setattr(api.WebSocket, "receive_text", receive)
    monkeypatch.setattr(event_bus, "disconnect", disconnect)
    with pytest.raises(RuntimeError, match="receive failed"):
        with client.websocket_connect(
            "/ws/live", headers={"Authorization": f"Bearer {WEB_TOKEN}"}
        ):
            pass
    receive.assert_awaited_once()
    disconnect.assert_called_once()
    assert not event_bus._connections


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["absent", None, 7, 42])
async def test_middleware_denies_absent_or_wrong_identity(app_settings, identity):
    data = {}
    if identity != "absent":
        data["event_from_user"] = (
            User(id=identity, is_bot=False, first_name="Test")
            if identity is not None
            else None
        )
    event = TelegramObject()
    handler = AsyncMock(return_value="handled")
    result = await telegram.AdminOnlyMiddleware()(handler, event, data)
    if identity == app_settings.tg_admin_id:
        handler.assert_awaited_once_with(event, data)
        assert result == "handled"
    else:
        handler.assert_not_awaited()
        assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", [None, 7])
async def test_denied_callback_is_acknowledged(app_settings, monkeypatch, identity):
    user = User(id=7, is_bot=False, first_name="Other")
    callback = CallbackQuery(id="test", from_user=user, chat_instance="test")
    answer = AsyncMock()
    monkeypatch.setattr(CallbackQuery, "answer", answer)
    handler = AsyncMock()
    await telegram.AdminOnlyMiddleware()(
        handler, callback, {"event_from_user": user if identity is not None else None}
    )
    handler.assert_not_awaited()
    answer.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_name", ["cb_start", "cb_stop", "cb_stats"])
@pytest.mark.parametrize("edit_fails", [False, True])
async def test_callback_handlers_acknowledge_even_if_edit_fails(
    database, handler_name, edit_fails
):
    edit = AsyncMock(side_effect=RuntimeError("edit failed") if edit_fails else None)
    callback = SimpleNamespace(
        answer=AsyncMock(), message=SimpleNamespace(edit_text=edit)
    )
    worker = SimpleNamespace(is_running=False, start=Mock(), stop=AsyncMock())
    kwargs = {"worker": worker}
    if handler_name == "cb_stats":
        kwargs["db"] = database
    handler = getattr(telegram, handler_name)
    if edit_fails:
        with pytest.raises(RuntimeError, match="edit failed"):
            await handler(callback, **kwargs)
    else:
        await handler(callback, **kwargs)
    callback.answer.assert_awaited_once_with()
    edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_match_notification_escapes_profile_name(app_settings):
    profile = CandidateProfile(
        platform="twinby",
        user_id="test-user",
        name='<b>Alice & "Bob"</b>',
        age=25,
        compatibility_score=90,
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    await telegram.send_match_notification(bot, profile, 90, "test")
    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == app_settings.tg_admin_id
    assert kwargs["parse_mode"] == "HTML"
    assert "&lt;b&gt;Alice &amp; &quot;Bob&quot;&lt;/b&gt;" in kwargs["text"]
    assert profile.name not in kwargs["text"]
