"""Oracle / SQLServer 探针测试：注入假查询结果，验证指标解析、速率差值、
连接失败静默出 db_available=0、调度谓词与内置规则补种。"""
import asyncio
from types import SimpleNamespace

import pytest

from app.collectors.db_probe import (
    collect_db_metrics,
    collect_mssql_metrics,
    collect_oracle_metrics,
)
from app.collectors.rate import RateCalculator
from app.core.database import SessionLocal
from app.main import _init_db
from app.models import AlertRule
from app.scheduler.scheduler import TASKS


class FakeDevice:
    id = 12
    ip = "203.0.113.70"


def run(coro):
    return asyncio.run(coro)


def fake_query(status):
    def query(host, payload):
        return dict(status)

    return query


ORACLE_STATUS = {
    "sessions": 85,
    "sessions_limit": 500,
    "active_sessions": 7,
    "tablespaces": [("SYSTEM", 62.5), ("USERS", 88.31)],
}


def test_oracle_metrics():
    """Oracle：会话数/活动会话/会话使用率/表空间（带 labels）+ 基础指标。"""
    points = run(collect_oracle_metrics(FakeDevice, {}, RateCalculator(), query=fake_query(ORACLE_STATUS)))
    by_metric = {}
    for p in points:
        by_metric.setdefault(p.metric, []).append(p)
    assert by_metric["db_available"][0].value == 1.0
    assert "db_latency" in by_metric
    assert by_metric["oracle_sessions"][0].value == 85.0
    assert by_metric["oracle_active_sessions"][0].value == 7.0
    assert by_metric["oracle_sessions_usage_pct"][0].value == pytest.approx(17.0)
    ts_points = by_metric["oracle_tablespace_usage_pct"]
    by_ts = {p.labels["tablespace"]: p.value for p in ts_points}
    assert by_ts == {"SYSTEM": 62.5, "USERS": 88.31}


def test_oracle_connection_failure_available_zero():
    """Oracle 连接失败：静默，只出 db_available=0。"""
    def bad_query(host, payload):
        raise ConnectionError("ORA-01017")

    points = run(collect_oracle_metrics(FakeDevice, {}, RateCalculator(), query=bad_query))
    assert len(points) == 1
    assert points[0].metric == "db_available"
    assert points[0].value == 0.0


MSSQL_STATUS = {
    "connections": 23,
    "bchr": 4523,
    "bchr_base": 4600,
    "batch_requests": 100000,
}


def test_mssql_buffer_hit_ratio_divided_by_base():
    """SQLServer：Buffer cache hit ratio 须除以 base 行。"""
    points = run(collect_mssql_metrics(FakeDevice, {}, RateCalculator(), query=fake_query(MSSQL_STATUS)))
    by_metric = {p.metric: p.value for p in points}
    assert by_metric["db_available"] == 1.0
    assert by_metric["mssql_connections"] == 23.0
    assert by_metric["mssql_buffer_cache_hit_pct"] == pytest.approx(4523 / 4600 * 100, rel=0.001)
    # 首周期无速率值
    assert "mssql_batch_per_sec" not in by_metric


def test_mssql_batch_rate_second_cycle():
    """SQLServer：Batch Requests/sec 累计计数器跨周期差值。"""
    rc = RateCalculator()
    run(collect_mssql_metrics(FakeDevice, {}, rc, query=fake_query(MSSQL_STATUS)))
    for key, (ts, raw) in list(rc._last.items()):
        rc._last[key] = (ts - 60, raw)
    status2 = dict(MSSQL_STATUS, batch_requests=103000)
    points = run(collect_mssql_metrics(FakeDevice, {}, rc, query=fake_query(status2)))
    by_metric = {p.metric: p.value for p in points}
    assert by_metric["mssql_batch_per_sec"] == pytest.approx(50.0, rel=0.01)  # 3000/60s


def test_mssql_connection_failure_available_zero():
    """SQLServer 连接失败：静默，只出 db_available=0。"""
    def bad_query(host, payload):
        raise ConnectionError("Login failed")

    points = run(collect_mssql_metrics(FakeDevice, {}, RateCalculator(), query=bad_query))
    assert len(points) == 1
    assert points[0].metric == "db_available"
    assert points[0].value == 0.0


def test_dispatch_oracle_and_sqlserver():
    """db_type 分发：oracle / sqlserver 各走各的分支。"""
    oracle_points = run(collect_db_metrics(
        FakeDevice, {"db_type": "oracle"}, RateCalculator(), query=fake_query(ORACLE_STATUS)))
    assert any(p.metric == "oracle_sessions" for p in oracle_points)
    mssql_points = run(collect_db_metrics(
        FakeDevice, {"db_type": "sqlserver"}, RateCalculator(), query=fake_query(MSSQL_STATUS)))
    assert any(p.metric == "mssql_connections" for p in mssql_points)


def test_db_probe_task_scope_covers_new_db_types():
    """调度谓词：database 设备 + database 凭据即命中 db_probe 任务（不看 payload.db_type）。"""
    task = next(t for t in TASKS if t.name == "db_probe")
    for db_type in ("mysql", "oracle", "sqlserver", "postgresql"):
        cred = SimpleNamespace(kind="database", get_payload=lambda dt=db_type: {"db_type": dt})
        device = SimpleNamespace(monitor_enabled=True, type="database", credential=cred)
        assert task.applies_to(device), f"db_type={db_type} 未命中 db_probe 谓词"
    # 无凭据 / 类型不符不命中
    assert not task.applies_to(SimpleNamespace(monitor_enabled=True, type="database", credential=None))
    assert not task.applies_to(SimpleNamespace(
        monitor_enabled=True, type="server_linux",
        credential=SimpleNamespace(kind="database", get_payload=lambda: {})))


def test_builtin_rules_seeded(client):
    """清空后播种：Oracle 表空间两条分级 + SQLServer 缓存命中率规则字段正确。"""
    db = SessionLocal()
    db.query(AlertRule).delete()
    db.commit()
    db.close()
    _init_db()
    db = SessionLocal()
    by_name = {r.name: r for r in db.query(AlertRule).all()}
    db.close()
    expected = [
        ("Oracle 表空间使用率过高", "oracle_tablespace_usage_pct", ">", 85, 3, "warning"),
        ("Oracle 表空间使用率临界", "oracle_tablespace_usage_pct", ">", 95, 2, "major"),
        ("SQLServer 缓存命中率过低", "mssql_buffer_cache_hit_pct", "<", 90, 3, "warning"),
        ("PostgreSQL 连接数使用率过高", "pg_conn_usage_pct", ">", 80, 3, "major"),
        ("PostgreSQL 缓存命中率过低", "pg_cache_hit_ratio", "<", 90, 3, "warning"),
    ]
    for name, metric, op, threshold, cycles, severity in expected:
        r = by_name.get(name)
        assert r is not None, f"缺少内置规则 {name}"
        assert (r.metric, r.op, r.threshold, r.duration_cycles, r.severity, r.device_type) == \
            (metric, op, threshold, cycles, severity, "database"), f"规则字段不符 {name}"
        assert r.builtin is True
