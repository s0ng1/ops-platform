"""采集调度器与指标查询 API 测试。"""
import asyncio
from datetime import datetime, timedelta
from urllib.parse import quote

from app.collectors.snmp_metrics import MetricPoint
from app.core.database import SessionLocal
from app.models import Metric
from app.scheduler.scheduler import CollectionTask, run_task_once
from conftest import auth


def _make_device(client, token, ip):
    r = client.post(
        "/api/devices",
        json={"ip": ip, "name": "调度测试机", "type": "network"},
        headers=auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_scheduler_writes_metrics_and_isolates_failures(client, admin_token):
    dev_id = _make_device(client, admin_token, "192.0.2.100")

    async def good_collect(device, ctx):
        return [MetricPoint(device.id, "cpu_usage", 42.0)]

    async def bad_collect(device, ctx):
        raise RuntimeError("采集炸了")

    ctx = {}
    n = asyncio.run(run_task_once(CollectionTask("good", 60, good_collect, lambda d: True), ctx))
    assert n >= 1
    # 失败任务不抛异常、不影响其他
    n2 = asyncio.run(run_task_once(CollectionTask("bad", 60, bad_collect, lambda d: True), ctx))
    assert n2 == 0

    r = client.get(
        f"/api/devices/{dev_id}/metrics?metric=cpu_usage", headers=auth(admin_token)
    )
    assert r.status_code == 200
    assert r.json()["points"][0]["value"] == 42.0

    r = client.get(f"/api/devices/{dev_id}/metrics/latest", headers=auth(admin_token))
    metrics = {i["metric"]: i["value"] for i in r.json()["items"]}
    assert metrics["cpu_usage"] == 42.0

    r = client.get(f"/api/devices/{dev_id}/metrics/catalog", headers=auth(admin_token))
    assert any(c["metric"] == "cpu_usage" for c in r.json()["catalog"])


def test_metrics_query_bad_device_and_time(client, admin_token):
    assert client.get("/api/devices/99999/metrics?metric=x", headers=auth(admin_token)).status_code == 404
    dev_id = _make_device(client, admin_token, "192.0.2.101")
    r = client.get(
        f"/api/devices/{dev_id}/metrics?metric=x&start=not-a-time",
        headers=auth(admin_token),
    )
    assert r.status_code == 400


def _insert_metric(device_id, metric, labels, value, time):
    db = SessionLocal()
    try:
        db.add(Metric(device_id=device_id, metric=metric, labels=labels, value=value, time=time))
        db.commit()
    finally:
        db.close()


def test_metrics_labels_filter_before_limit(client, admin_token):
    """labels 过滤下沉 SQL 层：先过滤再 limit，截断误伤的行也能取到。"""
    dev_id = _make_device(client, admin_token, "192.0.2.102")
    t0 = datetime(2026, 7, 1, 0, 0, 0)
    # 5 行 GE0/1（较早）+ 1 行 GE0/2（最新）：limit=5 不带过滤时 GE0/2 那行会被截掉
    for i in range(5):
        _insert_metric(dev_id, "if_in_bps", {"if": "GE0/1"}, float(i), t0 + timedelta(minutes=i))
    _insert_metric(dev_id, "if_in_bps", {"if": "GE0/2"}, 99.0, t0 + timedelta(minutes=5))

    # 不带 labels：行为不变，limit=5 截断，取到 5 行 GE0/1
    r = client.get(
        f"/api/devices/{dev_id}/metrics?metric=if_in_bps&limit=5", headers=auth(admin_token)
    )
    assert r.status_code == 200
    points = r.json()["points"]
    assert len(points) == 5
    assert all(p["labels"] == {"if": "GE0/1"} for p in points)

    # 带 labels：过滤在 limit 之前生效，能取到不带过滤时会被截断掉的 GE0/2 行
    r = client.get(
        f"/api/devices/{dev_id}/metrics?metric=if_in_bps&limit=5&labels="
        + quote('{"if":"GE0/2"}'),
        headers=auth(admin_token),
    )
    assert r.status_code == 200
    points = r.json()["points"]
    assert len(points) == 1
    assert points[0]["labels"] == {"if": "GE0/2"}
    assert points[0]["value"] == 99.0

    # 整体相等语义：超集（多键）不匹配
    r = client.get(
        f"/api/devices/{dev_id}/metrics?metric=if_in_bps&labels="
        + quote('{"if":"GE0/1","extra":"x"}'),
        headers=auth(admin_token),
    )
    assert r.status_code == 200
    assert r.json()["points"] == []

    # labels 非法 JSON 仍 400
    r = client.get(
        f"/api/devices/{dev_id}/metrics?metric=if_in_bps&labels=not-json",
        headers=auth(admin_token),
    )
    assert r.status_code == 400
