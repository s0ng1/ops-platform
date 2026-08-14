"""WebSocket 推送：设备状态变化、新告警事件。
浏览器 WS 不便带请求头，改用一次性短时票据：客户端先经 `GET /api/ws-ticket`
（HTTP Bearer 鉴权）换取 60s 有效、一次性使用的 ticket，再 `/api/ws?ticket=<ticket>` 连接，
避免长期有效的 JWT 进入 URL 查询串（nginx access log / 浏览器历史 / Referer 泄漏）。
票据存进程内存（单进程部署；多实例需共享存储，见核心决策）。
"""
import secrets
import time

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from ..core.broadcast import broadcaster
from .deps import get_current_user

router = APIRouter(tags=["实时推送"])

_TICKET_TTL = 60.0
_tickets: dict[str, float] = {}  # ticket -> 过期时刻（monotonic）


@router.get("/api/ws-ticket")
def ws_ticket(_=Depends(get_current_user)):
    """签发一次性短时 WS 连接票据（需登录）。"""
    _prune_tickets()
    ticket = secrets.token_urlsafe(24)
    _tickets[ticket] = time.monotonic() + _TICKET_TTL
    return {"ticket": ticket}


def _prune_tickets() -> None:
    now = time.monotonic()
    for t in [t for t, exp in _tickets.items() if exp < now]:
        _tickets.pop(t, None)


def _consume_ticket(ticket: str) -> bool:
    """校验并消费票据（一次性）：有效则移除并返回 True。"""
    if not ticket:
        return False
    exp = _tickets.pop(ticket, None)
    if exp is None:
        return False
    return exp >= time.monotonic()


@router.websocket("/api/ws")
async def ws_endpoint(websocket: WebSocket, ticket: str = ""):
    if not _consume_ticket(ticket):
        await websocket.close(code=4401)
        return
    await broadcaster.connect(websocket)
    try:
        while True:
            # 客户端消息（心跳等）仅作保活，内容忽略
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        broadcaster.disconnect(websocket)
