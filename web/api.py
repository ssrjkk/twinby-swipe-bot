import hmac
from pathlib import Path
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from config import settings
from core.proxy.manager import GeoProxyManager
from database.repository import DatabaseManager
from filters.engine import ProfileFilterEngine
from web.events import event_bus

STATIC_DIR = Path(__file__).parent / "static"


def _token_matches(candidate: str | None) -> bool:
    expected = settings.web_admin_token.get_secret_value()
    if not expected.strip() or not candidate or not candidate.strip():
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def _bearer_matches(authorization: str | None) -> bool:
    scheme, _, token = (authorization or "").partition(" ")
    return scheme.lower() == "bearer" and _token_matches(token)


def _allowed_origins() -> list[str]:
    return [o.strip() for o in settings.web_allowed_origins.split(",") if o.strip()]


def require_admin(authorization: str | None = Header(default=None)) -> None:
    if not _bearer_matches(authorization):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin token required"
        )


async def ws_live(websocket: WebSocket) -> None:
    # Non-browser clients must send Authorization; query tokens are never used.
    origin = websocket.headers.get("origin")
    if not _bearer_matches(websocket.headers.get("authorization")) or (
        origin is not None and origin not in _allowed_origins()
    ):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    try:
        await event_bus.connect(websocket)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        event_bus.disconnect(websocket)


def create_web_app(
    manager: Any,
    proxy_manager: GeoProxyManager,
    db: DatabaseManager,
    filter_engine: ProfileFilterEngine,
) -> FastAPI:
    app = FastAPI(title="<3 — Experimental API", version="3.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins(),
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.get("/", response_class=HTMLResponse)
    async def serve_landing() -> HTMLResponse:
        return HTMLResponse(
            content=(STATIC_DIR / "index.html").read_text(encoding="utf-8")
        )

    @app.websocket("/ws/live")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await ws_live(websocket)

    @app.get("/api/v1/overview", dependencies=[Depends(require_admin)])
    async def overview() -> dict[str, Any]:
        return {"stats": await db.get_stats()}

    @app.get("/api/v1/matches", dependencies=[Depends(require_admin)])
    async def matches() -> list[dict]:
        return await db.get_recent_matches()

    return app
