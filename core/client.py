import logging
from typing import Any

import httpx
from tenacity import before_sleep_log, retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config import settings
from core.base_client import BaseDatingClient, CandidateProfile
from core.metrics import API_ERRORS_TOTAL

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
except ImportError:
    CurlAsyncSession = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class RateLimitError(Exception):
    pass


class AuthExpiredError(Exception):
    pass


class TwinbyStealthClient(BaseDatingClient):
    """REST-клиент Twinby (использует curl_cffi TLS-спуфинг, если установлен, иначе httpx.AsyncClient)."""

    platform_name = "twinby"

    def __init__(
        self,
        access_token: str | None = None,
        refresh_token: str | None = None,
        base_url: str | None = None,
        proxy_url: str | None = None,
    ) -> None:
        tok = access_token or (settings.twinby_access_token.get_secret_value() if settings.twinby_access_token else "")
        if not tok:
            raise ValueError("Не задан access_token для TwinbyStealthClient (TWINBY_ACCESS_TOKEN).")
        self._access_token = tok
        self._refresh_token = refresh_token or (
            settings.twinby_refresh_token.get_secret_value() if settings.twinby_refresh_token else None
        )
        self.base_url = (base_url or settings.twinby_base_url).rstrip("/")
        self.proxy_url = proxy_url if proxy_url is not None else settings.proxy_url
        self._session: Any = None
        self._using_curl = False
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
        if CurlAsyncSession is not None:
            self._using_curl = True
            self._session = CurlAsyncSession(
                impersonate="safari17_2_ios",
                headers=self._build_headers(),
                proxy=self.proxy_url,
                timeout=15,
            )
        else:
            self._using_curl = False
            self._session = httpx.AsyncClient(
                headers=self._build_headers(),
                proxy=self.proxy_url,
                timeout=15.0,
            )
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._session:
            if self._using_curl:
                await self._session.close()
            else:
                await self._session.aclose()
            self._session = None

    async def _refresh_auth(self) -> None:
        if not self._refresh_token or not self._session:
            raise AuthExpiredError("Refresh token отсутствует.")
        resp = await self._session.post(f"{self.base_url}/auth/refresh", json={"refresh_token": self._refresh_token})
        if resp.status_code != 200:
            raise AuthExpiredError(f"Refresh failed: {resp.status_code}")
        self._access_token = resp.json()["access_token"]
        self._session.headers["Authorization"] = f"Bearer {self._access_token}"

    @retry(
        retry=retry_if_exception_type((ConnectionError, TimeoutError, httpx.TransportError, httpx.TimeoutException, RateLimitError)),
        wait=wait_exponential(multiplier=2, min=3, max=60),
        stop=stop_after_attempt(4),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        if not self._session:
            raise RuntimeError("Клиент не открыт. Используйте async with TwinbyStealthClient().")
        url = f"{self.base_url}{endpoint}"
        response = await self._session.request(method, url, **kwargs)
        if response.status_code == 401:
            await self._refresh_auth()
            response = await self._session.request(method, url, **kwargs)
        if response.status_code == 429:
            API_ERRORS_TOTAL.labels(status_code="429").inc()
            raise RateLimitError("Rate limit (429)")
        if response.status_code >= 400:
            API_ERRORS_TOTAL.labels(status_code=str(response.status_code)).inc()
        response.raise_for_status()
        return response.json()

    async def get_recommendations(self, limit: int = 20) -> list[CandidateProfile]:
        data = await self._request("GET", "/feed/recommendations", params={"limit": limit})
        profiles = []
        for item in data.get("items", []):
            photo_urls = [p["url"] for p in item.get("photos", []) if isinstance(p, dict) and "url" in p]
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
        res = await self._request("POST", f"/swipes/{action}", json={"target_user_id": target_user_id})
        return {"status": "ok", "is_match": bool(res.get("is_match", False))}

    async def get_matches_with_unread(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/matches", params={"unread_only": "true"})
        return data.get("items", [])

    async def get_chat_history(self, match_id: str, limit: int = 20) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/matches/{match_id}/messages", params={"limit": limit})
        return data.get("items", [])

    async def send_message(self, match_id: str, text: str) -> dict[str, Any]:
        return await self._request("POST", f"/matches/{match_id}/messages", json={"text": text})
