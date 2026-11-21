import asyncio
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import AsyncMock

from PIL import Image

from ai.bandits import PromptThompsonBandit
from ai.chat_agent import AutonomousChatAgent
from ai.universal_llm import UniversalAIGateway, extract_json_object
from core.adapters.declarative_client import DeclarativeRestDatingClient, extract_dot_path
from core.adapters.pure_and_cis import PureClient, TaborClient
from core.adapters_registry import AutomationDriverType, GLOBAL_DATING_REGISTRY, resolve_platform_defaults
from core.base_client import CandidateProfile
from core.humanizer import Humanizer
from core.image_utils import denormalize_point, is_blank_or_secure_screen, prepare_image_for_vlm
from core.proxy.manager import GeoProxyManager, Region
from core.ui_driver import MobileUIDriverFallback, WEB_ACTION_SELECTORS
from core.visual_chat_worker import VisualChatAssistant
from core.worker import SwipeWorker
from database.models import SwipeDecision
from filters.engine import FilterConfig, ProfileFilterEngine
from filters.face_checker import FaceConsistencyChecker, cosine_similarity, evaluate_embeddings
from filters.synthetic_scorer import SyntheticCompatibilityScorer
from filters.vision_card_parser import VisionProfileParser
from tools.har_importer import export_api_profile_from_har, inspect_har


