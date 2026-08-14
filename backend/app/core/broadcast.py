"""进程内 WebSocket 广播器：单实例部署下直接内存广播，不引 Redis。
多实例部署时再换 Redis pub/sub，只改这一个模块。
"""
import json
import logging

from fastapi import WebSocket

log = logging.getLogger(__name__)


class Broadcaster:
    def __init__(self) -> None:
        self._conns: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._conns.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._conns.discard(ws)

    @property
    def count(self) -> int:
        return len(self._conns)

    async def broadcast(self, message: dict) -> None:
        """向全部连接推送；发送失败的连接静默剔除。"""
        if not self._conns:
            return
        text = json.dumps(message, ensure_ascii=False, default=str)
        dead = []
        for ws in list(self._conns):
            try:
                await ws.send_text(text)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self._conns.discard(ws)


broadcaster = Broadcaster()
