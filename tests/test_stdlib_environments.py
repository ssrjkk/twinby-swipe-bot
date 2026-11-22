"""Средовые E2E и Chaos-тесты с реальными локальными серверами и исполняемым ADB-эмулятором:
1. Реальный subprocess ADB-эмулятор в временном PATH (connect, screencap, launch_app, tap, scroll, swipe, UTF-8 text, missing-adb error).
2. Реальный локальный W3C Appium HTTP-сервер (Android UiAutomator2 + iOS XCUITest: session, rect, screenshot, actions, back, activate_app, delete).
3. Реальный локальный Multi-Provider LLM + Chaos HTTP-сервер (OpenAI + Anthropic + Native Gemini + Custom Header + HTTP 400 response_format fallback + HTTP 429/503 retries).
4. Реальный локальный HTTP-сервер для GeoProxyManager.check_all_once() (latency, 3-fail deactivation, recovery) и GenericRestDatingClient + 204 No Content.
5. Полные сценарии SwipeWorker (FLAG_SECURE, match_popup auto-dismiss, promo_popup auto-dismiss, paywall, duplicate, daily_like_limit mid-batch enforcement).
"""
import asyncio
import base64
import contextlib
import io
import json
import os
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from PIL import Image

from ai.universal_llm import UniversalAIGateway
from core.adapters.declarative_client import DeclarativeAPIProfile, DeclarativeRestDatingClient, EndpointSpec
from core.adapters_registry import GLOBAL_DATING_REGISTRY
from core.base_client import CandidateProfile
from core.factory import GenericRestDatingClient
from core.proxy.manager import GeoProxyManager
from core.ui_driver import MobileUIDriverFallback
from core.worker import SwipeWorker
from database.models import SwipeDecision
from filters.engine import FilterConfig, ProfileFilterEngine
from filters.vision_card_parser import VisionProfileParser

try:  # pytest видит tests пакетом, а `unittest discover -s tests` — плоской папкой
    from tests._net_env import hermetic_proxy
except ImportError:
    from _net_env import hermetic_proxy

hermetic_proxy()


