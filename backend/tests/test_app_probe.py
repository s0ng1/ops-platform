"""应用仿真拨测：本地起真 HTTP/TCP/UDP-DNS 服务做真拨测 + 调度谓词 + 内置规则 + API 校验。
注意：全测试套件共享一个会话级库——API 建设备 IP 用 192.0.2.9x 段避开其他用例
（203.0.113.x 已被 test_ipam/test_logreceiver 占用，会撞「非入库设备」断言）；
真拨测目标用 127.0.0.1（type=application 唯一，不与其他用例冲突）。
"""
import asyncio
import struct
from types import SimpleNamespace

from conftest import auth

from app.collectors import app_probe
from app.collectors.app_probe import collect_app_metrics
from app.core.database import SessionLocal
from app.main import _init_db
from app.models import AlertRule
from app.scheduler.scheduler import TASKS


def run(coro):
    return asyncio.run(coro)


def _device(cfg, ip="127.0.0.1"):
    return SimpleNamespace(id=9001, ip=ip, type="application",
                           monitor_enabled=True, probe_config=cfg)


def _by_metric(points):
    return {p.metric: p for p in points}


# ============ 本地真服务 ============

async def _http_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """最小 HTTP 服务：/ok → 200 hello world，/err → 500，/hang → 不应答（5 秒后关）。"""
    try:
        head = await reader.readuntil(b"\r\n\r\n")
        path = head.split(b" ")[1]
        if path == b"/hang":
            await asyncio.sleep(5)
            return
        if path == b"/err":
            code, reason, body = 500, b"Internal Server Error", b"boom"
        else:
            code, reason, body = 200, b"OK", b"hello world"
        writer.write(
            b"HTTP/1.1 %d %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
            % (code, reason, len(body), body)
        )
        await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    finally:
        writer.close()


async def _banner_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """TCP 服务：连接即发 banner，随后挂起。"""
    try:
        writer.write(b"SSH-2.0-OpsTest_1.0\r\n")
        await writer.drain()
        await asyncio.sleep(5)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


class _FakeDnsProtocol(asyncio.DatagramProtocol):
    """假 DNS 服务器：任何查询都回一条 A 记录 203.0.113.99。"""

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        qid = data[:2]
        end = data.index(b"\x00", 12) + 5  # 问题段结束（\x00 + QTYPE/QCLASS）
        question = data[12:end]
        resp = qid + struct.pack(">HHHHH", 0x8180, 1, 1, 0, 0) + question
        resp += b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + bytes([203, 0, 113, 99])
        self.transport.sendto(resp, addr)


def _closed_port() -> int:
    """拿一个确定没人监听的端口（绑定后立即释放）。"""
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ============ http 拨测 ============

