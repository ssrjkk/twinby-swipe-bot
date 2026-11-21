from pydantic import BaseModel, Field


class WorkerStatusResponse(BaseModel):
    mode: str
    platform: str
    is_running: bool
    dry_run: bool
    today_likes: int
    daily_limit: int
    proxies_loaded: int


class FilterUpdateRequest(BaseModel):
    min_compatibility: int | None = Field(default=None, ge=0, le=100)
    max_distance_km: int | None = Field(default=None, ge=1, le=500)
    min_age: int | None = Field(default=None, ge=18)
    max_age: int | None = Field(default=None, le=99)
    require_bio: bool | None = None
    min_score_threshold: float | None = Field(default=None, ge=0.0, le=100.0)
    stop_words: list[str] | None = None
    priority_keywords: list[str] | None = None
    user_preferences: str | None = None


class ReplyPreviewRequest(BaseModel):
    thread_name: str = Field(default="Общий диалог")
    message: str = Field(min_length=1)
    profile_context: str = Field(default="")
    visual_hooks: list[str] = Field(default_factory=list)


class BanditRewardRequest(BaseModel):
    arm: str
    success: bool


class SwitchPlatformRequest(BaseModel):
    platform: str = Field(min_length=1)
    launch_on_device: bool = Field(default=False)


class BookCalendarRequest(BaseModel):
    slot_iso_string: str = Field(min_length=10)
    venue_name: str = Field(min_length=1)
    girl_name: str = Field(min_length=1)
