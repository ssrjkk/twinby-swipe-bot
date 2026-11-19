from typing import Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    twinby_base_url: str = Field(default="https://api.twinby.com/v1")
    twinby_access_token: SecretStr
    twinby_refresh_token: SecretStr | None = None
    proxy_url: str | None = None

    daily_like_limit: int = Field(default=150, ge=1, le=1000)
    min_swipe_delay: float = Field(default=2.5, ge=0.5, allow_inf_nan=False)
    max_swipe_delay: float = Field(default=7.5, ge=1.0, allow_inf_nan=False)

    min_compatibility: int = Field(default=75, ge=0, le=100)
    max_distance_km: int = Field(default=25, ge=1, le=500)
    min_age: int = Field(default=18, ge=18, le=99)
    max_age: int = Field(default=30, ge=18, le=99)
    require_bio: bool = Field(default=False)

    ai_provider: str = Field(
        default="openai",
        description="openai, anthropic, gemini, openrouter, deepseek, ollama, custom",
    )
    llm_base_url: str = Field(default="https://api.openai.com/v1")
    llm_api_key: SecretStr = Field(default=SecretStr("sk-dummy"))
    llm_model: str = Field(default="gpt-4o-mini")
    custom_auth_header: str | None = Field(
        default=None,
        description="Кастомный заголовок для нестандартных ключей, например 'x-api-key' или 'Authorization'",
    )

    google_access_token: SecretStr = SecretStr("")
    google_refresh_token: SecretStr = SecretStr("")
    google_client_id: SecretStr = SecretStr("")
    google_client_secret: SecretStr = SecretStr("")

    database_url: str = Field(default="sqlite+aiosqlite:///./data/twinby_bot.db")
    web_host: str = "127.0.0.1"
    web_admin_token: SecretStr = SecretStr("")
    web_allowed_origins: str = Field(
        default="http://localhost:8080,http://127.0.0.1:8080"
    )
    tg_bot_token: SecretStr
    tg_admin_id: int = Field(gt=0)

    @field_validator("tg_bot_token", "twinby_access_token")
    @classmethod
    def require_nonempty_token(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("Token must not be empty or whitespace-only")
        return value

    @field_validator("proxy_url", "custom_auth_header")
    @classmethod
    def normalize_empty_optional(cls, value: str | None) -> str | None:
        return None if value is not None and not value.strip() else value

    @model_validator(mode="after")
    def validate_ordered_bounds(self) -> Self:
        if self.min_age > self.max_age:
            raise ValueError("MIN_AGE must not exceed MAX_AGE")
        if self.min_swipe_delay > self.max_swipe_delay:
            raise ValueError("MIN_SWIPE_DELAY must not exceed MAX_SWIPE_DELAY")
        return self


settings = Settings()