def test_http_ok_and_metrics():
    async def main():
        server = await asyncio.start_server(_http_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            cfg = {"probe_kind": "http", "url": f"http://127.0.0.1:{port}/ok"}
            points = await collect_app_metrics(_device(cfg))
            m = _by_metric(points)
            assert m["app_available"].value == 1.0
            assert m["app_status_code"].value == 200.0
            assert m["app_latency"].value >= 0
            assert all(p.labels == {"probe_kind": "http"} for p in points)
    run(main())


def test_http_status_expectation():
    async def main():
        server = await asyncio.start_server(_http_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            # 默认期望 200~399：500 判不活
            cfg = {"probe_kind": "http", "url": f"http://127.0.0.1:{port}/err"}
            m = _by_metric(await collect_app_metrics(_device(cfg)))
            assert m["app_available"].value == 0.0
            assert m["app_status_code"].value == 500.0
            # 显式期望 500：判活
            cfg["expect_status"] = 500
            m = _by_metric(await collect_app_metrics(_device(cfg)))
            assert m["app_available"].value == 1.0
    run(main())


def test_http_keyword():
    async def main():
        server = await asyncio.start_server(_http_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            cfg = {"probe_kind": "http", "url": f"http://127.0.0.1:{port}/ok", "keyword": "hello"}
            m = _by_metric(await collect_app_metrics(_device(cfg)))
            assert m["app_available"].value == 1.0
            cfg["keyword"] = "不存在的关键字"
            m = _by_metric(await collect_app_metrics(_device(cfg)))
            assert m["app_available"].value == 0.0
    run(main())


def test_http_unreachable_and_timeout():
    async def main():
        # 连接被拒绝：可用性 0，只有 app_available 一个点
        cfg = {"probe_kind": "http", "url": f"http://127.0.0.1:{_closed_port()}/", "timeout": 2}
        points = await collect_app_metrics(_device(cfg))
        assert len(points) == 1 and points[0].metric == "app_available" and points[0].value == 0.0
        # 对端不应答：超时判不活
        server = await asyncio.start_server(_http_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            cfg = {"probe_kind": "http", "url": f"http://127.0.0.1:{port}/hang", "timeout": 1}
            points = await collect_app_metrics(_device(cfg))
            assert len(points) == 1 and points[0].value == 0.0
    run(main())


# ============ tcp 拨测 ============

def test_tcp_connect_and_banner():
    async def main():
        server = await asyncio.start_server(_banner_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            # 纯连通
            m = _by_metric(await collect_app_metrics(_device({"probe_kind": "tcp", "port": port})))
            assert m["app_available"].value == 1.0
            assert m["app_latency"].value >= 0
            assert m["app_available"].labels == {"probe_kind": "tcp"}
            # banner 命中 / 不命中
            cfg = {"probe_kind": "tcp", "port": port, "banner": "SSH-2.0"}
            m = _by_metric(await collect_app_metrics(_device(cfg)))
            assert m["app_available"].value == 1.0
            cfg["banner"] = "FTP"
            m = _by_metric(await collect_app_metrics(_device(cfg)))
            assert m["app_available"].value == 0.0
    run(main())


def test_tcp_unreachable():
    cfg = {"probe_kind": "tcp", "port": _closed_port(), "timeout": 2}
    points = run(collect_app_metrics(_device(cfg)))
    assert len(points) == 1 and points[0].metric == "app_available" and points[0].value == 0.0


# ============ dns 拨测 ============

def test_dns_system_resolver_localhost():
    cfg = {"probe_kind": "dns", "domain": "localhost"}
    m = _by_metric(run(collect_app_metrics(_device(cfg))))
    assert m["app_available"].value == 1.0
    assert m["app_latency"].value >= 0
    assert m["app_available"].labels == {"probe_kind": "dns"}
    # 期望 IP 命中 / 不命中
    cfg["expect_ip"] = "127.0.0.1"
    m = _by_metric(run(collect_app_metrics(_device(cfg))))
    assert m["app_available"].value == 1.0
    cfg["expect_ip"] = "203.0.113.1"
    m = _by_metric(run(collect_app_metrics(_device(cfg))))
    assert m["app_available"].value == 0.0


def test_dns_custom_server_udp():
    """指定 DNS 服务器：手写 UDP 查询包，假服务器回固定 A 记录。"""
    async def main():
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            _FakeDnsProtocol, local_addr=("127.0.0.1", 0)
        )
        port = transport.get_extra_info("sockname")[1]
        try:
            cfg = {"probe_kind": "dns", "domain": "example.com",
                   "server": f"127.0.0.1:{port}", "expect_ip": "203.0.113.99"}
            m = _by_metric(await collect_app_metrics(_device(cfg)))
            assert m["app_available"].value == 1.0
            cfg["expect_ip"] = "203.0.113.100"
            m = _by_metric(await collect_app_metrics(_device(cfg)))
            assert m["app_available"].value == 0.0
        finally:
            transport.close()
    run(main())


def test_dns_packet_roundtrip():
    """查询包/应答解析的纯函数单测（含压缩指针应答）。"""
    q = app_probe._build_dns_query("a.b.com", 0x1234)
    assert q[:2] == b"\x12\x34" and q[4:6] == b"\x00\x01"  # QID + QDCOUNT=1
    # 构造应答：问题段 + 指针名 A 记录 1.2.3.4
    end = q.index(b"\x00", 12) + 5
    resp = q[:2] + struct.pack(">HHHHH", 0x8180, 1, 1, 0, 0) + q[12:end]
    resp += b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + bytes([1, 2, 3, 4])
    assert app_probe._parse_dns_answers(resp, 0x1234) == ["1.2.3.4"]


# ============ 空配置容错 ============

def test_empty_config_tolerated():
    assert run(collect_app_metrics(_device({}))) == []
    assert run(collect_app_metrics(_device(None))) == []
    assert run(collect_app_metrics(_device({"probe_kind": "http"}))) == []      # 缺 url
    assert run(collect_app_metrics(_device({"probe_kind": "dns"}))) == []       # 缺 domain
    assert run(collect_app_metrics(_device({"probe_kind": "tcp"}))) == []       # 缺 port
    assert run(collect_app_metrics(_device({"probe_kind": "icmp"}))) == []      # 未知类型


# ============ 调度注册 ============

def test_scheduler_task_registered():
    task = next((t for t in TASKS if t.name == "app_probe"), None)
    assert task is not None, "TASKS 未注册 app_probe"
    assert task.interval == 60
    assert task.applies_to(SimpleNamespace(type="application", monitor_enabled=True))
    assert not task.applies_to(SimpleNamespace(type="application", monitor_enabled=False))
    assert not task.applies_to(SimpleNamespace(type="network", monitor_enabled=True))


# ============ 内置规则 ============

def test_builtin_rule_seeded(client):
    db = SessionLocal()
    try:
        db.query(AlertRule).filter(AlertRule.name == "应用不可达").delete()
        db.commit()
    finally:
        db.close()
    _init_db()
    db = SessionLocal()
    try:
        rules = db.query(AlertRule).filter(AlertRule.name == "应用不可达").all()
    finally:
        db.close()
    assert len(rules) == 1
    r = rules[0]
    assert (r.metric, r.op, r.threshold, r.duration_cycles, r.severity, r.device_type) == \
        ("app_available", "==", 0, 2, "major", "application")
    assert r.builtin is True


# ============ API：application 设备与 probe_config 校验 ============

def _create_app_device(client, token, ip, probe_config=None, **kw):
    body = {"ip": ip, "name": f"app-{ip}", "type": "application", **kw}
    if probe_config is not None:
        body["probe_config"] = probe_config
    return client.post("/api/devices", json=body, headers=auth(token))


def test_api_create_application_device(client, admin_token):
    cfg = {"probe_kind": "http", "url": "http://192.0.2.91:8080/health",
           "expect_status": 200, "keyword": "ok", "timeout": 5}
    r = _create_app_device(client, admin_token, "192.0.2.91", cfg)
    assert r.status_code == 201, r.text
    assert r.json()["probe_config"] == cfg
    # 编辑回读
    did = r.json()["id"]
    r = client.put(f"/api/devices/{did}",
                   json={"ip": "192.0.2.91", "type": "application",
                         "probe_config": {"probe_kind": "tcp", "port": 8080}},
                   headers=auth(admin_token))
    assert r.status_code == 200, r.text
    assert r.json()["probe_config"] == {"probe_kind": "tcp", "port": 8080}


def test_api_application_allows_hostname(client, admin_token):
    """application 的 ip 字段语义为目标主机：允许域名；其他类型仍要求 IP。"""
    r = _create_app_device(client, admin_token, "web-203.internal",
                           {"probe_kind": "dns", "domain": "web-203.internal"})
    assert r.status_code == 201, r.text
    r = client.post("/api/devices", json={"ip": "not-an-ip", "type": "network"},
                    headers=auth(admin_token))
    assert r.status_code == 400


def test_api_probe_config_validation(client, admin_token):
    # 缺 probe_kind / 空配置
    assert _create_app_device(client, admin_token, "192.0.2.92", {}).status_code == 422
    # 未知 probe_kind
    assert _create_app_device(
        client, admin_token, "192.0.2.92", {"probe_kind": "icmp"}).status_code == 422
    # http 缺 url / url 非 http(s)
    assert _create_app_device(
        client, admin_token, "192.0.2.92", {"probe_kind": "http"}).status_code == 422
    assert _create_app_device(
        client, admin_token, "192.0.2.92",
        {"probe_kind": "http", "url": "ftp://x"}).status_code == 422
    # dns 缺 domain
    assert _create_app_device(
        client, admin_token, "192.0.2.92", {"probe_kind": "dns"}).status_code == 422
    # tcp 缺 port / port 越界
    assert _create_app_device(
        client, admin_token, "192.0.2.92", {"probe_kind": "tcp"}).status_code == 422
    assert _create_app_device(
        client, admin_token, "192.0.2.92",
        {"probe_kind": "tcp", "port": 70000}).status_code == 422
    # 合法 tcp 配置能建
    r = _create_app_device(client, admin_token, "192.0.2.92",
                           {"probe_kind": "tcp", "port": 22, "banner": "SSH"})
    assert r.status_code == 201, r.text
