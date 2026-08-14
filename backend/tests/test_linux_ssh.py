"""Linux SSH 采集器测试：解析函数 + 注入假命令输出。"""
import asyncio

import pytest

from app.collectors.linux_ssh import (
    collect_linux_metrics,
    parse_df,
    parse_meminfo,
    parse_proc_stat,
)
from app.collectors.rate import RateCalculator

PROC_STAT = """cpu  100 0 50 800 20 0 5 0 0 0
cpu0 50 0 25 400 10 0 2 0 0 0
"""
PROC_MEMINFO = """MemTotal:       16384000 kB
MemFree:         8192000 kB
MemAvailable:   12288000 kB
Buffers:          500000 kB
"""
DF_OUT = """Filesystem     1024-blocks    Used Available Capacity Mounted on
/dev/sda1        102400000 81920000  20480000      80% /
tmpfs              8192000       0   8192000       0% /dev/shm
/dev/sdb1        204800000 40960000 163840000      20% /data
"""
LOADAVG = "0.50 1.20 2.30 1/200 12345\n"


class FakeDevice:
    id = 9
    ip = "192.0.2.60"


def test_parse_proc_stat():
    busy, total = parse_proc_stat(PROC_STAT)
    # total=975, idle=800+20=820, busy=155
    assert (busy, total) == (155, 975)
    assert parse_proc_stat("garbage") is None


def test_parse_meminfo():
    assert parse_meminfo(PROC_MEMINFO) == 25.0  # (16384-12288)/16384
    assert parse_meminfo("bad") is None


def test_parse_df():
    assert parse_df(DF_OUT) == [("/", 80.0), ("/data", 20.0)]  # tmpfs 被跳过


def test_collect_linux_two_cycles():
    outputs = {
        "cat /proc/stat": PROC_STAT,
        "cat /proc/meminfo": PROC_MEMINFO,
        "df -P": DF_OUT,
        "cat /proc/loadavg": LOADAVG,
    }

    async def fake_run(host, payload, commands):
        return [outputs[c] for c in commands]

    rc = RateCalculator()
    run = asyncio.run
    # 首周期：CPU 无值，其余有值
    points = run(collect_linux_metrics(FakeDevice, {}, rc, run_commands=fake_run))
    by_metric = {p.metric for p in points}
    assert "cpu_usage" not in by_metric
    assert {"mem_usage", "disk_usage", "load1", "load5", "load15"} <= by_metric

    # 回拨时间戳模拟 60 秒，第二周期 CPU 出值
    for key, (ts, raw) in list(rc._last.items()):
        rc._last[key] = (ts - 60, raw)
    outputs["cat /proc/stat"] = "cpu  160 0 80 1600 40 0 5 0 0 0\n"
    points = run(collect_linux_metrics(FakeDevice, {}, rc, run_commands=fake_run))
    cpu = next(p for p in points if p.metric == "cpu_usage")
    # busy 增量 245-155=90，total 增量 1885-975=910 → ≈9.89%
    assert cpu.value == pytest.approx(90 / 910 * 100, rel=0.01)


def test_collect_linux_ssh_failure_returns_empty():
    async def bad_run(host, payload, commands):
        raise OSError("connection refused")

    points = asyncio.run(collect_linux_metrics(FakeDevice, {}, RateCalculator(), run_commands=bad_run))
    assert points == []
