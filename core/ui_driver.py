import asyncio
import base64
import logging
import random
import re
import shutil
from pathlib import Path
from typing import Any

import httpx

from core.image_utils import denormalize_point

logger = logging.getLogger(__name__)


WEB_ACTION_SELECTORS: dict[str, dict[str, list[str]]] = {
    "mamba": {
        "like": ["button[data-name='like-action']", "button[aria-label*='Нравится']"],
        "pass": ["button[data-name='dislike-action']", "button[aria-label*='Пропустить']"],
    },
    "vk_dating": {
        "like": ["button[aria-label*='Лайк']", "button[data-testid='like-button']"],
        "pass": ["button[aria-label*='Пропустить']", "button[data-testid='dislike-button']"],
    },
    "ok_dating": {
        "like": ["button[aria-label*='Нравится']", "button[data-l*='like']"],
        "pass": ["button[aria-label*='Пропустить']", "button[data-l*='dislike']"],
    },
    "tabor": {
        "like": ["button.btn-like", "a[href*='like']", "button[aria-label*='Нравится']"],
        "pass": ["button.btn-skip", "a[href*='skip']", "button[aria-label*='Пропустить']"],
    },
    "loveplanet": {
        "like": ["button[data-action='like']", "button[title*='Нравится']"],
        "pass": ["button[data-action='skip']", "button[title*='Пропустить']"],
    },
    "fotostrana": {
        "like": ["button[data-action='yes']", "button[aria-label*='Да']"],
        "pass": ["button[data-action='no']", "button[aria-label*='Нет']"],
    },
    "kismia": {
        "like": ["button[data-testid='like']", "button.js-like"],
        "pass": ["button[data-testid='dislike']", "button.js-dislike"],
    },
    "badoo": {
        "like": ["div[data-qa-role='encounters-action-like']", "button[aria-label='Yes']"],
        "pass": ["div[data-qa-role='encounters-action-dislike']", "button[aria-label='No']"],
    },
    "tinder": {
        "like": ["button[aria-label='Like']", "button[aria-label='Нравится']"],
        "pass": ["button[aria-label='Nope']", "button[aria-label='Нет']"],
    },
    "bumble": {
        "like": ["div[data-qa-role='encounters-action-like']", "button[aria-label='Like']"],
        "pass": ["div[data-qa-role='encounters-action-dislike']", "button[aria-label='Pass']"],
    },
    "okcupid": {
        "like": ["button.dt-action-buttons-like", "button[aria-label='LIKE']"],
        "pass": ["button.dt-action-buttons-pass", "button[aria-label='PASS']"],
    },
}


