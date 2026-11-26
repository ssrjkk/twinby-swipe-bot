import os
import sys
from pathlib import Path
from typing import Literal
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def configure_utf8_console() -> None:
    """Point stdout/stderr at UTF-8 so the Russian copy and emoji the CLI and log lines carry survive a
    Windows console on the ANSI codepage (cp1251 on ru-RU cannot encode e.g. U+2705 and raises)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def write_private_text(path: str | Path, text: str) -> Path:
    target = Path(path)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.chmod(target, 0o600)
    return target


def upsert_env_values(values: dict[str, str], path: str | Path = ".env") -> Path:
    """Point-update `KEY=VALUE` lines in .env, leaving every other line (and its order) alone.

    The Control Panel's «Сохранить в .env» buttons go through here, so a change made in the browser
    survives the next restart without ever rewriting the whole file.
    """
    env_path = Path(path)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    pending = dict(values)
    for idx, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in pending and not line.lstrip().startswith("#"):
            lines[idx] = f"{key}={_one_line(pending.pop(key))}"
    lines.extend(f"{key}={_one_line(val)}" for key, val in pending.items())
    return write_private_text(env_path, "\n".join(lines) + "\n")


def _one_line(value: str) -> str:
    return str(value).replace("\r", "").replace("\n", "")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    mode: Literal["ui_vision", "web_vision", "api"] = Field(
        default="ui_vision",
        description=(
            "ui_vision — Android/iOS экран (ADB/Appium) + VLM; "
            "web_vision — браузер Playwright с сохранённой сессией + VLM; "
            "api — экспериментальные REST-клиенты (требуют сверки эндпоинтов через HAR/mitmproxy)"
        ),
    )
    platform: str = Field(default="twinby", description="Активная платформа из GLOBAL_DATING_REGISTRY")
    dry_run: bool = Field(default=True, description="Если True — анкеты разбираются, но реальный свайп не делается")
    auto_send_replies: bool = Field(default=False)
    user_preferences: str = Field(
        default="интеллект, чувство юмора, путешествия, кофе, искренность, без рекламы и скама",
    )

    device_driver: Literal["adb", "appium", "playwright"] = Field(default="adb")
    device_udid: str = Field(default="")
    device_os: Literal["Android", "iOS"] = Field(default="Android")
    appium_url: str = Field(default="http://localhost:4723")
    app_package: str = Field(default="", description="Пусто = автоподбор по PLATFORM из GLOBAL_DATING_REGISTRY")
    capture_second_view: bool = Field(
        default=False,
        description="Если True — делает скролл для второго скриншота (био + 2-е фото) перед оценкой VLM",
    )

    web_target_url: str = Field(default="", description="Пусто = автоподбор по PLATFORM из GLOBAL_DATING_REGISTRY")
    web_profile_dir: str = Field(default="./data/browser_profile")
    web_headless: bool = Field(default=False)

    twinby_base_url: str = Field(default="https://api.twinby.com/v1")
    twinby_access_token: SecretStr | None = Field(default=None)
    twinby_refresh_token: SecretStr | None = Field(default=None)
    api_platform_token: SecretStr | None = Field(default=None)
    proxy_url: str | None = None
    proxies_file: str | None = Field(default=None)

    daily_like_limit: int = Field(default=150, ge=1, le=1000)
    max_swipes_per_hour: int = Field(default=45, ge=5, le=300)
    session_burst_size: int = Field(default=25, ge=5, le=200)
    session_break_minutes_min: int = Field(default=8, ge=1, le=120)
    session_break_minutes_max: int = Field(default=20, ge=1, le=180)
    active_hours_start: int = Field(default=8, ge=0, le=23)
    active_hours_end: int = Field(default=23, ge=0, le=23)
    enforce_active_hours: bool = Field(default=False)
    min_swipe_delay: float = Field(default=2.5, ge=0.5)
    max_swipe_delay: float = Field(default=7.5, ge=1.0)

    min_compatibility: int = Field(default=70, ge=0, le=100)
    max_distance_km: int = Field(default=25, ge=1, le=500)
    min_age: int = Field(default=18, ge=18)
    max_age: int = Field(default=30, le=99)
    require_bio: bool = Field(default=False)
    min_score_threshold: float = Field(default=60.0, ge=0.0, le=100.0)
    face_check_enabled: bool = Field(default=False)
    face_similarity_threshold: float = Field(default=0.42, ge=0.1, le=0.95)

    llm_enabled: bool = Field(default=True)
    ai_provider: str = Field(default="openai")
    llm_base_url: str = Field(default="https://api.openai.com/v1")
    llm_api_key: SecretStr = Field(default=SecretStr("sk-dummy"))
    llm_model: str = Field(default="gpt-4o-mini")
    custom_auth_header: str | None = Field(default=None)
    llm_auth_prefix: str | None = Field(
        default=None,
        description="Префикс Authorization: '' = ключ без схемы, 'Token ' и т.п.; None = авто (Bearer)",
    )
    llm_api_key_query_param: str | None = Field(
        default=None,
        description="Имя query-параметра для ключа (key / api_key), если провайдер не читает заголовки",
    )

    database_url: str = Field(default="sqlite+aiosqlite:///./data/heart3.db")
    backup_interval_hours: int = Field(default=24, ge=1, le=168)
    backup_retention_count: int = Field(default=7, ge=1, le=90)
    web_host: str = Field(default="127.0.0.1")
    web_port: int = Field(default=8080, ge=1024, le=65535)
    web_api_token: SecretStr | None = Field(default=None, description="Опциональный токен защиты Web API и WebSocket (обязателен, если биндите не на loopback)")
    web_cors_origins: str = Field(
        default="http://127.0.0.1:8080,http://localhost:8080",
        description="Список origin'ов через запятую, которым разрешён кросс-доменный доступ к Web API",
    )
    metrics_port: int = Field(default=8000, ge=0, le=65535)
    tg_bot_token: SecretStr = Field(default=SecretStr("000000:dummy"))
    tg_admin_id: int = Field(default=0)
    redis_url: str | None = Field(default=None)


settings = Settings()
