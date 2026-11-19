"""Гейты движка v3: реестр, пороги, скоринг, worker, UI-драйвер, прокси, бэкап,
web-auth, универсальный шлюз, bandits, LangGraph и календарь.

Транспорт curl_cffi подменён httpx-сессией из conftest: respx и pytest-socket
видят каждый запрос, живая сеть из тестов невозможна.
"""

import asyncio
import importlib
import json
import sqlite3
import tomllib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
from fakeredis import aioredis as fakeredis

from ai.bandits import PromptThompsonBandit
from ai.universal_llm import UniversalAIGateway
from config import settings
from core.adapters_registry import AutomationDriverType, GLOBAL_DATING_REGISTRY
from core.backup_service import DatabaseBackupService
from core.base_client import BaseDatingClient, CandidateProfile
from core.factory import create_dating_client
from core.humanizer import Humanizer
from core.proxy.manager import GeoProxyManager, Region
from core.ui_driver import MobileUIDriverFallback
from core.worker import SwipeWorker
from database.models import SwipeDecision
from filters.engine import FilterConfig, ProfileFilterEngine
from filters.synthetic_scorer import SyntheticCompatibilityScorer
from web.api import create_web_app
from web.events import RealtimeEventBus

ROOT = Path(__file__).resolve().parent.parent
# модули продукта держат ссылку на тот же глобальный asyncio: патч их .sleep
# подменяет asyncio.sleep и в самом тесте, поэтому ждём сохранённой ссылкой.
REAL_SLEEP = asyncio.sleep


def make_profile(**over) -> CandidateProfile:
    base = dict(
        platform="pure",
        user_id="u1",
        name="Аня",
        age=25,
        bio="кофе, джаз и прогулки по Питеру",
        distance_km=3.0,
        compatibility_score=90,
        photo_urls=["https://cdn.example/p1.jpg"],
    )
    base.update(over)
    return CandidateProfile(**base)


