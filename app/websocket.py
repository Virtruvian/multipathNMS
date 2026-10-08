from fastapi import WebSocket
from collections.abc import Callable

from .public_nms import public_message


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._authorizers: dict[WebSocket, Callable[[], bool]] = {}

    async def connect(self, websocket: WebSocket, authorize: Callable[[], bool] | None = None) -> bool:
        if authorize is not None and not authorize():
            await websocket.close(code=1008)
            return False
        await websocket.accept()
        self._connections.add(websocket)
        if authorize is not None:
            self._authorizers[websocket] = authorize
        return True

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)
        self._authorizers.pop(websocket, None)

    async def broadcast(self, payload: dict) -> None:
        dead: list[WebSocket] = []
        anonymous = public_message(payload)
        # Sending yields to connect/disconnect handlers. Iterate a snapshot so
        # browser navigation cannot invalidate the iterator and stop a monitor.
        for connection in tuple(self._connections):
            try:
                authorize = self._authorizers.get(connection)
                if authorize is not None:
                    if not authorize():
                        dead.append(connection)
                        await connection.close(code=1008)
                        continue
                    await connection.send_json(payload)
                elif anonymous is not None:
                    await connection.send_json(anonymous)
            except Exception:
                dead.append(connection)
        for connection in dead:
            self.disconnect(connection)


manager = ConnectionManager()
