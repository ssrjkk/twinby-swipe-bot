from abc import ABC, abstractmethod
from typing import Any
from pydantic import BaseModel, Field

class CandidateProfile(BaseModel):
    platform: str
    user_id: str
    name: str
    age: int
    bio: str = ""
    distance_km: float = 0.0
    compatibility_score: int = 0
    photo_urls: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    is_verified: bool = False

    @property
    def photos_count(self) -> int:
        return len(self.photo_urls)

class BaseDatingClient(ABC):
    platform_name: str = "base"

    @abstractmethod
    async def __aenter__(self) -> "BaseDatingClient": pass

    @abstractmethod
    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None: pass

    @abstractmethod
    async def get_recommendations(self, limit: int = 20) -> list[CandidateProfile]: pass

    @abstractmethod
    async def swipe(self, target_user_id: str, is_like: bool) -> dict[str, Any]: pass

    @abstractmethod
    async def get_matches_with_unread(self) -> list[dict[str, Any]]: pass

    @abstractmethod
    async def get_chat_history(self, match_id: str, limit: int = 20) -> list[dict[str, Any]]: pass

    @abstractmethod
    async def send_message(self, match_id: str, text: str) -> dict[str, Any]: pass
