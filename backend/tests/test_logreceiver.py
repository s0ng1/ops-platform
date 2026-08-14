"""Syslog/Trap 接收器测试：RFC3164 解析、规则匹配、落库注入点、告警链路、
事件 API 过滤分页、规则 CRUD 与 RBAC、本机 UDP 端到端（Syslog 与 Trap）。
测试数据统一用 203.0.113.x 段，避免撞其他用例的设备与断言。
"""
import asyncio
import socket
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.alerting import engine
from app.core import logreceiver
from app.core.database import SessionLocal
from app.main import _init_db
from app.models import AlertEvent, LogEvent, LogRule
from conftest import auth


def run(coro):
    return asyncio.run(coro)


# ---- RFC3164 解析 ----


def test_parse_syslog_standard():
    """标准报文：<pri>时间戳 主机 tag: 消息，pri 拆 facility/severity，tag 保留在内容里。"""
    parsed = logreceiver.parse_syslog(
        b"<34>Oct 11 22:14:15 mymachine su: 'su root' failed for lonvick on /dev/pts/8"
    )
    assert parsed["facility"] == 4   # 34 // 8
    assert parsed["severity"] == 2   # 34 % 8
    assert parsed["host"] == "mymachine"
    assert parsed["message"] == "su: 'su root' failed for lonvick on /dev/pts/8"


def test_parse_syslog_tag_with_pid():
    parsed = logreceiver.parse_syslog(
        b"<13>Jul 27 10:00:00 coresw sshd[1234]: Failed password for admin"
    )
    assert parsed["facility"] == 1
    assert parsed["severity"] == 5
    assert parsed["message"] == "sshd[1234]: Failed password for admin"


def test_parse_syslog_no_pri():
    """无 <pri> 的宽松报文：等级为空，内容保留。"""
    parsed = logreceiver.parse_syslog(b"hello from some device")
    assert parsed["facility"] is None
    assert parsed["severity"] is None
    assert "hello from some device" in parsed["message"]


def test_parse_syslog_malformed():
    """畸形/二进制报文：不抛异常，宽松兜底存原文。"""
    raw = b"\xff\xfe\x00garbage \x01\x02"
    parsed = logreceiver.parse_syslog(raw)
    assert parsed["facility"] is None
    assert parsed["severity"] is None
    assert "garbage" in parsed["message"]


# ---- 规则匹配 ----


def _rule(**kw):
    fields = dict(id=1, name="r", source_ip=None, keyword=None,
                  severity_lte=None, alert_severity="warning")
    fields.update(kw)
    return SimpleNamespace(**fields)


def test_source_allowlist():
    """来源 IP 白名单：命中放行、未命中丢弃、空白名单全放行、非法项忽略。"""
    nets = logreceiver._parse_allowlist("192.168.1.0/24, 10.0.0.1")
    assert logreceiver._source_allowed("192.168.1.50", nets)
    assert logreceiver._source_allowed("10.0.0.1", nets)
    assert not logreceiver._source_allowed("192.168.2.1", nets)
    assert not logreceiver._source_allowed("172.16.0.1", nets)
    assert not logreceiver._source_allowed("bad-ip", nets)
    # 空白名单 = 全部放行
    assert logreceiver._source_allowed("1.2.3.4", [])
    # 非法项被忽略，合法项照常解析
    assert len(logreceiver._parse_allowlist("bad-item, 10.0.0.0/8")) == 1


