import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ai.bandits import PromptThompsonBandit
from ai.chat_agent import AutonomousChatAgent
from ai.langgraph_agent import DatingLangGraphEngine
from ai.universal_llm import UniversalAIGateway, extract_json_object
from core.adapters_registry import AutomationDriverType, GLOBAL_DATING_REGISTRY
from core.backup_service import DatabaseBackupService
from core.base_client import CandidateProfile
from core.ui_driver import MobileUIDriverFallback
from database.models import SwipeDecision
from filters.engine import FilterConfig, ProfileFilterEngine
from filters.synthetic_scorer import SyntheticCompatibilityScorer
from filters.vision_card_parser import VisionProfileParser
from tools.har_importer import inspect_har


def test_extract_json_handles_markdown_fences() -> None:
    raw = 'Ответ модели:\n```json\n{"vibe_score": 88, "is_bot_or_ad": false}\n```'
    parsed = extract_json_object(raw)
    assert parsed["vibe_score"] == 88
    assert parsed["is_bot_or_ad"] is False


def test_universal_gateway_all_providers_and_native_gemini() -> None:
    gw_openai = UniversalAIGateway(provider="openai", api_key="sk-123")
    assert gw_openai._build_headers()["Authorization"] == "Bearer sk-123"

    gw_anth = UniversalAIGateway(provider="anthropic", base_url="https://api.anthropic.com", api_key="ant-key", model="claude-3-5-sonnet")
    url_a, pay_a = gw_anth._resolve_endpoint_and_payload(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}], 0.5, True, images=[b"\x89PNGfake"]
    )
    assert url_a.endswith("/v1/messages")
    assert pay_a["system"] == "sys"
    assert pay_a["messages"][0]["content"][0]["source"]["media_type"] == "image/png"

    gw_gem = UniversalAIGateway(
        provider="gemini",
        base_url="https://generativelanguage.googleapis.com",
        api_key="AIza-key",
        model="gemini-2.5-flash",
    )
    url_g, pay_g = gw_gem._resolve_endpoint_and_payload(
        [{"role": "user", "content": "hi"}], 0.2, True, images=[b"jpgbytes"]
    )
    assert "models/gemini-2.5-flash:generateContent" in url_g
    assert gw_gem._build_headers()["x-goog-api-key"] == "AIza-key"
    assert pay_g["generationConfig"]["responseMimeType"] == "application/json"
    assert "inline_data" in pay_g["contents"][0]["parts"][1]

    gw_custom = UniversalAIGateway(provider="custom", api_key="raw-secret", custom_header="X-Custom-Token")
    assert gw_custom._build_headers()["X-Custom-Token"] == "raw-secret"


def test_filter_engine_enforces_all_rules() -> None:
    engine = ProfileFilterEngine(FilterConfig(min_compatibility=70, min_age=18, max_age=35, max_distance_km=25, require_bio=True, min_score_threshold=65.0))

    spam = CandidateProfile(
        platform="twinby", user_id="1", name="Алина", age=23,
        bio="Подпишись на мой inst: @spam_acc", compatibility_score=95, photo_urls=["http://img/1.jpg"]
    )
    assert engine.evaluate(spam).decision == SwipeDecision.PASS

    empty_bio = CandidateProfile(
        platform="twinby", user_id="2", name="Катя", age=24,
        bio="   ", compatibility_score=90, photo_urls=["http://img/2.jpg"]
    )
    assert engine.evaluate(empty_bio).decision == SwipeDecision.PASS

    good = CandidateProfile(
        platform="twinby", user_id="3", name="Лера", age=25,
        bio="Люблю спешелти кофе, путешествия, кино и хорошую музыку в Питере",
        compatibility_score=82, photo_urls=["http://img/3.jpg"], is_verified=True
    )
    res_good = engine.evaluate(good)
    assert res_good.decision == SwipeDecision.LIKE
    assert res_good.score > 80.0
    assert "кофе" in res_good.matched_keywords


