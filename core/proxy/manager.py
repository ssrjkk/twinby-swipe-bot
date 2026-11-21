import asyncio
import json
import logging
import time
from enum import Enum
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class Region(str, Enum):
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
        auth = f"{self.username}:{self.password}@" if self.username and self.password else ""
        return f"http://{auth}{self.host}:{self.port}"


class GeoProxyManager:
    def __init__(self, raw_proxies: list[dict[str, Any]] | None = None) -> None:
        self._proxies: list[ProxyConfig] = [ProxyConfig(**p) for p in (raw_proxies or [])]
        self._sticky_assignments: dict[str, ProxyConfig] = {}

    @classmethod
    def from_file(cls, path: str | None) -> "GeoProxyManager":
        if not path:
            return cls([])
        p = Path(path)
        if not p.exists():
            logger.warning("Файл прокси %s не найден — работа без пула прокси.", path)
            return cls([])
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(data if isinstance(data, list) else [])

    @property
    def proxies_count(self) -> int:
        return len(self._proxies)

    def get_proxy_for_session(self, session_id: str, region: Region = Region.ANY) -> str | None:
        if session_id in self._sticky_assignments:
            p = self._sticky_assignments[session_id]
            if p.is_active:
                return p.url

        available = [
            p
            for p in self._proxies
            if p.is_active and (p.region == region or region == Region.ANY or p.region == Region.ANY)
        ]
        if not available:
            return None
        selected = min(available, key=lambda x: (x.fails_count, x.latency_ms))
        self._sticky_assignments[session_id] = selected
        return selected.url

    async def check_all_once(self, check_url: str = "https://api.ipify.org?format=json") -> None:
        for proxy in self._proxies:
            started = time.perf_counter()
            try:
                async with httpx.AsyncClient(proxy=proxy.url, timeout=6.0) as client:
                    resp = await client.get(check_url)
                    resp.raise_for_status()
                proxy.latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
                proxy.fails_count = 0
                proxy.is_active = True
            except Exception:
                proxy.fails_count += 1
                if proxy.fails_count >= 3:
                    proxy.is_active = False

    async def health_check_loop(self, interval_seconds: int = 300) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            await self.check_all_once()
