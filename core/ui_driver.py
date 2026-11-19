import logging
import random
import httpx

logger = logging.getLogger(__name__)


class MobileUIDriverFallback:
    """Экспериментальный Android Appium-клиент; чтение анкет не реализовано."""

    def __init__(
        self,
        device_udid: str,
        platform_package: str,
        appium_url: str = "http://localhost:4723",
    ) -> None:
        self.device_udid = device_udid
        self.platform_package = platform_package
        self.appium_url = appium_url.rstrip("/")
        self._session_id: str | None = None

    async def connect(self) -> None:
        """Открывает W3C Appium-сессию на Android-устройстве."""
        payload = {
            "capabilities": {
                "alwaysMatch": {
                    "platformName": "Android",
                    "appium:automationName": "UiAutomator2",
                    "appium:udid": self.device_udid,
                    "appium:appPackage": self.platform_package,
                    "appium:noReset": True,
                }
            }
        }
        async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
            resp = await client.post(f"{self.appium_url}/session", json=payload)
            resp.raise_for_status()
            self._session_id = resp.json()["value"]["sessionId"]
            logger.info(
                "[Appium] Сессия %s открыта для %s",
                self._session_id,
                self.platform_package,
            )

    def _generate_bezier_points(
        self, start_x: int, start_y: int, end_x: int, end_y: int, steps: int = 12
    ) -> list[tuple[int, int]]:
        """Генерирует траекторию движения пальца по квадратичной кривой Безье с микро-дрожанием."""
        ctrl_x = (start_x + end_x) // 2 + random.randint(-45, 45)
        ctrl_y = min(start_y, end_y) - random.randint(40, 110)

        points: list[tuple[int, int]] = []
        for i in range(steps + 1):
            t = i / steps
            x = int((1 - t) ** 2 * start_x + 2 * (1 - t) * t * ctrl_x + t**2 * end_x)
            y = int((1 - t) ** 2 * start_y + 2 * (1 - t) * t * ctrl_y + t**2 * end_y)
            points.append((x + random.randint(-2, 2), y + random.randint(-2, 2)))
        return points

    async def swipe_with_bezier_curve(
        self, is_like: bool, screen_w: int = 1080, screen_h: int = 2340
    ) -> None:
        """Выполняет W3C Touch Action свайп вправо (LIKE) или влево (PASS)."""
        start_x = int(screen_w * 0.5) + random.randint(-30, 30)
        start_y = int(screen_h * 0.55) + random.randint(-40, 40)
        end_x = int(screen_w * (0.88 if is_like else 0.12))
        end_y = start_y + random.randint(-60, 60)

        points = self._generate_bezier_points(start_x, start_y, end_x, end_y)
        if not self._session_id:
            raise RuntimeError("Appium session is not connected")

        actions = [
            {
                "type": "pointerMove",
                "duration": 0,
                "x": points[0][0],
                "y": points[0][1],
            },
            {"type": "pointerDown", "button": 0},
        ]
        for px, py in points[1:]:
            actions.append(
                {
                    "type": "pointerMove",
                    "duration": random.randint(18, 35),
                    "x": px,
                    "y": py,
                }
            )
        actions.append({"type": "pointerUp", "button": 0})

        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            response = await client.post(
                f"{self.appium_url}/session/{self._session_id}/actions",
                json={
                    "actions": [
                        {
                            "type": "pointer",
                            "id": "finger1",
                            "parameters": {"pointerType": "touch"},
                            "actions": actions,
                        }
                    ]
                },
            )
            response.raise_for_status()
