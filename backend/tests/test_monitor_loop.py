"""监控循环批量路径测试（第 8 期）：批量读/写、状态变化检测 + WS 推送、分批逻辑、单台探测。
网络层（scanner.ping）与告警引擎按需打桩，断言批量语义与旧逐台路径一致。
"""
import asyncio

import pytest

from app.alerting import engine
from app.collectors import scanner
from app.core.broadcast import broadcaster
from app.core.database import SessionLocal
from app.models import Device, Metric
from app.scheduler import monitor_loop


def run(coro):
    return asyncio.run(coro)


def _add_device(ip, name, status="unknown"):
    db = SessionLocal()
    d = db.query(Device).filter(Device.ip == ip, Device.type == "network").first()
    if d is None:
        d = Device(name=name, ip=ip, type="network", monitor_enabled=True)
        db.add(d)
        db.commit()
        db.refresh(d)
    d.status = status
    d.monitor_enabled = True
    db.commit()
    db.refresh(d)
    db.close()
    return d.id


def _get_device(device_id):
    db = SessionLocal()
    d = db.get(Device, device_id)
    db.close()
    return d


@pytest.fixture()
def probe_stub(monkeypatch):
    """按 IP 表驱动 ping 结果；未列出的 IP 默认在线 1ms。"""

    def install(table: dict[str, tuple[bool, int | None]]):
        async def fake_ping(ip, timeout=1):
            await asyncio.sleep(0)
            return table.get(ip, (True, 1))

        monkeypatch.setattr(scanner, "ping", fake_ping)

    return install


@pytest.fixture()
def captured(monkeypatch):
    """捕获 WS 广播与引擎评估调用。"""
    box = {"broadcasts": [], "eval_calls": []}

    async def fake_broadcast(msg):
        box["broadcasts"].append(msg)

    async def fake_eval(points):
        box["eval_calls"].append(list(points))

    monkeypatch.setattr(broadcaster, "broadcast", fake_broadcast)
    monkeypatch.setattr(engine, "evaluate_points", fake_eval)
    return box


def test_batch_status_change_and_ws_and_eval(client, probe_stub, captured):
    """批量路径：状态正确回写、unknown→online/offline 触发 WS 推送、一轮只评估一次。"""
    d1 = _add_device("198.51.100.51", "批量机-在线")
    d2 = _add_device("198.51.100.52", "批量机-离线")
    probe_stub({"198.51.100.52": (False, None)})

    run(monitor_loop.run_check_once())

    dev1, dev2 = _get_device(d1), _get_device(d2)
    assert dev1.status == "online" and dev1.last_latency_ms == 1 and dev1.last_seen is not None
    assert dev2.status == "offline" and dev2.last_latency_ms is None
    assert dev1.last_checked is not None and dev2.last_checked is not None

    # 状态变化 WS 推送（unknown→online/offline 算变化），内容与旧路径一致
    ours = [b for b in captured["broadcasts"] if b["device_id"] in (d1, d2)]
    assert {(b["device_id"], b["status"]) for b in ours} == {(d1, "online"), (d2, "offline")}
    assert all(b["type"] == "device_status" and b["name"] for b in ours)

    # 引擎一轮只调一次；离线设备 device_online=0.0、ping 超时=-1.0
    assert len(captured["eval_calls"]) == 1
    pts = {(p.device_id, p.metric): p.value for p in captured["eval_calls"][0]}
    assert pts[(d1, "device_online")] == 1.0 and pts[(d1, "ping_latency_ms")] == 1.0
    assert pts[(d2, "device_online")] == 0.0 and pts[(d2, "ping_latency_ms")] == -1.0

    # 指标点已批量入库
    db = SessionLocal()
    rows = (
        db.query(Metric.metric)
        .filter(Metric.device_id.in_([d1, d2]), Metric.metric.in_(["device_online", "ping_latency_ms"]))
        .all()
    )
    db.close()
    assert {r.metric for r in rows} == {"device_online", "ping_latency_ms"}

    # 第二轮状态不变 → 无 WS 推送，引擎仍收到本轮点
    captured["broadcasts"].clear()
    run(monitor_loop.run_check_once())
    assert [b for b in captured["broadcasts"] if b["device_id"] in (d1, d2)] == []
    assert len(captured["eval_calls"]) == 2


def test_write_batching_by_chunk_size(client, probe_stub, captured, monkeypatch):
    """分批逻辑：WRITE_BATCH_SIZE=1 时每台一批，全部设备仍完整回写并产出评估点。"""
    monkeypatch.setattr(monitor_loop, "WRITE_BATCH_SIZE", 1)
    ids = [_add_device(f"198.51.100.6{i}", f"分批机-{i}") for i in range(3)]
    probe_stub({})

    run(monitor_loop.run_check_once())

    for did in ids:
        assert _get_device(did).status == "online"
    assert len(captured["eval_calls"]) == 1
    ours = {p.device_id for p in captured["eval_calls"][0]}
    assert set(ids) <= ours


def test_check_device_single_path(client, probe_stub, captured):
    """手动探测单台路径：回写状态 + 入库两点 + 返回两点供调用方评估。"""
    did = _add_device("198.51.100.70", "单探机")
    probe_stub({"198.51.100.70": (True, 7)})

    points = run(monitor_loop.check_device(did))

    assert points is not None and len(points) == 2
    assert {(p.metric, p.value) for p in points} == {("device_online", 1.0), ("ping_latency_ms", 7.0)}
    dev = _get_device(did)
    assert dev.status == "online" and dev.last_latency_ms == 7
    assert any(b["device_id"] == did and b["status"] == "online" for b in captured["broadcasts"])
    db = SessionLocal()
    n = db.query(Metric).filter(Metric.device_id == did, Metric.metric == "device_online").count()
    db.close()
    assert n == 1

    # 设备不存在 → None，不抛异常
    assert run(monitor_loop.check_device(999999)) is None
