"""告警静默窗口测试：窗口内跳过外部通知（事件照常入库/可查）、过期或选择器不匹配照常通知。"""
import asyncio
from datetime import datetime, timedelta

import pytest

from app.alerting import engine, notify
from app.collectors.snmp_metrics import MetricPoint
from app.core.database import SessionLocal
from app.models import AlertEvent, AlertRule, SilenceWindow
from conftest import auth


def run(coro):
    async def go():
        result = await coro
        await notify.wait_pending()  # 通知已解耦为后台 task，断言前等其发完
        return result

    return asyncio.run(go())


@pytest.fixture()
def sent(monkeypatch):
    """mock 外部通知，记录调用次数。"""
    calls = []

    async def fake_send(subject, body, channels=None):
        calls.append((subject, body, channels))

    monkeypatch.setattr(notify, "send_alert", fake_send)
    return calls


def _clean():
    engine.reset_counters()
    db = SessionLocal()
    db.query(AlertEvent).delete()
    db.query(AlertRule).delete()
    db.query(SilenceWindow).delete()
    db.commit()
    db.close()


def _device_id(client, admin_token):
    r = client.post(
        "/api/devices",
        json={"ip": "198.51.100.20", "name": "静默测试机", "type": "network"},
        headers=auth(admin_token),
    )
    if r.status_code == 409:
        r = client.get("/api/devices?keyword=198.51.100.20", headers=auth(admin_token))
        return r.json()[0]["id"]
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _make_rule(**kw):
    fields = dict(name="静默测试规则", metric="cpu_usage", op=">", threshold=80,
                  duration_cycles=1, severity="warning")
    fields.update(kw)
    db = SessionLocal()
    rule = AlertRule(**fields)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    db.close()
    return rule


def _make_window(**kw):
    now = datetime.now()
    fields = dict(name="维护窗口", start_at=now - timedelta(hours=1),
                  end_at=now + timedelta(hours=1), enabled=True)
    fields.update(kw)
    db = SessionLocal()
    w = SilenceWindow(**fields)
    db.add(w)
    db.commit()
    db.refresh(w)
    db.close()
    return w


def _events(rule_id):
    db = SessionLocal()
    rows = db.query(AlertEvent).filter(AlertEvent.rule_id == rule_id).all()
    db.close()
    return rows


def test_silenced_event_stored_but_no_notify(client, admin_token, sent):
    _clean()
    did = _device_id(client, admin_token)
    rule = _make_rule()
    _make_window()  # 全空选择器 = 全部设备，当前时间命中
    run(engine.evaluate_points([MetricPoint(did, "cpu_usage", 99.0)]))
    events = _events(rule.id)
    assert len(events) == 1
    assert events[0].status == "firing"
    assert events[0].silenced is True
    assert sent == []  # 外部通知被跳过
    # 事件查询响应带 silenced 字段
    r = client.get("/api/alert/events?status=firing", headers=auth(admin_token))
    item = [e for e in r.json() if e["rule_name"] == "静默测试规则"][0]
    assert item["silenced"] is True


def test_expired_window_notifies(client, admin_token, sent):
    _clean()
    did = _device_id(client, admin_token)
    rule = _make_rule()
    now = datetime.now()
    _make_window(start_at=now - timedelta(hours=2), end_at=now - timedelta(hours=1))
    run(engine.evaluate_points([MetricPoint(did, "cpu_usage", 99.0)]))
    events = _events(rule.id)
    assert len(events) == 1
    assert events[0].silenced is False
    assert len(sent) == 1


def test_disabled_window_notifies(client, admin_token, sent):
    _clean()
    did = _device_id(client, admin_token)
    rule = _make_rule()
    _make_window(enabled=False)
    run(engine.evaluate_points([MetricPoint(did, "cpu_usage", 99.0)]))
    assert _events(rule.id)[0].silenced is False
    assert len(sent) == 1


def test_selector_mismatch_notifies(client, admin_token, sent):
    _clean()
    did = _device_id(client, admin_token)  # type=network
    rule = _make_rule()
    _make_window(device_type="server_linux")  # 选择器不匹配
    run(engine.evaluate_points([MetricPoint(did, "cpu_usage", 99.0)]))
    assert _events(rule.id)[0].silenced is False
    assert len(sent) == 1


def test_selector_match_device_id_silences(client, admin_token, sent):
    _clean()
    did = _device_id(client, admin_token)
    rule = _make_rule()
    _make_window(device_id=did)
    run(engine.evaluate_points([MetricPoint(did, "cpu_usage", 99.0)]))
    assert _events(rule.id)[0].silenced is True
    assert sent == []


def test_silence_crud_api(client, admin_token, viewer_token):
    now = datetime.now()
    body = {
        "name": "周末维护",
        "start_at": now.isoformat(),
        "end_at": (now + timedelta(hours=2)).isoformat(),
    }
    # viewer 无写权限
    assert client.post("/api/alert/silences", json=body, headers=auth(viewer_token)).status_code == 403
    r = client.post("/api/alert/silences", json=body, headers=auth(admin_token))
    assert r.status_code == 201, r.text
    sid = r.json()["id"]

    r = client.get("/api/alert/silences", headers=auth(admin_token))
    assert any(w["id"] == sid for w in r.json())

    r = client.put(f"/api/alert/silences/{sid}",
                   json={**body, "name": "周末维护2", "enabled": False},
                   headers=auth(admin_token))
    assert r.json()["name"] == "周末维护2"
    assert r.json()["enabled"] is False

    # 结束早于开始 → 400
    bad = {**body, "end_at": (now - timedelta(hours=1)).isoformat()}
    assert client.post("/api/alert/silences", json=bad, headers=auth(admin_token)).status_code == 400

    assert client.delete(f"/api/alert/silences/{sid}", headers=auth(admin_token)).status_code == 200
    assert client.delete(f"/api/alert/silences/{sid}", headers=auth(admin_token)).status_code == 404
