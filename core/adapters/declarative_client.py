import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from core.base_client import BaseDatingClient, CandidateProfile
from core.metrics import API_ERRORS_TOTAL

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
except ImportError:
    CurlAsyncSession = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def extract_dot_path(obj: Any, path: str, default: Any = None) -> Any:
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return default
    return cur


class EndpointSpec(BaseModel):
    method: str = "GET"
    path: str
    items_path: str = "items"
    body_template: dict[str, Any] = Field(default_factory=dict)


class FieldMappingSpec(BaseModel):
    id: str = "id"
    name: str = "name"
    age: str = "age"
    bio: str = "bio"
    distance_km: str = "distance_km"
    compatibility: str = "compatibility.score"
    photos: str = "photos"
    photo_url_key: str = "url"
    tags: str = "interests"
    is_verified: str = "is_verified"
    is_match: str = "is_match"
    access_token_path: str = "access_token"


class DeclarativeAPIProfile(BaseModel):
    platform: str
    base_url: str
    impersonate: str = "safari17_2_ios"
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "
    static_headers: dict[str, str] = Field(default_factory=lambda: {"Accept": "application/json"})
    cookies: dict[str, str] = Field(default_factory=dict)
    feed: EndpointSpec
    like: EndpointSpec
    pass_action: EndpointSpec
    refresh: EndpointSpec | None = None
    matches: EndpointSpec | None = None
    messages_get: EndpointSpec | None = None
    messages_send: EndpointSpec | None = None
    fields: FieldMappingSpec = Field(default_factory=FieldMappingSpec)


