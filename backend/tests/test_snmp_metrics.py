"""SNMP 指标采集器测试：注入假 walk 数据，验证指标点生成。"""
import asyncio

import pytest

from app.collectors.rate import RateCalculator
from app.collectors.snmp_metrics import (
    OID_IF_HC_IN,
    OID_IF_HC_OUT,
    OID_IF_HIGH_SPEED,
    OID_IF_NAME,
    OID_IF_OPER_STATUS,
    collect_device_metrics,
)


class FakeDevice:
    id = 7
    ip = "192.0.2.1"
    sys_object_id = "1.3.6.1.4.1.2011.2.23.339"  # 华为前缀


def run(coro):
    return asyncio.run(coro)


def make_walk(tables):
    async def fake_walk(host, payload, base_oid):
        return dict(tables.get(base_oid, {}))

    return fake_walk


def if_tables(in_octets, out_octets):
    return {
        OID_IF_NAME: {f"{OID_IF_NAME}.9": "GE0/0/1"},
        OID_IF_HC_IN: {f"{OID_IF_HC_IN}.9": str(in_octets)},
        OID_IF_HC_OUT: {f"{OID_IF_HC_OUT}.9": str(out_octets)},
        OID_IF_HIGH_SPEED: {f"{OID_IF_HIGH_SPEED}.9": "1000"},  # 1000 Mbps
        OID_IF_OPER_STATUS: {f"{OID_IF_OPER_STATUS}.9": "1"},
    }


def test_first_cycle_only_status_no_rate():
    rc = RateCalculator()
    points = run(collect_device_metrics(
        FakeDevice, {"kind": "snmp_v2c"}, rc, fetch_walk=make_walk(if_tables(1000, 2000))
    ))
    by_metric = {p.metric for p in points}
    assert "if_status" in by_metric
    # 首周期无历史值，不产生速率/利用率
    assert "if_in_bps" not in by_metric


def test_second_cycle_rate_and_util():
    rc = RateCalculator()
    payload = {"kind": "snmp_v2c"}
    run(collect_device_metrics(FakeDevice, payload, rc, fetch_walk=make_walk(if_tables(0, 0))))
    # 手工回拨历史时间戳，模拟 60 秒间隔
    for key, (ts, raw) in list(rc._last.items()):
        rc._last[key] = (ts - 60, raw)
    points = run(collect_device_metrics(
        FakeDevice, payload, rc, fetch_walk=make_walk(if_tables(600000, 1200000))
    ))
    by_metric = {}
    for p in points:
        by_metric.setdefault(p.metric, p)
    # 600000 字节 / 60s = 10000 B/s = 80000 bps（间隔含微小真实耗时，用相对误差）
    assert by_metric["if_in_bps"].value == pytest.approx(80000.0, rel=0.01)
    assert by_metric["if_in_bps"].labels == {"if": "GE0/0/1"}
    # 80000 bps / 1Gbps ≈ 0.008%
    assert by_metric["if_in_util"].value == pytest.approx(0.008, rel=0.01)
    assert by_metric["if_out_bps"].value == pytest.approx(160000.0, rel=0.01)


def test_vendor_cpu_mem_huawei():
    rc = RateCalculator()
    tables = if_tables(0, 0)
    tables["1.3.6.1.4.1.2011.5.25.31.1.1.1.1.5"] = {
        "1.3.6.1.4.1.2011.5.25.31.1.1.1.1.5.16842753": "30"
    }
    tables["1.3.6.1.4.1.2011.5.25.31.1.1.1.1.7"] = {
        "1.3.6.1.4.1.2011.5.25.31.1.1.1.1.7.16842753": "55"
    }
    points = run(collect_device_metrics(
        FakeDevice, {"kind": "snmp_v2c"}, rc, fetch_walk=make_walk(tables)
    ))
    by_metric = {p.metric: p.value for p in points}
    assert by_metric["cpu_usage"] == 30.0
    assert by_metric["mem_usage"] == 55.0


def test_vendor_cpu_mem_h3c_walk_max():
    """H3C 框式设备：walk_max 取最忙板卡，空槽位 0 不稀释（真机 S10508 实测形态）。"""

    class H3CDevice(FakeDevice):
        sys_object_id = "1.3.6.1.4.1.25506.1.639"  # S10508-V

    rc = RateCalculator()
    tables = if_tables(0, 0)
    # 673 个实例大部分为 0（空槽），最忙板卡 16 / 54
    tables["1.3.6.1.4.1.25506.2.6.1.1.1.1.6"] = {
        f"1.3.6.1.4.1.25506.2.6.1.1.1.1.6.{i}": ("16" if i == 42 else "0") for i in range(1, 50)
    }
    tables["1.3.6.1.4.1.25506.2.6.1.1.1.1.8"] = {
        f"1.3.6.1.4.1.25506.2.6.1.1.1.1.8.{i}": ("54" if i == 42 else "0") for i in range(1, 50)
    }
    points = run(collect_device_metrics(
        H3CDevice, {"kind": "snmp_v2c"}, rc, fetch_walk=make_walk(tables)
    ))
    by_metric = {p.metric: p.value for p in points}
    assert by_metric["cpu_usage"] == 16.0
    assert by_metric["mem_usage"] == 54.0


def test_unknown_vendor_skips_cpu_mem():
    class UnknownVendor(FakeDevice):
        sys_object_id = "1.3.6.1.4.1.99999.1"

    rc = RateCalculator()
    points = run(collect_device_metrics(
        UnknownVendor, {"kind": "snmp_v2c"}, rc, fetch_walk=make_walk(if_tables(0, 0))
    ))
    assert "cpu_usage" not in {p.metric for p in points}


def test_walk_failure_returns_empty_not_raise():
    async def bad_walk(host, payload, base_oid):
        raise RuntimeError("timeout")

    rc = RateCalculator()
    points = run(collect_device_metrics(
        FakeDevice, {"kind": "snmp_v2c"}, rc, fetch_walk=bad_walk
    ))
    assert points == []
