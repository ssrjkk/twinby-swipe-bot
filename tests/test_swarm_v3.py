import httpx
import pytest
import respx

from config import settings
from core.adapters.pure_and_cis import PureClient
from core.adapters_registry import AutomationDriverType, GLOBAL_DATING_REGISTRY
from core.factory import create_dating_client
from core.proxy.manager import GeoProxyManager
from core.ui_driver import MobileUIDriverFallback
from filters.synthetic_scorer import SyntheticCompatibilityScorer


def test_global_registry_covers_all_28_russian_and_world_apps() -> None:
    required_cis = {
        "twinby",
        "vk_dating",
        "ok_dating",
        "mamba",
        "tabor",
        "loveplanet",
        "fotostrana",
        "beboo",
        "kismia",
        "drugvokrug",
        "teamo",
        "rusdate",
        "linkyou",
        "skylove",
        "juliadates",
        "vox",
    }
    required_world = {
        "pure",
        "feeld",
        "tinder",
        "badoo",
        "bumble",
        "hinge",
        "okcupid",
        "happn",
        "lovoo",
        "tantan",
        "pairs",
        "wippy",
    }
    assert required_cis.issubset(GLOBAL_DATING_REGISTRY.keys())
    assert required_world.issubset(GLOBAL_DATING_REGISTRY.keys())
    assert len(GLOBAL_DATING_REGISTRY) == 28

    # Pure не имеет нативного % и не требует лица на главном фото
    pure_spec = GLOBAL_DATING_REGISTRY["pure"]
    assert pure_spec.has_native_compatibility is False
    assert pure_spec.requires_face_on_primary_photo is False

    # Hinge, Bumble, Pairs, Wippy, Vox, Feeld используют UI-драйвер из-за App Attest / Play Integrity
    for app_id in ("hinge", "bumble", "pairs", "wippy", "vox", "feeld"):
        assert (
            GLOBAL_DATING_REGISTRY[app_id].driver == AutomationDriverType.UI_DRIVER_ONLY
        )


def test_factory_rejects_registry_entries_without_adapters() -> None:
    pm = GeoProxyManager([])
    for plat_id in GLOBAL_DATING_REGISTRY:
        if plat_id not in {"twinby", "pure", "tabor"}:
            with pytest.raises(NotImplementedError, match="adapter contract"):
                create_dating_client(plat_id, "s", {"token": "tok"}, pm)
        else:
            client = create_dating_client(plat_id, "s", {"token": "tok"}, pm)
            assert client.platform_name == plat_id


@pytest.mark.asyncio
@respx.mock
async def test_pure_synthetic_vibe_scoring_and_client() -> None:
    feed_route = respx.get("https://pure.dating/api/v2/feed").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "ad_901",
                        "title": "Ночной кофе и разговоры",
                        "age": 24,
                        "text": "Люблю джаз, спешелти фильтр и спонтанные прогулки по Питеру",
                        "distance_km": 2.1,
                        "photos": [{"url": "https://cdn.pure.dating/img1.jpg"}],
                        "kinks": ["coffee", "music"],
                    }
                ]
            },
        )
    )
    llm_route = respx.post(
        f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"vibe_score": 89, "is_bot_or_ad": false, '
                                '"reasoning": "Совпадение по атмосфере и интересам"}'
                            )
                        }
                    }
                ]
            },
        )
    )

    async with PureClient(device_token="dev_tok_123") as client:
        profiles = await client.get_recommendations(limit=5)

    assert feed_route.called, "запрос ленты ушёл мимо мока, в живую сеть"
    assert len(profiles) == 1
    assert profiles[0].compatibility_score == 0

    scorer = SyntheticCompatibilityScorer()
    verdict = await scorer.compute_compatibility(
        profiles[0], user_preferences="кофе, музыка, прогулки"
    )
    assert llm_route.called, "скоринг ушёл мимо мока, в живую сеть"
    assert verdict.vibe_score == 89
    assert verdict.is_bot_or_ad is False


def test_bezier_swipe_trajectory_generation() -> None:
    driver = MobileUIDriverFallback(
        device_udid="emulator-5554", platform_package="co.hinge.app"
    )
    pts = driver._generate_bezier_points(540, 1200, 950, 1180, steps=10)
    assert len(pts) == 11
    assert pts[-1][0] > pts[0][0]
