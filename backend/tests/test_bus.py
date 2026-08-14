"""第 8 期 M5：宿主机-应用总线视图（/api/bus）测试。

IP 段：192.0.2.5x（本文件专用，勿与其他测试文件冲突）。
firing 事件直接插库（绕过引擎，造数确定可控）；套件共享会话级库，
断言只按本文件设备 id/IP 过滤，不受其他文件数据影响。
"""
from conftest import auth

HOST_A_IP = "192.0.2.51"   # Linux 主机：挂 database + application
HOST_B_IP = "192.0.2.52"   # Windows 主机：光杆（无挂载对象）
ORPHAN_IP = "192.0.2.53"   # 孤儿 database：无同 IP 宿主机，不应出现
NET_IP = "192.0.2.54"      # 网络设备：与总线无关，不应出现


def _device(client, token, ip, name, type, **kw):
    r = client.post("/api/devices", json={"ip": ip, "name": name, "type": type, **kw},
                    headers=auth(token))
    if r.status_code == 409:
        r = client.get(f"/api/devices?keyword={ip}", headers=auth(token))
        return next(d["id"] for d in r.json() if d["ip"] == ip and d["type"] == type)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _mk_devices(client, admin_token):
    """造总线测试设备（幂等），返回 (host_a, db_a, app_a, host_b, orphan_db, net)。"""
    host_a = _device(client, admin_token, HOST_A_IP, "M5-主机A", "server_linux")
    db_a = _device(client, admin_token, HOST_A_IP, "M5-库A", "database")
    app_a = _device(client, admin_token, HOST_A_IP, "M5-应用A", "application",
                    probe_config={"probe_kind": "tcp", "port": 8080})
    host_b = _device(client, admin_token, HOST_B_IP, "M5-主机B", "server_windows")
    orphan = _device(client, admin_token, ORPHAN_IP, "M5-孤儿库", "database")
    net = _device(client, admin_token, NET_IP, "M5-交换机", "network")
    return host_a, db_a, app_a, host_b, orphan, net


def _fire(device_id, severity, status="firing"):
    """直接插一条告警事件（firing/resolved 由 status 控制）。"""
    from app.core.database import SessionLocal
    from app.models import AlertEvent

    db = SessionLocal()
    try:
        ev = AlertEvent(rule_name="M5测试规则", device_id=device_id, metric="cpu_usage",
                        severity=severity, status=status, value=99.0)
        db.add(ev)
        db.commit()
        return ev.id
    finally:
        db.close()


def _find_host(data, ip, type=None):
    return next((h for h in data["hosts"]
                 if h["ip"] == ip and (type is None or h["type"] == type)), None)


def test_bus_structure_and_grouping(client, admin_token):
    """总线结构：宿主机为基座、同 IP database/application 挂载、
    光杆宿主机空挂载、孤儿/网络设备不出现、字段齐全。"""
    host_a, db_a, app_a, host_b, orphan, net = _mk_devices(client, admin_token)

    # 把主机A置为在线（device.status 由监控协程更新，测试直接改库）
    from app.core.database import SessionLocal
    from app.models import Device

    db = SessionLocal()
    try:
        d = db.get(Device, host_a)
        d.status = "online"
        db.commit()
    finally:
        db.close()

    r = client.get("/api/bus", headers=auth(admin_token))
    assert r.status_code == 200, r.text
    data = r.json()

    # 主机A：字段齐全 + 在线口径 + 两个挂载对象
    ha = _find_host(data, HOST_A_IP, "server_linux")
    assert ha is not None, "宿主机A未出现在总线"
    for key in ("id", "name", "type", "ip", "status", "monitor_enabled", "online",
                "alerts", "alert_total", "max_severity", "objects"):
        assert key in ha, f"宿主机缺字段 {key}"
    assert ha["online"] is True and ha["status"] == "online"
    assert set(ha["alerts"]) == {"critical", "major", "warning", "info"}
    obj_ids = {o["id"] for o in ha["objects"]}
    assert obj_ids == {db_a, app_a}, "同 IP 的 database/application 应挂在宿主机下"

    # 主机B：光杆，无挂载对象也显示；无告警时最高等级为 null
    hb = _find_host(data, HOST_B_IP, "server_windows")
    assert hb is not None, "光杆宿主机B未出现在总线"
    assert hb["objects"] == []
    assert hb["alert_total"] == 0 and hb["max_severity"] is None
    assert hb["online"] is False  # 新建设备 status=unknown，不算在线

    # 孤儿 database（无同 IP 宿主机）与网络设备不出现在总线
    assert _find_host(data, ORPHAN_IP) is None
    assert _find_host(data, NET_IP) is None
    all_obj_ids = {o["id"] for h in data["hosts"] for o in h["objects"]}
    assert orphan not in all_obj_ids, "不同 IP 的 database 不应出现在任何宿主机下"
    assert net not in all_obj_ids


def test_bus_firing_counts(client, admin_token):
    """firing 计数按等级聚合、resolved 不计、最高等级取最重。"""
    host_a, db_a, app_a, host_b, _, _ = _mk_devices(client, admin_token)
    _fire(host_a, "critical")
    _fire(db_a, "warning")
    _fire(db_a, "info")
    _fire(app_a, "major", status="resolved")  # 已恢复，不计

    data = client.get("/api/bus", headers=auth(admin_token)).json()
    ha = _find_host(data, HOST_A_IP)
    assert ha["alerts"] == {"critical": 1, "major": 0, "warning": 0, "info": 0}
    assert ha["alert_total"] == 1 and ha["max_severity"] == "critical"

    objs = {o["id"]: o for o in ha["objects"]}
    assert objs[db_a]["alerts"] == {"critical": 0, "major": 0, "warning": 1, "info": 1}
    assert objs[db_a]["alert_total"] == 2 and objs[db_a]["max_severity"] == "warning"
    assert objs[app_a]["alert_total"] == 0 and objs[app_a]["max_severity"] is None

    # 无告警的宿主机最高等级仍为 null
    hb = _find_host(data, HOST_B_IP)
    assert hb["alert_total"] == 0 and hb["max_severity"] is None


def test_bus_viewer_can_access(client, viewer_token):
    """任意登录用户（含只读 viewer）可访问总线视图。"""
    r = client.get("/api/bus", headers=auth(viewer_token))
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["hosts"], list)


def test_bus_requires_login(client):
    """未登录不可访问。"""
    assert client.get("/api/bus").status_code == 401