class RecordingClient(BaseDatingClient):
    platform_name = "pure"

    def __init__(self, profiles=None, is_match=True):
        self.profiles = profiles or []
        self.is_match = is_match
        self.swipes: list[tuple[str, bool]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get_recommendations(self, limit=20):
        return list(self.profiles)

    async def swipe(self, target_user_id, is_like):
        self.swipes.append((target_user_id, is_like))
        return {"status": "ok", "is_match": self.is_match}

    async def get_matches_with_unread(self):
        return []

    async def get_chat_history(self, match_id, limit=20):
        return []

    async def send_message(self, match_id, text):
        return {"status": "ok"}


# ── реестр и пороги ───────────────────────────────────────────────────────────


def test_registry_driver_split_matches_published_audit():
    counts = {t: 0 for t in AutomationDriverType}
    for spec in GLOBAL_DATING_REGISTRY.values():
        counts[spec.driver] += 1
    assert len(GLOBAL_DATING_REGISTRY) == 28
    assert counts == {
        AutomationDriverType.DIRECT_API: 10,
        AutomationDriverType.HYBRID_API_UI: 12,
        AutomationDriverType.UI_DRIVER_ONLY: 6,
    }
    ui_only = {
        k
        for k, v in GLOBAL_DATING_REGISTRY.items()
        if v.driver == AutomationDriverType.UI_DRIVER_ONLY
    }
    assert ui_only == {"hinge", "bumble", "pairs", "wippy", "vox", "feeld"}


def test_native_score_platforms_match_scorer_allowlist():
    native = {
        k for k, v in GLOBAL_DATING_REGISTRY.items() if v.has_native_compatibility
    }
    assert native == SyntheticCompatibilityScorer.PLATFORMS_WITH_NATIVE_SCORE


def test_factory_unknown_platform_raises():
    pm = GeoProxyManager([])
    with pytest.raises(ValueError, match="отсутствует"):
        create_dating_client(
            "nonexistent", session_id="s", credentials={}, proxy_manager=pm
        )


def test_threshold_is_one_product_invariant(monkeypatch):
    monkeypatch.delenv("LIKE_THRESHOLD", raising=False)
    import device.config as device_config

    importlib.reload(device_config)
    assert device_config.LIKE_THRESHOLD == 75
    assert settings.min_compatibility == 75
    assert FilterConfig().min_compatibility == 75


# ── синтетический скоринг ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scorer_fallback_is_pinned_not_silent(monkeypatch):
    scorer = SyntheticCompatibilityScorer()

    async def boom(*a, **kw):
        raise RuntimeError("llm down")

    monkeypatch.setattr(scorer.gateway, "complete_chat", boom)
    long_bio = await scorer.compute_compatibility(
        make_profile(), user_preferences="кофе"
    )
    short_bio = await scorer.compute_compatibility(
        make_profile(bio="привет"), user_preferences="кофе"
    )
    assert long_bio.vibe_score == 72
    assert short_bio.vibe_score == 45
    assert long_bio.is_bot_or_ad is False
    assert "fallback" in long_bio.reasoning.lower()


@pytest.mark.asyncio
@respx.mock
async def test_scorer_uses_native_score_without_llm():
    route = respx.post(f"{settings.llm_base_url.rstrip('/')}/chat/completions")
    scorer = SyntheticCompatibilityScorer()
    verdict = await scorer.compute_compatibility(
        make_profile(platform="twinby", compatibility_score=88), user_preferences="кофе"
    )
    assert verdict.vibe_score == 88
    assert not route.called


@pytest.mark.asyncio
async def test_worker_passes_ads_flagged_by_scorer(monkeypatch, in_memory_db):
    worker = SwipeWorker(RecordingClient(), in_memory_db, ProfileFilterEngine())

    async def flagged(*a, **kw):
        return SimpleNamespace(
            vibe_score=99,
            is_bot_or_ad=True,
            scoring_failed=False,
            reasoning="эскорт-реклама",
        )

    monkeypatch.setattr(worker.synthetic_scorer, "compute_compatibility", flagged)
    decision, score, reason = await worker._evaluate_candidate(
        make_profile(compatibility_score=0)
    )
    assert decision == SwipeDecision.PASS
    assert score == 0.0
    assert "Антиспам" in reason


# ── worker ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_worker_likes_saves_and_dedups(in_memory_db):
    llm_route = respx.post(
        f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "vibe_score": 90,
                                    "is_bot_or_ad": False,
                                    "reasoning": "кофе и джаз",
                                }
                            )
                        }
                    }
                ]
            },
        )
    )
    client = RecordingClient([make_profile()])
    worker = SwipeWorker(client, in_memory_db, ProfileFilterEngine())
    profile = make_profile()
    decision, score, reason = await worker._evaluate_candidate(profile)
    assert llm_route.called, "скоринг ушёл мимо мока, в живую сеть"
    assert decision == SwipeDecision.LIKE
    is_match = await worker._execute_swipe(profile, is_like=True)
    assert is_match is True
    assert client.swipes == [("u1", True)]
    await in_memory_db.save_swipe(profile, decision, reason, score, is_match)
    assert await in_memory_db.is_already_swiped("u1") is True
    assert await in_memory_db.get_today_likes_count() == 1


@pytest.mark.asyncio
async def test_worker_ui_only_platform_never_hits_api(in_memory_db):
    client = RecordingClient([make_profile(platform="hinge")])
    calls = []

    class UiStub:
        async def swipe_with_bezier_curve(self, is_like):
            calls.append(is_like)

    worker = SwipeWorker(
        client, in_memory_db, ProfileFilterEngine(), ui_driver=UiStub()
    )
    is_match = await worker._execute_swipe(make_profile(platform="hinge"), is_like=True)
    assert is_match is False
    assert calls == [True]
    assert client.swipes == []


@pytest.mark.asyncio
async def test_worker_empty_feed_waits_five_minutes(monkeypatch, in_memory_db):
    import core.worker as worker_module

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(worker_module.asyncio, "sleep", fake_sleep)
    worker = SwipeWorker(RecordingClient([]), in_memory_db, ProfileFilterEngine())
    worker.start()
    await REAL_SLEEP(0.05)
    await worker.stop()
    assert sleeps and sleeps[0] == 300


