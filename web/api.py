import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Response, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import SecretStr

from ai.calendar_tool import book_dating_event, list_busy_slots
from ai.chat_agent import AutonomousChatAgent
from ai.universal_llm import PROVIDER_PRESETS, UniversalAIGateway, list_provider_presets
from config import settings, upsert_env_values
from core.adapters_registry import GLOBAL_DATING_REGISTRY, resolve_platform_defaults
from core.backup_service import DatabaseBackupService
from core.factory import create_dating_client, list_verified_api_profiles
from core.ui_driver import MobileUIDriverFallback
from core.visual_chat_worker import VisualChatAssistant
from core.proxy.manager import GeoProxyManager
from core.worker import SwipeWorker
from database.repository import DatabaseManager
from filters.engine import ProfileFilterEngine
from tools.har_importer import export_api_profile_from_har
from web.events import event_bus
from web.schemas import (
    AIConfigPatchRequest,
    ApplyConnectionRequest,
    BanditRewardRequest,
    BookCalendarRequest,
    FilterUpdateRequest,
    ReplyPreviewRequest,
    SwitchPlatformRequest,
    WorkerStatusResponse,
)

STATIC_DIR = Path(__file__).parent / "static"
logger = logging.getLogger(__name__)

# Какие драйверы имеет смысл предлагать панель для каждого режима (первый = значение по умолчанию).
MODE_DRIVERS: dict[str, tuple[str, ...]] = {
    "ui_vision": ("adb", "appium"),
    "web_vision": ("playwright",),
    "api": ("rest_api",),
}

LIVE_GATEWAY_PATHS: tuple[str, ...] = (
    "worker.vision_parser",
    "worker.synthetic_scorer",
    "chat_agent.engine",
    "visual_chat.vision_parser",
)


def _api_token_from_settings(platform: str) -> str:
    """Токен для MODE=api из уже загруженного .env — чтобы перезапуск панели не требовал ввода ключа платформы."""
    if platform == "twinby" and settings.twinby_access_token:
        return settings.twinby_access_token.get_secret_value()
    return settings.api_platform_token.get_secret_value() if settings.api_platform_token else ""


def _expected_api_token() -> str:
    if settings.web_api_token:
        return settings.web_api_token.get_secret_value().strip()
    return ""


_HTTP_CODE_RE = re.compile(r"\b([45]\d{2})\b")


def safe_error_suffix(exc: BaseException) -> str:
    """Кусок ошибки, который можно показать в HTTP-ответе: тип исключения и код ответа провайдера.

    ADB/Appium/Playwright и httpx кладут в текст абсолютные пути профиля браузера и URL с токенами,
    поэтому наружу идёт только классификация, а полный traceback пишется в лог.
    """
    name = type(exc).__name__
    code = _HTTP_CODE_RE.search(str(exc))
    return f"{name}, код {code.group(1)}" if code else name


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"})


def cors_origins() -> list[str]:
    """Origin'ы для CORSMiddleware: только явно перечисленные в WEB_CORS_ORIGINS, wildcard не поддерживается."""
    return [origin.strip() for origin in settings.web_cors_origins.split(",") if origin.strip()]


