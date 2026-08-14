"""通知解耦测试（第 8 期）：_fire/升级只产生事件并入队，后台发送不阻塞、失败静默。"""
import asyncio
from datetime import datetime, timedelta

from app.alerting import engine, escalation, notify
from app.collectors.snmp_metrics import MetricPoint
from app.core.database import SessionLocal
from app.models import AlertEvent, AlertRule
from conftest import auth


def run(coro):
    return asyncio.run(coro)


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
        json={"ip": "198.51.100.80", "name": "通知解耦机", "type": "network"},
        headers=auth(admin_token),
    )
    if r.status_code == 409:
        r = client.get("/api/devices?keyword=198.51.100.80", headers=auth(admin_token))
        return r.json()[0]["id"]
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _make_rule(**kw):
    fields = dict(name="解耦测试规则", metric="cpu_usage", op=">", threshold=80,
                  duration_cycles=1, severity="warning")
    fields.update(kw)
    db = SessionLocal()
    rule = AlertRule(**fields)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    db.close()
    return rule


def _events(rule_id):
    db = SessionLocal()
    rows = db.query(AlertEvent).filter(AlertEvent.rule_id == rule_id).all()
    db.close()
    return rows


def test_fire_does_not_wait_for_notify(client, admin_token, monkeypatch):
    """通知堵死（gate 不放行）时事件仍立即产生；放行后后台 task 完成发送。"""
    _clean()
    did = _device_id(client, admin_token)
    rule = _make_rule()
    gate = asyncio.Event()
    calls = []

    async def blocked_send(subject, body, channels=None):
        calls.append(subject)
        await gate.wait()

    monkeypatch.setattr(notify, "send_alert", blocked_send)

    async def go():
        await engine.evaluate_points([MetricPoint(did, "cpu_usage", 99.0)])
        # 评估已返回、事件已落库，而通知还堵在 gate（评估没有等它）
        events = _events(rule.id)
        assert len(events) == 1 and events[0].status == "firing"
        gate.set()
        await notify.wait_pending()
        assert len(calls) == 1

    run(go())


def test_notify_failure_silent(client, admin_token, monkeypatch):
    """通知发送抛异常：静默处理，事件照常 firing，评估不报错。"""
    _clean()
    did = _device_id(client, admin_token)
    rule = _make_rule()

    async def boom(subject, body, channels=None):
        raise RuntimeError("SMTP 挂了")

    monkeypatch.setattr(notify, "send_alert", boom)

    async def go():
        await engine.evaluate_points([MetricPoint(did, "cpu_usage", 99.0)])
        await notify.wait_pending()  # 不抛异常
        events = _events(rule.id)
        assert len(events) == 1 and events[0].status == "firing"

    run(go())


def test_escalation_notify_decoupled(client, admin_token, monkeypatch):
    """升级通知同样解耦：check_escalations 返回时升级已落库，通知后台补发。"""
    _clean()
    did = _device_id(client, admin_token)
    rule = _make_rule(escalate_minutes=5, notify=["smtp"])
    gate = asyncio.Event()
    calls = []

    async def blocked_send(subject, body, channels=None):
        calls.append((subject, channels))
        await gate.wait()

    monkeypatch.setattr(notify, "send_alert", blocked_send)

    async def go():
        await engine.evaluate_points([MetricPoint(did, "cpu_usage", 99.0)])
        # 首次触发通知也走后台，先放行让它发完，避免占用 calls
        gate.set()
        await notify.wait_pending()
        gate.clear()
        calls.clear()
        # fired_at 拨到 10 分钟前，满足升级条件
        db = SessionLocal()
        ev = db.query(AlertEvent).filter(AlertEvent.rule_id == rule.id).one()
        ev.fired_at = datetime.now() - timedelta(minutes=10)
        db.commit()
        event_id = ev.id
        db.close()

        n = await escalation.check_escalations()
        assert n == 1
        # 升级已落库（不等工作在 gate 后的通知）
        db = SessionLocal()
        ev2 = db.get(AlertEvent, event_id)
        db.close()
        assert ev2.severity == "major" and ev2.escalated is True
        gate.set()
        await notify.wait_pending()
        assert len(calls) == 1 and calls[0][1] == ["smtp"]

    run(go())