def test_rule_matches_combinations():
    m = logreceiver.rule_matches
    # 全空=全部命中
    assert m(_rule(), "syslog", "203.0.113.1", 7, "anything")
    # 源 IP 精确匹配
    assert m(_rule(source_ip="203.0.113.1"), "syslog", "203.0.113.1", 7, "x")
    assert not m(_rule(source_ip="203.0.113.1"), "syslog", "203.0.113.2", 7, "x")
    # 关键字子串
    assert m(_rule(keyword="down"), "syslog", "203.0.113.1", 7, "link is down")
    assert not m(_rule(keyword="down"), "syslog", "203.0.113.1", 7, "link is up")
    # severity_lte 仅 syslog 生效：等级小于等于才命中
    assert m(_rule(severity_lte=3), "syslog", "203.0.113.1", 2, "x")
    assert not m(_rule(severity_lte=3), "syslog", "203.0.113.1", 5, "x")
    # trap 无 severity，不命中带等级条件的规则；不带等级条件则命中
    assert not m(_rule(severity_lte=3), "trap", "203.0.113.1", None, "x")
    assert m(_rule(keyword="coldStart"), "trap", "203.0.113.1", None, "enterprise=1.3.6.1 coldStart")
    # 三条件叠加（与关系）
    r = _rule(source_ip="203.0.113.1", keyword="BGP", severity_lte=4)
    assert m(r, "syslog", "203.0.113.1", 3, "BGP neighbor down")
    assert not m(r, "syslog", "203.0.113.2", 3, "BGP neighbor down")
    assert not m(r, "syslog", "203.0.113.1", 6, "BGP neighbor down")


# ---- 落库注入点 ----


def test_ingest_store_injection(client):
    """store 注入：不调真库落事件，参数原样传给注入函数。"""
    calls = []

    def fake_store(source_ip, kind, facility, severity, message):
        calls.append((source_ip, kind, facility, severity, message))
        return 1

    logreceiver.reset_suppression()
    run(logreceiver.ingest_event("203.0.113.99", "syslog", 1, 5, "注入点测试报文", store=fake_store))
    assert calls == [("203.0.113.99", "syslog", 1, 5, "注入点测试报文")]


def test_ingest_store_to_db(client):
    """默认 store 真落库：log_events 能查到该事件。"""
    logreceiver.reset_suppression()
    run(logreceiver.ingest_event("203.0.113.99", "trap", None, None, "enterprise=1.3.6.1.4.1.25506 test"))
    db = SessionLocal()
    try:
        row = (
            db.query(LogEvent)
            .filter(LogEvent.source_ip == "203.0.113.99", LogEvent.kind == "trap")
            .first()
        )
        assert row is not None
        assert "enterprise=1.3.6.1.4.1.25506" in row.message
        assert row.severity is None
    finally:
        db.close()


# ---- 规则命中 → 告警引擎 ----


def _ensure_device(client, admin_token, ip, name):
    r = client.post("/api/devices", json={"ip": ip, "name": name, "type": "network"},
                    headers=auth(admin_token))
    assert r.status_code in (201, 409), r.text


def test_rule_hit_creates_alert(client, admin_token):
    """命中日志规则：产生对应等级的告警事件；5 分钟内重复命中抑制。"""
    _init_db()  # 前面用例可能清过 AlertRule，重种内置「日志事件-x」规则（幂等）
    engine.reset_counters()
    logreceiver.reset_suppression()
    _ensure_device(client, admin_token, "203.0.113.10", "日志源测试机")
    db = SessionLocal()
    try:
        db.add(LogRule(name="M2测试-关键字告警", keyword="LINK-DOWN-M2-TEST",
                       alert_severity="major"))
        db.commit()
    finally:
        db.close()

    msg = "%LINK-DOWN-M2-TEST GigabitEthernet1/0/1"
    run(logreceiver.ingest_event("203.0.113.10", "syslog", 4, 2, msg))
    db = SessionLocal()
    try:
        events = db.query(AlertEvent).filter(AlertEvent.rule_name == "日志事件-严重").all()
        assert len(events) == 1
        event = events[0]
        assert event.severity == "major"
        assert event.status == "firing"
        assert event.labels.get("rule") == "M2测试-关键字告警"
        # 5 分钟抑制：同源同规则重复命中不再产生新事件
        run(logreceiver.ingest_event("203.0.113.10", "syslog", 4, 2, msg))
        assert db.query(AlertEvent).filter(AlertEvent.rule_name == "日志事件-严重").count() == 1
    finally:
        db.close()
    # 清理：避免 firing 事件影响后续用例
    db = SessionLocal()
    try:
        db.query(AlertEvent).filter(AlertEvent.rule_name == "日志事件-严重").delete()
        db.query(LogRule).filter(LogRule.name == "M2测试-关键字告警").delete()
        db.commit()
    finally:
        db.close()
    engine.reset_counters()
    logreceiver.reset_suppression()


