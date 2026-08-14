"""动态基线告警（baseline_dev）测试：
基线计算（7 天同时段窗口均值/Nσ 偏离）、样本不足不触发、标准差为 0 边界、
批内缓存（整批只查一次）、查询异常静默、规则 CRUD 校验。
默认走 SQLite 路径（原始表 Python 聚合）；PG 回归覆盖 SQL 聚合分支。
"""
import asyncio
from datetime import timedelta

from app.alerting import baseline, engine
from app.collectors.snmp_metrics import MetricPoint
from app.core.database import SessionLocal
from app.models import AlertEvent, AlertRule, Metric
from app.models.metric import utcnow
from conftest import auth

# 测试 IP 段分配：192.0.2.7x 归告警增强（第 8 期 M1/M2）
IP = "192.0.2.71"
METRIC = "bl_cpu"  # 专用指标名，避开内置规则（内置规则不匹配它，无噪音）


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
        json={"ip": IP, "name": "基线测试机", "type": "other"},
        headers=auth(admin_token),
    )
    if r.status_code == 409:
        r = client.get(f"/api/devices?keyword={IP}", headers=auth(admin_token))
        return r.json()[0]["id"]
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _make_rule(**kw):
    fields = dict(
        name="基线偏离", metric=METRIC, op="baseline_dev", threshold=3,
        duration_cycles=1, severity="major",
    )
    fields.update(kw)
    db = SessionLocal()
    rule = AlertRule(**fields)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    db.close()
    return rule


def _seed_baseline(device_id, day_values):
    """在近 7 天每天的同时段窗口（当前时刻 ±30min）内播种样本点。
    day_values：每天窗口内播种的值列表（窗口内错开 ±20 分钟）。
    """
    now = utcnow()
    db = SessionLocal()
    for d in range(1, baseline.WINDOW_DAYS + 1):
        center = now - timedelta(days=d)
        for i, v in enumerate(day_values):
            t = center + timedelta(minutes=-20 + 20 * i)
            db.add(Metric(device_id=device_id, metric=METRIC, value=float(v), time=t))
    db.commit()
    db.close()


def _events(rule_id):
    db = SessionLocal()
    rows = db.query(AlertEvent).filter(AlertEvent.rule_id == rule_id).all()
    db.close()
    return rows


def test_normal_value_not_trigger(client, admin_token):
    """当前值贴近基线均值（|偏离| < Nσ）→ 不触发。"""
    _clean()
    did = _device_id(client, admin_token)
    _seed_baseline(did, [50, 52, 48])  # 21 个样本，均值 50、标准差 2
    rule = _make_rule()
    run(engine.evaluate_points([MetricPoint(did, METRIC, 51.0)]))
    assert _events(rule.id) == []


def test_anomaly_triggers(client, admin_token):
    """偏离超 N 倍标准差 → 触发（上下偏离都告）。"""
    _clean()
    did = _device_id(client, admin_token)
    _seed_baseline(did, [50, 52, 48])  # 均值 50、σ=2、3σ=6
    rule = _make_rule()
    run(engine.evaluate_points([MetricPoint(did, METRIC, 60.0)]))  # 偏高 10 > 6
    assert len(_events(rule.id)) == 1
    # 恢复后向下偏离同样触发（去抖计数已被恢复清零）
    run(engine.evaluate_points([MetricPoint(did, METRIC, 50.0)]))
    run(engine.evaluate_points([MetricPoint(did, METRIC, 40.0)]))  # 偏低 10 > 6
    assert len(_events(rule.id)) == 2


def test_insufficient_samples_not_trigger(client, admin_token):
    """有效样本 <10（新设备/新指标）→ 偏离再大也不触发。"""
    _clean()
    did = _device_id(client, admin_token)
    _seed_baseline(did, [50])  # 每天 1 个共 7 个样本 < MIN_SAMPLES
    rule = _make_rule()
    run(engine.evaluate_points([MetricPoint(did, METRIC, 999.0)]))
    assert _events(rule.id) == []
    # 去抖计数也不应累计（不计越限）
    assert not any(k[0] == rule.id for k in engine._breach_counts)


