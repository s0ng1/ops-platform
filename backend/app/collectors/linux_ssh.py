"""Linux 主机指标采集（SSH 主通道，北塔同款 btmon 只读账号模式）。
执行命令集读 /proc/stat、/proc/meminfo、df，回传解析。全部无 Agent。
"""
import asyncio
import logging
from datetime import datetime, timezone

from ..core.ssh_hostkeys import verify_connection_host_key
from ..models import Device
from .rate import RateCalculator
from .snmp_metrics import MetricPoint

log = logging.getLogger(__name__)

SSH_CONNECT_TIMEOUT = 5
SSH_CMD_TIMEOUT = 10


def parse_proc_stat(text: str) -> tuple[int, int] | None:
    """解析 /proc/stat 首行 cpu 累计值，返回 (busy_jiffies, total_jiffies)。"""
    for line in text.splitlines():
        if line.startswith("cpu "):
            parts = [int(x) for x in line.split()[1:] if x.isdigit()]
            if len(parts) < 5:
                return None
            idle = parts[3] + (parts[4] if len(parts) > 4 else 0)  # idle + iowait
            total = sum(parts)
            return total - idle, total
    return None


def parse_meminfo(text: str) -> float | None:
    """解析 /proc/meminfo，返回内存使用率百分比。"""
    values = {}
    for line in text.splitlines():
        if ":" in line:
            key, rest = line.split(":", 1)
            values[key.strip()] = rest.strip().split()[0]
    try:
        total = int(values["MemTotal"])
        available = int(values.get("MemAvailable", values.get("MemFree", 0)))
    except (KeyError, ValueError):
        return None
    if total <= 0:
        return None
    return (total - available) / total * 100


def parse_df(text: str) -> list[tuple[str, float]]:
    """解析 df -P 输出，返回 [(挂载点, 使用率%)]，跳过虚拟文件系统。"""
    skip_prefixes = ("tmpfs", "devtmpfs", "overlay", "shm", "cgroup", "udev")
    mounts = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6 or not parts[0] or parts[0] in ("Filesystem",):
            continue
        fs, pct, mount = parts[0], parts[4], parts[5]
        if fs.startswith(skip_prefixes) or not pct.endswith("%"):
            continue
        try:
            mounts.append((mount, float(pct[:-1])))
        except ValueError:
            continue
    return mounts


async def _run_ssh_commands(host: str, payload: dict, commands: list[str]) -> list[str]:
    """建立一次 SSH 连接，顺序执行命令，返回 stdout 列表。失败抛异常由调用方隔离。"""
    import asyncssh

    port = int(payload.get("port", 22))
    async with asyncio.timeout(SSH_CONNECT_TIMEOUT + SSH_CMD_TIMEOUT * len(commands)):
        async with asyncssh.connect(
            host,
            port=port,
            username=payload.get("username", ""),
            password=payload.get("password", ""),
            known_hosts=None,  # 改由 verify_connection_host_key 做 TOFU 指纹校验
            connect_timeout=SSH_CONNECT_TIMEOUT,
        ) as conn:
            verify_connection_host_key(conn, host, port)
            results = []
            for cmd in commands:
                r = await conn.run(cmd, timeout=SSH_CMD_TIMEOUT)
                results.append(r.stdout or "")
            return results


async def collect_linux_metrics(
    device: Device,
    payload: dict,
    rate_calc: RateCalculator,
    run_commands=_run_ssh_commands,
) -> list[MetricPoint]:
    """采集一台 Linux 主机指标。run_commands 可注入便于测试。"""
    points: list[MetricPoint] = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ts = datetime.now(timezone.utc).timestamp()
    host, did = device.ip, device.id

    try:
        stat_text, meminfo_text, df_text, load_text = await run_commands(
            host, payload,
            ["cat /proc/stat", "cat /proc/meminfo", "df -P", "cat /proc/loadavg"],
        )
    except Exception as e:  # noqa: BLE001 - SSH 失败本周期整体跳过
        log.debug("Linux SSH 采集失败 %s: %s", host, e)
        return points

    # CPU：累计 jiffies 差值 ÷ 总差值（跨周期用 rate_calc 存上次值，首周期无值）
    stat = parse_proc_stat(stat_text)
    if stat:
        busy, total = stat
        busy_rate = rate_calc.rate((did, "cpu_busy"), ts, busy)
        total_rate = rate_calc.rate((did, "cpu_total"), ts, total)
        if busy_rate is not None and total_rate:
            points.append(MetricPoint(did, "cpu_usage", round(busy_rate / total_rate * 100, 2), {}, now))

    mem = parse_meminfo(meminfo_text)
    if mem is not None:
        points.append(MetricPoint(did, "mem_usage", round(mem, 2), {}, now))

    for mount, pct in parse_df(df_text):
        points.append(MetricPoint(did, "disk_usage", pct, {"mount": mount}, now))

    parts = load_text.split()
    if len(parts) >= 3:
        for name, val in (("load1", parts[0]), ("load5", parts[1]), ("load15", parts[2])):
            try:
                points.append(MetricPoint(did, name, float(val), {}, now))
            except ValueError:
                continue

    return points
