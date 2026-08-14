"""Windows SNMP（HOST-RESOURCES-MIB）采集器测试：注入假 walk 数据。"""
import asyncio

import pytest

from app.collectors.windows import (
    OID_HR_PROCESSOR_LOAD,
    OID_HR_STORAGE_DESCR,
    OID_HR_STORAGE_SIZE,
    OID_HR_STORAGE_TYPE,
    OID_HR_STORAGE_UNITS,
    OID_HR_STORAGE_USED,
    TYPE_FIXED_DISK,
    TYPE_RAM,
    collect_windows_metrics,
    parse_storage_table,
)


class FakeDevice:
    id = 8
    ip = "192.0.2.50"


def run(coro):
    return asyncio.run(coro)


def storage_walk(mem_used_pct=50, disk_used_pct=80):
    """构造一内存行 + 一 C 盘行的 hrStorageTable，块大小 4096、各 1000 块。"""
    mem_used, disk_used = mem_used_pct * 10, disk_used_pct * 10
    return {
        OID_HR_STORAGE_TYPE: {
            f"{OID_HR_STORAGE_TYPE}.1": TYPE_RAM,
            f"{OID_HR_STORAGE_TYPE}.2": TYPE_FIXED_DISK,
        },
        OID_HR_STORAGE_DESCR: {
            f"{OID_HR_STORAGE_DESCR}.1": "Physical Memory",
            f"{OID_HR_STORAGE_DESCR}.2": "C:\\ Label:  Serial Number abcd",
        },
        OID_HR_STORAGE_UNITS: {f"{OID_HR_STORAGE_UNITS}.1": "4096", f"{OID_HR_STORAGE_UNITS}.2": "4096"},
        OID_HR_STORAGE_SIZE: {f"{OID_HR_STORAGE_SIZE}.1": "1000", f"{OID_HR_STORAGE_SIZE}.2": "1000"},
        OID_HR_STORAGE_USED: {f"{OID_HR_STORAGE_USED}.1": str(mem_used), f"{OID_HR_STORAGE_USED}.2": str(disk_used)},
    }


def make_walk(tables):
    async def fake_walk(host, payload, base_oid):
        return dict(tables.get(base_oid, {}))

    return fake_walk


def test_parse_storage_table():
    t = storage_walk(mem_used_pct=40, disk_used_pct=75)
    mem, disks = parse_storage_table(
        t[OID_HR_STORAGE_TYPE], t[OID_HR_STORAGE_DESCR],
        t[OID_HR_STORAGE_UNITS], t[OID_HR_STORAGE_SIZE], t[OID_HR_STORAGE_USED],
    )
    assert mem == 40.0
    assert disks == [("C:\\", 75.0)]


def test_collect_full():
    tables = storage_walk()
    tables[OID_HR_PROCESSOR_LOAD] = {
        f"{OID_HR_PROCESSOR_LOAD}.1": "20",
        f"{OID_HR_PROCESSOR_LOAD}.2": "40",
    }
    tables["1.3.6.1.2.1.25.4.2.1.1"] = {f"1.3.6.1.2.1.25.4.2.1.1.{i}": str(i) for i in range(120)}
    points = run(collect_windows_metrics(FakeDevice, {"kind": "snmp_v2c"}, fetch_walk=make_walk(tables)))
    by_metric = {}
    for p in points:
        by_metric.setdefault(p.metric, []).append(p)
    assert by_metric["cpu_usage"][0].value == 30.0  # 双核均值
    assert by_metric["mem_usage"][0].value == 50.0
    assert by_metric["disk_usage"][0].value == 80.0
    assert by_metric["disk_usage"][0].labels == {"disk": "C:\\"}
    assert by_metric["process_count"][0].value == 120.0


def test_collect_partial_failure_still_returns():
    async def flaky_walk(host, payload, base_oid):
        if base_oid == OID_HR_PROCESSOR_LOAD:
            raise RuntimeError("timeout")
        return {}

    points = run(collect_windows_metrics(FakeDevice, {"kind": "snmp_v2c"}, fetch_walk=flaky_walk))
    assert points == []  # 全空但不抛异常
