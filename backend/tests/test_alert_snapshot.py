"""告警时刻指标快照测试：_fire 抓设备最新指标全集存事件、失败静默存 None、
详情 API 带出（列表不带）、恢复不抓快照。
"""
import asyncio

from app.alerting import engine
from app.collectors.snmp_metrics import MetricPoint
from app.core.database import SessionLocal
from app.models import AlertEvent, AlertRule, Metric
from conftest import auth

# 测试 IP 段分配：192.0.2.7x 归告警增强（第 8 期 M1/M2）
IP = "192.0.2.72"


def run(coro):
    return asyncio.run(coro)


def _clean():
    engine.reset_counters()
    db = SessionLocal()
    db.query(AlertEvent).delete()
    db.query(AlertRule).delete()
    db.query(Metric).delete()
    db.commit()
    db.close()


def _device_id(client, admin_token):
    r = client.post(
        "/api/devices",
        json={"ip": IP, "name": "快照测试机", "type": "other"},
        headers=auth(admin_token),
    )
    if r.status_code == 409:
        r = client.get(f"/api/devices?keyword={IP}", headers=auth(admin_token))
        return r.json()[0]["id"]
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _make_rule(**kw):
    fields = dict(
        name="快照测试规则", metric="snap_cpu", op=">", threshold=80,
        duration_cycles=1, severity="critical",
    )
    fields.update(kw)
    db = SessionLocal()
    rule = AlertRule(**fields)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    db.close()
    return rule


def _seed_metrics(device_id):
    """播种设备当前指标：snap_cpu 一旧一新两条（验证取最新）、mem_usage 一条、带 labels 一条。"""
    from datetime import timedelta

    from app.models.metric import utcnow

    now = utcnow()
    db = SessionLocal()
    db.add(Metric(device_id=device_id, metric="snap_cpu", value=10.0,
                  time=now - timedelta(minutes=10)))
    db.add(Metric(device_id=device_id, metric="snap_cpu", value=20.0, time=now))
    db.add(Metric(device_id=device_id, metric="snap_mem", value=66.0, time=now))
    db.add(Metric(device_id=device_id, metric="snap_if", labels={"if": "GE0/0/1"},
                  value=1.0, time=now))
    db.commit()
    db.close()


def _events(rule_id):
    db = SessionLocal()
    rows = db.query(AlertEvent).filter(AlertEvent.rule_id == rule_id).all()
    db.close()
    return rows


def test_fire_captures_snapshot(client, admin_token):
    """触发事件后 snapshot 含该设备每 metric+labels 最新一条。"""
    _clean()
    did = _device_id(client, admin_token)
    _seed_metrics(did)
    rule = _make_rule()
    run(engine.evaluate_points([MetricPoint(did, "snap_cpu", 90.0)]))
    events = _events(rule.id)
    assert len(events) == 1
    snap = events[0].snapshot
    assert snap and snap["items"]
    items = {(i["metric"], tuple(sorted(i["labels"].items()))): i for i in snap["items"]}
    assert items[("snap_cpu", ())]["value"] == 20.0  # 取最新一条而非旧的 10.0
    assert items[("snap_mem", ())]["value"] == 66.0
    assert items[("snap_if", (("if", "GE0/0/1"),))]["value"] == 1.0


def test_snapshot_failure_silent(client, admin_token, monkeypatch):
    """快照查询异常 → 事件照常触发，snapshot 存 None。"""
    _clean()
    did = _device_id(client, admin_token)
    rule = _make_rule()

    def boom(device_id):
        raise RuntimeError("DB 抖动")

    monkeypatch.setattr(engine, "_capture_snapshot", boom)
    run(engine.evaluate_points([MetricPoint(did, "snap_cpu", 90.0)]))
    events = _events(rule.id)
    assert len(events) == 1
    assert events[0].status == "firing"
    assert events[0].snapshot is None


def test_snapshot_once_per_device_per_batch(client, admin_token, monkeypatch):
    """同批同设备多条规则触发，快照只抓一次。"""
    _clean()
    did = _device_id(client, admin_token)
    _seed_metrics(did)
    rule1 = _make_rule(name="快照规则A")
    rule2 = _make_rule(name="快照规则B", metric="snap_mem", threshold=50)
    calls = []
    original = engine._capture_snapshot

    def spy(device_id):
        calls.append(device_id)
        return original(device_id)

    monkeypatch.setattr(engine, "_capture_snapshot", spy)
    run(engine.evaluate_points([
        MetricPoint(did, "snap_cpu", 90.0),
        MetricPoint(did, "snap_mem", 88.0),
    ]))
    assert calls == [did]
    assert len(_events(rule1.id)) == 1
    assert len(_events(rule2.id)) == 1


def test_resolve_does_not_capture(client, admin_token, monkeypatch):
    """恢复（_resolve）不抓快照：触发抓一次，恢复时调用次数不增加。"""
    _clean()
    did = _device_id(client, admin_token)
    _seed_metrics(did)
    rule = _make_rule()
    calls = []
    original = engine._capture_snapshot

    def spy(device_id):
        calls.append(device_id)
        return original(device_id)

    monkeypatch.setattr(engine, "_capture_snapshot", spy)
    run(engine.evaluate_points([MetricPoint(did, "snap_cpu", 90.0)]))
    assert len(calls) == 1
    run(engine.evaluate_points([MetricPoint(did, "snap_cpu", 10.0)]))  # 恢复
    assert len(calls) == 1
    events = _events(rule.id)
    assert events[0].status == "resolved"
    assert events[0].snapshot and events[0].snapshot["items"]  # 触发时的快照保留


def test_detail_api_returns_snapshot(client, admin_token):
    """详情接口带 snapshot；列表接口不带（避免大 payload）。"""
    _clean()
    did = _device_id(client, admin_token)
    _seed_metrics(did)
    rule = _make_rule()
    run(engine.evaluate_points([MetricPoint(did, "snap_cpu", 90.0)]))
    event_id = _events(rule.id)[0].id

    r = client.get("/api/alert/events", headers=auth(admin_token))
    assert r.status_code == 200
    item = [e for e in r.json() if e["id"] == event_id][0]
    assert "snapshot" not in item

    r = client.get(f"/api/alert/events/{event_id}", headers=auth(admin_token))
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["device_name"] == "快照测试机"
    metrics = {i["metric"] for i in detail["snapshot"]["items"]}
    assert {"snap_cpu", "snap_mem", "snap_if"} <= metrics


def test_detail_api_404(client, admin_token):
    assert client.get("/api/alert/events/999999",
                      headers=auth(admin_token)).status_code == 404
