import json
from typing import Any
from fastapi import WebSocket

class RealtimeEventBus:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        if not self._connections: return
        msg = json.dumps({"type": event_type, "data": payload}, ensure_ascii=False)
        for ws in list(self._connections):
            try:
                await ws.send_text(msg)
            except Exception:
                self._connections.discard(ws)

event_bus = RealtimeEventBus()
