from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from ai.calendar_tool import book_dating_event, list_busy_slots
from ai.chat_agent import AutonomousChatAgent
from ai.universal_llm import UniversalAIGateway
from config import settings
from core.adapters_registry import GLOBAL_DATING_REGISTRY, resolve_platform_defaults
from core.backup_service import DatabaseBackupService
from core.factory import list_verified_api_profiles
from core.visual_chat_worker import VisualChatAssistant
from core.proxy.manager import GeoProxyManager
from core.worker import SwipeWorker
from database.repository import DatabaseManager
from filters.engine import ProfileFilterEngine
from web.events import event_bus
from web.schemas import BanditRewardRequest, BookCalendarRequest, FilterUpdateRequest, ReplyPreviewRequest, SwitchPlatformRequest, WorkerStatusResponse

STATIC_DIR = Path(__file__).parent / "static"


def _expected_api_token() -> str:
    if settings.web_api_token:
        return settings.web_api_token.get_secret_value().strip()
    return ""


async def _verify_api_token(
    x_api_token: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> None:
    expected = _expected_api_token()
    supplied = x_api_token or token
    if expected and supplied != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный X-API-Token")


def create_web_app(
    worker: SwipeWorker,
    proxy_manager: GeoProxyManager,
    db: DatabaseManager,
    filter_engine: ProfileFilterEngine,
    chat_agent: AutonomousChatAgent | None = None,
    backup_service: DatabaseBackupService | None = None,
    visual_chat: VisualChatAssistant | None = None,
) -> FastAPI:
    app = FastAPI(title="<3 AI Matchmaking Control Plane", version="3.9.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    agent = chat_agent or AutonomousChatAgent()
    vchat = visual_chat or VisualChatAssistant(ui_driver=worker.ui_driver, db=db, chat_agent=agent, platform=worker.platform, user_preferences=worker.user_preferences)
    auth = [Depends(_verify_api_token)]

    @app.get("/", response_class=HTMLResponse)
    async def serve_landing() -> HTMLResponse:
        return HTMLResponse(content=(STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    @app.get("/dashboard", response_class=HTMLResponse)
    async def serve_dashboard() -> HTMLResponse:
        return HTMLResponse(content=(STATIC_DIR / "dashboard.html").read_text(encoding="utf-8"))

    @app.get("/api/v1/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": "3.9.0", "mode": worker.mode, "platform": worker.platform}

    @app.websocket("/ws/live")
    async def ws_endpoint(websocket: WebSocket, token: str = "") -> None:
        expected = _expected_api_token()
        if expected and token != expected:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        await event_bus.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            event_bus.disconnect(websocket)

    @app.get("/api/v1/status", response_model=WorkerStatusResponse, dependencies=auth)
    async def get_status() -> WorkerStatusResponse:
        today_likes = await db.get_today_likes_count()
        return WorkerStatusResponse(
            mode=worker.mode,
            platform=worker.platform,
            is_running=worker.is_running,
            dry_run=worker.dry_run,
            today_likes=today_likes,
            daily_limit=settings.daily_like_limit,
            proxies_loaded=proxy_manager.proxies_count,
        )

    @app.get("/api/v1/overview", dependencies=auth)
    async def overview() -> dict[str, Any]:
        cfg = filter_engine.config
        return {
            "stats": await db.get_stats(),
            "worker": {
                "mode": worker.mode,
                "platform": worker.platform,
                "is_running": worker.is_running,
                "dry_run": worker.dry_run,
                "user_preferences": worker.user_preferences,
                "has_screenshot": worker.last_screenshot_bytes is not None,
                "last_verdict": worker.last_verdict,
            },
            "filters": {
                "min_compatibility": cfg.min_compatibility,
                "max_distance_km": cfg.max_distance_km,
                "min_age": cfg.min_age,
                "max_age": cfg.max_age,
                "require_bio": cfg.require_bio,
                "min_score_threshold": cfg.min_score_threshold,
                "stop_words": cfg.stop_words,
                "priority_keywords": cfg.priority_keywords,
            },
        }

    @app.post("/api/v1/worker/start", dependencies=auth)
    async def start_worker() -> dict[str, Any]:
        started = worker.start()
        return {"started": started, "is_running": worker.is_running}

    @app.post("/api/v1/worker/stop", dependencies=auth)
    async def stop_worker() -> dict[str, Any]:
        stopped = await worker.stop()
        return {"stopped": stopped, "is_running": worker.is_running}

    @app.post("/api/v1/worker/dry-run", dependencies=auth)
    async def toggle_dry_run() -> dict[str, Any]:
        worker.dry_run = not worker.dry_run
        return {"dry_run": worker.dry_run}

    @app.post("/api/v1/worker/step", dependencies=auth)
    async def run_single_visual_step() -> dict[str, Any]:
        try:
            res = await worker.process_single_visual_screen()
            return {"ok": True, "result": res}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Ошибка визуального шага: {exc}") from exc

    @app.get("/api/v1/worker/last-screenshot", dependencies=auth)
    async def get_last_screenshot() -> Response:
        if not worker.last_screenshot_bytes:
            raise HTTPException(status_code=404, detail="Скриншот ещё не снимался.")
        media = "image/png" if worker.last_screenshot_bytes[:4] == b"\x89PNG" else "image/jpeg"
        return Response(content=worker.last_screenshot_bytes, media_type=media)

    @app.patch("/api/v1/filters", dependencies=auth)
    async def update_filters(payload: FilterUpdateRequest) -> dict[str, Any]:
        cfg = filter_engine.config
        if payload.min_compatibility is not None:
            cfg.min_compatibility = payload.min_compatibility
        if payload.max_distance_km is not None:
            cfg.max_distance_km = payload.max_distance_km
        if payload.min_age is not None:
            cfg.min_age = payload.min_age
        if payload.max_age is not None:
            cfg.max_age = payload.max_age
        if payload.require_bio is not None:
            cfg.require_bio = payload.require_bio
        if payload.min_score_threshold is not None:
            cfg.min_score_threshold = payload.min_score_threshold
        if payload.stop_words is not None:
            cfg.stop_words = payload.stop_words
        if payload.priority_keywords is not None:
            cfg.priority_keywords = payload.priority_keywords
        if payload.user_preferences is not None:
            worker.user_preferences = payload.user_preferences
        return {"status": "updated"}

    @app.get("/api/v1/swipes", dependencies=auth)
    async def recent_swipes(limit: int = 30) -> list[dict]:
        return await db.get_recent_swipes(limit=limit)

    @app.get("/api/v1/matches", dependencies=auth)
    async def recent_matches(limit: int = 25) -> list[dict]:
        return await db.get_recent_matches(limit=limit)

    @app.get("/api/v1/platforms", dependencies=auth)
    async def list_platforms() -> list[dict[str, Any]]:
        return [
            {
                "id": s.id,
                "name": s.name,
                "region": s.region,
                "driver": s.driver.value,
                "has_native_compatibility": s.has_native_compatibility,
                "requires_face_on_primary_photo": s.requires_face_on_primary_photo,
                "notes": s.anti_bot_notes,
            }
            for s in GLOBAL_DATING_REGISTRY.values()
        ]

    @app.post("/api/v1/ai/test", dependencies=auth)
    async def test_ai_connection() -> dict[str, Any]:
        ok, msg = await UniversalAIGateway().health_check()
        return {"ok": ok, "message": msg}

    @app.get("/api/v1/dialogues", dependencies=auth)
    async def list_dialogues() -> list[dict]:
        return await db.list_threads()

    @app.get("/api/v1/dialogues/{thread_id}", dependencies=auth)
    async def get_dialogue_messages(thread_id: int) -> list[dict[str, str]]:
        return await db.get_thread_history(thread_id, limit=50)

    @app.delete("/api/v1/dialogues/{thread_id}", dependencies=auth)
    async def delete_dialogue(thread_id: int) -> dict[str, Any]:
        deleted = await db.delete_thread(thread_id)
        return {"deleted": deleted}

    @app.post("/api/v1/ai/reply", dependencies=auth)
    async def preview_reply(payload: ReplyPreviewRequest) -> dict[str, Any]:
        try:
            tkey = f"{worker.platform}:{payload.thread_name.strip().lower()}"
            thread = await db.get_or_create_thread(
                thread_key=tkey,
                girl_name=payload.thread_name.strip(),
                platform=worker.platform,
                profile_context=payload.profile_context or worker.user_preferences,
                visual_hooks=payload.visual_hooks,
            )
            await db.append_message(thread.id, "user", payload.message)
            history = await db.get_thread_history(thread.id)

            result = await agent.generate_reply(
                history=history,
                profile_context=thread.profile_context or worker.user_preferences,
                visual_hooks=payload.visual_hooks,
            )
            for chunk in result.get("chunks", []):
                await db.append_message(thread.id, "assistant", chunk)
            await db.update_thread_meta(
                thread.id,
                stage=result.get("stage", "rapport"),
                bandit_arm=result.get("bandit_arm"),
                is_handed_off=bool(result.get("should_handoff", False)),
            )
            result["thread_id"] = thread.id
            result["history_len"] = len(history)
            return result
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Ошибка LLM-ответа: {exc}") from exc

    @app.get("/api/v1/bandits", dependencies=auth)
    async def get_bandit_stats() -> dict[str, Any]:
        return await agent.bandit.get_summary()

    @app.post("/api/v1/bandits/reward", dependencies=auth)
    async def record_bandit_reward(payload: BanditRewardRequest) -> dict[str, Any]:
        await agent.bandit.record_reward(payload.arm, payload.success)
        return {"status": "recorded", "summary": await agent.bandit.get_summary()}

    @app.post("/api/v1/backup", dependencies=auth)
    async def trigger_backup() -> dict[str, Any]:
        if not backup_service:
            raise HTTPException(status_code=400, detail="Бэкап SQLite недоступен для текущей БД.")
        path = backup_service.create_safe_backup_sync()
        return {"backup_file": str(path) if path else None}


    @app.get("/api/v1/api-profiles", dependencies=auth)
    async def get_api_profiles() -> dict[str, Any]:
        return {"verified_har_profiles": list_verified_api_profiles()}

    @app.post("/api/v1/visual-chat/capture", dependencies=auth)
    async def visual_chat_capture() -> dict[str, Any]:
        try:
            return await vchat.capture_and_draft(auto_send=False)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Ошибка чтения чата: {exc}") from exc

    @app.post("/api/v1/visual-chat/send/{draft_id}", dependencies=auth)
    async def visual_chat_send(draft_id: str) -> dict[str, Any]:
        try:
            return await vchat.send_draft(draft_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Ошибка отправки в чат: {exc}") from exc


    @app.post("/api/v1/worker/platform", dependencies=auth)
    async def switch_platform(payload: SwitchPlatformRequest) -> dict[str, Any]:
        plat = payload.platform.lower().strip()
        if plat not in GLOBAL_DATING_REGISTRY:
            raise HTTPException(status_code=404, detail=f"Неизвестная платформа: {plat}")
        worker.platform = plat
        vchat.platform = plat
        pkg, web_url = resolve_platform_defaults(plat, settings.device_os)
        if payload.launch_on_device and worker.ui_driver:
            try:
                await worker.ui_driver.switch_platform(plat, pkg, web_url)
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"Не удалось открыть {plat} на устройстве: {exc}") from exc
        return {"platform": plat, "package": pkg, "web_url": web_url, "launched": payload.launch_on_device}

    @app.get("/api/v1/calendar/slots", dependencies=auth)
    async def calendar_slots(days_ahead: int = 3) -> dict[str, Any]:
        return {"slots": await list_busy_slots(days_ahead=days_ahead)}

    @app.post("/api/v1/calendar/book", dependencies=auth)
    async def calendar_book(payload: BookCalendarRequest) -> dict[str, Any]:
        res = await book_dating_event(payload.slot_iso_string, payload.venue_name, payload.girl_name)
        return {"result": res}

    return app
