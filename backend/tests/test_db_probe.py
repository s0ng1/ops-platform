"""MySQL / PostgreSQL 探针测试：注入假状态字典，验证指标点生成与速率计算。"""
import asyncio

import pytest

from app.collectors.db_probe import (
    collect_db_metrics,
    collect_mysql_metrics,
    collect_postgres_metrics,
)
from app.collectors.rate import RateCalculator


class FakeDevice:
    id = 11
    ip = "192.0.2.70"


def run(coro):
    return asyncio.run(coro)


STATUS_1 = {
    "Threads_connected": "12",
    "Threads_running": "2",
    "max_connections": "151",
    "Queries": "100000",
    "Com_commit": "3000",
    "Com_rollback": "100",
    "Slow_queries": "5",
    "replication_delay": "0",
}


def fake_query(status):
    def query(host, payload):
        return dict(status)

    return query


def test_first_cycle_instant_metrics_only():
    rc = RateCalculator()
    points = run(collect_mysql_metrics(FakeDevice, {}, rc, query=fake_query(STATUS_1)))
    by_metric = {p.metric: p.value for p in points}
    assert by_metric["mysql_threads_connected"] == 12.0
    assert by_metric["mysql_max_connections"] == 151.0
    assert by_metric["mysql_replication_delay"] == 0.0
    # 首周期无速率值
    assert "mysql_qps" not in by_metric


def test_second_cycle_rates():
    rc = RateCalculator()
    run(collect_mysql_metrics(FakeDevice, {}, rc, query=fake_query(STATUS_1)))
    for key, (ts, raw) in list(rc._last.items()):
        rc._last[key] = (ts - 60, raw)
    status2 = dict(STATUS_1, Queries="106000", Com_commit="3300", Slow_queries="8")
    points = run(collect_mysql_metrics(FakeDevice, {}, rc, query=fake_query(status2)))
    by_metric = {p.metric: p.value for p in points}
    assert by_metric["mysql_qps"] == pytest.approx(100.0, rel=0.01)   # 6000/60s
    assert by_metric["mysql_tps"] == pytest.approx(5.0, rel=0.01)     # 300/60s
    assert by_metric["mysql_slow_qps"] == pytest.approx(0.05, rel=0.05)  # 3/60s


def test_connection_failure_returns_empty():
    def bad_query(host, payload):
        raise ConnectionError("access denied")

    points = run(collect_mysql_metrics(FakeDevice, {}, RateCalculator(), query=bad_query))
    assert points == []


def test_dispatch_unknown_db_type_skips():
    points = run(collect_db_metrics(FakeDevice, {"db_type": "db2"}, RateCalculator()))
    assert points == []


# ---------------- PostgreSQL ----------------

PG_STATUS = {
    "connections": 34,
    "max_connections": 200,
    "blks_hit": 90000,
    "blks_read": 10000,
}


def test_postgres_metrics():
    """PostgreSQL：连接数/上限/使用率/缓存命中率 + 基础指标。"""
    points = run(collect_postgres_metrics(FakeDevice, {}, RateCalculator(), query=fake_query(PG_STATUS)))
    by_metric = {p.metric: p.value for p in points}
    assert by_metric["db_available"] == 1.0
    assert "db_latency" in by_metric
    assert by_metric["pg_connections"] == 34.0
    assert by_metric["pg_max_connections"] == 200.0
    assert by_metric["pg_conn_usage_pct"] == pytest.approx(17.0)
    assert by_metric["pg_cache_hit_ratio"] == pytest.approx(90.0)


def test_postgres_connection_failure_available_zero():
    """PostgreSQL 连接失败：静默，只出 db_available=0。"""
    def bad_query(host, payload):
        raise ConnectionError("connection refused")

    points = run(collect_postgres_metrics(FakeDevice, {}, RateCalculator(), query=bad_query))
    assert len(points) == 1
    assert points[0].metric == "db_available"
    assert points[0].value == 0.0


def test_postgres_cache_hit_ratio_skipped_when_no_reads():
    """无读块（total=0）时不出缓存命中率，避免除零。"""
    status = dict(PG_STATUS, blks_hit=0, blks_read=0)
    points = run(collect_postgres_metrics(FakeDevice, {}, RateCalculator(), query=fake_query(status)))
    by_metric = {p.metric for p in points}
    assert "pg_cache_hit_ratio" not in by_metric


def test_dispatch_postgresql():
    """db_type 分发：postgresql 走 PostgreSQL 分支。"""
    points = run(collect_db_metrics(FakeDevice, {"db_type": "postgresql"}, RateCalculator(), query=fake_query(PG_STATUS)))
    assert any(p.metric == "pg_connections" for p in points)