@pytest.mark.asyncio
async def test_worker_daily_limit_pauses_half_hour(monkeypatch, in_memory_db):
    import core.worker as worker_module

    profile = make_profile()
    await in_memory_db.save_swipe(profile, SwipeDecision.LIKE, "r", 80.0, False)
    monkeypatch.setattr(settings, "daily_like_limit", 1)
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(worker_module.asyncio, "sleep", fake_sleep)
    worker = SwipeWorker(
        RecordingClient([make_profile(user_id="u2")]),
        in_memory_db,
        ProfileFilterEngine(),
    )
    worker.start()
    await REAL_SLEEP(0.05)
    await worker.stop()
    assert sleeps and sleeps[0] == 1800


# ── humanizer и UI-драйвер ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_humanizer_delay_stays_in_bounds(monkeypatch):
    import core.humanizer as humanizer_module

    monkeypatch.setattr(humanizer_module.random, "gauss", lambda mean, std: mean)
    monkeypatch.setattr(humanizer_module.random, "uniform", lambda a, b: a)
    monkeypatch.setattr(humanizer_module.random, "random", lambda: 0.5)
    monkeypatch.setattr(humanizer_module.asyncio, "sleep", lambda s: REAL_SLEEP(0))
    h = Humanizer(2.5, 7.5)
    assert await h.sleep_between_swipes() == 5.0
    assert await h.sleep_between_swipes(has_long_bio=True) == 6.5
    monkeypatch.setattr(humanizer_module.random, "random", lambda: 0.01)
    monkeypatch.setattr(humanizer_module.random, "uniform", lambda a, b: 10.0)
    assert await h.sleep_between_swipes() == 15.0


def test_bezier_trajectory_ends_where_finger_should():
    import random

    random.seed(7)
    driver = MobileUIDriverFallback(
        device_udid="emulator-5554", platform_package="co.hinge.app"
    )
    pts = driver._generate_bezier_points(540, 1200, 950, 1180, steps=10)
    assert len(pts) == 11
    assert abs(pts[0][0] - 540) <= 2 and abs(pts[0][1] - 1200) <= 2
    assert abs(pts[-1][0] - 950) <= 2 and abs(pts[-1][1] - 1180) <= 2
    assert pts[-1][0] > pts[0][0]


@pytest.mark.asyncio
@respx.mock
async def test_ui_driver_unconnected_fails_without_network():
    driver = MobileUIDriverFallback(
        device_udid="emulator-5554", platform_package="co.hinge.app"
    )
    with pytest.raises(RuntimeError, match="not connected"):
        await driver.swipe_with_bezier_curve(is_like=True)
    assert driver._session_id is None


# ── прокси ────────────────────────────────────────────────────────────────────


def test_proxy_manager_sticky_region_and_failover():
    pm = GeoProxyManager(
        [
            {"host": "10.0.0.1", "port": 9000, "region": "cis", "fails_count": 2},
            {"host": "10.0.0.2", "port": 9000, "region": "cis", "fails_count": 0},
            {"host": "10.0.0.3", "port": 9000, "region": "us"},
        ]
    )
    first = pm.get_proxy_for_session("s1", region=Region.CIS)
    assert first == "http://10.0.0.2:9000"
    assert (
        pm.get_proxy_for_session("s1", region=Region.US) == first
    ), "sticky-сессия должна держать прокси"
    assert pm.get_proxy_for_session("s2", region=Region.US) == "http://10.0.0.3:9000"
    assert pm.get_proxy_for_session("s3", region=Region.ASIA) is None


def test_proxy_url_embeds_credentials():
    pm = GeoProxyManager([{"host": "h", "port": 1, "username": "u", "password": "p"}])
    assert pm.get_proxy_for_session("s") == "http://u:p@h:1"


# ── бэкап ─────────────────────────────────────────────────────────────────────


