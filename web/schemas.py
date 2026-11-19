from pydantic import BaseModel


class WorkerStatusResponse(BaseModel):
    account_id: str
    platform: str
    region: str
    is_running: bool
    today_likes: int
    daily_limit: int
    assigned_proxy: str | None