def test_unknown_source_only_stores(client):
    """来源 IP 未入库设备：只存日志事件，不产生告警。"""
    _init_db()
    logreceiver.reset_suppression()
    db = SessionLocal()
    try:
        db.add(LogRule(name="M2测试-未入库源", keyword="NOSEEN-M2-TEST", alert_severity="info"))
        db.commit()
    finally:
        db.close()
    run(logreceiver.ingest_event("203.0.113.250", "syslog", 0, 0, "NOSEEN-M2-TEST hello"))
    db = SessionLocal()
    try:
        assert db.query(LogEvent).filter(LogEvent.source_ip == "203.0.113.250").count() == 1
        assert db.query(AlertEvent).filter(AlertEvent.metric == "log_event").count() == 0
        db.query(LogRule).filter(LogRule.name == "M2测试-未入库源").delete()
        db.query(LogEvent).filter(LogEvent.source_ip == "203.0.113.250").delete()
        db.commit()
    finally:
        db.close()
    logreceiver.reset_suppression()


# ---- 事件 API 过滤分页 ----


def _seed_events():
    now = datetime.now()
    rows = [
        LogEvent(source_ip="203.0.113.20", kind="syslog", facility=1, severity=3,
                 message="API种子 error on port", created_at=now - timedelta(hours=2)),
        LogEvent(source_ip="203.0.113.20", kind="syslog", facility=1, severity=6,
                 message="API种子 info message", created_at=now - timedelta(hours=1)),
        LogEvent(source_ip="203.0.113.21", kind="trap", facility=None, severity=None,
                 message="API种子 enterprise=1.3.6.1.4.1.9 linkDown", created_at=now),
    ]
    db = SessionLocal()
    try:
        db.add_all(rows)
        db.commit()
    finally:
        db.close()


def test_events_api_filter_and_pagination(client, admin_token):
    _seed_events()
    # 按来源 IP 过滤
    r = client.get("/api/logs/events?source_ip=203.0.113.20", headers=auth(admin_token))
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 2
    assert all(i["source_ip"] == "203.0.113.20" for i in data["items"])
    # 按类型过滤
    r = client.get("/api/logs/events?kind=trap", headers=auth(admin_token))
    assert all(i["kind"] == "trap" for i in r.json()["items"])
    assert any(i["source_ip"] == "203.0.113.21" for i in r.json()["items"])
    # 按等级过滤
    r = client.get("/api/logs/events?source_ip=203.0.113.20&severity=3", headers=auth(admin_token))
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["severity"] == 3
    # 关键字过滤
    r = client.get("/api/logs/events?keyword=linkDown", headers=auth(admin_token))
    assert r.json()["total"] >= 1
    assert all("linkDown" in i["message"] for i in r.json()["items"])
    # 时间范围过滤（种子中 2 小时前那条在 90 分钟窗口外）
    start = (datetime.now() - timedelta(minutes=90)).isoformat()
    r = client.get(f"/api/logs/events?source_ip=203.0.113.20&start={start}",
                   headers=auth(admin_token))
    assert r.json()["total"] == 1
    # 分页
    r = client.get("/api/logs/events?page=1&page_size=2", headers=auth(admin_token))
    data = r.json()
    assert len(data["items"]) == 2
    assert data["total"] >= 3
    r2 = client.get("/api/logs/events?page=2&page_size=2", headers=auth(admin_token))
    assert r2.json()["items"][0]["id"] != data["items"][0]["id"]


# ---- 规则 CRUD + RBAC ----


