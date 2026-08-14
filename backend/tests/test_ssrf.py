"""SSRF 防护：封禁段判定、回环开关、异步解析与抛错。"""
import asyncio
from types import SimpleNamespace

import pytest

from app.core import ssrf


def run(coro):
    return asyncio.run(coro)


def test_is_blocked_ip_dangerous_ranges():
    """云元数据/链路本地/未指定/组播/广播 一律封禁（与回环开关无关）。"""
    assert ssrf.is_blocked_ip("169.254.169.254")
    assert ssrf.is_blocked_ip("169.254.1.1")
    assert ssrf.is_blocked_ip("0.0.0.0")
    assert ssrf.is_blocked_ip("224.0.0.1")
    assert ssrf.is_blocked_ip("255.255.255.255")
    assert ssrf.is_blocked_ip("fe80::1")
    assert ssrf.is_blocked_ip("::ffff:169.254.169.254")  # IPv4-mapped IPv6
    assert not ssrf.is_blocked_ip("not-an-ip")


def test_private_ranges_not_blocked():
    """内网 NMS 拨测场景：私网段不封。"""
    assert not ssrf.is_blocked_ip("192.168.1.1")
    assert not ssrf.is_blocked_ip("10.0.0.1")
    assert not ssrf.is_blocked_ip("172.16.0.1")
    assert not ssrf.is_blocked_ip("192.0.2.1")  # TEST-NET


def test_loopback_block_toggle(monkeypatch):
    """回环封禁可配置：测试环境 conftest 关闭（本地假服务），开启后封禁。"""
    assert not ssrf.is_blocked_ip("127.0.0.1")  # 测试环境已关
    monkeypatch.setattr(ssrf, "get_settings", lambda: SimpleNamespace(ssrf_block_loopback=True))
    assert ssrf.is_blocked_ip("127.0.0.1")
    assert ssrf.is_blocked_ip("::1")


def test_resolve_blocked(monkeypatch):
    monkeypatch.setattr(ssrf, "get_settings", lambda: SimpleNamespace(ssrf_block_loopback=True))
    assert run(ssrf.resolve_blocked("169.254.169.254")) is True
    assert run(ssrf.resolve_blocked("127.0.0.1")) is True
    assert run(ssrf.resolve_blocked("192.168.1.1")) is False


def test_ensure_not_blocked_raises(monkeypatch):
    monkeypatch.setattr(ssrf, "get_settings", lambda: SimpleNamespace(ssrf_block_loopback=True))

    async def go():
        with pytest.raises(ssrf.SSRFBlockedError):
            await ssrf.ensure_not_blocked("169.254.169.254")

    run(go())