class DeclarativeRestDatingClient(BaseDatingClient):
    """Универсальный реальный HTTP-клиент по HAR-профилю `./data/api_profiles/<platform>.json`.
    Поддерживает ретраи на 429/5xx, авторефреш токена на 401, кастомные cookies/headers,
    `curl_cffi` TLS-спуфинг и `httpx` fallback.
    """

    def __init__(
        self,
        profile: DeclarativeAPIProfile,
        token: str = "",
        refresh_token: str = "",
        proxy_url: str | None = None,
        extra_headers: dict[str, str] | None = None,
        max_retries: int = 3,
    ) -> None:
        self.profile = profile
        self.platform_name = profile.platform
        self.token = token
        self.refresh_token = refresh_token
        self.proxy_url = proxy_url
        self.extra_headers = extra_headers or {}
        self.max_retries = max_retries
        self._session: Any = None
        self._using_curl = False

    @classmethod
    def from_profile_file(
        cls,
        profile_path: str | Path,
        token: str = "",
        refresh_token: str = "",
        proxy_url: str | None = None,
    ) -> "DeclarativeRestDatingClient":
        data = json.loads(Path(profile_path).read_text(encoding="utf-8"))
        return cls(
            DeclarativeAPIProfile.model_validate(data),
            token=token,
            refresh_token=refresh_token,
            proxy_url=proxy_url,
        )

    def _build_headers(self) -> dict[str, str]:
        headers = dict(self.profile.static_headers)
        headers.update(self.extra_headers)
        if self.token and self.profile.auth_header:
            val = self.token
            if self.profile.auth_prefix and not val.lower().startswith(self.profile.auth_prefix.lower()):
                val = f"{self.profile.auth_prefix}{val}"
            headers[self.profile.auth_header] = val
        return headers

    async def __aenter__(self) -> "DeclarativeRestDatingClient":
        headers = self._build_headers()
        if CurlAsyncSession is not None:
            self._using_curl = True
            self._session = CurlAsyncSession(
                impersonate=self.profile.impersonate,
                headers=headers,
                cookies=self.profile.cookies or None,
                proxy=self.proxy_url,
                timeout=15,
            )
        else:
            self._using_curl = False
            self._session = httpx.AsyncClient(
                headers=headers,
                cookies=self.profile.cookies or None,
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

    def _render_template(self, obj: Any, variables: dict[str, Any]) -> Any:
        if isinstance(obj, str):
            return obj.format(**variables)
        if isinstance(obj, dict):
            return {k: self._render_template(v, variables) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._render_template(v, variables) for v in obj]
        return obj

    async def _try_refresh_token(self) -> bool:
        if not self.profile.refresh or not self.refresh_token or not self._session:
            return False
        spec = self.profile.refresh
        url = spec.path if spec.path.startswith("http") else f"{self.profile.base_url.rstrip('/')}/{spec.path.lstrip('/')}"
        body = (
            self._render_template(spec.body_template, {"refresh_token": self.refresh_token})
            if spec.body_template
            else {"refresh_token": self.refresh_token}
        )
        resp = await self._session.request(spec.method.upper(), url, json=body)
        if resp.status_code != 200:
            return False
        new_tok = extract_dot_path(resp.json(), self.profile.fields.access_token_path, default="")
        if not new_tok:
            return False
        self.token = str(new_tok)
        new_hdrs = self._build_headers()
        if self.profile.auth_header in new_hdrs:
            self._session.headers[self.profile.auth_header] = new_hdrs[self.profile.auth_header]
        return True

    async def _call(
        self,
        spec: EndpointSpec,
        variables: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if not self._session:
            raise RuntimeError("Клиент не открыт. Используйте async with DeclarativeRestDatingClient(...).")
        vars_map = variables or {}
        path = spec.path.format(**vars_map)
        url = path if path.startswith("http") else f"{self.profile.base_url.rstrip('/')}/{path.lstrip('/')}"
        body = self._render_template(spec.body_template, vars_map) if spec.body_template else None

        kwargs: dict[str, Any] = {}
        if params:
            kwargs["params"] = params
        if body is not None and spec.method.upper() != "GET":
            kwargs["json"] = body

        refreshed = False
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = await self._session.request(spec.method.upper(), url, **kwargs)
            except (ConnectionError, TimeoutError, httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    await asyncio.sleep(min(2 ** attempt, 10))
                    continue
                raise

            if resp.status_code == 401 and not refreshed:
                refreshed = True
                if await self._try_refresh_token():
                    continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                API_ERRORS_TOTAL.labels(status_code=str(resp.status_code)).inc()
                await asyncio.sleep(min(2 ** attempt, 12))
                continue
            if resp.status_code >= 400:
                API_ERRORS_TOTAL.labels(status_code=str(resp.status_code)).inc()
            resp.raise_for_status()
            if resp.status_code == 204:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {}
        if last_exc is not None:
            raise last_exc

        raise RuntimeError(f"Исчерпаны попытки запроса к {url}")

    async def get_recommendations(self, limit: int = 20) -> list[CandidateProfile]:
        data = await self._call(self.profile.feed, params={"limit": limit})
        items = extract_dot_path(data, self.profile.feed.items_path, default=[])
        if not isinstance(items, list):
            items = []

        fm = self.profile.fields
        profiles: list[CandidateProfile] = []
        for item in items[:limit]:
            raw_photos = extract_dot_path(item, fm.photos, default=[]) or []
            photo_urls = [
                p[fm.photo_url_key] if isinstance(p, dict) and fm.photo_url_key in p else str(p)
                for p in raw_photos
                if p
            ]
            compat = extract_dot_path(item, fm.compatibility, default=0) or 0
            profiles.append(
                CandidateProfile(
                    platform=self.platform_name,
                    user_id=str(extract_dot_path(item, fm.id, default="")),
                    name=str(extract_dot_path(item, fm.name, default="Без имени") or "Без имени"),
                    age=int(extract_dot_path(item, fm.age, default=20) or 20),
                    bio=str(extract_dot_path(item, fm.bio, default="") or ""),
                    distance_km=float(extract_dot_path(item, fm.distance_km, default=0.0) or 0.0),
                    compatibility_score=int(compat),
                    photo_urls=photo_urls,
                    tags=list(extract_dot_path(item, fm.tags, default=[]) or []),
                    is_verified=bool(extract_dot_path(item, fm.is_verified, default=False)),
                )
            )
        return profiles

    async def swipe(self, target_user_id: str, is_like: bool) -> dict[str, Any]:
        spec = self.profile.like if is_like else self.profile.pass_action
        res = await self._call(spec, variables={"target_user_id": target_user_id})
        is_match = bool(extract_dot_path(res, self.profile.fields.is_match, default=False))
        return {"status": "ok", "is_match": is_match, "raw": res}

    async def get_matches_with_unread(self) -> list[dict[str, Any]]:
        if not self.profile.matches:
            return []
        data = await self._call(self.profile.matches)
        items = extract_dot_path(data, self.profile.matches.items_path, default=[])
        return items if isinstance(items, list) else []

    async def get_chat_history(self, match_id: str, limit: int = 20) -> list[dict[str, Any]]:
        if not self.profile.messages_get:
            return []
        data = await self._call(self.profile.messages_get, variables={"match_id": match_id}, params={"limit": limit})
        items = extract_dot_path(data, self.profile.messages_get.items_path, default=[])
        return items if isinstance(items, list) else []

    async def send_message(self, match_id: str, text: str) -> dict[str, Any]:
        if not self.profile.messages_send:
            raise RuntimeError(f"В профиле {self.platform_name} не задан эндпоинт messages_send.")
        return await self._call(self.profile.messages_send, variables={"match_id": match_id, "text": text})