class MobileUIDriverFallback:
    """Единый визуальный драйвер автоматизации для трёх реальных сред:
    1. `adb` — Android по USB/Wi-Fi (скриншот, тап, скролл, свайп, ввод текста без Appium).
    2. `appium` — Android (UiAutomator2) и iOS (XCUITest) со свайпами по квадратичной кривой Безье.
    3. `playwright` — веб-версии любых дейтингов с постоянным профилем браузера, кликами по VLM-координатам,
       CSS-селекторам и клавиатуре.
    """

    def __init__(
        self,
        device_udid: str = "",
        platform_package: str = "com.twinby.app",
        appium_url: str = "http://localhost:4723",
        backend: str = "adb",
        device_os: str = "Android",
        platform_id: str = "twinby",
        web_target_url: str = "https://mamba.ru/rating",
        web_profile_dir: str = "./data/browser_profile",
        web_headless: bool = False,
    ) -> None:
        self.device_udid = device_udid.strip()
        self.platform_package = platform_package
        self.appium_url = appium_url.rstrip("/")
        self.backend = backend.lower().strip()
        self.device_os = device_os
        self.platform_id = platform_id.lower().strip()
        self.web_target_url = web_target_url
        self.web_profile_dir = web_profile_dir
        self.web_headless = web_headless

        self._session_id: str | None = None
        self._screen_size: tuple[int, int] | None = None
        self._pw: Any = None
        self._pw_context: Any = None
        self._pw_page: Any = None

    @property
    def screen_size(self) -> tuple[int, int]:
        if self._screen_size:
            return self._screen_size
        return (1280, 900) if self.backend == "playwright" else (1080, 2340)

    def _adb_base_cmd(self) -> list[str]:
        # CreateProcess only appends `.exe`, so a bare "adb" never resolves to adb.cmd/adb.bat
        # on Windows. shutil.which applies PATHEXT, which also keeps the not-found message clean.
        adb_path = shutil.which("adb")
        if not adb_path:
            raise RuntimeError(
                "Утилита 'adb' не найдена в PATH. Установите Android Platform-Tools и добавьте adb в PATH."
            )
        cmd = [adb_path]
        if self.device_udid:
            cmd.extend(["-s", self.device_udid])
        return cmd

    async def _run_adb(self, *args: str) -> bytes:
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._adb_base_cmd(),
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "Утилита 'adb' не найдена в PATH. Установите Android Platform-Tools и добавьте adb в PATH."
            ) from exc
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"ADB ошибка ({' '.join(args)}): {stderr.decode(errors='ignore').strip()}")
        return stdout

    async def connect(self) -> None:
        if self.backend == "adb":
            out = (await self._run_adb("shell", "wm", "size")).decode(errors="ignore")
            match = re.search(r"(\d+)x(\d+)", out)
            self._screen_size = (int(match.group(1)), int(match.group(2))) if match else (1080, 2340)
            logger.info("[ADB] Устройство подключено, экран: %s", self._screen_size)
            return

        if self.backend == "playwright":
            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:
                raise RuntimeError(
                    "Установите playwright: pip install 'heart3-matchmaking[web-browser]' && playwright install chromium"
                ) from exc

            Path(self.web_profile_dir).mkdir(parents=True, exist_ok=True)
            self._pw = await async_playwright().start()
            self._pw_context = await self._pw.chromium.launch_persistent_context(
                user_data_dir=self.web_profile_dir,
                headless=self.web_headless,
                viewport={"width": 1280, "height": 900},
            )
            self._pw_page = self._pw_context.pages[0] if self._pw_context.pages else await self._pw_context.new_page()
            await self._pw_page.goto(self.web_target_url, wait_until="domcontentloaded")
            self._screen_size = (1280, 900)
            logger.info("[Playwright] Открыт %s (профиль: %s)", self.web_target_url, self.web_profile_dir)
            return

        if self.device_os.lower() == "ios":
            caps = {
                "platformName": "iOS",
                "appium:automationName": "XCUITest",
                "appium:udid": self.device_udid,
                "appium:bundleId": self.platform_package,
                "appium:noReset": True,
            }
        else:
            caps = {
                "platformName": "Android",
                "appium:automationName": "UiAutomator2",
                "appium:udid": self.device_udid,
                "appium:appPackage": self.platform_package,
                "appium:noReset": True,
            }

        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(f"{self.appium_url}/session", json={"capabilities": {"alwaysMatch": caps}})
            resp.raise_for_status()
            self._session_id = resp.json()["value"]["sessionId"]
            try:
                rect_resp = await client.get(f"{self.appium_url}/session/{self._session_id}/window/rect")
                if rect_resp.status_code == 200:
                    val = rect_resp.json().get("value", {})
                    if val.get("width") and val.get("height"):
                        self._screen_size = (int(val["width"]), int(val["height"]))
            except Exception:
                pass
            logger.info("[Appium %s] Сессия %s открыта для %s", self.device_os, self._session_id, self.platform_package)

    async def disconnect(self) -> None:
        if self.backend == "playwright":
            if self._pw_context:
                await self._pw_context.close()
                self._pw_context = None
            if self._pw:
                await self._pw.stop()
                self._pw = None
            return

        if self.backend == "appium" and self._session_id:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.delete(f"{self.appium_url}/session/{self._session_id}")
            finally:
                self._session_id = None


    async def launch_app(self, package_or_url: str | None = None) -> None:
        """Открывает нужное дейтинг-приложение на телефоне (ADB) или переходит на URL в Playwright."""
        if self.backend == "playwright":
            if not self._pw_page:
                raise RuntimeError("Playwright не подключён.")
            target = package_or_url or self.web_target_url
            await self._pw_page.goto(target, wait_until="domcontentloaded")
            logger.info("[Playwright] Переход на %s", target)
            return

        if self.backend == "adb":
            pkg = package_or_url or self.platform_package
            await self._run_adb("shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")
            await asyncio.sleep(1.5)
            logger.info("[ADB] Приложение %s выведено на передний план", pkg)
            return

        if self._session_id:
            pkg = package_or_url or self.platform_package
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.post(
                    f"{self.appium_url}/session/{self._session_id}/appium/device/activate_app",
                    json={"appId": pkg},
                )
            await asyncio.sleep(1.5)

    async def switch_platform(self, platform_id: str, package_or_bundle: str, web_url: str) -> None:
        """Переключает активную платформу на лету и открывает её на устройстве/в браузере."""
        self.platform_id = platform_id.lower().strip()
        self.platform_package = package_or_bundle
        self.web_target_url = web_url
        await self.launch_app(web_url if self.backend == "playwright" else package_or_bundle)

    async def capture_screenshot(self) -> bytes:
        if self.backend == "adb":
            return await self._run_adb("exec-out", "screencap", "-p")

        if self.backend == "playwright":
            if not self._pw_page:
                raise RuntimeError("Playwright не подключён. Вызовите connect().")
            return await self._pw_page.screenshot(type="jpeg", quality=85)

        if not self._session_id:
            raise RuntimeError("Appium сессия не открыта. Сначала вызовите connect().")
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{self.appium_url}/session/{self._session_id}/screenshot")
            resp.raise_for_status()
            return base64.b64decode(resp.json()["value"])

    async def tap(self, x: int, y: int) -> None:
        if self.backend == "adb":
            await self._run_adb("shell", "input", "tap", str(x), str(y))
            return
        if self.backend == "playwright" and self._pw_page:
            await self._pw_page.mouse.click(x, y)
            return
        if self._session_id:
            actions = [
                {"type": "pointerMove", "duration": 0, "x": x, "y": y},
                {"type": "pointerDown", "button": 0},
                {"type": "pause", "duration": random.randint(40, 90)},
                {"type": "pointerUp", "button": 0},
            ]
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"{self.appium_url}/session/{self._session_id}/actions",
                    json={"actions": [{"type": "pointer", "id": "finger1", "parameters": {"pointerType": "touch"}, "actions": actions}]},
                )

    async def tap_normalized(self, norm_xy: tuple[int, int]) -> tuple[int, int]:
        """Нажимает по нормализованным координатам VLM [0..1000, 0..1000]."""
        w, h = self.screen_size
        px, py = denormalize_point(norm_xy, w, h, jitter_px=4)
        await self.tap(px, py)
        return px, py

    async def press_back_or_escape(self) -> None:
        """Закрывает всплывающие окна (попап мэтча/куки/реклама), если кнопка не найдена."""
        if self.backend == "adb":
            await self._run_adb("shell", "input", "keyevent", "4")
            return
        if self.backend == "playwright" and self._pw_page:
            await self._pw_page.keyboard.press("Escape")
            return
        if self._session_id:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(f"{self.appium_url}/session/{self._session_id}/back", json={})

    async def dismiss_popup(self, dismiss_button: tuple[int, int] | None = None) -> None:
        if dismiss_button is not None:
            await self.tap_normalized(dismiss_button)
        else:
            await self.press_back_or_escape()
        await asyncio.sleep(random.uniform(0.5, 1.0))

    async def type_and_send_text(
        self,
        text: str,
        input_box: tuple[int, int] | None = None,
        send_button: tuple[int, int] | None = None,
    ) -> None:
        """Вводит сообщение (включая кириллицу) в активный чат и отправляет его."""
        if input_box is not None:
            await self.tap_normalized(input_box)
            await asyncio.sleep(random.uniform(0.25, 0.5))

        if self.backend == "playwright":
            if not self._pw_page:
                raise RuntimeError("Playwright не подключён.")
            await self._pw_page.keyboard.type(text, delay=random.randint(35, 80))
            await asyncio.sleep(random.uniform(0.2, 0.5))
            if send_button is not None:
                await self.tap_normalized(send_button)
            else:
                await self._pw_page.keyboard.press("Enter")
            return

        if self.backend == "adb":
            # Для кириллицы пробуем ADBKeyboard broadcast, затем fallback на clipboard/ASCII
            b64_text = base64.b64encode(text.encode("utf-8")).decode("ascii")
            try:
                await self._run_adb("shell", "am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", b64_text)
            except Exception:
                safe_ascii = text.replace(" ", "%s")
                await self._run_adb("shell", "input", "text", safe_ascii)
            await asyncio.sleep(random.uniform(0.25, 0.5))
            if send_button is not None:
                await self.tap_normalized(send_button)
            else:
                await self._run_adb("shell", "input", "keyevent", "66")  # KEYCODE_ENTER
            return

        if self._session_id:
            key_actions = []
            for ch in text:
                key_actions.append({"type": "keyDown", "value": ch})
                key_actions.append({"type": "keyUp", "value": ch})
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.post(
                    f"{self.appium_url}/session/{self._session_id}/actions",
                    json={"actions": [{"type": "key", "id": "keyboard", "actions": key_actions}]},
                )
            await asyncio.sleep(random.uniform(0.25, 0.5))
            if send_button is not None:
                await self.tap_normalized(send_button)

    async def scroll_down(self) -> None:
        w, h = self.screen_size
        if self.backend == "playwright" and self._pw_page:
            await self._pw_page.mouse.wheel(0, 420)
            await asyncio.sleep(0.5)
            return
        if self.backend == "adb":
            await self._run_adb("shell", "input", "swipe", str(w // 2), str(int(h * 0.72)), str(w // 2), str(int(h * 0.28)), "320")
            await asyncio.sleep(0.5)
            return
        if self._session_id:
            pts = self._generate_bezier_points(w // 2, int(h * 0.72), w // 2, int(h * 0.28), steps=8)
            await self._send_appium_gesture(pts)
            await asyncio.sleep(0.5)

    async def capture_profile_views(self, include_second_view: bool = False) -> list[bytes]:
        first = await self.capture_screenshot()
        if not include_second_view:
            return [first]
        await self.scroll_down()
        second = await self.capture_screenshot()
        return [first, second]

    def _generate_bezier_points(
        self, start_x: int, start_y: int, end_x: int, end_y: int, steps: int = 12
    ) -> list[tuple[int, int]]:
        ctrl_x = (start_x + end_x) // 2 + random.randint(-45, 45)
        ctrl_y = min(start_y, end_y) - random.randint(40, 110)

        points: list[tuple[int, int]] = []
        for i in range(steps + 1):
            t = i / steps
            x = int((1 - t) ** 2 * start_x + 2 * (1 - t) * t * ctrl_x + t**2 * end_x)
            y = int((1 - t) ** 2 * start_y + 2 * (1 - t) * t * ctrl_y + t**2 * end_y)
            points.append((x + random.randint(-2, 2), y + random.randint(-2, 2)))
        return points

    async def _send_appium_gesture(self, points: list[tuple[int, int]]) -> None:
        actions = [
            {"type": "pointerMove", "duration": 0, "x": points[0][0], "y": points[0][1]},
            {"type": "pointerDown", "button": 0},
        ]
        for px, py in points[1:]:
            actions.append({"type": "pointerMove", "duration": random.randint(18, 35), "x": px, "y": py})
        actions.append({"type": "pointerUp", "button": 0})
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{self.appium_url}/session/{self._session_id}/actions",
                json={"actions": [{"type": "pointer", "id": "finger1", "parameters": {"pointerType": "touch"}, "actions": actions}]},
            )

    async def _playwright_action(self, is_like: bool) -> None:
        if not self._pw_page:
            raise RuntimeError("Playwright не подключён.")
        action_key = "like" if is_like else "pass"
        selectors = WEB_ACTION_SELECTORS.get(self.platform_id, {}).get(action_key, [])
        for sel in selectors:
            try:
                loc = self._pw_page.locator(sel).first
                if await loc.is_visible(timeout=500):
                    await loc.click()
                    await asyncio.sleep(random.uniform(0.4, 0.9))
                    return
            except Exception:
                continue
        await self._pw_page.keyboard.press("ArrowRight" if is_like else "ArrowLeft")
        await asyncio.sleep(random.uniform(0.4, 0.9))

    async def execute_profile_action(
        self,
        is_like: bool,
        like_button: tuple[int, int] | None = None,
        pass_button: tuple[int, int] | None = None,
    ) -> str:
        """Универсальное выполнение LIKE/PASS:
        1. Если VLM локализовал точную кнопку (работает в Hinge, Pure, Tabor, OK, LovePlanet, веб-сайтах и т.д.) — нажимает её.
        2. Иначе выполняет свайп по кривой Безье (мобильные) или селектор/стрелку (Playwright).
        """
        target_btn = like_button if is_like else pass_button
        if target_btn is not None:
            px, py = await self.tap_normalized(target_btn)
            await asyncio.sleep(random.uniform(0.35, 0.75))
            return f"vlm_tap({px},{py})"

        await self.swipe_with_bezier_curve(is_like=is_like)
        return "bezier_swipe" if self.backend != "playwright" else "web_action"

    async def swipe_with_bezier_curve(
        self, is_like: bool, screen_w: int | None = None, screen_h: int | None = None
    ) -> None:
        if self.backend == "playwright":
            await self._playwright_action(is_like)
            return

        w, h = self.screen_size
        w = screen_w or w
        h = screen_h or h

        start_x = int(w * 0.5) + random.randint(-30, 30)
        start_y = int(h * 0.55) + random.randint(-40, 40)
        end_x = int(w * (0.88 if is_like else 0.12))
        end_y = start_y + random.randint(-60, 60)
        points = self._generate_bezier_points(start_x, start_y, end_x, end_y)

        if self.backend == "adb":
            duration_ms = random.randint(240, 430)
            await self._run_adb(
                "shell",
                "input",
                "swipe",
                str(points[0][0]),
                str(points[0][1]),
                str(points[-1][0]),
                str(points[-1][1]),
                str(duration_ms),
            )
            return

        if not self._session_id:
            logger.info("[Appium Dry-Run] Свайп %s по %d точкам Безье", "LIKE" if is_like else "PASS", len(points))
            await asyncio.sleep(random.uniform(0.35, 0.65))
            return

        await self._send_appium_gesture(points)