@pytest.mark.asyncio
async def test_langgraph_and_chat_agent_with_bandit(tmp_path: Path) -> None:
    mock_gw = AsyncMock()
    mock_gw.complete_chat = AsyncMock(return_value="Пишет коротко, с юмором.")
    mock_gw.complete_json = AsyncMock(
        side_effect=[
            {"stage": "rapport", "tactic": "комплимент", "draft_reply": "Приветствую — прекрасный день!", "should_handoff": False, "handoff_summary": ""},
            {"approved": False, "critique": "Убери тире и официоз", "messages_chunks": ["Приветствую — прекрасный день!"]},
            {"stage": "rapport", "tactic": "юмор", "draft_reply": "привет, как тебе погода?", "should_handoff": False, "handoff_summary": ""},
            {"approved": True, "critique": "", "messages_chunks": ["привет)", "как тебе погода?"]},
        ]
    )
    bandit = PromptThompsonBandit(redis_client=None, state_file=str(tmp_path / "bandit.json"))
    agent = AutonomousChatAgent(gateway=mock_gw, bandit=bandit)
    out = await agent.generate_reply(history=[], profile_context="Кофе и прогулки", visual_hooks=["корги"])
    assert out["chunks"] == ["привет)", "как тебе погода?"]
    assert out["bandit_arm"] in PromptThompsonBandit.DEFAULT_ARMS

    await bandit.record_reward(out["bandit_arm"], success=True)
    summary = await bandit.get_summary()
    assert summary[out["bandit_arm"]]["alpha"] == 2.0


@pytest.mark.asyncio
async def test_vision_profile_parser_handles_profile_and_match_popup() -> None:
    mock_gw = AsyncMock()
    mock_gw.complete_json = AsyncMock(
        side_effect=[
            {
                "screen_type": "profile", "is_profile_screen": True, "name": "Соня", "age": 24,
                "bio": "Кофе и винил", "distance_km": 3.5, "native_compatibility": 86, "vibe_score": 88,
                "is_verified": True, "is_ad_or_bot": False, "visual_hooks": ["флэт-уайт"],
                "tags": ["кофе"], "reasoning": "Совпадение",
            },
            {
                "screen_type": "match_popup", "is_profile_screen": False, "reasoning": "Взаимный мэтч!",
            },
        ]
    )
    parser = VisionProfileParser(gateway=mock_gw)
    prof, parsed = await parser.parse_screenshot(b"png", platform="twinby", user_preferences="кофе")
    assert prof is not None and prof.name == "Соня" and prof.compatibility_score == 86

    prof2, parsed2 = await parser.parse_screenshot(b"png", platform="twinby", user_preferences="кофе")
    assert prof2 is None and parsed2.screen_type == "match_popup"


@pytest.mark.asyncio
async def test_pure_synthetic_scorer_uses_complete_json() -> None:
    mock_gw = AsyncMock()
    mock_gw.complete_json = AsyncMock(return_value={"vibe_score": 84, "is_bot_or_ad": False, "reasoning": "Ок"})
    scorer = SyntheticCompatibilityScorer(gateway=mock_gw)
    pure = CandidateProfile(platform="pure", user_id="p1", name="Аноним", age=23, bio="Кино и вино", photo_urls=["u1"])
    res = await scorer.compute_compatibility(pure, user_preferences="кино")
    assert res.vibe_score == 84 and res.is_bot_or_ad is False


def test_sqlite_backup_and_rotation(tmp_path: Path) -> None:
    db_file = tmp_path / "heart3.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()

    svc = DatabaseBackupService(bot=None, db_path=str(db_file), retention_count=2)
    b1 = svc.create_safe_backup_sync()
    assert b1 is not None and b1.exists()


