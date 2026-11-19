import curl_cffi
import httpx
import pytest
import respx

from config import settings
from core.base_client import CandidateProfile
from core.client import TwinbyStealthClient
from core.factory import create_dating_client
from core.proxy.manager import GeoProxyManager
from core.ui_driver import MobileUIDriverFallback
from filters.engine import FilterConfig, ProfileFilterEngine
from database.models import SwipeDecision


class NoNativeHandle:
    def __getattr__(self, name):
        raise AssertionError(f"Native transport reached: {name}")


@pytest.mark.parametrize(
    "entry", [curl_cffi.Curl.perform, curl_cffi.AsyncCurl.add_handle]
)
def test_native_curl_is_blocked_before_handles_are_used(entry):
    with pytest.raises(RuntimeError, match="Native curl network is disabled"):
        entry(NoNativeHandle(), NoNativeHandle())


@pytest.mark.asyncio
async def test_failed_scoring_cannot_like_below_fallback_threshold(
    monkeypatch, in_memory_db
):
    from core.worker import SwipeWorker
    from types import SimpleNamespace

    worker = SwipeWorker(
        SimpleNamespace(),
        in_memory_db,
        ProfileFilterEngine(FilterConfig(min_compatibility=0)),
    )

    async def fail(*a, **kw):
        raise RuntimeError("offline")

    monkeypatch.setattr(worker.synthetic_scorer.gateway, "complete_chat", fail)
    profile = CandidateProfile(
        platform="pure",
        user_id="u",
        name="User",
        age=25,
        distance_km=1,
        compatibility_score=0,
        photo_urls=["https://example.test/photo"],
        bio="A very long description",
    )
    decision, _, _ = await worker._evaluate_candidate(profile)
    assert decision == SwipeDecision.PASS


@pytest.mark.asyncio
@respx.mock
async def test_unknown_post_outcome_is_not_retried():
    route = respx.post(f"{settings.twinby_base_url}/swipes/like").mock(
        side_effect=TimeoutError("unknown")
    )
    async with TwinbyStealthClient(access_token="test-token") as client:
        with pytest.raises(TimeoutError):
            await client.swipe("u", True)
    assert route.call_count == 1


def test_fixture_overrides_inherited_credentials_and_dotenv():
    assert settings.twinby_access_token.get_secret_value() == "test-only-twinby-token"
    assert settings.llm_api_key.get_secret_value() == "sk-dummy"
    assert settings.database_url == "sqlite+aiosqlite:///:memory:"


@pytest.mark.asyncio
@respx.mock
async def test_twinby_refresh_retries_with_new_authorization():
    headers = []

    def feed(request):
        headers.append(request.headers["authorization"])
        if len(headers) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"items": []})

    feed_route = respx.get(f"{settings.twinby_base_url}/feed/recommendations").mock(
        side_effect=feed
    )
    refresh_route = respx.post(f"{settings.twinby_base_url}/auth/refresh").mock(
        return_value=httpx.Response(200, json={"access_token": "renewed-test-token"})
    )
    async with TwinbyStealthClient(
        access_token="old-test-token", refresh_token="test-refresh"
    ) as client:
        assert await client.get_recommendations() == []
    assert headers == ["Bearer old-test-token", "Bearer renewed-test-token"]
    assert feed_route.call_count == 2
    assert refresh_route.call_count == 1


@pytest.mark.asyncio
async def test_factory_preserves_explicit_account_credentials():
    pm = GeoProxyManager([{"host": "127.0.0.1", "port": 9000, "region": "cis"}])
    client = create_dating_client(
        "twinby", "isolated-session", {"token": "explicit-account"}, pm
    )
    async with client:
        assert client._session.headers["Authorization"] == "Bearer explicit-account"
        assert client._session.proxy == "http://127.0.0.1:9000"
        assert client.session_id == "isolated-session"


@pytest.mark.asyncio
@respx.mock
async def test_appium_failure_is_not_reported_as_success():
    driver = MobileUIDriverFallback("device", "package")
    driver._session_id = "test-session"
    route = respx.post("http://localhost:4723/session/test-session/actions").mock(
        return_value=httpx.Response(500)
    )
    with pytest.raises(httpx.HTTPStatusError):
        await driver.swipe_with_bezier_curve(True)
    assert route.call_count == 1


@pytest.mark.parametrize(
    "bio,expected",
    [
        ("", SwipeDecision.PASS),
        ("   ", SwipeDecision.PASS),
        ("coffee", SwipeDecision.LIKE),
    ],
)
def test_require_bio_is_enforced(bio, expected):
    profile = CandidateProfile(
        platform="twinby",
        user_id="u",
        name="User",
        age=25,
        distance_km=1,
        compatibility_score=90,
        photo_urls=["https://example.test/photo"],
        bio=bio,
    )
    result = ProfileFilterEngine(FilterConfig(require_bio=True)).evaluate(profile)
    assert result.decision == expected
