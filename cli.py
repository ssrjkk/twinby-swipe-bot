import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

from ai.chat_agent import AutonomousChatAgent
from ai.universal_llm import PROVIDER_PRESETS, UniversalAIGateway
from config import configure_utf8_console, settings, write_private_text
from core.adapters_registry import GLOBAL_DATING_REGISTRY, resolve_platform_defaults
from core.factory import list_verified_api_profiles
from core.ui_driver import MobileUIDriverFallback
from core.visual_chat_worker import VisualChatAssistant
from core.worker import SwipeWorker
from database.repository import DatabaseManager
from filters.engine import ProfileFilterEngine
from tools.har_importer import export_api_profile_from_har, inspect_har

# Every subcommand prints Russian copy with emoji, so the console encoding is settled at import time:
# that covers `heart3 <cmd>` through main() and direct calls like cli._cmd_setup() alike.
configure_utf8_console()


def _resolve_visual_backend() -> str:
    return "playwright" if settings.mode == "web_vision" else settings.device_driver


def _build_visual_driver() -> MobileUIDriverFallback:
    default_pkg, default_url = resolve_platform_defaults(settings.platform, settings.device_os)
    return MobileUIDriverFallback(
        device_udid=settings.device_udid,
        platform_package=settings.app_package or default_pkg,
        appium_url=settings.appium_url,
        backend=_resolve_visual_backend(),
        device_os=settings.device_os,
        platform_id=settings.platform,
        web_target_url=settings.web_target_url or default_url,
        web_profile_dir=settings.web_profile_dir,
        web_headless=settings.web_headless,
    )


def _cmd_setup() -> int:
    """Интерактивный мастер настройки .env за 30 секунд."""
    print("=== <3 Setup Wizard ===")
    mode = input("Режим работы [ui_vision / web_vision / api] (по умолчанию ui_vision): ").strip() or "ui_vision"
    platform = input("Платформа [twinby / pure / mamba / vk_dating / tinder / hinge / ...] (twinby): ").strip() or "twinby"
    provider = input(
        "Провайдер ИИ [openai / groq / anthropic / gemini / openrouter / deepseek / mistral / "
        "together / xai / ollama / vllm / lmstudio / custom] (openai): "
    ).strip().lower() or "openai"
    def_url, def_model = PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS["openai"])
    base_url = input(f"LLM_BASE_URL ({def_url}): ").strip() or def_url
    model = input(f"LLM_MODEL ({def_model}): ").strip() or def_model
    api_key = input("LLM_API_KEY (можно оставить пустым для Ollama): ").strip()
    custom_hdr = input("CUSTOM_AUTH_HEADER (Enter = авто по провайдеру): ").strip()
    prefs = input("Критерии отбора анкет (Enter = стандартные): ").strip() or (
        "интеллект, чувство юмора, путешествия, кофе, искренность, без рекламы и скама"
    )
    tg_token = input("TG_BOT_TOKEN (опционально, Enter = без Telegram): ").strip()
    tg_admin = input("TG_ADMIN_ID (опционально, Enter = 0): ").strip() or "0"

    env_lines = [
        f"MODE={mode}",
        f"PLATFORM={platform}",
        "DRY_RUN=true",
        "AUTO_SEND_REPLIES=false",
        f"USER_PREFERENCES={prefs}",
        f"DEVICE_DRIVER={'playwright' if mode == 'web_vision' else 'adb'}",
        f"AI_PROVIDER={provider}",
        f"LLM_BASE_URL={base_url}",
        f"LLM_API_KEY={api_key}",
        f"LLM_MODEL={model}",
        f"CUSTOM_AUTH_HEADER={custom_hdr}",
        "WEB_HOST=127.0.0.1",
        "WEB_PORT=8080",
        "DATABASE_URL=sqlite+aiosqlite:///./data/heart3.db",
        f"TG_BOT_TOKEN={tg_token}",
        f"TG_ADMIN_ID={tg_admin}",
    ]
    env_path = Path(".env")
    if env_path.exists():
        backup = env_path.with_name(".env.bak")
        shutil.copyfile(env_path, backup)
        os.chmod(backup, 0o600)
        print("ℹ️  Предыдущий .env сохранён в .env.bak")
    write_private_text(env_path, "\n".join(env_lines) + "\n")
    print("✅ Файл .env создан (DRY_RUN=true для безопасного первого запуска). Проверьте командой: heart3 doctor")
    return 0


async def _cmd_test_ai() -> int:
    ok, msg = await UniversalAIGateway().health_check()
    print(("✅ " if ok else "❌ ") + msg)
    return 0 if ok else 1


async def _cmd_test_device() -> int:
    backend = _resolve_visual_backend()
    drv = _build_visual_driver()
    try:
        await drv.connect()
        shot = await drv.capture_screenshot()
        out_path = Path("./data/test_screenshot.png")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(shot)
        print(f"✅ Скриншот успешно снят ({backend}, {len(shot)} байт) -> {out_path}")
        return 0
    except Exception as exc:
        print(f"❌ Ошибка устройства ({backend}): {exc}")
        return 1
    finally:
        await drv.disconnect()
        if 'db' in locals():
            await db.close()