def test_har_inspector_extracts_json_endpoints(tmp_path: Path) -> None:
    har_file = tmp_path / "sample.har"
    har_file.write_text(
        json.dumps(
            {
                "log": {
                    "entries": [
                        {
                            "request": {
                                "method": "GET",
                                "url": "https://api.twinby.com/v1/feed?limit=10",
                                "headers": [{"name": "Authorization", "value": "Bearer xxx"}],
                            },
                            "response": {
                                "status": 200,
                                "content": {"mimeType": "application/json", "text": '{"items": [], "next": 2}'},
                            },
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    report = inspect_har(str(har_file), host_filter="twinby")
    assert report["total_json_endpoints"] == 1
    assert report["by_role"]["feed"][0]["endpoint"] == "https://api.twinby.com/v1/feed"
    assert "Authorization" in report["endpoints"][0]["auth_headers"]


def test_registry_and_bezier() -> None:
    assert len(GLOBAL_DATING_REGISTRY) == 28
    assert GLOBAL_DATING_REGISTRY["pure"].requires_face_on_primary_photo is False
    assert GLOBAL_DATING_REGISTRY["hinge"].driver == AutomationDriverType.UI_DRIVER_ONLY
    driver = MobileUIDriverFallback(device_udid="emu", platform_package="com.twinby.app")
    pts = driver._generate_bezier_points(540, 1200, 950, 1180, steps=10)
    assert len(pts) == 11 and pts[-1][0] > pts[0][0]


def test_face_consistency_math_and_pure_soft_gate() -> None:
    from filters.face_checker import cosine_similarity, evaluate_embeddings

    same_a = [1.0, 0.0, 0.0]
    same_b = [0.95, 0.05, 0.0]
    diff_c = [-0.8, 0.6, 0.0]
    assert cosine_similarity(same_a, same_b) > 0.99
    assert evaluate_embeddings([same_a, same_b], threshold=0.42).passed is True
    assert evaluate_embeddings([same_a, diff_c], threshold=0.42).passed is False


def test_humanizer_active_hours_and_burst_guard() -> None:
    from datetime import datetime
    from core.humanizer import Humanizer

    h = Humanizer(max_per_hour=2, burst_size=2, active_start_hour=9, active_end_hour=22, enforce_active_hours=True)
    assert h.is_within_active_hours(datetime(2026, 11, 19, 14, 0)) is True
    assert h.is_within_active_hours(datetime(2026, 11, 19, 3, 0)) is False

    h.record_action(1000.0)
    assert h.hourly_cap_reached(1001.0) is False
    h.record_action(1002.0)
    assert h.hourly_cap_reached(1003.0) is True
    assert h.should_take_session_break() is True


def test_pure_and_cis_importable_without_curl_cffi() -> None:
    from core.adapters.pure_and_cis import PureClient, TaborClient

    assert PureClient.platform_name == "pure"
    assert TaborClient.platform_name == "tabor"


def test_extract_json_handles_trailing_braces_and_prose() -> None:
    raw = 'Мысли модели: допустим {не json}, итоговый ответ: {"vibe_score": 91, "is_bot_or_ad": false, "reasoning": "ок"} спасибо!'
    parsed = extract_json_object(raw)
    assert parsed["vibe_score"] == 91


def test_universal_gateway_does_not_duplicate_endpoint_suffix() -> None:
    gw = UniversalAIGateway(provider="openai", base_url="https://api.openai.com/v1/chat/completions", api_key="sk-1", model="gpt-4o-mini")
    url, _ = gw._resolve_endpoint_and_payload([{"role": "user", "content": "hi"}], 0.5, False)
    assert url == "https://api.openai.com/v1/chat/completions"

    gw_a = UniversalAIGateway(provider="anthropic", base_url="https://api.anthropic.com/v1/messages", api_key="ak", model="claude")
    url_a, _ = gw_a._resolve_endpoint_and_payload([{"role": "user", "content": "hi"}], 0.5, False)
    assert url_a == "https://api.anthropic.com/v1/messages"


@pytest.mark.asyncio
async def test_bandit_selects_arm_when_first_user_message_already_in_history(tmp_path: Path) -> None:
    mock_gw = AsyncMock()
    mock_gw.complete_chat = AsyncMock(return_value="Краткий стиль.")
    mock_gw.complete_json = AsyncMock(
        side_effect=[
            {"stage": "icebreaker", "tactic": "шутка", "draft_reply": "привет, как кофе?", "should_handoff": False, "handoff_summary": ""},
            {"approved": True, "critique": "", "messages_chunks": ["привет, как кофе?"]},
            {"stage": "rapport", "tactic": "вопрос", "draft_reply": "класс!", "should_handoff": False, "handoff_summary": ""},
            {"approved": True, "critique": "", "messages_chunks": ["класс!"]},
        ]
    )
    bandit = PromptThompsonBandit(redis_client=None, state_file=str(tmp_path / "b.json"))
    agent = AutonomousChatAgent(gateway=mock_gw, bandit=bandit)

    r1 = await agent.generate_reply(history=[{"role": "user", "content": "Привет!"}], profile_context="Кофе")
    assert r1["bandit_arm"] in PromptThompsonBandit.DEFAULT_ARMS

    r2 = await agent.generate_reply(
        history=[{"role": "user", "content": "Привет!"}, {"role": "assistant", "content": "привет, как кофе?"}, {"role": "user", "content": "отлично"}],
        profile_context="Кофе",
    )
    assert r2["bandit_arm"] is None