def _make_png(black: bool = False, size: tuple[int, int] = (480, 960)) -> bytes:
    img = Image.new("RGB", size, (0, 0, 0))
    if not black:
        px = img.load()
        for y in range(0, size[1], 12):
            for x in range(0, size[0], 12):
                px[x, y] = (x % 255, y % 255, (x + y) % 255)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestLiveEnvironments(unittest.TestCase):
    def test_01_real_subprocess_adb_environment_and_missing_adb_error(self) -> None:
        png_bytes = _make_png(black=False, size=(320, 640))
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            log_file = td_p / "adb.log"
            shot_file = td_p / "frame.png"
            shot_file.write_bytes(png_bytes)

            # Create a real executable fake `adb` binary (cross-platform: Windows .cmd + POSIX executable)
            import sys as _sys
            bin_dir = td_p / "bin"
            bin_dir.mkdir()
            adb_py_code = (
                "import sys\n"
                f"with open({str(log_file)!r}, 'a', encoding='utf-8') as f:\n"
                "    f.write(' '.join(sys.argv[1:]) + chr(10))\n"
                "args = sys.argv[1:]\n"
                "if 'wm' in args and 'size' in args:\n"
                "    print('Physical size: 1080x2400')\n"
                "elif 'screencap' in args:\n"
                f"    sys.stdout.buffer.write(open({str(shot_file)!r}, 'rb').read())\n"
                "elif 'monkey' in args:\n"
                "    print('Events injected: 1')\n"
            )
            if os.name == "nt":
                adb_py = bin_dir / "adb_stub.py"
                adb_py.write_text(adb_py_code, encoding="utf-8")
                (bin_dir / "adb.cmd").write_text(f'@echo off\r\n"{_sys.executable}" "{adb_py}" %*\r\n', encoding="utf-8")
            else:
                adb_bin = bin_dir / "adb"
                adb_bin.write_text("#!/usr/bin/env python3\n" + adb_py_code, encoding="utf-8")
                os.chmod(adb_bin, os.stat(adb_bin).st_mode | stat.S_IEXEC)

            old_path = os.environ.get("PATH", "")

            async def _run_adb_suite() -> None:
                os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
                drv = MobileUIDriverFallback(device_udid="emu-5554", platform_package="com.twinby.app", backend="adb")
                await drv.connect()
                self.assertEqual(drv.screen_size, (1080, 2400))

                await drv.launch_app("com.twinby.app")
                views = await drv.capture_profile_views(include_second_view=True)
                self.assertEqual(len(views), 2)
                self.assertEqual(views[0][:4], b"\x89PNG")

                action = await drv.execute_profile_action(is_like=True, like_button=(750, 850))
                self.assertTrue(action.startswith("vlm_tap("))
                await drv.dismiss_popup(None)  # keyevent 4
                await drv.type_and_send_text("Привет <3!", input_box=(500, 900), send_button=(920, 900))
                await drv.swipe_with_bezier_curve(is_like=False)

                # Verify missing adb binary produces clean RuntimeError (NOT raw FileNotFoundError!)
                os.environ["PATH"] = str(td_p / "empty_path")
                with self.assertRaises(RuntimeError) as ctx:
                    await drv.connect()
                self.assertIn("adb", str(ctx.exception).lower())

            try:
                asyncio.run(_run_adb_suite())
            finally:
                os.environ["PATH"] = old_path

            cmds = log_file.read_text(encoding="utf-8")
            self.assertIn("-s emu-5554 shell wm size", cmds)
            self.assertIn("monkey -p com.twinby.app", cmds)
            self.assertIn("exec-out screencap -p", cmds)
            self.assertIn("ADB_INPUT_B64", cmds)
            self.assertIn("keyevent 4", cmds)
            self.assertIn("input swipe", cmds)

    def test_02_real_w3c_appium_http_server_android_and_ios(self) -> None:
        png_b64 = base64.b64encode(_make_png(black=False, size=(200, 400))).decode("ascii")
        events_seen: list[tuple[str, str]] = []

        class _AppiumHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args): pass
            def _send(self, code: int, obj: dict):
                b = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                events_seen.append(("POST", self.path))
                if self.path == "/session":
                    caps = body["capabilities"]["alwaysMatch"]
                    sid = f"sess_{caps['platformName'].lower()}"
                    self._send(200, {"value": {"sessionId": sid}})
                else:
                    self._send(200, {"value": None})

            def do_GET(self):
                events_seen.append(("GET", self.path))
                if self.path.endswith("/window/rect"):
                    self._send(200, {"value": {"width": 1170, "height": 2532}})
                elif self.path.endswith("/screenshot"):
                    self._send(200, {"value": png_b64})
                else:
                    self._send(404, {})

            def do_DELETE(self):
                events_seen.append(("DELETE", self.path))
                self._send(200, {"value": None})

        srv = HTTPServer(("127.0.0.1", 0), _AppiumHandler)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()

        async def _run_appium() -> None:
            url = f"http://127.0.0.1:{port}"
            for os_name, pkg in [("Android", "com.twinby.app"), ("iOS", "co.hinge.mobile.ios")]:
                drv = MobileUIDriverFallback(
                    device_udid="dev-1",
                    platform_package=pkg,
                    appium_url=url,
                    backend="appium",
                    device_os=os_name,
                )
                await drv.connect()
                self.assertEqual(drv.screen_size, (1170, 2532))
                shot = await drv.capture_screenshot()
                self.assertEqual(shot[:4], b"\x89PNG")
                await drv.launch_app(pkg)
                await drv.execute_profile_action(is_like=True, like_button=(700, 850))
                await drv.swipe_with_bezier_curve(is_like=False)
                await drv.type_and_send_text("hi", input_box=(400, 900), send_button=(900, 900))
                await drv.dismiss_popup(None)
                await drv.disconnect()

        try:
            asyncio.run(_run_appium())
        finally:
            srv.shutdown()

        paths = [p for _, p in events_seen]
        self.assertIn("/session", paths)
        self.assertTrue(any(p.endswith("/appium/device/activate_app") for p in paths))
        self.assertTrue(any(p.endswith("/actions") for p in paths))
        self.assertTrue(any(p.endswith("/back") for p in paths))
        self.assertIn(("DELETE", "/session/sess_ios"), events_seen)

    def test_03_multi_provider_llm_and_chaos_retries_live_tcp(self) -> None:
        counters = {"openai_attempts": 0, "anthropic_ok": 0, "gemini_ok": 0, "custom_ok": 0}

        class _LLMServer(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args): pass
            def _send(self, code: int, obj: dict):
                b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                # 1. OpenAI chaos: 1st call returns 400 when response_format present, 2nd returns 429, 3rd succeeds!
                if self.path == "/openai/v1/chat/completions":
                    counters["openai_attempts"] += 1
                    if "response_format" in body:
                        self._send(400, {"error": "response_format unsupported"})
                        return
                    if counters["openai_attempts"] == 2:
                        self._send(429, {"error": "rate limit"})
                        return
                    self._send(200, {"choices": [{"message": {"content": 'Here is JSON: {"ok": true, "src": "openai"}'}}]})
                    return
                # 2. Anthropic native /v1/messages
                if self.path == "/anthropic/v1/messages":
                    if self.headers.get("x-api-key") != "ant-secret":
                        self._send(401, {"error": "bad key"})
                        return
                    counters["anthropic_ok"] += 1
                    self._send(200, {"content": [{"type": "text", "text": '{"ok": true, "src": "anthropic"}'}]})
                    return
                # 3. Gemini native :generateContent
                if self.path == "/gemini/v1beta/models/gemini-2.5-flash:generateContent":
                    if self.headers.get("x-goog-api-key") != "gem-secret":
                        self._send(401, {"error": "bad key"})
                        return
                    counters["gemini_ok"] += 1
                    self._send(200, {"candidates": [{"content": {"parts": [{"text": '{"ok": true, "src": "gemini"}'}]}}]})
                    return
                # 4. Custom header endpoint
                if self.path == "/custom/chat/completions":
                    if self.headers.get("X-My-Key") != "custom-secret":
                        self._send(401, {"error": "bad key"})
                        return
                    counters["custom_ok"] += 1
                    self._send(200, {"choices": [{"message": {"content": '{"ok": true, "src": "custom"}'}}]})
                    return
                self._send(404, {})

        srv = HTTPServer(("127.0.0.1", 0), _LLMServer)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()

        async def _run_llm() -> None:
            base_origin = f"http://127.0.0.1:{port}"
            png = _make_png(black=False, size=(64, 64))

            gw_o = UniversalAIGateway(provider="openai", base_url=f"{base_origin}/openai/v1", api_key="sk-1", model="gpt-4o-mini", max_retries=3)
            res_o = await gw_o.complete_json([{"role": "user", "content": "hi"}], images=[png])
            self.assertEqual(res_o["src"], "openai")

            gw_a = UniversalAIGateway(provider="anthropic", base_url=f"{base_origin}/anthropic", api_key="ant-secret", model="claude-3-5-sonnet")
            res_a = await gw_a.complete_json([{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}], images=[png])
            self.assertEqual(res_a["src"], "anthropic")

            gw_g = UniversalAIGateway(provider="gemini", base_url=f"{base_origin}/gemini", api_key="gem-secret", model="gemini-2.5-flash")
            res_g = await gw_g.complete_json([{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}], images=[png])
            self.assertEqual(res_g["src"], "gemini")

            gw_c = UniversalAIGateway(provider="custom", base_url=f"{base_origin}/custom", api_key="custom-secret", model="local-vlm", custom_header="X-My-Key")
            res_c = await gw_c.complete_json([{"role": "user", "content": "hi"}])
            self.assertEqual(res_c["src"], "custom")

        try:
            asyncio.run(_run_llm())
        finally:
            srv.shutdown()

        self.assertGreaterEqual(counters["openai_attempts"], 3)  # 400 fallback + 429 retry + 200 ok
        self.assertEqual(counters["anthropic_ok"], 1)
        self.assertEqual(counters["gemini_ok"], 1)
        self.assertEqual(counters["custom_ok"], 1)

    def test_04_generic_client_204_no_content_and_proxy_healthcheck(self) -> None:
        class _ApiServer(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args): pass
            def do_GET(self):
                if "/ip" in self.path:
                    b = b'{"ip":"127.0.0.1"}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    self.wfile.write(b)
                elif "/recommendations" in self.path:
                    b = json.dumps({"items": [{"id": "g1", "name": "Мила", "age": 22, "bio": "Кофе", "photos": [{"url": "http://x/1.jpg"}]}]}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    self.wfile.write(b)
                else:
                    self.send_response(204)
                    self.end_headers()

            def do_POST(self):
                # HTTP/1.0-мок закрывает сокет сразу: непрочитанное тело -> RST -> httpx ReadError.
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path == "/swipes/pass_204":
                    self.send_response(204)
                    self.end_headers()
                else:
                    b = b'{"is_match": true}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    self.wfile.write(b)

        srv = HTTPServer(("127.0.0.1", 0), _ApiServer)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        origin = f"http://127.0.0.1:{port}"

        async def _run() -> None:
            # 1. GenericRestDatingClient works over httpx fallback!
            spec = GLOBAL_DATING_REGISTRY["mamba"]
            async with GenericRestDatingClient(spec=spec, token="tok", base_url=origin) as gc:
                recs = await gc.get_recommendations(5)
                self.assertEqual(recs[0].name, "Мила")
                sw = await gc.swipe("g1", is_like=True)
                self.assertTrue(sw["is_match"])

            # 2. DeclarativeRestDatingClient handles 204 No Content without crashing!
            prof = DeclarativeAPIProfile(
                platform="test204",
                base_url=origin,
                feed=EndpointSpec(method="GET", path="/recommendations"),
                like=EndpointSpec(method="POST", path="/swipes/like"),
                pass_action=EndpointSpec(method="POST", path="/swipes/pass_204"),
            )
            async with DeclarativeRestDatingClient(prof, token="t") as dc:
                res_pass = await dc.swipe("g1", is_like=False)
                self.assertFalse(res_pass["is_match"])

            # 3. GeoProxyManager health check updates latency_ms
            pm = GeoProxyManager([{"host": "127.0.0.1", "port": port, "region": "cis"}])
            await pm.check_all_once(check_url=f"{origin}/ip")
            self.assertTrue(pm._proxies[0].is_active)
            self.assertGreaterEqual(pm._proxies[0].latency_ms, 0.0)

        try:
            asyncio.run(_run())
        finally:
            srv.shutdown()

    def test_05_worker_popups_paywall_flag_secure_and_daily_limit(self) -> None:
        black_png = _make_png(black=True, size=(200, 400))
        real_png = _make_png(black=False, size=(200, 400))
        dismissed: list = []

        class _FakeDrv:
            def __init__(self, frames):
                self.frames = list(frames)
            async def capture_profile_views(self, include_second_view=False):
                return [self.frames.pop(0)]
            async def dismiss_popup(self, btn=None):
                dismissed.append(btn)
            async def execute_profile_action(self, is_like, like_button=None, pass_button=None):
                return "vlm_tap"

        class _FakeGW:
            def __init__(self, responses):
                self.responses = list(responses)
            async def complete_json(self, **kw):
                return self.responses.pop(0)

        class _MemDB:
            def __init__(self): self.seen = set()
            async def is_already_swiped(self, u): return u in self.seen
            async def save_swipe(self, profile, *a, **kw): self.seen.add(profile.user_id)
            async def get_today_likes_count(self): return 0

        async def _run() -> None:
            drv = _FakeDrv([black_png, real_png, real_png, real_png])
            gw = _FakeGW([
                {"screen_type": "match_popup", "is_profile_screen": False, "reasoning": "Мэтч!", "dismiss_button": [500, 900]},
                {"screen_type": "promo_popup", "is_profile_screen": False, "reasoning": "Куки", "dismiss_button": [920, 80]},
                {"screen_type": "paywall", "is_profile_screen": False, "reasoning": "Лимит"},
            ])
            w = SwipeWorker(
                db=_MemDB(),
                filter_engine=ProfileFilterEngine(FilterConfig(min_compatibility=70, min_age=18, max_age=35, max_distance_km=25, require_bio=False, min_score_threshold=60.0)),
                ui_driver=drv,
                vision_parser=VisionProfileParser(gateway=gw),
                mode="ui_vision",
                platform="twinby",
                dry_run=False,
            )
            r1 = await w.process_single_visual_screen()
            self.assertEqual(r1["screen_type"], "secure_black_screen")

            r2 = await w.process_single_visual_screen()
            self.assertEqual(r2["screen_type"], "match_popup")

            r3 = await w.process_single_visual_screen()
            self.assertEqual(r3["screen_type"], "promo_popup")

            r4 = await w.process_single_visual_screen()
            self.assertEqual(r4["screen_type"], "paywall")

            self.assertEqual(dismissed, [(500, 900), (920, 80)])

        asyncio.run(_run())



    def test_06_cli_driver_builder_and_setup_wizard(self) -> None:
        import builtins
        import cli as cli_mod

        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as td:
            os.chdir(td)
            answers = iter([
                "web_vision", "mamba", "gemini", "", "", "AIza-demo", "", "кофе", "", "0"
            ])
            old_input = builtins.input
            builtins.input = lambda prompt="": next(answers)
            try:
                rc = cli_mod._cmd_setup()
                self.assertEqual(rc, 0)
            finally:
                builtins.input = old_input
                os.chdir(old_cwd)

            env_text = (Path(td) / ".env").read_text(encoding="utf-8")
            self.assertIn("MODE=web_vision", env_text)
            self.assertIn("PLATFORM=mamba", env_text)
            self.assertIn("AI_PROVIDER=gemini", env_text)
            self.assertIn("DRY_RUN=true", env_text)

        from config import settings as _s
        old_plat, old_pkg, old_os = _s.platform, _s.app_package, _s.device_os
        try:
            _s.platform, _s.app_package, _s.device_os = "twinby", "", "Android"
            drv = cli_mod._build_visual_driver()
            self.assertEqual(drv.platform_package, "com.twinby.app")
        finally:
            _s.platform, _s.app_package, _s.device_os = old_plat, old_pkg, old_os



    def test_07_recursive_repo_sync_prunes_nested_stale_files_and_preserves_user_data(self) -> None:
        from scripts.sync_to_repo import sync_directories

        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            src_dir = td_p / "src"
            repo_dir = td_p / "repo"

            (src_dir / "core").mkdir(parents=True)
            (src_dir / "core" / "keep.py").write_text("v = 2", encoding="utf-8")

            # Existing repo has stale nested file, stale subdir, AND user secrets/data
            (repo_dir / ".git").mkdir(parents=True)
            (repo_dir / ".git" / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
            (repo_dir / "data").mkdir(parents=True)
            (repo_dir / "data" / "heart3.db").write_text("sqlite_data", encoding="utf-8")
            (repo_dir / ".env").write_text("LLM_API_KEY=real", encoding="utf-8")
            (repo_dir / ".env.bak").write_text("LLM_API_KEY=old", encoding="utf-8")
            (repo_dir / "core").mkdir(parents=True)
            (repo_dir / "core" / "stale_nested.py").write_text("old", encoding="utf-8")
            (repo_dir / "legacy_pkg").mkdir(parents=True)
            (repo_dir / "legacy_pkg" / "old.py").write_text("old", encoding="utf-8")

            stats = sync_directories(src_dir, repo_dir, dry_run=False)
            self.assertEqual(stats["copied"], 1)
            self.assertEqual(stats["deleted"], 2)
            # Stale nested file & empty legacy dir MUST be deleted!
            self.assertFalse((repo_dir / "core" / "stale_nested.py").exists())
            self.assertFalse((repo_dir / "legacy_pkg").exists())
            # New file synced & all user data preserved!
            self.assertTrue((repo_dir / "core" / "keep.py").exists())
            self.assertTrue((repo_dir / ".git" / "HEAD").exists())
            self.assertTrue((repo_dir / ".env").exists())
            self.assertTrue((repo_dir / ".env.bak").exists())
            self.assertTrue((repo_dir / "data" / "heart3.db").exists())

    def test_08_llm_disabled_heuristic_callback_isolation_and_cli_platforms(self) -> None:
        import builtins
        import cli as cli_mod
        from config import settings
        from filters.synthetic_scorer import SyntheticCompatibilityScorer

        # 1. cli._cmd_platforms() lists all 28 platforms and returns 0
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(cli_mod._cmd_platforms(), 0)
        listing = stdout.getvalue()
        missing = [pid for pid in GLOBAL_DATING_REGISTRY if pid not in listing]
        self.assertEqual(missing, [], f"CLI не показывает платформы реестра: {missing}")
        self.assertEqual(len([ln for ln in listing.splitlines() if "|" in ln]), len(GLOBAL_DATING_REGISTRY))

        # 2. cli._cmd_setup() backs up existing .env to .env.bak
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as td:
            os.chdir(td)
            Path(".env").write_text("OLD_KEY=123\n", encoding="utf-8")
            answers = iter(["ui_vision", "twinby", "openai", "", "", "sk-new", "", "", "", "0"])
            old_input = builtins.input
            builtins.input = lambda prompt="": next(answers)
            try:
                self.assertEqual(cli_mod._cmd_setup(), 0)
            finally:
                builtins.input = old_input
                os.chdir(old_cwd)
            self.assertEqual((Path(td) / ".env.bak").read_text(encoding="utf-8"), "OLD_KEY=123\n")

        # 3. SyntheticCompatibilityScorer respects settings.llm_enabled=False without calling LLM
        old_llm = settings.llm_enabled
        settings.llm_enabled = False
        try:
            class _NeverCallGW:
                async def complete_json(self, **kw):
                    raise AssertionError("LLM must NOT be called when LLM_ENABLED=false!")
            sc = SyntheticCompatibilityScorer(gateway=_NeverCallGW())
            res = asyncio.run(
                sc.compute_compatibility(
                    CandidateProfile(platform="pure", user_id="p9", name="Аноним", age=24, bio="Очень люблю спешелти кофе и джаз", photo_urls=["u"]),
                    user_preferences="кофе",
                )
            )
            self.assertEqual(res.vibe_score, 74)
            self.assertIn("LLM_ENABLED=false", res.reasoning)
        finally:
            settings.llm_enabled = old_llm

        # 4. SwipeWorker._record_and_emit isolates failing on_decision / on_match callbacks!
        class _DB:
            async def save_swipe(self, *a, **kw): pass
        async def _boom(*a, **kw):
            raise RuntimeError("Telegram temporary outage")

        w = SwipeWorker(
            db=_DB(),
            filter_engine=ProfileFilterEngine(),
            on_decision=_boom,
            on_match=_boom,
            dry_run=True,
        )
        prof = CandidateProfile(platform="twinby", user_id="u_iso", name="Ника", age=23, bio="Кофе", photo_urls=["u"])
        # Must NOT raise even though both callbacks throw RuntimeError!
        asyncio.run(w._record_and_emit(prof, SwipeDecision.LIKE, 88.0, "ok", is_match=True))


if __name__ == "__main__":
    unittest.main()
