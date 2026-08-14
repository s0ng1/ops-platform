"""轻量 IPAM：OID 解析（mock fetch_walk）、upsert 新建/更新/last_seen 刷新、
新终端事件触发 + 白名单抑制、清单 API 过滤分页、子网汇总、viewer 权限。
注意：全测试套件共享一个会话级库——测试数据 IP 用 203.0.113.x 段避开其他用例。
"""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from conftest import auth

from app.collectors import ipam
from app.core.database import SessionLocal
from app.models import Device, IpInventory

# 假 walk 数据用的 MAC / OID 常量
MAC_A = "8c:16:45:aa:bb:01"
MAC_B = "8c:16:45:aa:bb:02"
# MAC_A 的 6 段十进制（dot1dTpFdbTable OID 后缀）
MAC_A_DECIMAL = "140.22.69.170.187.1"


def _make_device(client, token, ip):
    """建一台 network 设备，返回设备 id。"""
    r = client.post(
        "/api/devices",
        json={"ip": ip, "name": f"sw-{ip}", "type": "network"},
        headers=auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _db_device(device_id):
    db = SessionLocal()
    try:
        return db.get(Device, device_id)
    finally:
        db.close()


def _inventory_row(ip):
    db = SessionLocal()
    try:
        return db.query(IpInventory).filter(IpInventory.ip == ip).first()
    finally:
        db.close()


def _fake_walk(tables):
    async def fake_walk(host, payload, base_oid):
        return tables.get(base_oid, {})
    return fake_walk


# ---- 纯解析函数 ----


def test_normalize_mac_formats():
    assert ipam.normalize_mac("0x8c1645aabb01") == MAC_A
    assert ipam.normalize_mac("8C:16:45:AA:BB:01") == MAC_A
    assert ipam.normalize_mac("8c-16-45-aa-bb-01") == MAC_A
    assert ipam.normalize_mac("8c16.45aa.bb01") == MAC_A
    assert ipam.normalize_mac(MAC_A_DECIMAL) == MAC_A          # 6 段十进制 OID 后缀
    assert ipam.normalize_mac("8c1645aabb01") == MAC_A          # 裸 12 位十六进制
    assert ipam.normalize_mac("") is None
    assert ipam.normalize_mac("not-a-mac") is None
    assert ipam.normalize_mac("0x123") is None
    assert ipam.normalize_mac("1.2.3.4") is None                # 4 段十进制是 IP 不是 MAC


def test_parse_arp_table():
    raw = {
        f"{ipam.OID_ARP_MAC}.100.203.0.113.10": "0x8c1645aabb01",
        f"{ipam.OID_ARP_MAC}.100.203.0.113.11": "8C:16:45:AA:BB:02",
        f"{ipam.OID_ARP_MAC}.100.203.0.113.12": "garbage",      # 无效 MAC 跳过
        f"{ipam.OID_ARP_MAC}.100.not-an-ip": "0x8c1645aabb03",  # 无效 IP 跳过
    }
    entries = ipam.parse_arp_table(raw)
    by_ip = {e["ip"]: e for e in entries}
    assert set(by_ip) == {"203.0.113.10", "203.0.113.11"}
    assert by_ip["203.0.113.10"]["mac"] == MAC_A
    assert by_ip["203.0.113.11"]["mac"] == MAC_B


def test_parse_fdb_and_if_names():
    fdb = ipam.parse_fdb_table({
        f"{ipam.OID_FDB_PORT}.{MAC_A_DECIMAL}": "15",
        f"{ipam.OID_FDB_PORT}.140.22.69.170.187.2": "bad",  # 非数字端口跳过
    })
    assert fdb == {MAC_A: 15}
    names = ipam.parse_if_names({
        f"{ipam.OID_IF_NAME}.15": "GigabitEthernet1/0/15",
        f"{ipam.OID_IF_NAME}.16": "",
    })
    assert names == {15: "GigabitEthernet1/0/15"}


# ---- 采集 + upsert ----


def _arp_tables(ip_last, mac_hex):
    return {
        ipam.OID_ARP_MAC: {f"{ipam.OID_ARP_MAC}.100.203.0.113.{ip_last}": mac_hex},
        ipam.OID_FDB_PORT: {f"{ipam.OID_FDB_PORT}.{MAC_A_DECIMAL}": "15"},
        ipam.OID_IF_NAME: {f"{ipam.OID_IF_NAME}.15": "GigabitEthernet1/0/15"},
    }


def test_collect_insert_then_update(client, admin_token):
    did = _make_device(client, admin_token, "203.0.113.1")
    device = _db_device(did)

    # 首次采集：新终端入库（arp 来源，接入端口由 MAC 表+ifName 补出）
    points = asyncio.run(ipam.collect_ipam(device, {}, fetch_walk=_fake_walk(_arp_tables(10, "0x8c1645aabb01"))))
    assert len(points) == 1
    assert points[0].metric == "new_terminal"
    assert points[0].device_id == did
    assert points[0].labels["ip"] == "203.0.113.10"
    assert points[0].labels["mac"] == MAC_A
    assert points[0].labels["if"] == "GigabitEthernet1/0/15"

    row = _inventory_row("203.0.113.10")
    assert row is not None
    assert row.mac == MAC_A
    assert row.device_id == did
    assert row.if_name == "GigabitEthernet1/0/15"
    assert row.source == "arp"
    assert row.whitelisted is False
    assert row.first_seen == row.last_seen

    # 把 last_seen 拨旧再采集一次：更新路径——last_seen 刷新、不再出新终端点
    db = SessionLocal()
    old_seen = datetime.now() - timedelta(hours=1)
    db.query(IpInventory).filter(IpInventory.ip == "203.0.113.10").update({"last_seen": old_seen})
    db.commit()
    db.close()

    points = asyncio.run(ipam.collect_ipam(device, {}, fetch_walk=_fake_walk(_arp_tables(10, "0x8c1645aabb01"))))
    assert points == []
    row = _inventory_row("203.0.113.10")
    assert row.last_seen > old_seen
    assert row.first_seen < row.last_seen


def test_upsert_mac_change_no_alert_and_ping_source(client, admin_token):
    did = _make_device(client, admin_token, "203.0.113.2")
    device = _db_device(did)

    # ping 来源先建档（无 MAC）
    points = asyncio.run(ipam.upsert_scan_results(["203.0.113.20"]))
    # 该 IP 不是已入库设备，事件无处挂载 → 只入台账不出点
    assert points == []
    row = _inventory_row("203.0.113.20")
    assert row is not None and row.source == "ping" and row.mac is None

    # arp 采集学到同一 IP：补 MAC/接入设备/端口，来源升 arp，更新不告警
    points = asyncio.run(ipam.collect_ipam(device, {}, fetch_walk=_fake_walk(_arp_tables(20, "0x8c1645aabb01"))))
    assert points == []
    row = _inventory_row("203.0.113.20")
    assert row.mac == MAC_A
    assert row.device_id == did
    assert row.source == "arp"

    # MAC 变化（终端换网卡）：更新但不告警（本期只告新 IP）
    points = asyncio.run(ipam.collect_ipam(device, {}, fetch_walk=_fake_walk(_arp_tables(20, "0x8c1645aabb02"))))
    assert points == []
    assert _inventory_row("203.0.113.20").mac == MAC_B

    # arp 建档后再被扫描看到：ping 不覆盖 arp 来源，只刷新 last_seen
    asyncio.run(ipam.upsert_scan_results(["203.0.113.20"]))
    assert _inventory_row("203.0.113.20").source == "arp"


def test_upsert_failure_silent(client, admin_token):
    """walk 抛异常：本轮整体跳过、静默返回空点。"""
    did = _make_device(client, admin_token, "203.0.113.3")
    device = _db_device(did)

    async def bad_walk(host, payload, base_oid):
        raise OSError("timeout")

    assert asyncio.run(ipam.collect_ipam(device, {}, fetch_walk=bad_walk)) == []
    # ARP 表为空也是正常情况（无终端），不报错
    assert asyncio.run(ipam.collect_ipam(device, {}, fetch_walk=_fake_walk({}))) == []


# ---- 新终端事件 + 白名单抑制 ----


def test_new_terminal_event_and_whitelist_suppression(client, admin_token):
    from app.alerting import engine as alert_engine
    from app.main import _init_db

    _init_db()  # 幂等补种内置规则（其他用例可能清过规则表）
    did = _make_device(client, admin_token, "203.0.113.4")
    device = _db_device(did)

    # 新终端 → 指标点过引擎 → info 级「新终端接入」事件
    points = asyncio.run(ipam.collect_ipam(device, {}, fetch_walk=_fake_walk(_arp_tables(30, "0x8c1645aabb01"))))
    asyncio.run(alert_engine.evaluate_points(points))
    resp = client.get(f"/api/alert/events?device_id={did}", headers=auth(admin_token))
    events = [e for e in resp.json() if e["rule_name"] == "新终端接入"]
    assert len(events) == 1
    assert events[0]["severity"] == "info"
    assert events[0]["labels"]["ip"] == "203.0.113.30"
    assert events[0]["labels"]["mac"] == MAC_A

    # 白名单记录再出现不触发：预置 whitelisted=True 的台账行，upsert 走更新路径无点
    db = SessionLocal()
    db.add(IpInventory(ip="203.0.113.31", source="ping", whitelisted=True,
                       first_seen=datetime.now(), last_seen=datetime.now()))
    db.commit()
    db.close()
    points = asyncio.run(ipam.collect_ipam(device, {}, fetch_walk=_fake_walk(_arp_tables(31, "0x8c1645aabb01"))))
    assert points == []
    resp = client.get(f"/api/alert/events?device_id={did}", headers=auth(admin_token))
    events = [e for e in resp.json() if e["rule_name"] == "新终端接入" and e["labels"].get("ip") == "203.0.113.31"]
    assert events == []


# ---- 清单 API ----


def _seed_inventory_rows():
    """造 3 条清单数据（ping 2 条 + 已有 arp 若干条来自前面用例）。"""
    asyncio.run(ipam.upsert_scan_results(["203.0.113.40", "203.0.113.41", "203.0.113.42"]))


def test_inventory_api_filter_and_pagination(client, admin_token):
    _seed_inventory_rows()

    resp = client.get("/api/ipam/inventory?subnet=203.0.113.0/24&page_size=2&page=1",
                      headers=auth(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 3
    assert len(data["items"]) == 2
    # 按 last_seen 倒序
    assert data["items"][0]["last_seen"] >= data["items"][1]["last_seen"]

    page2 = client.get("/api/ipam/inventory?subnet=203.0.113&page_size=2&page=2",
                       headers=auth(admin_token)).json()
    assert page2["total"] == data["total"]
    assert page2["items"][0]["id"] != data["items"][0]["id"]

    # 关键字 / 来源 / MAC 过滤
    r = client.get("/api/ipam/inventory?keyword=203.0.113.40", headers=auth(admin_token)).json()
    assert r["total"] == 1 and r["items"][0]["ip"] == "203.0.113.40"
    r = client.get("/api/ipam/inventory?source=ping&subnet=203.0.113.0/24",
                   headers=auth(admin_token)).json()
    assert all(i["source"] == "ping" for i in r["items"])
    r = client.get("/api/ipam/inventory?mac=8c:16:45", headers=auth(admin_token)).json()
    assert r["total"] >= 1
    assert all(i["mac"] and "8c:16:45" in i["mac"] for i in r["items"])
    r = client.get("/api/ipam/inventory?whitelisted=false", headers=auth(admin_token)).json()
    assert all(i["whitelisted"] is False for i in r["items"])


def test_inventory_update_and_viewer_forbidden(client, admin_token, viewer_token):
    row = _inventory_row("203.0.113.40")
    assert row is not None

    # viewer 无权改白名单
    r = client.put(f"/api/ipam/inventory/{row.id}", json={"whitelisted": True},
                   headers=auth(viewer_token))
    assert r.status_code == 403
    # viewer 只读接口可看
    r = client.get("/api/ipam/inventory?keyword=203.0.113.40", headers=auth(viewer_token))
    assert r.status_code == 200

    # operator/admin 可改 whitelisted + hostname（备注），其余字段不动
    r = client.put(f"/api/ipam/inventory/{row.id}",
                   json={"whitelisted": True, "hostname": "财务-打印机"},
                   headers=auth(admin_token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["whitelisted"] is True
    assert body["hostname"] == "财务-打印机"
    assert body["source"] == "ping" and body["ip"] == "203.0.113.40"

    # 空串 hostname = 清空备注
    r = client.put(f"/api/ipam/inventory/{row.id}", json={"hostname": ""},
                   headers=auth(admin_token))
    assert r.json()["hostname"] is None

    # 不存在的记录 404
    r = client.put("/api/ipam/inventory/999999", json={"whitelisted": True},
                   headers=auth(admin_token))
    assert r.status_code == 404


# ---- 子网汇总 ----


def test_subnet_summary(client, admin_token):
    # 一条拨到 1 小时前（active7d），一条拨到 8 天前（stale）
    db = SessionLocal()
    db.query(IpInventory).filter(IpInventory.ip == "203.0.113.41").update(
        {"last_seen": datetime.now() - timedelta(hours=1)})
    db.query(IpInventory).filter(IpInventory.ip == "203.0.113.42").update(
        {"last_seen": datetime.now() - timedelta(days=8)})
    db.commit()
    db.close()

    resp = client.get("/api/ipam/subnets", headers=auth(admin_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["prefix_len"] == 24
    assert data["total_subnets"] >= 1
    assert len(data["subnets"]) <= 16

    subnet = next(s for s in data["subnets"] if s["subnet"] == "203.0.113.0/24")
    assert subnet["total"] >= 3
    by_ip = {i["ip"]: i for i in subnet["ips"]}
    assert by_ip["203.0.113.40"]["status"] == "online"     # 刚被扫描刷新
    assert by_ip["203.0.113.41"]["status"] == "active7d"
    assert by_ip["203.0.113.42"]["status"] == "stale"
    assert subnet["online"] >= 1
    assert subnet["active7d"] >= 2
    # 网格数组带前端 tooltip 所需字段；库存外的 IP 不出现（前端自行补「未见」）
    assert "203.0.113.250" not in by_ip
    assert "device_name" in by_ip["203.0.113.40"]


# ---- 调度器注册 ----


def test_scheduler_task_registered():
    from app.scheduler.scheduler import TASKS

    task = next(t for t in TASKS if t.name == "ipam_collect")
    assert task.interval == 300

    snmp_cred = SimpleNamespace(kind="snmp_v2c")
    device = SimpleNamespace(monitor_enabled=True, type="network", credential=snmp_cred)
    assert task.applies_to(device)
    device.type = "server_linux"  # 服务器不在 IPAM 采集范围
    assert not task.applies_to(device)
    device.type = "network"
    device.credential = SimpleNamespace(kind="ssh")  # 无 SNMP 凭据不适用
    assert not task.applies_to(device)
