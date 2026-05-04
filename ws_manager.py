import asyncio
import logging
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self):
        self._connections: dict[str, list[WebSocket]] = {}

    async def connect(self, user_id: str, ws: WebSocket):
        # Already accepted in the endpoint before auth check
        if user_id not in self._connections:
            self._connections[user_id] = []
        self._connections[user_id].append(ws)
        logger.info(f"WS connected: user={user_id}, total={len(self._connections[user_id])}")

    def disconnect(self, user_id: str, ws: WebSocket):
        if user_id in self._connections:
            try:
                self._connections[user_id].remove(ws)
            except ValueError:
                pass
            if not self._connections[user_id]:
                del self._connections[user_id]

    async def send_to_user(self, user_id: str, data: dict):
        # Snapshot the connection list — `await ws.send_json` yields to the event loop,
        # and a concurrent `disconnect()` could mutate the list mid-iteration.
        sockets = list(self._connections.get(user_id, ()))
        if not sockets:
            return
        dead = []
        for ws in sockets:
            try:
                await ws.send_json(data)
            except Exception as e:
                # Logged at debug — closed sockets during reconnect are normal,
                # but recurring failures with the same payload type signal a real bug.
                logger.debug(f"WS send to {user_id[:8]} failed ({data.get('type', '?')}): {e}")
                dead.append(ws)
        for ws in dead:
            self.disconnect(user_id, ws)

    @property
    def connections(self) -> dict:
        return self._connections

    async def broadcast(self, data: dict):
        """Send a message to all connected users."""
        for user_id in list(self._connections.keys()):
            await self.send_to_user(user_id, data)

    async def broadcast_heartbeat(self):
        """Send heartbeat to all connected users"""
        for user_id in list(self._connections.keys()):
            await self.send_to_user(user_id, {"type": "heartbeat"})


ws_manager = ConnectionManager()