def _make_png(black: bool = False, size: tuple[int, int] = (1440, 2800)) -> bytes:
    img = Image.new("RGB", size, (0, 0, 0))
    if not black:
        px = img.load()
        for y in range(0, size[1], 16):
            for x in range(0, size[0], 16):
                px[x, y] = (x % 255, y % 255, (x + y) % 255)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestHeart3Comprehensive(unittest.TestCase):
    def test_01_registry_all_28_platforms_have_packages_and_urls(self) -> None:
        self.assertEqual(len(GLOBAL_DATING_REGISTRY), 28)
        for pid, spec in GLOBAL_DATING_REGISTRY.items():
            self.assertEqual(spec.id, pid)
            self.assertTrue(spec.android_package, f"Missing android_package for {pid}")
            self.assertTrue(spec.web_url.startswith("http"), f"Missing web_url for {pid}")
        pkg, url = resolve_platform_defaults("tinder", "Android")
        self.assertEqual(pkg, "com.tinder")
        self.assertIn("tinder.com", url)
        pkg_ios, _ = resolve_platform_defaults("hinge", "iOS")
        self.assertEqual(pkg_ios, "co.hinge.mobile.ios")
        self.assertFalse(GLOBAL_DATING_REGISTRY["pure"].requires_face_on_primary_photo)
        self.assertEqual(GLOBAL_DATING_REGISTRY["hinge"].driver, AutomationDriverType.UI_DRIVER_ONLY)

    def test_02_image_utils_flag_secure_compression_and_grounding(self) -> None:
        black_png = _make_png(black=True, size=(300, 600))
        real_png = _make_png(black=False, size=(1440, 2800))
        self.assertTrue(is_blank_or_secure_screen(black_png))
        self.assertFalse(is_blank_or_secure_screen(real_png))
        jpeg = prepare_image_for_vlm(real_png, max_side=1024, quality=80)
        self.assertLess(len(jpeg), len(real_png))
        with Image.open(io.BytesIO(jpeg)) as out:
            self.assertEqual(max(out.size), 1024)
            self.assertEqual(out.format, "JPEG")
        self.assertEqual(denormalize_point((500, 800), 1080, 2400, jitter_px=0), (540, 1920))
        self.assertEqual(denormalize_point((1500, -50), 1080, 2400, jitter_px=0), (1078, 2))

    def test_03_json_extractor_and_universal_gateway(self) -> None:
        self.assertEqual(extract_json_object('```json\n{"vibe_score": 88}\n```')["vibe_score"], 88)
        self.assertEqual(extract_json_object('Note {x} -> {"vibe_score": 93, "ok": true} end')["vibe_score"], 93)

        gw_o = UniversalAIGateway(provider="openai", base_url="https://api.openai.com/v1/chat/completions", api_key="sk-1", model="gpt-4o-mini")
        self.assertEqual(gw_o._build_headers()["Authorization"], "Bearer sk-1")
        self.assertEqual(gw_o._resolve_endpoint_and_payload([{"role": "user", "content": "hi"}], 0.2, False)[0], "https://api.openai.com/v1/chat/completions")

        gw_a = UniversalAIGateway(provider="anthropic", base_url="https://api.anthropic.com", api_key="ant", model="claude")
        url_a, pay_a = gw_a._resolve_endpoint_and_payload([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}], 0.4, True, images=[b"\x89PNGx"])
        self.assertTrue(url_a.endswith("/v1/messages"))
        self.assertEqual(pay_a["system"], "s")
        self.assertEqual(gw_a._build_headers()["x-api-key"], "ant")

        gw_g = UniversalAIGateway(provider="gemini", base_url="https://generativelanguage.googleapis.com", api_key="AIza", model="gemini-2.5-flash")
        url_g, pay_g = gw_g._resolve_endpoint_and_payload([{"role": "user", "content": "hi"}], 0.3, True, images=[b"jpg"])
        self.assertIn("models/gemini-2.5-flash:generateContent", url_g)
        self.assertEqual(gw_g._build_headers()["x-goog-api-key"], "AIza")
        self.assertEqual(pay_g["generationConfig"]["responseMimeType"], "application/json")

        gw_c = UniversalAIGateway(provider="custom", api_key="tok", custom_header="X-Secret")
        self.assertEqual(gw_c._build_headers()["X-Secret"], "tok")

    def test_04_filter_engine_all_rules(self) -> None:
        eng = ProfileFilterEngine(FilterConfig(min_compatibility=70, min_age=18, max_age=35, max_distance_km=25, require_bio=True, min_score_threshold=65.0))
        spam = CandidateProfile(platform="twinby", user_id="1", name="Алина", age=23, bio="Пиши в inst: @spam", compatibility_score=98, photo_urls=["u"])
        self.assertEqual(eng.evaluate(spam).decision, SwipeDecision.PASS)

        empty = CandidateProfile(platform="twinby", user_id="2", name="Катя", age=24, bio="  ", compatibility_score=95, photo_urls=["u"])
        self.assertEqual(eng.evaluate(empty).decision, SwipeDecision.PASS)

        far = CandidateProfile(platform="twinby", user_id="3", name="Оля", age=24, bio="Кофе и кино", distance_km=99.0, compatibility_score=95, photo_urls=["u"])
        self.assertEqual(eng.evaluate(far).decision, SwipeDecision.PASS)

        good = CandidateProfile(platform="twinby", user_id="4", name="Лера", age=25, bio="Люблю спешелти кофе, путешествия и кино в Питере", compatibility_score=84, photo_urls=["u"], is_verified=True)
        res = eng.evaluate(good)
        self.assertEqual(res.decision, SwipeDecision.LIKE)
        self.assertGreater(res.score, 85.0)
        self.assertIn("кофе", res.matched_keywords)

    def test_05_face_checker_humanizer_proxy_and_ui_selectors(self) -> None:
        self.assertGreater(cosine_similarity([1, 0, 0], [0.95, 0.05, 0]), 0.99)
        self.assertTrue(evaluate_embeddings([[1, 0, 0], [0.95, 0.05, 0]], threshold=0.42).passed)
        self.assertFalse(evaluate_embeddings([[1, 0, 0], [-0.8, 0.6, 0]], threshold=0.42).passed)

        h = Humanizer(max_per_hour=2, burst_size=2, active_start_hour=9, active_end_hour=22, enforce_active_hours=True)
        self.assertTrue(h.is_within_active_hours(datetime(2026, 11, 19, 15, 0)))
        self.assertFalse(h.is_within_active_hours(datetime(2026, 11, 19, 3, 0)))
        h.record_action(1000.0)
        h.record_action(1001.0)
        self.assertTrue(h.hourly_cap_reached(1002.0))
        self.assertTrue(h.should_take_session_break())

        pm = GeoProxyManager([{"host": "1.1.1.1", "port": 8080, "region": "cis"}, {"host": "2.2.2.2", "port": 8080, "region": "us"}])
        self.assertEqual(pm.get_proxy_for_session("a", Region.CIS), "http://1.1.1.1:8080")
        self.assertEqual(pm.get_proxy_for_session("b", Region.US), "http://2.2.2.2:8080")

        self.assertIn("mamba", WEB_ACTION_SELECTORS)
        self.assertIn("okcupid", WEB_ACTION_SELECTORS)
        self.assertEqual(PureClient.platform_name, "pure")
        self.assertEqual(TaborClient.platform_name, "tabor")
        self.assertEqual(extract_dot_path({"a": {"b": [10, 20]}}, "a.b.1"), 20)

    def test_06_live_tcp_http_har_declarative_refresh_and_visual_pipelines(self) -> None:
        state = {"refreshed": False, "llm_calls": 0}

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def _send(self, code: int, obj: dict) -> None:
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                auth = self.headers.get("Authorization", "")
                if self.path.startswith("/api/feed"):
                    if auth != "Bearer fresh_tok":
                        self._send(401, {"error": "expired"})
                        return
                    self._send(200, {"data": {"items": [{"id": "u1", "name": "Соня", "age": 24, "bio": "Кофе и кино", "compatibility": {"score": 89}, "photos": [{"url": "http://x/1.jpg"}]}]}})
                    return
                self._send(404, {})

            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length", "0"))
                req = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                if self.path.startswith("/api/refresh"):
                    state["refreshed"] = True
                    self._send(200, {"access_token": "fresh_tok"})
                    return
                if self.path.startswith("/api/like"):
                    self._send(200, {"is_match": req.get("target_user_id") == "u1"})
                    return
                if self.path.startswith("/v1/chat/completions"):
                    state["llm_calls"] += 1
                    all_txt = json.dumps(req["messages"], ensure_ascii=False)
                    if "экрана переписки" in all_txt:
                        c = json.dumps({"is_chat_screen": True, "girl_name": "Соня", "messages": [{"role": "user", "content": "Привет!"}], "input_box": [500, 920], "send_button": [920, 920], "notes": "ok"})
                    elif "скриншот(ы) дейтинг-сервиса" in all_txt:
                        c = json.dumps({"screen_type": "profile", "is_profile_screen": True, "name": "Соня", "age": 24, "bio": "Кофе и винил", "distance_km": 2.0, "native_compatibility": 89, "vibe_score": 89, "is_verified": True, "is_ad_or_bot": False, "visual_hooks": ["чашка"], "tags": ["кофе"], "reasoning": "отлично", "like_button": [700, 850], "pass_button": [300, 850]})
                    elif "Проанализируй анкету" in all_txt:
                        c = "Короткий стиль."
                    elif "мастер живого общения" in all_txt:
                        c = json.dumps({"stage": "icebreaker", "tactic": "кофе", "draft_reply": "привет, как твой день?", "should_handoff": False, "handoff_summary": ""})
                    elif "редактор живой переписки" in all_txt:
                        c = json.dumps({"approved": True, "critique": "", "messages_chunks": ["привет)", "как твой день?"]})
                    elif "Pure" in all_txt:
                        c = json.dumps({"vibe_score": 86, "is_bot_or_ad": False, "reasoning": "вайб ок"})
                    else:
                        c = "OK"
                    self._send(200, {"choices": [{"message": {"content": c}}]})
                    return
                self._send(404, {})

        srv = HTTPServer(("127.0.0.1", 0), _Handler)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()

        async def _run() -> None:
            origin = f"http://127.0.0.1:{port}"
            with tempfile.TemporaryDirectory() as td:
                td_p = Path(td)
                # 1. HAR export + 401 auto-refresh + GET feed + POST like over real TCP/HTTP
                har_f = td_p / "cap.har"
                har_f.write_text(json.dumps({
                    "log": {"entries": [
                        {"request": {"method": "GET", "url": f"{origin}/api/feed", "headers": [{"name": "Authorization", "value": "Bearer x"}]},
                         "response": {"status": 200, "content": {"mimeType": "application/json", "text": '{"data": {"items": []}}'}}},
                        {"request": {"method": "POST", "url": f"{origin}/api/like", "headers": []},
                         "response": {"status": 200, "content": {"mimeType": "application/json", "text": '{"is_match": true}'}}},
                    ]}
                }), encoding="utf-8")
                rep = inspect_har(str(har_f), host_filter="127.0.0.1")
                self.assertEqual(rep["total_json_endpoints"], 2)
                prof_file = export_api_profile_from_har(str(har_f), platform="demo", host_filter="127.0.0.1", output_dir=str(td_p))
                # Inject refresh endpoint into declarative profile
                pdata = json.loads(prof_file.read_text(encoding="utf-8"))
                pdata["refresh"] = {"method": "POST", "path": "/api/refresh"}
                prof_file.write_text(json.dumps(pdata), encoding="utf-8")

                async with DeclarativeRestDatingClient.from_profile_file(prof_file, token="expired_tok", refresh_token="ref_123") as cl:
                    cands = await cl.get_recommendations(limit=5)
                    self.assertTrue(state["refreshed"])
                    self.assertEqual(len(cands), 1)
                    self.assertEqual(cands[0].name, "Соня")
                    sw = await cl.swipe("u1", is_like=True)
                    self.assertTrue(sw["is_match"])

                # 2. Pure synthetic scorer over live TCP
                gw = UniversalAIGateway(provider="openai", base_url=f"{origin}/v1", api_key="sk-live", model="gpt-4o-mini")
                sc = SyntheticCompatibilityScorer(gateway=gw)
                sr = await sc.compute_compatibility(CandidateProfile(platform="pure", user_id="p1", name="Аноним", age=23, bio="Джаз", photo_urls=["u"]), "джаз")
                self.assertEqual(sr.vibe_score, 86)

                # 3. Visual swipe + VisualChatAssistant over live TCP
                real_png = _make_png(black=False, size=(800, 1600))
                taps, typed = [], []

                class _Drv:
                    screen_size = (1080, 2400)
                    async def capture_screenshot(self): return real_png
                    async def capture_profile_views(self, include_second_view=False): return [real_png]
                    async def execute_profile_action(self, is_like, like_button=None, pass_button=None):
                        taps.append((is_like, like_button))
                        return "vlm_tap"
                    async def type_and_send_text(self, text, input_box=None, send_button=None):
                        typed.append((text, input_box, send_button))

                class _DB:
                    def __init__(self): self.s, self.t, self.m = [], {}, {}
                    async def is_already_swiped(self, u): return u in self.s
                    async def save_swipe(self, profile, *a, **kw): self.s.append(profile.user_id)
                    async def get_today_likes_count(self): return 0
                    async def get_or_create_thread(self, thread_key, girl_name, platform="twinby", profile_context="", visual_hooks=None):
                        if thread_key not in self.t:
                            tid = len(self.t) + 1
                            self.t[thread_key] = type("T", (), {"id": tid, "girl_name": girl_name, "profile_context": profile_context})()
                            self.m[tid] = []
                        return self.t[thread_key]
                    async def append_message(self, tid, role, content): self.m[tid].append({"role": role, "content": content})
                    async def get_thread_history(self, tid, limit=16): return list(self.m[tid][-limit:])
                    async def update_thread_meta(self, *a, **kw): pass

                db = _DB()
                w = SwipeWorker(db=db, filter_engine=ProfileFilterEngine(FilterConfig(min_compatibility=70, min_age=18, max_age=35, max_distance_km=25, require_bio=False, min_score_threshold=60.0)), ui_driver=_Drv(), vision_parser=VisionProfileParser(gateway=gw), mode="ui_vision", platform="twinby", dry_run=False)
                r = await w.process_single_visual_screen()
                self.assertEqual(r["decision"], "like")
                self.assertEqual(taps, [(True, (700, 850))])

                bandit = PromptThompsonBandit(redis_client=None, state_file=str(td_p / "bandit.json"))
                agent = AutonomousChatAgent(gateway=gw, bandit=bandit)
                vchat = VisualChatAssistant(ui_driver=_Drv(), db=db, chat_agent=agent, vision_parser=VisionProfileParser(gateway=gw), platform="twinby")
                dr = await vchat.capture_and_draft(auto_send=False)
                self.assertTrue(dr["ok"])
                self.assertEqual(dr["chunks"], ["привет)", "как твой день?"])
                self.assertIn(dr["bandit_arm"], PromptThompsonBandit.DEFAULT_ARMS)
                sr_chat = await vchat.send_draft(dr["draft_id"])
                self.assertTrue(sr_chat["sent"])
                self.assertEqual(len(typed), 2)

        try:
            asyncio.run(_run())
        finally:
            srv.shutdown()



    def test_07_backup_service_events_calendar_metrics_and_chat_worker_bandit_reward(self) -> None:
        from ai.calendar_tool import book_dating_event, list_busy_slots
        from core.backup_service import DatabaseBackupService
        from core.chat_worker import ChatWorker, PendingDraft
        from core.client import TwinbyStealthClient
        from core.factory import create_dating_client, list_verified_api_profiles
        from core.metrics import API_ERRORS_TOTAL, CV_INFERENCE_SECONDS, HANDOFFS_TOTAL, MATCHES_TOTAL, SWIPES_TOTAL, WORKER_ACTIVE
        from scripts.sync_to_repo import sync_directories
        from web.events import RealtimeEventBus
        from web.schemas import BookCalendarRequest, FilterUpdateRequest, SwitchPlatformRequest

        # Metrics smoke check
        SWIPES_TOTAL.labels(decision="like").inc()
        API_ERRORS_TOTAL.labels(status_code="429").inc()
        MATCHES_TOTAL.inc()
        HANDOFFS_TOTAL.inc()
        CV_INFERENCE_SECONDS.observe(0.12)
        WORKER_ACTIVE.set(1)

        # Schemas validation
        self.assertEqual(SwitchPlatformRequest(platform="pure", launch_on_device=True).platform, "pure")
        self.assertEqual(FilterUpdateRequest(min_compatibility=80).min_compatibility, 80)
        self.assertEqual(BookCalendarRequest(slot_iso_string="2026-11-21T19:00:00", venue_name="Surf", girl_name="Соня").girl_name, "Соня")

        # Factory for all 28 platforms (without curl_cffi!)
        pm = GeoProxyManager([])
        for pid in GLOBAL_DATING_REGISTRY:
            c = create_dating_client(pid, f"s_{pid}", {"token": "tok_123"}, pm, profiles_dir="/tmp/nonexistent_profiles_dir")
            self.assertEqual(c.platform_name, pid)

        # Real SQLite online backup & rotation
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            db_f = td_p / "heart3.db"
            conn = sqlite3.connect(db_f)
            conn.execute("CREATE TABLE swipe_history (id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute("INSERT INTO swipe_history VALUES (1, 'Соня')")
            conn.commit()
            conn.close()

            bsvc = DatabaseBackupService(bot=None, db_path=str(db_f), retention_count=2)
            b1 = bsvc.create_safe_backup_sync()
            self.assertIsNotNone(b1)
            assert b1 is not None
            c2 = sqlite3.connect(b1)
            self.assertEqual(c2.execute("SELECT name FROM swipe_history WHERE id=1").fetchone()[0], "Соня")
            c2.close()

            # Test scripts/sync_to_repo.py preserving .git, .env, data/
            fake_src = td_p / "src"
            fake_repo = td_p / "repo"
            (fake_src / "core").mkdir(parents=True)
            (fake_src / "core" / "new.py").write_text("x = 1", encoding="utf-8")
            (fake_repo / ".git").mkdir(parents=True)
            (fake_repo / "data").mkdir(parents=True)
            (fake_repo / ".env").write_text("SECRET=1", encoding="utf-8")
            (fake_repo / "data" / "heart3.db").write_text("db", encoding="utf-8")
            (fake_repo / "stale_old.py").write_text("old", encoding="utf-8")

            res_sync = sync_directories(fake_src, fake_repo)
            self.assertEqual(res_sync["copied"], 1)
            self.assertEqual(res_sync["deleted"], 1)
            self.assertTrue((fake_repo / ".env").exists())
            self.assertTrue((fake_repo / "data" / "heart3.db").exists())
            self.assertFalse((fake_repo / "stale_old.py").exists())
            self.assertTrue((fake_repo / "core" / "new.py").exists())

            async def _async_checks() -> None:
                # Calendar safe message when no OAuth token configured (isolated from host env)
                import os as _os
                saved_g = {k: _os.environ.pop(k, None) for k in ("GOOGLE_ACCESS_TOKEN", "GOOGLE_REFRESH_TOKEN")}
                try:
                    slots_msg = await list_busy_slots(3)
                    self.assertIn("не настроен", slots_msg)
                    book_msg = await book_dating_event("2026-11-21T19:00:00", "Surf", "Соня")
                    self.assertIn("не настроен", book_msg)
                finally:
                    for k, v in saved_g.items():
                        if v is not None:
                            _os.environ[k] = v

                # RealtimeEventBus live publish & dead socket pruning
                bus = RealtimeEventBus()
                sent_payloads = []

                class _LiveWS:
                    async def accept(self): pass
                    async def send_text(self, txt): sent_payloads.append(json.loads(txt))

                class _DeadWS:
                    async def accept(self): pass
                    async def send_text(self, txt): raise ConnectionError("closed")

                ws_ok, ws_dead = _LiveWS(), _DeadWS()
                await bus.connect(ws_ok)
                await bus.connect(ws_dead)
                await bus.publish("swipe", {"name": "Соня"})
                self.assertEqual(len(sent_payloads), 1)
                self.assertEqual(sent_payloads[0]["data"]["name"], "Соня")
                self.assertNotIn(ws_dead, bus._connections)

                # ChatWorker draft approve tracks bandit arm
                fake_cl = AsyncMock()
                fake_cl.send_message = AsyncMock()
                bandit = PromptThompsonBandit(redis_client=None, state_file=str(td_p / "b2.json"))
                agent = AutonomousChatAgent(gateway=AsyncMock(), bandit=bandit)
                cw = ChatWorker(client=fake_cl, bot=AsyncMock(), chat_agent=agent, auto_send=False)
                cw.pending_drafts["d1"] = PendingDraft("d1", "m1", "Соня", "bio", [], ["привет)"], "icebreaker", bandit_arm="bold_tease")
                msg = await cw.approve_draft("d1")
                self.assertIn("Отправлено", msg)
                self.assertEqual(cw._match_arms.get("m1"), "bold_tease")

            asyncio.run(_async_checks())

    def test_08_live_tcp_twinby_pure_tabor_clients_with_httpx_fallback(self) -> None:
        from core.client import TwinbyStealthClient

        class _MultiHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args): pass
            def _send(self, code, obj):
                b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            def do_GET(self):
                if "recommendations" in self.path:
                    self._send(200, {"items": [{"id": "t1", "name": "Ника", "age": 23, "compatibility": {"score": 88}, "photos": [{"url": "http://u/1.jpg"}]}]})
                elif "feed" in self.path:
                    self._send(200, {"items": [{"id": "p1", "title": "Кофе ночью", "age": 24, "text": "Джаз", "photos": [{"url": "http://u/p.jpg"}]}]})
                elif "encounters" in self.path:
                    self._send(200, {"data": [{"id": "tb1", "name": "Аня", "age": 25, "about": "Кино", "photos": [{"url": "http://u/t.jpg"}]}]})
                else:
                    self._send(200, {"items": [], "data": []})
            def do_POST(self):
                # HTTP/1.0-мок закрывает сокет сразу: непрочитанное тело -> RST -> httpx ReadError.
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self._send(200, {"is_match": True, "is_matched": True, "is_mutual": True})

        srv = HTTPServer(("127.0.0.1", 0), _MultiHandler)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        origin = f"http://127.0.0.1:{port}"

        async def _run_clients():
            async with TwinbyStealthClient(access_token="tok", base_url=origin) as tw:
                recs = await tw.get_recommendations(5)
                self.assertEqual(recs[0].name, "Ника")
                self.assertTrue((await tw.swipe("t1", True))["is_match"])
            async with PureClient(device_token="dtok", base_url=origin) as pu:
                recs = await pu.get_recommendations(5)
                self.assertEqual(recs[0].compatibility_score, 0)
                self.assertTrue((await pu.swipe("p1", True))["is_match"])
            async with TaborClient(session_token="stok", base_url=origin) as tb:
                recs = await tb.get_recommendations(5)
                self.assertEqual(recs[0].name, "Аня")
                self.assertTrue((await tb.swipe("tb1", True))["is_match"])

        try:
            asyncio.run(_run_clients())
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
