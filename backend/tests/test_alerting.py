"""告警引擎状态机测试：触发（去抖）、不重复、恢复关闭、选择器/labels 匹配。"""
import asyncio

from app.alerting import engine
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
    """确保测试设备存在，返回 id。"""
    r = client.post(
        "/api/devices",
        json={"ip": "198.51.100.9", "name": "告警测试机", "type": "network"},
        headers=auth(admin_token),
    )
    if r.status_code == 409:
        r = client.get("/api/devices?keyword=198.51.100.9", headers=auth(admin_token))
        return r.json()[0]["id"]
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _make_rule(**kw):
    fields = dict(
        name="CPU 过高", metric="cpu_usage", op=">", threshold=80,
        duration_cycles=2, severity="critical",
    )
    fields.update(kw)
    db = SessionLocal()
    rule = AlertRule(**fields)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    db.close()
    return rule


def _open_events(rule_id):
    db = SessionLocal()
    rows = db.query(AlertEvent).filter(AlertEvent.rule_id == rule_id).all()
    db.close()
    return rows


def test_debounce_and_fire(client, admin_token):
    _clean()
    did = _device_id(client, admin_token)
    rule = _make_rule()
    # 第 1 次越限：去抖未达 2 周期，不触发
    run(engine.evaluate_points([MetricPoint(did, "cpu_usage", 90.0)]))
    assert _open_events(rule.id) == []
    # 第 2 次：触发
    run(engine.evaluate_points([MetricPoint(did, "cpu_usage", 95.0)]))
    events = _open_events(rule.id)
    assert len(events) == 1
    assert events[0].status == "firing"
    assert events[0].severity == "critical"
    # 持续越限：不重复开事件
    run(engine.evaluate_points([MetricPoint(did, "cpu_usage", 99.0)]))
    assert len(_open_events(rule.id)) == 1


def test_resolve_on_recovery(client, admin_token):
    _clean()
    did = _device_id(client, admin_token)
    rule = _make_rule()
    for v in (90, 95):
        run(engine.evaluate_points([MetricPoint(did, "cpu_usage", v)]))
    assert len(_open_events(rule.id)) == 1
    run(engine.evaluate_points([MetricPoint(did, "cpu_usage", 50.0)]))
    events = _open_events(rule.id)
    assert events[0].status == "resolved"
    assert events[0].resolved_at is not None


def test_normal_then_breach_resets_count(client, admin_token):
    _clean()
    did = _device_id(client, admin_token)
    rule = _make_rule()
    run(engine.evaluate_points([MetricPoint(did, "cpu_usage", 90)]))   # 越限 1 次
    run(engine.evaluate_points([MetricPoint(did, "cpu_usage", 10)]))   # 恢复，计数清零
    run(engine.evaluate_points([MetricPoint(did, "cpu_usage", 90)]))   # 重新计 1 次
    assert _open_events(rule.id) == []


def test_selector_and_labels_filter(client, admin_token):
    _clean()
    did = _device_id(client, admin_token)
    # 设备选择器不匹配（device_id=999 不存在）
    rule = _make_rule(name="接口利用率高", metric="if_in_util",
                      device_id=999, labels_filter={"if": "GE0/0/1"})
    for _ in range(2):
        run(engine.evaluate_points([MetricPoint(did, "if_in_util", 95, {"if": "GE0/0/1"})]))
    assert _open_events(rule.id) == []
    # labels 不匹配
    rule2 = _make_rule(name="接口利用率高2", metric="if_in_util", labels_filter={"if": "GE0/0/1"})
    for _ in range(2):
        run(engine.evaluate_points([MetricPoint(did, "if_in_util", 95, {"if": "GE0/0/2"})]))
    assert _open_events(rule2.id) == []
    # labels 匹配 → 触发
    for _ in range(2):
        run(engine.evaluate_points([MetricPoint(did, "if_in_util", 95, {"if": "GE0/0/1"})]))
    assert len(_open_events(rule2.id)) == 1


def test_disabled_rule_ignored(client, admin_token):
    _clean()
    did = _device_id(client, admin_token)
    rule = _make_rule(enabled=False)
    for _ in range(2):
        run(engine.evaluate_points([MetricPoint(did, "cpu_usage", 99)]))
    assert _open_events(rule.id) == []


def test_batch_same_key_fires_once(client, admin_token):
    """同批两个相同 (规则,设备,labels) 的点只触发一次（预加载字典去重）。"""
    _clean()
    did = _device_id(client, admin_token)
    rule = _make_rule(duration_cycles=1)
    run(engine.evaluate_points([
        MetricPoint(did, "cpu_usage", 90.0),
        MetricPoint(did, "cpu_usage", 95.0),
    ]))
    assert len(_open_events(rule.id)) == 1


