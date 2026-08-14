"""Windows 主机指标采集（SNMP 主通道，HOST-RESOURCES-MIB）。
覆盖：CPU（hrProcessorLoad 多核均值）、内存（hrStorage Ram 行）、
磁盘分区用量（hrStorage 固定盘行）、进程数（hrSWRunTable 行数）。
已知边界：磁盘 IO 标准 SNMP 覆盖不到，WMI 补充通道按现场条件另议。
"""
import logging
from datetime import datetime, timezone

from ..models import Device
from . import snmp
from .snmp_metrics import MetricPoint

log = logging.getLogger(__name__)

# HOST-RESOURCES-MIB
OID_HR_PROCESSOR_LOAD = "1.3.6.1.2.1.25.3.3.1.2"      # hrProcessorLoad（每核一行）
OID_HR_STORAGE_TYPE = "1.3.6.1.2.1.25.2.3.1.2"        # hrStorageType
OID_HR_STORAGE_DESCR = "1.3.6.1.2.1.25.2.3.1.3"       # hrStorageDescr
OID_HR_STORAGE_UNITS = "1.3.6.1.2.1.25.2.3.1.4"       # hrStorageAllocationUnits（字节/块）
OID_HR_STORAGE_SIZE = "1.3.6.1.2.1.25.2.3.1.5"        # hrStorageSize（块数）
OID_HR_STORAGE_USED = "1.3.6.1.2.1.25.2.3.1.6"        # hrStorageUsed（块数）
OID_HR_SW_RUN_INDEX = "1.3.6.1.2.1.25.4.2.1.1"        # hrSWRunIndex（行数=进程数）

TYPE_RAM = "1.3.6.1.2.1.25.2.1.2"      # hrStorageRam
TYPE_FIXED_DISK = "1.3.6.1.2.1.25.2.1.4"  # hrStorageFixedDisk


def _suffix(oid: str, base: str) -> str:
    return oid[len(base) + 1:]


def _f(value: str) -> float | None:
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def parse_storage_table(types, descrs, units, sizes, useds):
    """解析 hrStorageTable 五个列，返回 (内存使用率|None, [(盘符, 使用率), ...])。"""
    mem_usage = None
    disks = []
    for oid, stype in types.items():
        idx = _suffix(oid, OID_HR_STORAGE_TYPE)
        size = _f(sizes.get(f"{OID_HR_STORAGE_SIZE}.{idx}", ""))
        used = _f(useds.get(f"{OID_HR_STORAGE_USED}.{idx}", ""))
        if not size or used is None or size <= 0:
            continue
        pct = min(used / size * 100, 100.0)
        if stype == TYPE_RAM:
            mem_usage = pct
        elif stype == TYPE_FIXED_DISK:
            descr = descrs.get(f"{OID_HR_STORAGE_DESCR}.{idx}", idx)
            # descr 形如 "C:\\ Label:  Serial Number xxxx"，盘符取首段
            disk = descr.split(" ")[0] or idx
            disks.append((disk, pct))
    return mem_usage, disks


async def collect_windows_metrics(
    device: Device,
    payload: dict,
    fetch_walk=snmp.walk,
) -> list[MetricPoint]:
    """采集一台 Windows 主机指标。单项失败仅记日志。"""
    points: list[MetricPoint] = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    host, did = device.ip, device.id

    try:
        loads = await fetch_walk(host, payload, OID_HR_PROCESSOR_LOAD)
        values = [v for v in (_f(x) for x in loads.values()) if v is not None]
        if values:
            points.append(MetricPoint(did, "cpu_usage", round(sum(values) / len(values), 2), {}, now))
    except Exception as e:  # noqa: BLE001
        log.debug("Windows CPU 采集失败 %s: %s", host, e)

    try:
        types = await fetch_walk(host, payload, OID_HR_STORAGE_TYPE)
        if types:
            descrs = await fetch_walk(host, payload, OID_HR_STORAGE_DESCR)
            units = await fetch_walk(host, payload, OID_HR_STORAGE_UNITS)
            sizes = await fetch_walk(host, payload, OID_HR_STORAGE_SIZE)
            useds = await fetch_walk(host, payload, OID_HR_STORAGE_USED)
            mem_usage, disks = parse_storage_table(types, descrs, units, sizes, useds)
            if mem_usage is not None:
                points.append(MetricPoint(did, "mem_usage", round(mem_usage, 2), {}, now))
            for disk, pct in disks:
                points.append(MetricPoint(did, "disk_usage", round(pct, 2), {"disk": disk}, now))
    except Exception as e:  # noqa: BLE001
        log.debug("Windows 存储采集失败 %s: %s", host, e)

    try:
        procs = await fetch_walk(host, payload, OID_HR_SW_RUN_INDEX)
        if procs:
            points.append(MetricPoint(did, "process_count", float(len(procs)), {}, now))
    except Exception as e:  # noqa: BLE001
        log.debug("Windows 进程数采集失败 %s: %s", host, e)

    return points