async def _cmd_step() -> int:
    db = DatabaseManager()
    await db.init_db()
    drv = _build_visual_driver()
    try:
        await drv.connect()
        worker = SwipeWorker(
            db=db,
            filter_engine=ProfileFilterEngine(),
            ui_driver=drv,
            mode=settings.mode if settings.mode != "api" else "ui_vision",
            platform=settings.platform,
            dry_run=settings.dry_run,
            capture_second_view=settings.capture_second_view,
            user_preferences=settings.user_preferences,
        )
        res = await worker.process_single_visual_screen()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"❌ Ошибка шага: {exc}")
        return 1
    finally:
        await drv.disconnect()
        if 'db' in locals():
            await db.close()


async def _cmd_chat_step(auto_send: bool) -> int:
    db = DatabaseManager()
    await db.init_db()
    drv = _build_visual_driver()
    try:
        await drv.connect()
        vchat = VisualChatAssistant(
            ui_driver=drv,
            db=db,
            chat_agent=AutonomousChatAgent(),
            platform=settings.platform,
            user_preferences=settings.user_preferences,
        )
        res = await vchat.capture_and_draft(auto_send=auto_send)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    except Exception as exc:
        print(f"❌ Ошибка чата: {exc}")
        return 1
    finally:
        await drv.disconnect()
        if 'db' in locals():
            await db.close()


def _cmd_platforms() -> int:
    verified = set(list_verified_api_profiles())
    print(f"=== <3 Platforms Registry ({len(GLOBAL_DATING_REGISTRY)}) ===")
    for pid, spec in GLOBAL_DATING_REGISTRY.items():
        har_mark = "HAR-API" if pid in verified else "UI/Web-Vision"
        print(f"  {pid:14s} | {spec.region:6s} | {har_mark:13s} | pkg={spec.android_package:26s} | {spec.web_url}")
    return 0


async def _cmd_doctor() -> int:
    print("=== <3 Doctor (v3.9.0) ===")
    print(f"MODE={settings.mode} | PLATFORM={settings.platform} | DRY_RUN={settings.dry_run}")
    print(f"AI_PROVIDER={settings.ai_provider} | MODEL={settings.llm_model} | BASE_URL={settings.llm_base_url}")
    print(f"VISUAL_BACKEND={_resolve_visual_backend()} | DB={settings.database_url}")
    return await _cmd_test_ai()


def main() -> None:
    parser = argparse.ArgumentParser(prog="heart3", description="<3 AI Matchmaking Agent CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="Интерактивно создать .env под ваш провайдер ИИ и режим")
    sub.add_parser("doctor", help="Проверить конфигурацию и подключение к ИИ")
    sub.add_parser("platforms", help="Показать все 28 платформ, пакеты, URL и статус HAR-профилей")
    sub.add_parser("test-ai", help="Отправить тестовый запрос в UniversalAIGateway")
    sub.add_parser("test-device", help="Проверить ADB/Appium/Playwright и сохранить тестовый скриншот")
    sub.add_parser("step", help="Выполнить 1 визуальный шаг (скриншот анкеты -> VLM -> вердикт)")
    cs_p = sub.add_parser("chat-step", help="Прочитать открытый чат с экрана и сгенерировать ответ")
    cs_p.add_argument("--send", action="store_true", help="Сразу напечатать и отправить ответ в чат")
    sub.add_parser("run", help="Запустить основной сервис (бот + воркер + Control Panel)")

    har_p = sub.add_parser("har-inspect", help="Разобрать .har дамп (mitmproxy/Proxyman) для поиска реальных API")
    har_p.add_argument("path", help="Путь к .har файлу")
    har_p.add_argument("--host", default="", help="Фильтр по домену (например twinby)")

    hare_p = sub.add_parser("har-export", help="Экспортировать .har дамп в готовый рабочий API-профиль ./data/api_profiles/<platform>.json")
    hare_p.add_argument("path", help="Путь к .har файлу")
    hare_p.add_argument("--platform", required=True, help="ID платформы (например twinby, pure, mamba)")
    hare_p.add_argument("--host", default="", help="Фильтр по домену (по умолчанию = platform)")

    args = parser.parse_args()

    if args.command == "setup":
        sys.exit(_cmd_setup())
    elif args.command == "platforms":
        sys.exit(_cmd_platforms())
    elif args.command == "doctor":
        sys.exit(asyncio.run(_cmd_doctor()))
    elif args.command == "test-ai":
        sys.exit(asyncio.run(_cmd_test_ai()))
    elif args.command == "test-device":
        sys.exit(asyncio.run(_cmd_test_device()))
    elif args.command == "step":
        sys.exit(asyncio.run(_cmd_step()))
    elif args.command == "chat-step":
        sys.exit(asyncio.run(_cmd_chat_step(auto_send=args.send)))
    elif args.command == "har-inspect":
        report = inspect_har(args.path, host_filter=args.host)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.command == "har-export":
        out = export_api_profile_from_har(args.path, platform=args.platform, host_filter=args.host)
        print(f"✅ Профиль сохранён: {out} — теперь MODE=api PLATFORM={args.platform} подхватит его автоматически.")
    elif args.command == "run":
        from main import main as run_main
        asyncio.run(run_main())


if __name__ == "__main__":
    main()
