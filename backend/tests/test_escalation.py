"""告警升级测试：超时未 ack 升一级并重发通知、critical 封顶、只升一次、0 不升级。"""
import asyncio
from datetime import datetime, timedelta

import pytest

from app.alerting import engine, escalation, notify
from app.collectors.snmp_metrics import MetricPoint
from app.core.database import SessionLocal
from app.models import AlertEvent, AlertRule
from conftest import auth


def run(coro):
    async def go():
        result = await coro
        await notify.wait_pending()  # 通知已解耦为后台 task，断言前等其发完
        return result

    return asyncio.run(go())


@pytest.fixture()
def sent(monkeypatch):
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
    db.commit()
    db.close()


def _device_id(client, admin_token):
    r = client.post(
        "/api/devices",
        json={"ip": "198.51.100.30", "name": "升级测试机", "type": "server_linux"},
        headers=auth(admin_token),
    )
    if r.status_code == 409:
        r = client.get("/api/devices?keyword=198.51.100.30", headers=auth(admin_token))
        return r.json()[0]["id"]
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _fire_event(did, **rule_kw):
    """触发一条 firing 事件，并把 fired_at 拨到 10 分钟前。"""
    fields = dict(name="升级测试规则", metric="cpu_usage", op=">", threshold=80,
                  duration_cycles=1, severity="warning", escalate_minutes=5)
    fields.update(rule_kw)
    db = SessionLocal()
    rule = AlertRule(**fields)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    db.close()
    run(engine.evaluate_points([MetricPoint(did, "cpu_usage", 99.0)]))
    db = SessionLocal()
    ev = db.query(AlertEvent).filter(AlertEvent.rule_id == rule.id).one()
    ev.fired_at = datetime.now() - timedelta(minutes=10)
    db.commit()
    db.refresh(ev)
    db.close()
    return rule, ev


def _get_event(event_id):
    db = SessionLocal()
    ev = db.get(AlertEvent, event_id)
    db.close()
    return ev


def test_escalates_one_level_and_renotifies(client, admin_token, sent):
    _clean()
    did = _device_id(client, admin_token)
    rule, ev = _fire_event(did, severity="warning", notify=["smtp"])
    sent.clear()  # 清掉触发时的首次通知
    n = run(escalation.check_escalations())
    assert n == 1
    ev2 = _get_event(ev.id)
    assert ev2.severity == "major"  # warning → major
    assert ev2.escalated is True
    assert "升级" in ev2.note and "warning" in ev2.note
    assert len(sent) == 1
    assert sent[0][2] == ["smtp"]  # 走该规则的 notify 渠道


def test_escalate_only_once(client, admin_token, sent):
    _clean()
    did = _device_id(client, admin_token)
    _, ev = _fire_event(did, severity="warning")
    sent.clear()
    assert run(escalation.check_escalations()) == 1
    assert run(escalation.check_escalations()) == 0  # 已升过，不再升
    ev2 = _get_event(ev.id)
    assert ev2.severity == "major"
    assert len(sent) == 1


def test_critical_not_escalated(client, admin_token, sent):
    _clean()
    did = _device_id(client, admin_token)
    _, ev = _fire_event(did, severity="critical")
    sent.clear()
    assert run(escalation.check_escalations()) == 0
    ev2 = _get_event(ev.id)
    assert ev2.severity == "critical"
    assert ev2.escalated is False
    assert sent == []


def test_zero_minutes_no_escalation(client, admin_token, sent):
    _clean()
    did = _device_id(client, admin_token)
    _, ev = _fire_event(did, escalate_minutes=0)
    sent.clear()
    assert run(escalation.check_escalations()) == 0
    assert _get_event(ev.id).severity == "warning"
    assert sent == []


def test_not_overdue_no_escalation(client, admin_token, sent):
    _clean()
    did = _device_id(client, admin_token)
    # escalate_minutes=60，fired_at 仅 10 分钟前，未超时
    _, ev = _fire_event(did, escalate_minutes=60)
    sent.clear()
    assert run(escalation.check_escalations()) == 0
    assert _get_event(ev.id).escalated is False
    assert sent == []


def test_acked_event_no_escalation(client, admin_token, sent):
    _clean()
    did = _device_id(client, admin_token)
    _, ev = _fire_event(did)
    db = SessionLocal()
    e = db.get(AlertEvent, ev.id)
    e.ack_by = "admin"
    e.ack_at = datetime.now()
    db.commit()
    db.close()
    sent.clear()
    assert run(escalation.check_escalations()) == 0
    assert _get_event(ev.id).escalated is False
    assert sent == []


def test_info_escalates_to_warning(client, admin_token, sent):
    _clean()
    did = _device_id(client, admin_token)
    _, ev = _fire_event(did, severity="info")
    sent.clear()
    assert run(escalation.check_escalations()) == 1
    assert _get_event(ev.id).severity == "warning"
