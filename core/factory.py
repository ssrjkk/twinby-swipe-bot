import logging
from pathlib import Path
from typing import Any

import httpx

from core.adapters.declarative_client import DeclarativeRestDatingClient
from core.adapters.pure_and_cis import PureClient, TaborClient
from core.adapters_registry import GLOBAL_DATING_REGISTRY, PlatformSpec
from core.base_client import BaseDatingClient, CandidateProfile
from core.client import TwinbyStealthClient
from core.metrics import API_ERRORS_TOTAL
from core.proxy.manager import GeoProxyManager, Region
from core.safe_paths import resolve_within

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
except ImportError:
    CurlAsyncSession = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class GenericRestDatingClient(BaseDatingClient):
    """Универсальный REST-адаптер (используется если ещё не экспортирован HAR-профиль).
    Автоматически использует curl_cffi (если установлен) или httpx.AsyncClient.
    """

    def __init__(
        self,
        spec: PlatformSpec,
        token: str,
        proxy_url: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.spec = spec
        self.platform_name = spec.id
        self.token = token
        self.proxy_url = proxy_url
        self.base_url = (base_url or spec.api_base_url).rstrip("/")
        self._session: Any = None
        self._using_curl = False

    async def __aenter__(self) -> "GenericRestDatingClient":
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if CurlAsyncSession is not None:
            self._using_curl = True
            self._session = CurlAsyncSession(
                impersonate="safari17_2_ios",
                proxy=self.proxy_url,
                headers=headers,
                timeout=15,
            )
        else:
            self._using_curl = False
            self._session = httpx.AsyncClient(
                proxy=self.proxy_url,
                headers=headers,
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

    def _check_status(self, resp: Any) -> Any:
        if resp.status_code >= 400:
            API_ERRORS_TOTAL.labels(status_code=str(resp.status_code)).inc()
        resp.raise_for_status()
        return resp

    async def get_recommendations(self, limit: int = 20) -> list[CandidateProfile]:
        assert self._session is not None
        resp = self._check_status(
            await self._session.get(f"{self.base_url}/recommendations", params={"limit": limit})
        )
        items = resp.json().get("items", [])
        return [
            CandidateProfile(
                platform=self.platform_name,
                user_id=str(item["id"]),
                name=item.get("name", "User"),
                age=item.get("age", 20),
                bio=item.get("bio", ""),
                distance_km=item.get("distance_km", 0.0),
                compatibility_score=item.get("compatibility", 0) if self.spec.has_native_compatibility else 0,
                photo_urls=[p["url"] for p in item.get("photos", []) if isinstance(p, dict) and "url" in p],
                is_verified=item.get("is_verified", False),
            )
            for item in items
        ]

    async def swipe(self, target_user_id: str, is_like: bool) -> dict[str, Any]:
        assert self._session is not None
        action = "like" if is_like else "pass"
        resp = self._check_status(
            await self._session.post(f"{self.base_url}/swipes/{action}", json={"target_user_id": target_user_id})
        )
        return {"status": "ok", "is_match": bool(resp.json().get("is_match", False))}

    async def get_matches_with_unread(self) -> list[dict[str, Any]]:
        assert self._session is not None
        resp = self._check_status(await self._session.get(f"{self.base_url}/matches"))
        return resp.json().get("items", [])

    async def get_chat_history(self, match_id: str, limit: int = 20) -> list[dict[str, Any]]:
        assert self._session is not None
        resp = self._check_status(
            await self._session.get(f"{self.base_url}/matches/{match_id}/messages", params={"limit": limit})
        )
        return resp.json().get("items", [])

    async def send_message(self, match_id: str, text: str) -> dict[str, Any]:
        assert self._session is not None
        resp = self._check_status(
            await self._session.post(f"{self.base_url}/matches/{match_id}/messages", json={"text": text})
        )
        return resp.json()


def list_verified_api_profiles(profiles_dir: str = "./data/api_profiles") -> list[str]:
    p = Path(profiles_dir)
    if not p.exists():
        return []
    return sorted(f.stem for f in p.glob("*.json"))


def create_dating_client(
    platform: str,
    session_id: str,
    credentials: dict[str, str],
    proxy_manager: GeoProxyManager,
    profiles_dir: str = "./data/api_profiles",
) -> BaseDatingClient:
    plat = platform.lower()
    spec = GLOBAL_DATING_REGISTRY.get(plat)
    region_map = {"CIS": Region.CIS, "US": Region.US, "EU": Region.EU, "ASIA": Region.ASIA}
    region = region_map.get(spec.region, Region.ANY) if spec else Region.ANY
    proxy_url = proxy_manager.get_proxy_for_session(session_id, region=region)
    token = credentials.get("token", "")

    # 1. Приоритет — подтверждённый HAR-профиль `./data/api_profiles/<platform>.json`
    har_profile = resolve_within(profiles_dir, f"{plat}.json")
    if har_profile.exists():
        logger.info("[Factory] Загружен реальный HAR-профиль API: %s", har_profile)
        return DeclarativeRestDatingClient.from_profile_file(
            har_profile,
            token=token,
            refresh_token=credentials.get("refresh_token", ""),
            proxy_url=proxy_url,
            profiles_dir=profiles_dir,
        )

    if not spec:
        raise ValueError(
            f"Платформа '{platform}' отсутствует в GLOBAL_DATING_REGISTRY и нет файла {har_profile}."
        )

    if plat == "twinby":
        return TwinbyStealthClient(
            access_token=token or None,
            refresh_token=credentials.get("refresh_token"),
            proxy_url=proxy_url,
        )
    if plat == "pure":
        return PureClient(device_token=token, proxy_url=proxy_url)
    if plat == "tabor":
        return TaborClient(session_token=token, proxy_url=proxy_url)

    return GenericRestDatingClient(spec=spec, token=token, proxy_url=proxy_url)