def test_zero_stddev_boundary(client, admin_token):
    """标准差为 0：值等于均值不触发，任何非零偏离都触发。"""
    _clean()
    did = _device_id(client, admin_token)
    _seed_baseline(did, [50, 50, 50])  # 21 个样本，均值 50、σ=0
    rule = _make_rule()
    run(engine.evaluate_points([MetricPoint(did, METRIC, 50.0)]))
    assert _events(rule.id) == []
    run(engine.evaluate_points([MetricPoint(did, METRIC, 50.5)]))
    assert len(_events(rule.id)) == 1


def test_batch_loads_baseline_once(client, admin_token, monkeypatch):
    """批内缓存：一批多个同 (设备,指标) 的点 + 多条基线规则，整批只查一次基线。"""
    _clean()
    did = _device_id(client, admin_token)
    _seed_baseline(did, [50, 52, 48])
    _make_rule(name="基线偏离A")
    _make_rule(name="基线偏离B", threshold=5)
    calls = []
    original = baseline.load_baselines

    def spy(pairs, now):
        calls.append(set(pairs))
        return original(pairs, now)

    monkeypatch.setattr(baseline, "load_baselines", spy)
    points = [MetricPoint(did, METRIC, 60.0) for _ in range(5)]
    run(engine.evaluate_points(points))
    assert len(calls) == 1
    assert calls[0] == {(did, METRIC)}


def test_baseline_query_failure_silent(client, admin_token, monkeypatch):
    """基线查询整体异常 → 静默跳过（baseline 规则不触发），同批普通规则照常评估。"""
    _clean()
    did = _device_id(client, admin_token)
    _seed_baseline(did, [50, 52, 48])
    bl_rule = _make_rule()
    normal_rule = _make_rule(name="普通阈值", op=">", threshold=80)

    def boom(pairs, now):
        raise RuntimeError("DB 抖动")

    monkeypatch.setattr(baseline, "load_baselines", boom)
    run(engine.evaluate_points([MetricPoint(did, METRIC, 999.0)]))
    assert _events(bl_rule.id) == []            # 基线规则不误报
    assert len(_events(normal_rule.id)) == 1    # 普通规则不受影响


def test_no_history_not_trigger(client, admin_token):
    """完全没有历史数据 → 不触发（查询不到基线样本）。"""
    _clean()
    did = _device_id(client, admin_token)
    rule = _make_rule()
    run(engine.evaluate_points([MetricPoint(did, METRIC, 999.0)]))
    assert _events(rule.id) == []


# ---- 规则 CRUD 校验 ----


def test_rule_crud_baseline_op(client, admin_token):
    body = {
        "name": "CPU 基线偏离", "metric": "cpu_usage", "op": "baseline_dev",
        "threshold": 3, "duration_cycles": 2, "severity": "warning",
    }
    r = client.post("/api/alert/rules", json=body, headers=auth(admin_token))
    assert r.status_code == 201, r.text
    assert r.json()["op"] == "baseline_dev"
    rule_id = r.json()["id"]
    r = client.put(f"/api/alert/rules/{rule_id}", json={**body, "threshold": 2.5},
                   headers=auth(admin_token))
    assert r.status_code == 200, r.text
    assert r.json()["threshold"] == 2.5
    assert client.delete(f"/api/alert/rules/{rule_id}",
                         headers=auth(admin_token)).status_code == 200


def test_rule_baseline_threshold_must_be_positive(client, admin_token):
    body = {
        "name": "非法基线", "metric": "cpu_usage", "op": "baseline_dev",
        "threshold": 0, "severity": "warning",
    }
    assert client.post("/api/alert/rules", json=body,
                       headers=auth(admin_token)).status_code == 422
    assert client.post("/api/alert/rules", json={**body, "threshold": -1},
                       headers=auth(admin_token)).status_code == 422


def test_rule_invalid_op_rejected(client, admin_token):
    body = {
        "name": "非法比较符", "metric": "cpu_usage", "op": "approx",
        "threshold": 1, "severity": "warning",
    }
    assert client.post("/api/alert/rules", json=body,
                       headers=auth(admin_token)).status_code == 422