def test_backup_is_a_consistent_sqlite_copy(tmp_path):
    db_path = tmp_path / "twinby_bot.db"
    conn = sqlite3.connect(db_path)
    conn.execute("create table t (x int)")
    conn.execute("insert into t values (42)")
    conn.commit()
    conn.close()
    service = DatabaseBackupService(bot=object(), db_path=str(db_path))
    backup = service._create_safe_backup_sync()
    assert backup.parent == tmp_path / "backups"
    check = sqlite3.connect(backup)
    assert check.execute("select x from t").fetchone() == (42,)
    check.close()


@pytest.mark.asyncio
async def test_backup_loop_sends_file_to_admin(monkeypatch, tmp_path):
    import core.backup_service as backup_module

    db_path = tmp_path / "twinby_bot.db"
    sqlite3.connect(db_path).close()
    sent = []

    class BotStub:
        async def send_document(self, **kwargs):
            sent.append(kwargs)

    sleeps = []
    service = DatabaseBackupService(bot=BotStub(), db_path=str(db_path))
    service._is_running = True

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 1:
            service._is_running = False

    monkeypatch.setattr(backup_module.asyncio, "sleep", fake_sleep)
    await service._loop()
    assert sleeps == [60, service.interval_seconds]
    assert len(sent) == 1
    assert sent[0]["chat_id"] == settings.tg_admin_id
    assert sent[0]["document"].filename.startswith("heart3_backup_")


