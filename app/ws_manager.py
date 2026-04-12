"""WebSocket connection managers for real-time collaboration."""
import json
import logging
from collections import defaultdict

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    MAX_CONNECTIONS_PER_TRACK = 50
    MAX_TOTAL_CONNECTIONS = 200

    def __init__(self) -> None:
        self.active_connections: dict[int, list[WebSocket]] = defaultdict(list)
        self._total_count = 0

    async def connect(self, track_id: int, websocket: WebSocket) -> bool:
        """Accept and register a WebSocket. Returns False if limits exceeded."""
        if self._total_count >= self.MAX_TOTAL_CONNECTIONS:
            await websocket.accept()
            await websocket.close(code=1013, reason="Server connection limit reached")
            return False
        if len(self.active_connections[track_id]) >= self.MAX_CONNECTIONS_PER_TRACK:
            await websocket.accept()
            await websocket.close(code=1013, reason="Track connection limit reached")
            return False
        await websocket.accept()
        self.active_connections[track_id].append(websocket)
        self._total_count += 1
        return True

    def disconnect(self, track_id: int, websocket: WebSocket) -> None:
        conns = self.active_connections.get(track_id, [])
        if websocket in conns:
            conns.remove(websocket)
            self._total_count -= 1
        if not conns and track_id in self.active_connections:
            del self.active_connections[track_id]

    async def broadcast(self, track_id: int, message: dict) -> None:
        payload = json.dumps(message)
        dead: list[WebSocket] = []
        for ws in self.active_connections.get(track_id, []):
            try:
                await ws.send_text(payload)
            except Exception as exc:  # noqa: BLE001
                logger.warning("WebSocket send failed: %s", exc)
                dead.append(ws)
        for ws in dead:
            self.disconnect(track_id, ws)


manager = ConnectionManager()


class UserConnectionManager:
    MAX_CONNECTIONS_PER_USER = 10
    MAX_TOTAL_CONNECTIONS = 500

    def __init__(self) -> None:
        self.active_connections: dict[int, list[WebSocket]] = defaultdict(list)
        self._total_count = 0

    async def connect(self, user_id: int, websocket: WebSocket) -> bool:
        """Accept and register a WebSocket. Returns False if limits exceeded."""
        if self._total_count >= self.MAX_TOTAL_CONNECTIONS:
            await websocket.accept()
            await websocket.close(code=1013, reason="Server connection limit reached")
            return False
        if len(self.active_connections[user_id]) >= self.MAX_CONNECTIONS_PER_USER:
            await websocket.accept()
            await websocket.close(code=1013, reason="User connection limit reached")
            return False
        await websocket.accept()
        self.active_connections[user_id].append(websocket)
        self._total_count += 1
        return True

    def disconnect(self, user_id: int, websocket: WebSocket) -> None:
        conns = self.active_connections.get(user_id, [])
        if websocket in conns:
            conns.remove(websocket)
            self._total_count -= 1
        if not conns and user_id in self.active_connections:
            del self.active_connections[user_id]

    async def broadcast(self, user_id: int, message: dict) -> None:
        payload = json.dumps(message)
        dead: list[WebSocket] = []
        for ws in self.active_connections.get(user_id, []):
            try:
                await ws.send_text(payload)
            except Exception as exc:  # noqa: BLE001
                logger.warning("User WebSocket send failed: %s", exc)
                dead.append(ws)
        for ws in dead:
            self.disconnect(user_id, ws)

    async def broadcast_many(self, user_ids: list[int], message: dict) -> None:
        for user_id in dict.fromkeys(user_ids):
            await self.broadcast(user_id, message)


notification_manager = UserConnectionManager()
