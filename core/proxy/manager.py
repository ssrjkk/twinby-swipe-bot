import asyncio
import logging
from enum import StrEnum
from typing import Any
from curl_cffi.requests import AsyncSession
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class Region(StrEnum):
    US = "us"
    EU = "eu"
    CIS = "cis"
    ASIA = "asia"
    ANY = "any"


class ProxyConfig(BaseModel):
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    region: Region = Region.ANY
    is_active: bool = True
    fails_count: int = 0
    latency_ms: float = 0.0

    @property
    def url(self) -> str:
        auth = (
            f"{self.username}:{self.password}@"
            if self.username and self.password
            else ""
        )
        return f"http://{auth}{self.host}:{self.port}"


class GeoProxyManager:
    def __init__(self, raw_proxies: list[dict[str, Any]]) -> None:
        self._proxies: list[ProxyConfig] = [ProxyConfig(**p) for p in raw_proxies]
        self._sticky_assignments: dict[str, ProxyConfig] = {}

    def get_proxy_for_session(
        self, session_id: str, region: Region = Region.ANY
    ) -> str | None:
        if session_id in self._sticky_assignments:
            p = self._sticky_assignments[session_id]
            if p.is_active:
                return p.url
        available = [
            p
            for p in self._proxies
            if p.is_active
            and (p.region == region or region == Region.ANY or p.region == Region.ANY)
        ]
        if not available:
            return None
        selected = min(available, key=lambda x: (x.fails_count, x.latency_ms))
        self._sticky_assignments[session_id] = selected
        return selected.url

    async def health_check_loop(self, interval_seconds: int = 300) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            for proxy in self._proxies:
                try:

                    async def check():
                        async with AsyncSession(proxy=proxy.url, timeout=5) as s:
                            await s.get("https://ipapi.co/json/")

                    await asyncio.to_thread(asyncio.run, check())
                    proxy.is_active = True
                except Exception:
                    proxy.fails_count += 1
                    if proxy.fails_count >= 3:
                        proxy.is_active = False
