"""WebSocket 与广播器测试。"""
import asyncio

import pytest
from starlette.websockets import WebSocketDisconnect

from app.core.broadcast import Broadcaster
from conftest import auth


class FakeWS:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def accept(self):
        pass

    async def send_text(self, text):
        if self.fail:
            raise RuntimeError("连接断了")
        self.sent.append(text)


def test_broadcaster_fanout_and_dead_cleanup():
    bc = Broadcaster()
    good, dead = FakeWS(), FakeWS(fail=True)
    bc._conns.update({good, dead})

    async def go():
        await bc.broadcast({"type": "alert", "x": 1})

    asyncio.run(go())
    assert len(good.sent) == 1
    assert '"alert"' in good.sent[0]
    assert bc.count == 1  # 死连接已剔除


def test_broadcast_empty_noop():
    asyncio.run(Broadcaster().broadcast({"a": 1}))  # 无连接不抛异常


def test_ws_rejects_bad_ticket(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/ws?ticket=bad-ticket"):
            pass


def test_ws_accepts_valid_ticket(client, admin_token):
    r = client.get("/api/ws-ticket", headers=auth(admin_token))
    assert r.status_code == 200
    ticket = r.json()["ticket"]
    with client.websocket_connect(f"/api/ws?ticket={ticket}"):
        pass  # 能连上即通过


def test_ws_ticket_requires_login(client):
    assert client.get("/api/ws-ticket").status_code == 401


def test_ws_ticket_is_one_time(client, admin_token):
    """票据一次性：第二次使用同一票据被拒。"""
    r = client.get("/api/ws-ticket", headers=auth(admin_token))
    ticket = r.json()["ticket"]
    with client.websocket_connect(f"/api/ws?ticket={ticket}"):
        pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/ws?ticket={ticket}"):
            pass
