import logging
from typing import Any
from curl_cffi.requests import AsyncSession
from config import settings
from core.base_client import BaseDatingClient, CandidateProfile

logger = logging.getLogger(__name__)


class RateLimitError(Exception):
    pass


class AuthExpiredError(Exception):
    pass


class TwinbyStealthClient(BaseDatingClient):
    platform_name = "twinby"

    def __init__(
        self,
        access_token: str | None = None,
        refresh_token: str | None = None,
        proxy_url: str | None = None,
    ) -> None:
        if access_token is None:
            access_token = settings.twinby_access_token.get_secret_value()
            refresh_token = (
                settings.twinby_refresh_token.get_secret_value()
                if settings.twinby_refresh_token
                else None
            )
            proxy_url = settings.proxy_url
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._proxy_url = proxy_url
        self._session: AsyncSession | None = None
        self.session_id = "default_twinby"
        self.region = "cis"

    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Platform": "ios",
            "X-App-Version": "2.15.0",
            "Accept-Language": "ru-RU,ru;q=0.9",
        }

    async def __aenter__(self) -> "TwinbyStealthClient":
        self._session = AsyncSession(
            impersonate="safari17_2_ios",
            headers=self._build_headers(),
            proxy=self._proxy_url,
            timeout=15,
        )
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._session:
            await self._session.close()

    async def _refresh_auth(self) -> None:
        if not self._refresh_token or not self._session:
            raise AuthExpiredError("Refresh token missing.")
        resp = await self._session.post(
            f"{settings.twinby_base_url}/auth/refresh",
            json={"refresh_token": self._refresh_token},
        )
        if resp.status_code != 200:
            raise AuthExpiredError(f"Refresh failed: {resp.status_code}")
        self._access_token = resp.json()["access_token"]
        self._session.headers["Authorization"] = f"Bearer {self._access_token}"

    async def _request(
        self, method: str, endpoint: str, **kwargs: Any
    ) -> dict[str, Any]:
        if not self._session:
            raise RuntimeError("Client not opened")
        url = f"{settings.twinby_base_url}{endpoint}"
        response = await self._session.request(method, url, **kwargs)
        if response.status_code == 401:
            await self._refresh_auth()
            response = await self._session.request(method, url, **kwargs)
        if response.status_code == 429:
            raise RateLimitError("Rate limit (429)")
        response.raise_for_status()
        return response.json()

    async def get_recommendations(self, limit: int = 20) -> list[CandidateProfile]:
        data = await self._request(
            "GET", "/feed/recommendations", params={"limit": limit}
        )
        profiles = []
        for item in data.get("items", []):
            photo_urls = [
                p["url"]
                for p in item.get("photos", [])
                if isinstance(p, dict) and "url" in p
            ]
            profiles.append(
                CandidateProfile(
                    platform=self.platform_name,
                    user_id=str(item["id"]),
                    name=item.get("name", "Без имени"),
                    age=item.get("age", 18),
                    bio=item.get("bio") or "",
                    distance_km=item.get("distance_km", 0.0),
                    compatibility_score=item.get("compatibility", {}).get("score", 0),
                    photo_urls=photo_urls,
                    tags=item.get("interests", []),
                    is_verified=item.get("is_verified", False),
                )
            )
        return profiles

    async def swipe(self, target_user_id: str, is_like: bool) -> dict[str, Any]:
        action = "like" if is_like else "pass"
        res = await self._request(
            "POST", f"/swipes/{action}", json={"target_user_id": target_user_id}
        )
        return {"status": "ok", "is_match": res.get("is_match", False)}

    async def get_matches_with_unread(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/matches", params={"unread_only": "true"})
        return data.get("items", [])

    async def get_chat_history(
        self, match_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        data = await self._request(
            "GET", f"/matches/{match_id}/messages", params={"limit": limit}
        )
        return data.get("items", [])

    async def send_message(self, match_id: str, text: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/matches/{match_id}/messages", json={"text": text}
        )
