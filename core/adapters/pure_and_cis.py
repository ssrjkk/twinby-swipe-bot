from typing import Any

import httpx

from core.base_client import BaseDatingClient, CandidateProfile
from core.metrics import API_ERRORS_TOTAL

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
except ImportError:
    CurlAsyncSession = None  # type: ignore[assignment]


class _HttpSessionMixin:
    _session: Any = None
    _using_curl: bool = False

    def _open_session(self, impersonate: str, headers: dict[str, str], proxy_url: str | None) -> None:
        if CurlAsyncSession is not None:
            self._using_curl = True
            self._session = CurlAsyncSession(impersonate=impersonate, proxy=proxy_url, headers=headers, timeout=15)
        else:
            self._using_curl = False
            self._session = httpx.AsyncClient(proxy=proxy_url, headers=headers, timeout=15.0)

    async def _close_session(self) -> None:
        if self._session:
            if self._using_curl:
                await self._session.close()
            else:
                await self._session.aclose()
            self._session = None

    def _check_status(self, resp: Any) -> Any:
        if resp.status_code >= 400:
            API_ERRORS_TOTAL.labels(status_code=str(resp.status_code)).inc()
        resp.raise_for_status()
        return resp


class PureClient(_HttpSessionMixin, BaseDatingClient):
    """REST-адаптер для Pure (без % совместимости, с TTL-объявлениями)."""

    platform_name = "pure"

    def __init__(
        self,
        device_token: str,
        proxy_url: str | None = None,
        base_url: str = "https://pure.dating/api/v2",
    ) -> None:
        self.device_token = device_token
        self.proxy_url = proxy_url
        self.base_url = base_url.rstrip("/")

    async def __aenter__(self) -> "PureClient":
        self._open_session(
            impersonate="safari17_2_ios",
            proxy_url=self.proxy_url,
            headers={
                "X-Device-Token": self.device_token,
                "User-Agent": "Pure/4.34.0 (iPhone; iOS 18.1; Scale/3.00)",
                "Accept": "application/json",
            },
        )
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self._close_session()

    async def get_recommendations(self, limit: int = 20) -> list[CandidateProfile]:
        assert self._session is not None
        resp = self._check_status(await self._session.get(f"{self.base_url}/feed", params={"limit": limit}))
        ads = resp.json().get("items", [])
        return [
            CandidateProfile(
                platform=self.platform_name,
                user_id=str(ad["id"]),
                name=ad.get("title") or "Аноним",
                age=ad.get("age", 21),
                bio=ad.get("text", ""),
                distance_km=ad.get("distance_km", 0.0),
                compatibility_score=0,
                photo_urls=[p["url"] for p in ad.get("photos", []) if isinstance(p, dict) and "url" in p],
                tags=ad.get("kinks", []),
                is_verified=ad.get("is_verified", False),
            )
            for ad in ads
        ]

    async def swipe(self, target_user_id: str, is_like: bool) -> dict[str, Any]:
        assert self._session is not None
        action = "like" if is_like else "pass"
        resp = self._check_status(await self._session.post(f"{self.base_url}/ads/{target_user_id}/{action}"))
        return {"status": "ok", "is_match": bool(resp.json().get("is_matched", False))}

    async def get_matches_with_unread(self) -> list[dict[str, Any]]:
        assert self._session is not None
        resp = self._check_status(await self._session.get(f"{self.base_url}/chats", params={"unread": "true"}))
        return resp.json().get("items", [])

    async def get_chat_history(self, match_id: str, limit: int = 20) -> list[dict[str, Any]]:
        assert self._session is not None
        resp = self._check_status(await self._session.get(f"{self.base_url}/chats/{match_id}/messages", params={"limit": limit}))
        return resp.json().get("items", [])

    async def send_message(self, match_id: str, text: str) -> dict[str, Any]:
        assert self._session is not None
        resp = self._check_status(await self._session.post(f"{self.base_url}/chats/{match_id}/messages", json={"text": text}))
        return resp.json()


class TaborClient(_HttpSessionMixin, BaseDatingClient):
    """REST-адаптер для Табор (tabor.ru)."""

    platform_name = "tabor"

    def __init__(
        self,
        session_token: str,
        proxy_url: str | None = None,
        base_url: str = "https://tabor.ru/api/v1",
    ) -> None:
        self.session_token = session_token
        self.proxy_url = proxy_url
        self.base_url = base_url.rstrip("/")

    async def __aenter__(self) -> "TaborClient":
        self._open_session(
            impersonate="chrome120",
            proxy_url=self.proxy_url,
            headers={"Authorization": f"Bearer {self.session_token}", "Accept": "application/json"},
        )
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self._close_session()

    async def get_recommendations(self, limit: int = 20) -> list[CandidateProfile]:
        assert self._session is not None
        resp = self._check_status(await self._session.get(f"{self.base_url}/encounters", params={"limit": limit}))
        users = resp.json().get("data", [])
        return [
            CandidateProfile(
                platform=self.platform_name,
                user_id=str(u["id"]),
                name=u.get("name", "Пользователь"),
                age=u.get("age", 20),
                bio=u.get("about", ""),
                distance_km=u.get("distance", 0.0),
                compatibility_score=0,
                photo_urls=[p["url"] for p in u.get("photos", []) if isinstance(p, dict) and "url" in p],
                is_verified=u.get("is_verified", False),
            )
            for u in users
        ]

    async def swipe(self, target_user_id: str, is_like: bool) -> dict[str, Any]:
        assert self._session is not None
        action = "like" if is_like else "skip"
        resp = self._check_status(await self._session.post(f"{self.base_url}/encounters/{target_user_id}/{action}"))
        return {"status": "ok", "is_match": bool(resp.json().get("is_mutual", False))}

    async def get_matches_with_unread(self) -> list[dict[str, Any]]:
        assert self._session is not None
        resp = self._check_status(await self._session.get(f"{self.base_url}/dialogs", params={"unread": 1}))
        return resp.json().get("data", [])

    async def get_chat_history(self, match_id: str, limit: int = 20) -> list[dict[str, Any]]:
        assert self._session is not None
        resp = self._check_status(await self._session.get(f"{self.base_url}/dialogs/{match_id}/messages", params={"limit": limit}))
        return resp.json().get("data", [])

    async def send_message(self, match_id: str, text: str) -> dict[str, Any]:
        assert self._session is not None
        resp = self._check_status(await self._session.post(f"{self.base_url}/dialogs/{match_id}/messages", json={"text": text}))
        return resp.json()