def guard_public_bind(host: str) -> None:
    """Панель на незакрытом интерфейсе без токена отдаёт чужие анкеты любому в сети."""
    if host.strip() in LOOPBACK_HOSTS or _expected_api_token():
        return
    raise RuntimeError(
        f"WEB_HOST={host!r} — не loopback, а WEB_API_TOKEN пуст. "
        "Задайте WEB_API_TOKEN в .env или верните WEB_HOST=127.0.0.1."
    )


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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins(),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    agent = chat_agent or AutonomousChatAgent()
    vchat = visual_chat or VisualChatAssistant(ui_driver=worker.ui_driver, db=db, chat_agent=agent, platform=worker.platform, user_preferences=worker.user_preferences)
    auth = [Depends(_verify_api_token)]

    def _live_gateways() -> dict[str, UniversalAIGateway]:
        """Живые шлюзы ИИ текущей сборки: путь -> экземпляр. PATCH /ai/config обязан обновить каждый."""
        holders = (worker.vision_parser, worker.synthetic_scorer, agent.engine, vchat.vision_parser)
        seen: set[int] = set()
        found: dict[str, UniversalAIGateway] = {}
        for path_name, holder in zip(LIVE_GATEWAY_PATHS, holders):
            gw = getattr(holder, "gateway", None)
            if isinstance(gw, UniversalAIGateway) and id(gw) not in seen:
                seen.add(id(gw))
                found[path_name] = gw
        return found

    def _active_driver_name() -> str:
        if worker.client is not None:
            return "rest_api"
        if worker.ui_driver is not None:
            return worker.ui_driver.backend
        return "none"

    async def _close_quietly(drv: MobileUIDriverFallback | None, client: Any | None) -> None:
        """Обрываем старое подключение; ошибка здесь не должна отменять уже успешное переключение."""
        if drv is not None:
            try:
                await drv.disconnect()
            except Exception as exc:
                logger.warning("[Connection] Не удалось корректно закрыть визуальный драйвер: %s", exc)
        if client is not None:
            try:
                await client.__aexit__(None, None, None)
            except Exception as exc:
                logger.warning("[Connection] Не удалось корректно закрыть API-клиент: %s", exc)

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
            logger.warning("Визуальный шаг не выполнен", exc_info=True)
            raise HTTPException(status_code=502, detail=f"Ошибка визуального шага: {safe_error_suffix(exc)}") from exc

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
        gateway = next(iter(_live_gateways().values()), None) or UniversalAIGateway()
        ok, msg = await gateway.health_check()
        return {"ok": ok, "message": msg, "config": gateway.describe()}

    @app.get("/api/v1/ai/config", dependencies=auth)
    async def get_ai_config() -> dict[str, Any]:
        gateways = _live_gateways()
        if not gateways:
            raise HTTPException(status_code=503, detail="Живой шлюз ИИ не найден: воркер собран без VLM-компонентов.")
        signatures = {(gw.provider, gw.base_url, gw.model) for gw in gateways.values()}
        return {
            "current": next(iter(gateways.values())).describe(),
            "gateways_in_sync": len(signatures) == 1,
            "gateway_paths": list(gateways),
            "presets": list_provider_presets(),
        }

    @app.patch("/api/v1/ai/config", dependencies=auth)
    async def patch_ai_config(payload: AIConfigPatchRequest) -> dict[str, Any]:
        gateways = _live_gateways()
        if not gateways:
            raise HTTPException(status_code=503, detail="Живой шлюз ИИ не найден: воркер собран без VLM-компонентов.")
        current = next(iter(gateways.values()))
        provided = payload.model_fields_set

        provider = (payload.provider or current.provider).strip().lower()
        if provider not in PROVIDER_PRESETS and not (payload.base_url and payload.model):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Провайдер «{provider}» не среди пресетов ({', '.join(PROVIDER_PRESETS)}) — "
                    "пришлите вместе с ним base_url и model."
                ),
            )
        preset_url, preset_model = PROVIDER_PRESETS.get(provider, (current.base_url, current.model))
        switched = provider != current.provider
        base_url = (payload.base_url or (preset_url if switched else current.base_url)).strip().rstrip("/")
        model = (payload.model or (preset_model if switched else current.model)).strip()
        api_key = current.api_key if "api_key" not in provided else (payload.api_key or "").strip()
        custom_header = current.custom_header if "custom_auth_header" not in provided else (payload.custom_auth_header or "").strip()
        auth_prefix = current.auth_prefix if "auth_prefix" not in provided else (payload.auth_prefix or "")
        query_param = current.api_key_query_param if "api_key_query_param" not in provided else (payload.api_key_query_param or None)

        for gw in gateways.values():
            gw.provider, gw.base_url, gw.model = provider, base_url, model
            gw.api_key, gw.custom_header = api_key, custom_header
            gw.auth_prefix, gw.api_key_query_param = auth_prefix, query_param

        # Единая точка истины для всего процесса: компоненты, созданные позже (бот, рестарт воркера),
        # должны увидеть тот же конфиг, что только что выставила панель.
        settings.ai_provider = provider
        settings.llm_base_url = base_url
        settings.llm_model = model
        settings.llm_api_key = SecretStr(api_key)
        settings.custom_auth_header = custom_header or None
        settings.llm_auth_prefix = auth_prefix if "auth_prefix" in provided else settings.llm_auth_prefix
        settings.llm_api_key_query_param = query_param

        env_saved: str | None = None
        if payload.save_to_env:
            env_saved = str(
                upsert_env_values(
                    {
                        "AI_PROVIDER": provider,
                        "LLM_BASE_URL": base_url,
                        "LLM_MODEL": model,
                        "LLM_API_KEY": api_key,
                        "CUSTOM_AUTH_HEADER": custom_header,
                        "LLM_AUTH_PREFIX": auth_prefix,
                        "LLM_API_KEY_QUERY_PARAM": query_param or "",
                    }
                )
            )

        result: dict[str, Any] = {
            "ok": True,
            "config": current.describe(),
            "rewired": list(gateways),
            "env_saved": env_saved,
        }
        if payload.test_after:
            ok, msg = await current.health_check()
            result["test"] = {"ok": ok, "message": msg}
        return result

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
            logger.warning("LLM не ответил", exc_info=True)
            raise HTTPException(status_code=502, detail=f"Ошибка LLM-ответа: {safe_error_suffix(exc)}") from exc

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
            logger.warning("Чат не прочитан", exc_info=True)
            raise HTTPException(status_code=502, detail=f"Ошибка чтения чата: {safe_error_suffix(exc)}") from exc

    @app.post("/api/v1/visual-chat/send/{draft_id}", dependencies=auth)
    async def visual_chat_send(draft_id: str) -> dict[str, Any]:
        try:
            return await vchat.send_draft(draft_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Черновик {draft_id} не найден") from exc
        except Exception as exc:
            logger.warning("Сообщение в чат не отправлено", exc_info=True)
            raise HTTPException(status_code=502, detail=f"Ошибка отправки в чат: {safe_error_suffix(exc)}") from exc


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
                logger.warning("Платформа %s не открылась на устройстве", plat, exc_info=True)
                raise HTTPException(
                    status_code=502,
                    detail=f"Не удалось открыть {plat} на устройстве: {safe_error_suffix(exc)}",
                ) from exc
        return {"platform": plat, "package": pkg, "web_url": web_url, "launched": payload.launch_on_device}

    @app.get("/api/v1/connection", dependencies=auth)
    async def get_connection() -> dict[str, Any]:
        spec = GLOBAL_DATING_REGISTRY.get(worker.platform)
        drv = worker.ui_driver
        return {
            "mode": worker.mode,
            "platform": worker.platform,
            "platform_name": spec.name if spec else worker.platform,
            "region": spec.region if spec else "",
            "driver": _active_driver_name(),
            "connected": worker.client is not None or drv is not None,
            "worker_running": worker.is_running,
            "visual": {
                "device_udid": drv.device_udid if drv else settings.device_udid,
                "device_os": drv.device_os if drv else settings.device_os,
                "platform_package": drv.platform_package if drv else settings.app_package,
                "appium_url": drv.appium_url if drv else settings.appium_url,
                "web_target_url": drv.web_target_url if drv else settings.web_target_url,
                "web_headless": drv.web_headless if drv else settings.web_headless,
            },
            "api": {
                "client_active": worker.client is not None,
                "har_profiles": list_verified_api_profiles(),
                "registry_base_url": spec.api_base_url if spec else "",
                "token_from_env": bool(_api_token_from_settings(worker.platform)),
            },
            "options": {
                "modes": [{"mode": m, "drivers": list(d)} for m, d in MODE_DRIVERS.items()],
                "platforms": [
                    {
                        "id": s.id,
                        "name": s.name,
                        "region": s.region,
                        "android_package": s.android_package,
                        "ios_bundle_id": s.ios_bundle_id,
                        "web_url": s.web_url,
                        "api_base_url": s.api_base_url,
                    }
                    for s in GLOBAL_DATING_REGISTRY.values()
                ],
            },
        }

    @app.post("/api/v1/connection", dependencies=auth)
    async def apply_connection(payload: ApplyConnectionRequest) -> dict[str, Any]:
        mode = payload.mode.strip().lower()
        plat = payload.platform.strip().lower()
        if mode not in MODE_DRIVERS:
            raise HTTPException(
                status_code=400,
                detail=f"Неизвестный режим «{mode}». Доступны: {', '.join(MODE_DRIVERS)}.",
            )
        if plat not in GLOBAL_DATING_REGISTRY:
            raise HTTPException(status_code=404, detail=f"Неизвестная платформа: {plat}")
        driver = (payload.driver or MODE_DRIVERS[mode][0]).strip().lower()
        if driver not in MODE_DRIVERS[mode]:
            raise HTTPException(
                status_code=400,
                detail=f"Драйвер «{driver}» недоступен в режиме «{mode}». Доступны: {', '.join(MODE_DRIVERS[mode])}.",
            )

        was_running = worker.is_running
        if was_running:
            await worker.stop()

        default_pkg, default_url = resolve_platform_defaults(plat, payload.device_os)
        pkg = (payload.app_package or "").strip() or default_pkg
        web_url = (payload.web_target_url or "").strip() or default_url
        old_driver, old_client = worker.ui_driver, worker.client
        new_driver: MobileUIDriverFallback | None = None
        new_client: Any | None = None
        # Новое подключение поднимается ДО разрыва старого: иначе сорванная кнопка оставила бы панель без связи.
        try:
            if mode == "api":
                new_client = create_dating_client(
                    platform=plat,
                    session_id=f"panel_{plat}",
                    credentials={"token": payload.api_token or _api_token_from_settings(plat)},
                    proxy_manager=proxy_manager,
                )
                await new_client.__aenter__()
            else:
                new_driver = MobileUIDriverFallback(
                    device_udid=payload.device_udid.strip(),
                    platform_package=pkg,
                    appium_url=(payload.appium_url or settings.appium_url).strip(),
                    backend="playwright" if mode == "web_vision" else driver,
                    device_os=payload.device_os,
                    platform_id=plat,
                    web_target_url=web_url,
                    web_profile_dir=settings.web_profile_dir,
                    web_headless=settings.web_headless if payload.web_headless is None else payload.web_headless,
                )
                await new_driver.connect()
                if payload.launch:
                    await new_driver.switch_platform(plat, pkg, web_url)
        except Exception as exc:
            await _close_quietly(new_driver, new_client)
            logger.warning("Подключение %s/%s для %s не удалось", mode, driver, plat, exc_info=True)
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Не удалось подключить {mode}/{driver} для «{plat}»: {safe_error_suffix(exc)}. "
                    "Прежнее подключение сохранено."
                ),
            ) from exc

        await _close_quietly(old_driver, old_client)
        worker.mode, worker.platform = mode, plat
        worker.ui_driver, worker.client = new_driver, new_client
        vchat.ui_driver, vchat.platform = new_driver, plat

        env_saved: str | None = None
        if payload.save_to_env:
            env = {"MODE": mode, "PLATFORM": plat}
            if new_driver is not None:
                env["DEVICE_DRIVER"] = "playwright" if mode == "web_vision" else driver
                env["DEVICE_OS"] = payload.device_os
                env["DEVICE_UDID"] = payload.device_udid.strip()
                if driver == "appium":
                    env["APPIUM_URL"] = new_driver.appium_url
                if mode == "web_vision":
                    env["WEB_HEADLESS"] = "true" if new_driver.web_headless else "false"
            if (payload.app_package or "").strip():
                env["APP_PACKAGE"] = pkg
            if (payload.web_target_url or "").strip():
                env["WEB_TARGET_URL"] = web_url
            env_saved = str(upsert_env_values(env))

        return {
            "ok": True,
            "mode": mode,
            "platform": plat,
            "driver": driver,
            "package": pkg,
            "web_url": web_url,
            "connected": True,
            "worker_was_running": was_running,
            "env_saved": env_saved,
        }

    @app.post("/api/v1/connection/har-import", dependencies=auth)
    async def import_connection_har(
        file: UploadFile = File(...),
        platform: str = Form(...),
        host: str = Form(""),
    ) -> dict[str, Any]:
        plat = platform.strip().lower()
        if plat not in GLOBAL_DATING_REGISTRY:
            raise HTTPException(status_code=404, detail=f"Неизвестная платформа: {plat}")
        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=400, detail="Пустой .har файл.")
        fd, tmp_name = tempfile.mkstemp(suffix=".har")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
            profile_path = export_api_profile_from_har(tmp_name, platform=plat, host_filter=host.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"HAR не подошёл: {exc}") from exc
        except Exception as exc:
            logger.warning("HAR не разобран", exc_info=True)
            raise HTTPException(status_code=502, detail=f"Не удалось разобрать HAR: {safe_error_suffix(exc)}") from exc
        finally:
            Path(tmp_name).unlink(missing_ok=True)
        return {
            "ok": True,
            "platform": plat,
            "profile_path": str(profile_path),
            "har_profiles": list_verified_api_profiles(),
            "apply": {"mode": "api", "platform": plat, "driver": "rest_api"},
        }


    @app.get("/api/v1/calendar/slots", dependencies=auth)
    async def calendar_slots(days_ahead: int = 3) -> dict[str, Any]:
        try:
            return {"slots": await list_busy_slots(days_ahead=days_ahead)}
        except Exception as exc:
            logger.warning("Не удалось получить слоты календаря", exc_info=True)
            raise HTTPException(
                status_code=502,
                detail="Не удалось получить слоты календаря",
            ) from exc

    @app.post("/api/v1/calendar/book", dependencies=auth)
    async def calendar_book(payload: BookCalendarRequest) -> dict[str, Any]:
        try:
            res = await book_dating_event(payload.slot_iso_string, payload.venue_name, payload.girl_name)
            return {"result": res}
        except Exception as exc:
            logger.warning("Не удалось забронировать слот календаря", exc_info=True)
            raise HTTPException(
                status_code=502,
                detail="Не удалось забронировать слот календаря",
            ) from exc

    return app
