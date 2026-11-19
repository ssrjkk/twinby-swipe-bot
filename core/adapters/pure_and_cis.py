from typing import Any
from curl_cffi.requests import AsyncSession
from core.base_client import BaseDatingClient, CandidateProfile


class PureClient(BaseDatingClient):
    """Адаптер для анонимного дейтинга Pure (без процентов совместимости, с TTL-объявлениями)."""

    platform_name = "pure"

    def __init__(self, device_token: str, proxy_url: str | None = None) -> None:
        self.device_token = device_token
        self.proxy_url = proxy_url
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> "PureClient":
        self._session = AsyncSession(
            impersonate="safari17_2_ios",
            proxy=self.proxy_url,
            headers={
                "X-Device-Token": self.device_token,
                "User-Agent": "Pure/4.34.0 (iPhone; iOS 18.1; Scale/3.00)",
                "Accept": "application/json",
            },
            timeout=15,
        )
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._session:
            await self._session.close()

    async def get_recommendations(self, limit: int = 20) -> list[CandidateProfile]:
        assert self._session is not None
        resp = await self._session.get(
            "https://pure.dating/api/v2/feed", params={"limit": limit}
        )
        resp.raise_for_status()
        ads = resp.json().get("items", [])
        return [
            CandidateProfile(
                platform=self.platform_name,
                user_id=str(ad["id"]),
                name=ad.get("title") or "Аноним",
                age=ad.get("age", 21),
                bio=ad.get("text", ""),
                distance_km=ad.get("distance_km", 0.0),
                compatibility_score=0,  # В Pure нет процентов — вычислит SyntheticCompatibilityScorer
                photo_urls=[p["url"] for p in ad.get("photos", []) if "url" in p],
                tags=ad.get("kinks", []),
                is_verified=ad.get("is_verified", False),
            )
            for ad in ads
        ]

    async def swipe(self, target_user_id: str, is_like: bool) -> dict[str, Any]:
        assert self._session is not None
        action = "like" if is_like else "pass"
        resp = await self._session.post(
            f"https://pure.dating/api/v2/ads/{target_user_id}/{action}"
        )
        resp.raise_for_status()
        return {"status": "ok", "is_match": bool(resp.json().get("is_matched", False))}

    async def get_matches_with_unread(self) -> list[dict[str, Any]]:
        assert self._session is not None
        resp = await self._session.get(
            "https://pure.dating/api/v2/chats", params={"unread": "true"}
        )
        resp.raise_for_status()
        return resp.json().get("items", [])

    async def get_chat_history(
        self, match_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        assert self._session is not None
        resp = await self._session.get(
            f"https://pure.dating/api/v2/chats/{match_id}/messages",
            params={"limit": limit},
        )
        resp.raise_for_status()
        return resp.json().get("items", [])

    async def send_message(self, match_id: str, text: str) -> dict[str, Any]:
        assert self._session is not None
        resp = await self._session.post(
            f"https://pure.dating/api/v2/chats/{match_id}/messages", json={"text": text}
        )
        resp.raise_for_status()
        return resp.json()


class TaborClient(BaseDatingClient):
    """Адаптер для российской платформы Табор (tabor.ru)."""

    platform_name = "tabor"

    def __init__(self, session_token: str, proxy_url: str | None = None) -> None:
        self.session_token = session_token
        self.proxy_url = proxy_url
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> "TaborClient":
        self._session = AsyncSession(
            impersonate="chrome120",
            proxy=self.proxy_url,
            headers={
                "Authorization": f"Bearer {self.session_token}",
                "Accept": "application/json",
            },
            timeout=15,
        )
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._session:
            await self._session.close()

    async def get_recommendations(self, limit: int = 20) -> list[CandidateProfile]:
        assert self._session is not None
        resp = await self._session.get(
            "https://tabor.ru/api/v1/encounters", params={"limit": limit}
        )
        resp.raise_for_status()
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
                photo_urls=[p["url"] for p in u.get("photos", []) if "url" in p],
                is_verified=u.get("is_verified", False),
            )
            for u in users
        ]

    async def swipe(self, target_user_id: str, is_like: bool) -> dict[str, Any]:
        assert self._session is not None
        action = "like" if is_like else "skip"
        resp = await self._session.post(
            f"https://tabor.ru/api/v1/encounters/{target_user_id}/{action}"
        )
        resp.raise_for_status()
        return {"status": "ok", "is_match": bool(resp.json().get("is_mutual", False))}

    async def get_matches_with_unread(self) -> list[dict[str, Any]]:
        assert self._session is not None
        resp = await self._session.get(
            "https://tabor.ru/api/v1/dialogs", params={"unread": 1}
        )
        resp.raise_for_status()
        return resp.json().get("data", [])

    async def get_chat_history(
        self, match_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        assert self._session is not None
        resp = await self._session.get(
            f"https://tabor.ru/api/v1/dialogs/{match_id}/messages",
            params={"limit": limit},
        )
        resp.raise_for_status()
        return resp.json().get("data", [])

    async def send_message(self, match_id: str, text: str) -> dict[str, Any]:
        assert self._session is not None
        resp = await self._session.post(
            f"https://tabor.ru/api/v1/dialogs/{match_id}/messages", json={"text": text}
        )
        resp.raise_for_status()
        return resp.json()
