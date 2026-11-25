"""Проверка собранного пакета: запускать из каталога ВНЕ репозитория после `pip install .`.

Editable-установка прячет два класса поломок — необъявленные package-data и чтение файлов по относительным
путям. Здесь же видно, доезжают ли страницы панели до site-packages и поднимается ли конфигурация без .env.
"""
import sys
import tempfile
from importlib import import_module
from pathlib import Path


def check_imports() -> list[str]:
    problems: list[str] = []
    for name in ("config", "cli", "main", "web.api", "bot.telegram", "core.factory", "filters.engine", "ai.universal_llm"):
        try:
            import_module(name)
        except Exception as exc:  # пакеты обязаны импортироваться молча, без устройства и сети
            problems.append(f"import {name}: {type(exc).__name__}: {exc}")
    return problems


def check_static_pages() -> list[str]:
    """index.html и dashboard.html — часть пакета web, а не соседний с репозиторием каталог."""
    import web

    static = Path(web.__file__).parent / "static"
    problems: list[str] = []
    for name in ("index.html", "dashboard.html"):
        page = static / name
        if not page.exists():
            problems.append(f"{page} не попал в установленный пакет (tool.setuptools.package-data)")
            continue
        text = page.read_text(encoding="utf-8")
        if "#F6F2EC" not in text:
            problems.append(f"{name}: в installed-странице нет инвариантного фона #F6F2EC")
        if 'class="bubble bubble-1"' not in text:
            problems.append(f"{name}: в installed-странице потерялся фирменный bubble-слой")
    return problems


def check_settings_without_env_file() -> list[str]:
    """Настраиваемость по умолчанию: из чужого каталога .env нет, а панель обязана собираться."""
    from core.proxy.manager import GeoProxyManager
    from core.worker import SwipeWorker
    from database.repository import DatabaseManager
    from filters.engine import ProfileFilterEngine
    from web.api import cors_origins, create_web_app, guard_public_bind

    problems: list[str] = []
    try:
        guard_public_bind("127.0.0.1")
    except Exception as exc:
        problems.append(f"guard_public_bind(127.0.0.1) поднял {type(exc).__name__}: {exc}")
    try:
        guard_public_bind("0.0.0.0")  # без токена открытого bind'а быть не должно
        problems.append("guard_public_bind('0.0.0.0') не защитил панель без WEB_API_TOKEN")
    except RuntimeError:
        pass
    try:
        db = DatabaseManager()
        worker = SwipeWorker(db=db, filter_engine=ProfileFilterEngine(), mode="ui_vision", platform="twinby", dry_run=True)
        app = create_web_app(worker=worker, proxy_manager=GeoProxyManager(), db=db, filter_engine=ProfileFilterEngine())
        paths = {getattr(route, "path", "") for route in app.routes}
        for required in ("/api/v1/status", "/api/v1/connection", "/api/v1/ai/config"):
            if required not in paths:
                problems.append(f"в установленном пакете нет маршрута {required}")
    except Exception as exc:
        problems.append(f"create_web_app: {type(exc).__name__}: {exc}")
    if "*" in cors_origins():
        problems.append("WEB_CORS_ORIGINS содержит wildcard")
    if not cors_origins():
        problems.append("WEB_CORS_ORIGINS пуст — панель не пустит даже loopback-дашборд")
    return problems


def main() -> int:
    cwd = Path(tempfile.mkdtemp(prefix="heart3-pack-"))
    import os

    os.chdir(cwd)  # ничего не должно читаться из дерева репозитория
    problems = check_imports() + check_static_pages() + check_settings_without_env_file()
    for line in problems:
        print(f"FAIL {line}", file=sys.stderr)
    if problems:
        return 1
    print("OK: пакет самодостаточен вне дерева репозитория")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