def test_log_rule_crud(client, admin_token):
    body = {
        "name": "M2测试-CRUD规则",
        "enabled": True,
        "source_ip": "203.0.113.30",
        "keyword": "BGP",
        "severity_lte": 4,
        "alert_severity": "critical",
    }
    r = client.post("/api/logs/rules", json=body, headers=auth(admin_token))
    assert r.status_code == 201, r.text
    rule = r.json()
    assert rule["severity_lte"] == 4 and rule["alert_severity"] == "critical"

    r = client.get("/api/logs/rules", headers=auth(admin_token))
    assert any(x["name"] == "M2测试-CRUD规则" for x in r.json())

    body["keyword"] = "OSPF"
    body["enabled"] = False
    r = client.put(f"/api/logs/rules/{rule['id']}", json=body, headers=auth(admin_token))
    assert r.status_code == 200
    assert r.json()["keyword"] == "OSPF" and r.json()["enabled"] is False

    r = client.delete(f"/api/logs/rules/{rule['id']}", headers=auth(admin_token))
    assert r.status_code == 200
    r = client.get("/api/logs/rules", headers=auth(admin_token))
    assert not any(x["name"] == "M2测试-CRUD规则" for x in r.json())


def test_viewer_cannot_write_log_rule(client, viewer_token):
    body = {"name": "x", "alert_severity": "info"}
    assert client.post("/api/logs/rules", json=body, headers=auth(viewer_token)).status_code == 403
    assert client.put("/api/logs/rules/1", json=body, headers=auth(viewer_token)).status_code == 403
    assert client.delete("/api/logs/rules/1", headers=auth(viewer_token)).status_code == 403
    # 只读接口 viewer 可用
    assert client.get("/api/logs/rules", headers=auth(viewer_token)).status_code == 200
    assert client.get("/api/logs/events", headers=auth(viewer_token)).status_code == 200


# ---- UDP 端到端（本机 ephemeral 端口，不依赖 1514/1162）----


def test_syslog_udp_end_to_end(monkeypatch):
    """真发 UDP 报文到本机接收协议：解析后经 ingest_event 入口处理。"""
    received = []

    async def fake_ingest(source_ip, kind, facility, severity, message, store=None):
        received.append((source_ip, kind, facility, severity, message))

    monkeypatch.setattr(logreceiver, "ingest_event", fake_ingest)

    async def scenario():
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            logreceiver._SyslogProtocol, local_addr=("127.0.0.1", 0)
        )
        port = transport.get_extra_info("sockname")[1]
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(b"<11>Jul 27 12:00:00 sw1 kernel: UDP-E2E-M2-TEST packet", ("127.0.0.1", port))
            sock.close()
            for _ in range(50):
                if received:
                    break
                await asyncio.sleep(0.02)
        finally:
            transport.close()

    run(scenario())
    assert len(received) == 1
    source_ip, kind, facility, severity, message = received[0]
    assert source_ip == "127.0.0.1"
    assert kind == "syslog"
    assert (facility, severity) == (1, 3)  # pri 11
    assert message == "kernel: UDP-E2E-M2-TEST packet"


def test_trap_udp_end_to_end():
    """pysnmp 起真接收器（ephemeral 端口），hlapi 发 v2c Trap（任意 community 宽松收）。"""
    from app.collectors import snmp
    from pysnmp.hlapi.v3arch.asyncio import (
        CommunityData,
        ContextData,
        NotificationType,
        ObjectIdentity,
        SnmpEngine,
        UdpTransportTarget,
        send_notification,
    )

    received = []

    async def scenario():
        handle = snmp.start_trap_receiver(0, lambda ip, msg: received.append((ip, msg)))
        try:
            # open_server_mode 是异步绑定：先等绑定完成再取实际端口（测试专用，探内部属性）
            await handle._transport._lport
            sockname = handle._transport.transport.get_extra_info("sockname")
            port = sockname[1]
            target = await UdpTransportTarget.create(("127.0.0.1", port), timeout=1, retries=0)
            # community 故意用未注册值，验证宽松模式
            await send_notification(
                SnmpEngine(),
                CommunityData("no-such-community", mpModel=1),
                target,
                ContextData(),
                "trap",
                NotificationType(ObjectIdentity("1.3.6.1.4.1.25506.2.6.2.0.1")),
            )
            for _ in range(50):
                if received:
                    break
                await asyncio.sleep(0.02)
        finally:
            handle.close()

    run(scenario())
    assert len(received) == 1
    source_ip, message = received[0]
    assert source_ip == "127.0.0.1"
    assert "enterprise=1.3.6.1.4.1.25506.2.6.2.0.1" in message