# ── web API ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rest_endpoints_require_admin_bearer(in_memory_db):
    app = create_web_app(
        object(), GeoProxyManager([]), in_memory_db, ProfileFilterEngine()
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/v1/overview")).status_code == 401
        assert (
            await client.get(
                "/api/v1/matches", headers={"Authorization": "Bearer wrong"}
            )
        ).status_code == 401
        ok = await client.get(
            "/api/v1/overview",
            headers={
                "Authorization": f"Bearer {settings.web_admin_token.get_secret_value()}"
            },
        )
        assert ok.status_code == 200
        assert "stats" in ok.json()


@pytest.mark.asyncio
async def test_cors_only_allows_configured_origins(in_memory_db):
    app = create_web_app(
        object(), GeoProxyManager([]), in_memory_db, ProfileFilterEngine()
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        evil = await client.options(
            "/api/v1/overview",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert "access-control-allow-origin" not in evil.headers
        good = await client.options(
            "/api/v1/overview",
            headers={
                "Origin": "http://localhost:8080",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert good.headers["access-control-allow-origin"] == "http://localhost:8080"


@pytest.mark.asyncio
async def test_websocket_rejects_wrong_token_and_cleans_up():
    from fastapi import WebSocketDisconnect

    from web.api import ws_live
    from web.events import event_bus

    class FakeSocket:
        def __init__(self, token):
            self.headers = {"authorization": f"Bearer {token}"}
            self.closed_code = None
            self.accepted = False

        async def accept(self):
            self.accepted = True

        async def close(self, code):
            self.closed_code = code

        async def receive_text(self):
            raise WebSocketDisconnect()

        async def send_text(self, msg):
            raise AssertionError("не должно отправляться в этом тесте")

    wrong = FakeSocket("wrong")
    await ws_live(wrong)
    assert wrong.closed_code == 1008
    assert wrong.accepted is False

    right = FakeSocket(settings.web_admin_token.get_secret_value())
    await ws_live(right)
    assert right.accepted is True
    assert right not in event_bus._connections


@pytest.mark.asyncio
async def test_event_bus_drops_dead_connections():
    bus = RealtimeEventBus()
    sent = []

    class Live:
        async def accept(self):
            pass

        async def send_text(self, msg):
            sent.append(json.loads(msg))

    class Dead:
        async def accept(self):
            pass

        async def send_text(self, msg):
            raise ConnectionError("gone")

    live, dead = Live(), Dead()
    await bus.connect(live)
    await bus.connect(dead)
    await bus.publish("swipe", {"user_id": "u1"})
    assert sent == [{"type": "swipe", "data": {"user_id": "u1"}}]
    assert dead not in bus._connections
    assert live in bus._connections


# ── универсальный шлюз ────────────────────────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_gateway_auth_header_matrix(monkeypatch):
    route = respx.post("https://api.test/v1/openai/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "{}"}}]}
        )
    )
    monkeypatch.setattr(settings, "llm_base_url", "https://api.test/v1/openai")
    monkeypatch.setattr(
        settings, "llm_api_key", type(settings.llm_api_key)("secret-key")
    )
    monkeypatch.setattr(settings, "custom_auth_header", None)

    monkeypatch.setattr(settings, "ai_provider", "openai")
    await UniversalAIGateway().complete_chat([{"role": "user", "content": "hi"}])
    assert route.calls.last.request.headers["authorization"] == "Bearer secret-key"

    monkeypatch.setattr(settings, "ai_provider", "gemini")
    await UniversalAIGateway().complete_chat([{"role": "user", "content": "hi"}])
    assert route.calls.last.request.headers["x-goog-api-key"] == "secret-key"

    monkeypatch.setattr(settings, "custom_auth_header", "x-my-key")
    await UniversalAIGateway().complete_chat([{"role": "user", "content": "hi"}])
    assert route.calls.last.request.headers["x-my-key"] == "secret-key"

    monkeypatch.setattr(settings, "custom_auth_header", None)
    monkeypatch.setattr(settings, "ai_provider", "openai")
    monkeypatch.setattr(
        settings, "llm_api_key", type(settings.llm_api_key)("Bearer prefixed")
    )
    await UniversalAIGateway().complete_chat([{"role": "user", "content": "hi"}])
    assert route.calls.last.request.headers["authorization"] == "Bearer prefixed"


@pytest.mark.asyncio
@respx.mock
async def test_gateway_anthropic_uses_messages_endpoint(monkeypatch):
    route = respx.post("https://api.test/v1/messages").mock(
        return_value=httpx.Response(200, json={"content": [{"text": "привет"}]})
    )
    monkeypatch.setattr(settings, "llm_base_url", "https://api.test/v1")
    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    monkeypatch.setattr(settings, "llm_api_key", type(settings.llm_api_key)("sk-ant"))
    out = await UniversalAIGateway().complete_chat([{"role": "user", "content": "hi"}])
    assert out == "привет"
    assert route.calls.last.request.headers["x-api-key"] == "sk-ant"
    assert "response_format" not in json.loads(route.calls.last.request.content)


@pytest.mark.asyncio
@respx.mock
async def test_gateway_dummy_key_sends_no_auth(monkeypatch):
    route = respx.post("https://api.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "{}"}}]}
        )
    )
    monkeypatch.setattr(settings, "llm_base_url", "https://api.test/v1")
    monkeypatch.setattr(settings, "llm_api_key", type(settings.llm_api_key)("sk-dummy"))
    await UniversalAIGateway().complete_chat([{"role": "user", "content": "hi"}])
    assert "authorization" not in route.calls.last.request.headers


# ── bandits ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bandit_rewards_update_beta_posterior():
    import numpy as np

    redis_client = fakeredis.FakeRedis()
    bandit = PromptThompsonBandit(redis_client, experiment_name="test_exp")
    np.random.seed(0)
    assert await bandit.select_arm(["A", "B"]) in ("A", "B")
    await bandit.record_reward("A", success=True)
    await bandit.record_reward("A", success=False)
    assert await redis_client.hget("bandit:test_exp:A", "alpha") == b"2"
    assert await redis_client.hget("bandit:test_exp:A", "beta") == b"2"
    for _ in range(30):
        await bandit.record_reward("A", success=True)
    np.random.seed(1)
    picks = [await bandit.select_arm(["A", "B"]) for _ in range(20)]
    assert picks.count("A") > picks.count("B")
    await redis_client.aclose()


# ── LangGraph и календарь (опциональные зависимости) ──────────────────────────


@pytest.mark.asyncio
async def test_langgraph_turn_uses_llm_and_bounds_revisions(monkeypatch):
    pytest.importorskip("langgraph")
    from ai.langgraph_agent import MAX_EDITOR_PASSES, DatingLangGraphEngine

    engine = DatingLangGraphEngine()
    replies = [
        json.dumps({"psychological_profile": "любит кофе и джаз"}),
        json.dumps(
            {
                "stage": "icebreaker",
                "tactic": "вопрос про кофе",
                "draft_reply": "Привет! Какой кофе любишь?",
                "should_handoff": False,
                "handoff_summary": "",
            }
        ),
        json.dumps(
            {
                "approved": False,
                "critique": "слишком формально",
                "messages_chunks": ["привет)"],
            }
        ),
        json.dumps(
            {
                "stage": "icebreaker",
                "tactic": "вопрос про кофе",
                "draft_reply": "привет) какой кофе любишь?",
                "should_handoff": False,
                "handoff_summary": "",
            }
        ),
        json.dumps(
            {
                "approved": True,
                "critique": "",
                "messages_chunks": ["привет)", "какой кофе любишь?"],
            }
        ),
    ]
    calls = []

    async def fake_complete(messages, temperature=0.7, json_mode=True):
        calls.append(messages[-1]["content"])
        return replies[len(calls) - 1]

    monkeypatch.setattr(engine.gateway, "complete_chat", fake_complete)
    out = await engine.run_turn(
        [{"role": "user", "content": "привет"}], profile_context="анкета"
    )
    assert out["chunks"] == ["привет)", "какой кофе любишь?"]
    assert out["stage"] == "icebreaker"
    assert len(calls) == 5, "цикл правок должен сделать ровно один возврат к стратегу"
    assert "слишком формально" in calls[3]

    strat = json.dumps(
        {
            "stage": "rapport",
            "tactic": "t",
            "draft_reply": "d",
            "should_handoff": False,
            "handoff_summary": "",
        }
    )
    reject = json.dumps(
        {"approved": False, "critique": "нет", "messages_chunks": ["d"]}
    )
    always_reject = [
        json.dumps({"psychological_profile": "p"}),
        strat,
        reject,
        strat,
        reject,
    ]
    calls2 = []

    async def reject_complete(messages, temperature=0.7, json_mode=True):
        reply = always_reject[len(calls2)]
        calls2.append(messages[-1]["content"])
        return reply

    monkeypatch.setattr(engine.gateway, "complete_chat", reject_complete)
    out2 = await engine.run_turn([], profile_context="анкета")
    assert len(calls2) == 1 + 2 * MAX_EDITOR_PASSES
    assert out2["chunks"] == [
        "d"
    ], "после исчерпания правок ответ всё равно должен уйти"


@pytest.mark.asyncio
async def test_calendar_tool_books_ninety_minute_slot(monkeypatch):
    pytest.importorskip("googleapiclient")
    from ai import calendar_tool

    inserted = {}

    class Events:
        def insert(self, calendarId, body):
            inserted["calendarId"] = calendarId
            inserted["body"] = body
            return SimpleNamespace(
                execute=lambda: {"htmlLink": "https://cal.example/e1"}
            )

    class Service:
        def events(self):
            return Events()

    monkeypatch.setattr(calendar_tool, "build", lambda *a, **kw: Service())
    out = await calendar_tool.book_dating_event_tool.ainvoke(
        {
            "slot_iso_string": "2026-11-20T19:00:00",
            "venue_name": "Кофейня",
            "girl_name": "Аня",
        }
    )
    assert "https://cal.example/e1" in out
    body = inserted["body"]
    assert inserted["calendarId"] == "primary"
    assert body["summary"] == "Свидание: Аня"
    assert body["start"]["timeZone"] == "Europe/Moscow"
    from datetime import datetime

    delta = datetime.fromisoformat(body["end"]["dateTime"]) - datetime.fromisoformat(
        body["start"]["dateTime"]
    )
    assert delta.total_seconds() == 90 * 60


# ── согласованность версий ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_version_is_consistent_across_surfaces(in_memory_db):
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    app = create_web_app(
        object(), GeoProxyManager([]), in_memory_db, ProfileFilterEngine()
    )
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert pyproject["project"]["version"] == "3.0.0"
    assert app.version == "3.0.0"
    assert app.title == "<3 — Experimental API"
    assert changelog.splitlines()[0].find("3.0.0") != -1
