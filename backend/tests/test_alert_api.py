"""告警 API 测试：规则 CRUD、事件确认/关闭、等级计数、通知渠道、内置规则播种。"""
from conftest import auth


def test_builtin_rules_seeded(client, admin_token):
    r = client.get("/api/alert/rules", headers=auth(admin_token))
    assert r.status_code == 200
    names = {rule["name"] for rule in r.json()}
    # 测试库是全新空表，应播种两条内置规则
    assert {"设备离线", "接口 down"} <= names


def test_rule_crud(client, admin_token):
    body = {
        "name": "时延过高", "metric": "ping_latency_ms", "op": ">", "threshold": 100,
        "duration_cycles": 3, "severity": "warning",
    }
    r = client.post("/api/alert/rules", json=body, headers=auth(admin_token))
    assert r.status_code == 201, r.text
    rule_id = r.json()["id"]

    r = client.put(f"/api/alert/rules/{rule_id}", json={**body, "threshold": 200},
                   headers=auth(admin_token))
    assert r.json()["threshold"] == 200.0

    assert client.delete(f"/api/alert/rules/{rule_id}", headers=auth(admin_token)).status_code == 200
    assert client.get(f"/api/alert/rules", headers=auth(admin_token)).status_code == 200


def test_viewer_cannot_create_rule(client, viewer_token):
    body = {"name": "x", "metric": "cpu_usage", "op": ">", "threshold": 1, "severity": "info"}
    assert client.post("/api/alert/rules", json=body, headers=auth(viewer_token)).status_code == 403


def test_event_ack_resolve_and_summary(client, admin_token):
    # 造一条事件（直接走引擎）
    import asyncio

    from app.alerting import engine
    from app.collectors.snmp_metrics import MetricPoint
    from app.core.database import SessionLocal
    from app.models import AlertRule

    r = client.post(
        "/api/devices",
        json={"ip": "198.51.100.10", "name": "告警API测试机", "type": "server_linux"},
        headers=auth(admin_token),
    )
    if r.status_code == 409:
        r = client.get("/api/devices?keyword=198.51.100.10", headers=auth(admin_token))
        did = r.json()[0]["id"]
    else:
        did = r.json()["id"]

    engine.reset_counters()
    db = SessionLocal()
    rule = AlertRule(name="API测试规则", metric="cpu_usage", op=">", threshold=50,
                     duration_cycles=1, severity="major")
    db.add(rule)
    db.commit()
    db.refresh(rule)
    db.close()
    asyncio.run(engine.evaluate_points([MetricPoint(did, "cpu_usage", 80)]))

    r = client.get("/api/alert/events?status=firing", headers=auth(admin_token))
    events = [e for e in r.json() if e["rule_name"] == "API测试规则"]
    assert len(events) == 1
    event = events[0]
    assert event["device_name"]

    r = client.post(f"/api/alert/events/{event['id']}/ack", headers=auth(admin_token))
    assert r.json()["ack_by"] == "admin"

    r = client.post(f"/api/alert/events/{event['id']}/resolve", headers=auth(admin_token))
    assert r.json()["status"] == "resolved"

    r = client.get("/api/alert/summary", headers=auth(admin_token))
    assert r.status_code == 200
    assert set(r.json()) == {"critical", "major", "warning", "info", "total"}


def test_notify_config_crud(client, admin_token):
    body = {
        "name": "运维群机器人", "kind": "dingtalk",
        "config": {"webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=xxx"},
    }
    r = client.post("/api/notify/configs", json=body, headers=auth(admin_token))
    assert r.status_code == 201, r.text
    cfg_id = r.json()["id"]
    # 列表不回显 url
    r = client.get("/api/notify/configs", headers=auth(admin_token))
    assert "access_token" not in r.text
    # 空 config 更新不改密钥
    r = client.put(f"/api/notify/configs/{cfg_id}",
                   json={**body, "config": {}, "enabled": False}, headers=auth(admin_token))
    assert r.json()["enabled"] is False
    assert client.delete(f"/api/notify/configs/{cfg_id}", headers=auth(admin_token)).status_code == 200
