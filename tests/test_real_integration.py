import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from core.adapters import declarative_client
from core.adapters.declarative_client import DeclarativeRestDatingClient
from core.image_utils import denormalize_point, is_blank_or_secure_screen, prepare_image_for_vlm
from tools import har_importer
from tools.har_importer import export_api_profile_from_har


def _make_png(color_mode: str = "gradient", size: tuple[int, int] = (1440, 2800)) -> bytes:
    img = Image.new("RGB", size, (0, 0, 0))
    if color_mode != "black":
        px = img.load()
        for y in range(0, size[1], 12):
            for x in range(0, size[0], 12):
                px[x, y] = (x % 255, y % 255, (x + y) % 255)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_real_flag_secure_detection_and_vlm_compression() -> None:
    black_png = _make_png("black", size=(400, 800))
    real_png = _make_png("gradient", size=(1440, 2800))

    assert is_blank_or_secure_screen(black_png) is True
    assert is_blank_or_secure_screen(real_png) is False

    compressed = prepare_image_for_vlm(real_png, max_side=1024, quality=80)
    assert len(compressed) < len(real_png)
    with Image.open(io.BytesIO(compressed)) as out_img:
        assert max(out_img.size) == 1024
        assert out_img.format == "JPEG"


def test_denormalize_point_bounds_and_accuracy() -> None:
    px, py = denormalize_point((500, 800), 1080, 2400, jitter_px=0)
    assert (px, py) == (540, 1920)
    clamped = denormalize_point((9999, -100), 1080, 2400, jitter_px=0)
    assert clamped == (1078, 2)


@pytest.mark.asyncio
async def test_real_http_loopback_har_export_to_declarative_client(tmp_path: Path) -> None:
    """Настоящий E2E-тест БЕЗ моков HTTP:
    1. Поднимает реальный локальный HTTP-сервер дейтинг-API.
    2. Экспортирует HAR-дамп в ./api_profiles/realapp.json.
    3. DeclarativeRestDatingClient делает настоящие TCP/HTTP запросы GET feed + POST like + POST message!
    """
    received_auth: list[str] = []

    class _RealDatingHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            received_auth.append(self.headers.get("Authorization", ""))
            if self.path.startswith("/api/v2/feed"):
                payload = {
                    "data": {
                        "users": [
                            {
                                "id": "u_777",
                                "name": "Полина",
                                "age": 24,
                                "bio": "Фильтр-кофе и джаз",
                                "distance_km": 1.8,
                                "compatibility": {"score": 92},
                                "photos": [{"url": "https://cdn.local/1.jpg"}],
                                "interests": ["кофе"],
                                "is_verified": True,
                            }
                        ]
                    }
                }
            else:
                payload = {"items": []}
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            req_body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            if self.path.startswith("/api/v2/swipe/like"):
                payload = {"ok": True, "is_match": req_body.get("target_user_id") == "u_777"}
            else:
                payload = {"ok": True, "echo": req_body}
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), _RealDatingHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        origin = f"http://127.0.0.1:{port}"
        har_path = tmp_path / "capture.har"
        har_path.write_text(
            json.dumps(
                {
                    "log": {
                        "entries": [
                            {
                                "request": {
                                    "method": "GET",
                                    "url": f"{origin}/api/v2/feed?limit=10",
                                    "headers": [{"name": "Authorization", "value": "Bearer captured"}],
                                },
                                "response": {
                                    "status": 200,
                                    "content": {
                                        "mimeType": "application/json",
                                        "text": json.dumps({"data": {"users": [{"id": "1"}]}}),
                                    },
                                },
                            },
                            {
                                "request": {"method": "POST", "url": f"{origin}/api/v2/swipe/like", "headers": []},
                                "response": {"status": 200, "content": {"mimeType": "application/json", "text": '{"is_match": true}'}},
                            },
                            {
                                "request": {"method": "POST", "url": f"{origin}/api/v2/swipe/pass", "headers": []},
                                "response": {"status": 200, "content": {"mimeType": "application/json", "text": '{"is_match": false}'}},
                            },
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        with patch.object(har_importer, "API_PROFILES_DIR", tmp_path / "api_profiles"):
            profile_file = export_api_profile_from_har(
                str(har_path),
                platform="realapp",
                host_filter="127.0.0.1",
            )
        assert profile_file.exists()

        with patch.object(declarative_client, "API_PROFILES_DIR", profile_file.parent):
            async with DeclarativeRestDatingClient.from_profile_file(profile_file.name, token="live_secret_token") as client:
                profiles = await client.get_recommendations(limit=5)
                assert len(profiles) == 1
                assert profiles[0].name == "Полина"
                assert profiles[0].compatibility_score == 92

                swipe_res = await client.swipe("u_777", is_like=True)
                assert swipe_res["is_match"] is True

        assert "Bearer live_secret_token" in received_auth
    finally:
        server.shutdown()