def test_batch_equals_point_by_point(client, admin_token):
    """批量评估与逐点评估结果一致（触发/恢复/去抖中混合）。"""
    batch = [
        MetricPoint(0, "cpu_usage", 90.0),  # 越限 1/2（去抖中）
        MetricPoint(0, "cpu_usage", 95.0),  # 越限 2/2 → 触发
        MetricPoint(0, "cpu_usage", 10.0),  # 恢复 → 关闭
        MetricPoint(0, "cpu_usage", 99.0),  # 重新越限 1/2（去抖中，不再触发）
    ]

    def scenario(evaluate):
        _clean()
        did = _device_id(client, admin_token)
        rule = _make_rule()
        points = [MetricPoint(did, p.metric, p.value) for p in batch]
        evaluate(points)
        return _open_events(rule.id)

    # 一批一次性评估
    batch_events = scenario(lambda pts: run(engine.evaluate_points(pts)))
    assert len(batch_events) == 1
    assert batch_events[0].status == "resolved"  # 同批内触发后又恢复
    assert batch_events[0].resolved_at is not None
    # 逐点各调一次评估，结果应一致
    serial_events = scenario(lambda pts: [run(engine.evaluate_points([p])) for p in pts])
    assert len(serial_events) == 1
    assert serial_events[0].status == batch_events[0].status
    assert serial_events[0].value == batch_events[0].value


def test_run_check_once_evaluates_engine_once(client, admin_token, monkeypatch):
    """monitor_loop 一轮多台设备只调一次 evaluate_points（第 8 期批量路径）。"""
    from app.scheduler import monitor_loop

    did = _device_id(client, admin_token)
    calls = []

    async def fake_probe(device_id, snap):
        return device_id, "online", 5, {}

    async def fake_eval(points):
        calls.append(list(points))

    monkeypatch.setattr(monitor_loop, "_probe_device", fake_probe)
    monkeypatch.setattr(engine, "evaluate_points", fake_eval)
    run(monitor_loop.run_check_once())
    assert len(calls) == 1
    assert did in {p.device_id for p in calls[0]}


# ---- 接口 down 降噪：只告「历史上曾 up 过」的口 ----


def _ifdown_rule(**kw):
    fields = dict(
        name="接口 down", metric="if_status", op="==", threshold=0,
        duration_cycles=2, severity="major",
    )
    fields.update(kw)
    return _make_rule(**fields)


def _insert_if_metric(device_id, value, if_name):
    """直接写一行 if_status 时序点，模拟采集历史。"""
    from app.models import Metric

    db = SessionLocal()
    db.add(Metric(device_id=device_id, metric="if_status",
                  labels={"if": if_name}, value=float(value)))
    db.commit()
    db.close()


def test_ifdown_fires_when_interface_was_up(client, admin_token):
    """接口曾有 up 历史 → down 正常触发。"""
    _clean()
    did = _device_id(client, admin_token)
    _insert_if_metric(did, 1, "XGE-UP-HIST")
    rule = _ifdown_rule()
    for _ in range(2):
        run(engine.evaluate_points(
            [MetricPoint(did, "if_status", 0.0, {"if": "XGE-UP-HIST"})]))
    events = _open_events(rule.id)
    assert len(events) == 1
    assert events[0].status == "firing"


def test_ifdown_suppressed_when_never_up(client, admin_token):
    """从未 up 过的口 → 不触发，去抖计数也不累计。"""
    _clean()
    did = _device_id(client, admin_token)
    # 只有 down（value=0）历史，没有 up 记录
    _insert_if_metric(did, 0, "XGE-NEVER-UP")
    rule = _ifdown_rule()
    for _ in range(3):
        run(engine.evaluate_points(
            [MetricPoint(did, "if_status", 0.0, {"if": "XGE-NEVER-UP"})]))
    assert _open_events(rule.id) == []
    assert not any(k[0] == rule.id for k in engine._breach_counts)


def test_ifdown_fires_when_history_query_fails(client, admin_token, monkeypatch):
    """曾 up 历史查询异常 → 静默放行（宁可误报不丢报），down 照常触发。"""
    _clean()
    did = _device_id(client, admin_token)

    class _Boom:
        # 属性访问即抛异常，模拟 metrics 查询失败（异常在引擎内被吞并放行）
        def __getattr__(self, name):
            raise RuntimeError("DB 抖动")

    monkeypatch.setattr(engine, "Metric", _Boom())
    rule = _ifdown_rule()
    for _ in range(2):
        run(engine.evaluate_points(
            [MetricPoint(did, "if_status", 0.0, {"if": "XGE-QUERY-FAIL"})]))
    assert len(_open_events(rule.id)) == 1
