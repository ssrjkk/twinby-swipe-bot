"""Полные интеграционные pytest-тесты:
- Real SQLite DatabaseManager (CRUD, duplicate safety, stats, dialogue threads)
- FastAPI Control Panel (all endpoints, live platform switch, calendar, filters)
- Security: WEB_API_TOKEN 401/200 on REST, screenshot ?token=, WebSocket /ws/live policy violation
- Telegram Bot: AdminOnlyMiddleware, /start, /step, /chatstep, /reply, /slots, /book, /backup, callbacks, HTML escaping
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from ai.bandits import PromptThompsonBandit
from ai.chat_agent import AutonomousChatAgent
from bot.telegram import (
    AdminOnlyMiddleware,
    cb_draft_regen,
    cb_draft_send,
    cb_draft_skip,
    cb_start,
    cb_stats,
    cb_step,
    cb_stop,
    cb_toggle_dry,
    cb_vchat_send,
    cb_vchat_skip,
    cmd_backup,
    cmd_book,
    cmd_chatstep,
    cmd_reply,
    cmd_slots,
    cmd_start,
    cmd_step,
    send_dry_run_preview,
    send_match_notification,
)
from config import settings
from core.backup_service import DatabaseBackupService
from core.base_client import CandidateProfile
from core.chat_worker import ChatWorker, PendingDraft
from core.proxy.manager import GeoProxyManager
from core.visual_chat_worker import VisualChatAssistant
from core.worker import SwipeWorker
from database.models import SwipeDecision
from database.repository import DatabaseManager
from filters.engine import ProfileFilterEngine
from web.api import create_web_app


@pytest.mark.asyncio
async def test_sqlite_repository_full_lifecycle() -> None:
    db = DatabaseManager(db_url="sqlite+aiosqlite:///:memory:")
    await db.init_db()

    prof = CandidateProfile(
        platform="twinby",
        user_id="u_db_1",
        name="Соня",
        age=24,
        bio="Спешелти кофе и джаз",
        distance_km=2.1,
        compatibility_score=91,
        photo_urls=["http://img/1.jpg"],
    )
    assert await db.is_already_swiped("u_db_1") is False
    await db.save_swipe(prof, SwipeDecision.LIKE, "Score 91", 91.0, is_match=True, is_dry_run=False, visual_hooks=["кофе"])
    assert await db.is_already_swiped("u_db_1") is True

    # Duplicate insert must NOT crash
    await db.save_swipe(prof, SwipeDecision.LIKE, "Dup", 91.0, is_match=True, is_dry_run=False)

    stats = await db.get_stats()
    assert stats["total_processed"] == 1
    assert stats["total_likes"] == 1
    assert stats["total_matches"] == 1
    assert stats["match_rate"] == 100.0
    assert len(await db.get_recent_swipes(10)) == 1
    assert len(await db.get_recent_matches(10)) == 1

    # Dialogue threads CRUD
    th = await db.get_or_create_thread("twinby:соня", "Соня", platform="twinby", profile_context="кофе")
    await db.append_message(th.id, "user", "Привет!")
    await db.append_message(th.id, "assistant", "привет)")
    await db.update_thread_meta(th.id, stage="rapport", bandit_arm="bold_tease")

    hist = await db.get_thread_history(th.id)
    assert len(hist) == 2 and hist[0]["role"] == "user" and hist[1]["role"] == "assistant"
    assert len(await db.list_threads()) == 1
    assert await db.delete_thread(th.id) is True
    assert len(await db.list_threads()) == 0
    await db.close()


def test_fastapi_control_panel_endpoints_and_token_security(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = DatabaseManager(db_url=f"sqlite+aiosqlite:///{tmp_path / 'api_test.db'}")
    asyncio.run(db.init_db())

    mock_gw = AsyncMock()
    mock_gw.complete_chat = AsyncMock(return_value="Короткий стиль")
    mock_gw.complete_json = AsyncMock(
        side_effect=[
            {"stage": "icebreaker", "tactic": "кофе", "draft_reply": "привет!", "should_handoff": False, "handoff_summary": ""},
            {"approved": True, "critique": "", "messages_chunks": ["привет!"]},
        ]
    )
    agent = AutonomousChatAgent(gateway=mock_gw, bandit=PromptThompsonBandit(redis_client=None, state_file=str(tmp_path / "b.json")))
    fe = ProfileFilterEngine()
    worker = SwipeWorker(db=db, filter_engine=fe, mode="ui_vision", platform="twinby", dry_run=True)
    worker.last_screenshot_bytes = b"\x89PNG\r\n\x1a\nfakebytes"

    app = create_web_app(worker=worker, proxy_manager=GeoProxyManager([]), db=db, filter_engine=fe, chat_agent=agent)
    client = TestClient(app)

    # 1. Open mode (no WEB_API_TOKEN & isolated calendar env)
    monkeypatch.setattr(settings, "web_api_token", None)
    monkeypatch.delenv("GOOGLE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("GOOGLE_REFRESH_TOKEN", raising=False)
    assert client.get("/").status_code == 200
    assert client.get("/dashboard").status_code == 200
    assert client.get("/api/v1/health").json()["version"] == "3.9.0"
    assert len(client.get("/api/v1/platforms").json()) == 28
    assert client.get("/api/v1/status").status_code == 200
    assert client.get("/api/v1/worker/last-screenshot").status_code == 200

    # Live platform switch (valid + invalid 404)
    sw = client.post("/api/v1/worker/platform", json={"platform": "pure", "launch_on_device": False})
    assert sw.status_code == 200 and sw.json()["platform"] == "pure"
    assert client.post("/api/v1/worker/platform", json={"platform": "nonexistent_app"}).status_code == 404

    # Live filter update
    assert client.patch("/api/v1/filters", json={"min_compatibility": 80, "user_preferences": "кино"}).status_code == 200
    assert client.get("/api/v1/overview").json()["filters"]["min_compatibility"] == 80

    # Dialogue preview + memory + bandit reward + deletion
    rep = client.post("/api/v1/ai/reply", json={"thread_name": "Соня", "message": "Привет!"})
    assert rep.status_code == 200
    tid = rep.json()["thread_id"]
    arm = rep.json()["bandit_arm"]
    assert arm in PromptThompsonBandit.DEFAULT_ARMS
    assert len(client.get(f"/api/v1/dialogues/{tid}").json()) == 2
    assert client.post("/api/v1/bandits/reward", json={"arm": arm, "success": True}).status_code == 200
    assert client.get("/api/v1/bandits").json()[arm]["alpha"] == 2.0
    assert client.delete(f"/api/v1/dialogues/{tid}").json()["deleted"] is True

    # Calendar endpoints
    cal = client.get("/api/v1/calendar/slots")
    assert cal.status_code == 200 and "не настроен" in cal.json()["slots"]
    book = client.post("/api/v1/calendar/book", json={"slot_iso_string": "2026-11-21T19:00:00", "venue_name": "Surf", "girl_name": "Соня"})
    assert book.status_code == 200 and "не настроен" in book.json()["result"]

    # 2. Protected mode (WEB_API_TOKEN enabled!)
    monkeypatch.setattr(settings, "web_api_token", SecretStr("super-secret-token"))
    # Public routes stay open
    assert client.get("/api/v1/health").status_code == 200
    # Protected routes MUST reject missing or wrong token with 401
    assert client.get("/api/v1/overview").status_code == 401
    assert client.get("/api/v1/overview", headers={"X-API-Token": "wrong"}).status_code == 401
    assert client.get("/api/v1/worker/last-screenshot").status_code == 401
    # Valid X-API-Token header succeeds
    assert client.get("/api/v1/overview", headers={"X-API-Token": "super-secret-token"}).status_code == 200
    # Valid ?token= query param allows <img src="/api/v1/worker/last-screenshot?token=...">!
    assert client.get("/api/v1/worker/last-screenshot?token=super-secret-token").status_code == 200

    # WebSocket MUST reject empty or wrong token, and accept valid token
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/live"):
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/live?token=wrong"):
            pass
    with client.websocket_connect("/ws/live?token=super-secret-token") as ws:
        assert ws is not None
    asyncio.run(db.close())


@pytest.mark.asyncio
async def test_telegram_middleware_commands_callbacks_and_html_escaping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tg_admin_id", 777)
    monkeypatch.delenv("GOOGLE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("GOOGLE_REFRESH_TOKEN", raising=False)
    db = DatabaseManager(db_url=f"sqlite+aiosqlite:///{tmp_path / 'tg.db'}")
    await db.init_db()

    # 1. AdminOnlyMiddleware
    mw = AdminOnlyMiddleware()
    called = False
    async def _handler(event, data):
        nonlocal called
        called = True
        return "ok"

    assert await mw(_handler, SimpleNamespace(), {"event_from_user": SimpleNamespace(id=999)}) is None
    assert called is False
    assert await mw(_handler, SimpleNamespace(), {"event_from_user": SimpleNamespace(id=777)}) == "ok"
    assert called is True

    # 2. Commands & Callbacks with <3 / <script> HTML escaping check
    sent_texts: list[str] = []
    class _Msg:
        def __init__(self, text: str = ""):
            self.text = text
            self.reply_markup = None
        async def answer(self, text: str, **kw):
            sent_texts.append(text)
        async def edit_text(self, text: str, **kw):
            sent_texts.append(text)
        async def answer_document(self, doc, caption: str = "", **kw):
            sent_texts.append(caption)

    class _Cb:
        def __init__(self, data: str):
            self.data = data
            self.message = _Msg()
        async def answer(self, text: str = "", **kw):
            sent_texts.append(text)

    fe = ProfileFilterEngine()
    worker = SwipeWorker(db=db, filter_engine=fe, mode="ui_vision", platform="twinby", dry_run=True)
    worker.process_single_visual_screen = AsyncMock(return_value={"name": "Аня <3", "decision": "like"})

    await cmd_start(_Msg("/start"), worker)
    await cmd_step(_Msg("/step"), worker)
    assert any("&lt;3" in t for t in sent_texts)

    # /slots and /book
    await cmd_slots(_Msg("/slots"))
    await cmd_book(_Msg("/book bad"))
    await cmd_book(_Msg("/book 2026-11-21T19:00:00 | Surf | Соня"))
    assert any("не настроен" in t for t in sent_texts)

    # /backup
    bsvc = DatabaseBackupService(bot=None, db_path=str(tmp_path / "tg.db"))
    await cmd_backup(_Msg("/backup"), bsvc)
    assert any("Ручной бэкап" in t for t in sent_texts)

    # /reply with persistent memory + HTML escaping
    mock_gw = AsyncMock()
    mock_gw.complete_chat = AsyncMock(return_value="Кратко")
    mock_gw.complete_json = AsyncMock(
        side_effect=[
            {"stage": "rapport", "tactic": "кофе", "draft_reply": "привет <3", "should_handoff": False, "handoff_summary": ""},
            {"approved": True, "critique": "", "messages_chunks": ["привет <3"]},
        ]
    )
    agent = AutonomousChatAgent(gateway=mock_gw, bandit=PromptThompsonBandit(redis_client=None, state_file=str(tmp_path / "b.json")))
    await cmd_reply(_Msg("/reply Соня <script> | Привет!"), db, agent)
    assert any("&lt;script&gt;" in t and "&lt;3" in t for t in sent_texts)

    # Callbacks: start, stop, toggle dry_run, step, stats
    await cb_start(_Cb("worker:start"), worker)
    assert worker.is_running is True
    await cb_stop(_Cb("worker:stop"), worker)
    assert worker.is_running is False
    await cb_toggle_dry(_Cb("worker:toggle_dry_run"), worker)
    assert worker.dry_run is False
    await cb_step(_Cb("worker:step"), worker)
    await cb_stats(_Cb("stats:show"), db, worker)

    # Draft callbacks & VisualChat callbacks
    cw = ChatWorker(client=AsyncMock(), bot=AsyncMock(), chat_agent=agent, auto_send=False)
    cw.pending_drafts["d1"] = PendingDraft("d1", "m1", "Оля", "bio", [], ["ок"], "rapport")
    await cb_draft_send(_Cb("draft:send:d1"), cw)
    assert "d1" not in cw.pending_drafts
    mock_gw.complete_json = AsyncMock(
        side_effect=[
            {"stage": "rapport", "tactic": "шутка", "draft_reply": "новый черновик", "should_handoff": False, "handoff_summary": ""},
            {"approved": True, "critique": "", "messages_chunks": ["новый черновик"]},
        ]
    )
    cw.pending_drafts["d2"] = PendingDraft("d2", "m2", "Оля", "bio", [{"role": "user", "content": "привет"}], ["ок"], "rapport")
    await cb_draft_regen(_Cb("draft:regen:d2"), cw)
    assert cw.pending_drafts["d2"].chunks == ["новый черновик"]
    await cb_draft_skip(_Cb("draft:skip:d2"), cw)
    assert "d2" not in cw.pending_drafts

    vchat = VisualChatAssistant(ui_driver=AsyncMock(), db=db, chat_agent=agent, platform="twinby")
    vchat.capture_and_draft = AsyncMock(return_value={"ok": True, "draft_id": "v1", "girl_name": "Лера <3", "stage": "rapport", "chunks": ["привет"]})
    vchat.send_draft = AsyncMock(return_value={"sent": True, "girl_name": "Лера <3", "chunks": ["привет"]})
    await cmd_chatstep(_Msg("/chatstep"), vchat)
    await cb_vchat_send(_Cb("vchat:send:v1"), vchat)
    await cb_vchat_skip(_Cb("vchat:skip:v2"), vchat)

    # Match & dry-run preview notifications escape HTML
    fake_bot = AsyncMock()
    prof = CandidateProfile(platform="twinby", user_id="p1", name="Лера <3", age=23, bio="a & b", photo_urls=["u"])
    await send_match_notification(fake_bot, prof, 92.0, "a < b")
    await send_dry_run_preview(fake_bot, prof, SwipeDecision.LIKE, 92.0, "a < b", dry_run=True)
    assert fake_bot.send_message.await_count == 2
    for call in fake_bot.send_message.await_args_list:
        txt = call.kwargs["text"]
        assert "&lt;3" in txt and "a &lt; b" in txt
    await db.close()
