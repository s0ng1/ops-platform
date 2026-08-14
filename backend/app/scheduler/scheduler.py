"""采集调度器：指标任务注册表 + 每任务独立周期循环。
每个任务：筛出适用设备 → 协程池并发采集 → 批量写入 metrics 表。
单设备/单任务失败隔离，不阻塞其他任务。
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from ..collectors.app_probe import collect_app_metrics
from ..collectors.config_backup import collect_config_backup
from ..collectors.db_probe import collect_db_metrics
from ..collectors.ipam import collect_ipam
from ..collectors.linux_ssh import collect_linux_metrics
from ..collectors.rate import RateCalculator
from ..collectors.snmp_metrics import MetricPoint, collect_device_metrics
from ..collectors.windows import collect_windows_metrics
from ..core.database import SessionLocal
from ..models import Device, Metric
from ..models.metric import utcnow

log = logging.getLogger(__name__)

COLLECT_CONCURRENCY = 50

# 采集函数签名：async (device, ctx) -> list[MetricPoint]
CollectFunc = Callable[[Device, dict], Awaitable[list[MetricPoint]]]
AppliesFunc = Callable[[Device], bool]


@dataclass
class CollectionTask:
    name: str
    interval: int  # 秒
    collect: CollectFunc
    applies_to: AppliesFunc


def _has_snmp_credential(device: Device) -> bool:
    cred = device.credential
    return bool(cred and cred.kind in ("snmp_v2c", "snmp_v3"))


def _snmp_metric_scope(device: Device) -> bool:
    return device.monitor_enabled and device.type in ("network", "security") and _has_snmp_credential(device)


async def _collect_snmp_metrics(device: Device, ctx: dict) -> list[MetricPoint]:
    payload = device.credential.get_payload()
    payload["kind"] = device.credential.kind
    return await collect_device_metrics(device, payload, ctx["rate_calc"])


def _has_ssh_credential(device: Device) -> bool:
    cred = device.credential
    return bool(cred and cred.kind == "ssh")


def _windows_scope(device: Device) -> bool:
    return device.monitor_enabled and device.type == "server_windows" and _has_snmp_credential(device)


def _linux_scope(device: Device) -> bool:
    return device.monitor_enabled and device.type == "server_linux" and _has_ssh_credential(device)


async def _collect_windows(device: Device, ctx: dict) -> list[MetricPoint]:
    payload = device.credential.get_payload()
    payload["kind"] = device.credential.kind
    return await collect_windows_metrics(device, payload)


async def _collect_linux(device: Device, ctx: dict) -> list[MetricPoint]:
    return await collect_linux_metrics(
        device, device.credential.get_payload(), ctx["rate_calc"]
    )


def _db_scope(device: Device) -> bool:
    cred = device.credential
    return bool(device.monitor_enabled and device.type == "database" and cred and cred.kind == "database")


async def _collect_db(device: Device, ctx: dict) -> list[MetricPoint]:
    return await collect_db_metrics(device, device.credential.get_payload(), ctx["rate_calc"])


def _config_backup_scope(device: Device) -> bool:
    # 只认辅槽备份凭据：主槽通常挂 SNMP 凭据，交换机两类凭据不能同挂
    return device.monitor_enabled and device.type in ("network", "security") and device.ssh_credential_id is not None


async def _collect_config_backup(device: Device, ctx: dict) -> list[MetricPoint]:
    return await collect_config_backup(device, device.ssh_credential.get_payload())


async def _collect_ipam(device: Device, ctx: dict) -> list[MetricPoint]:
    payload = device.credential.get_payload()
    payload["kind"] = device.credential.kind
    return await collect_ipam(device, payload)


def _app_probe_scope(device: Device) -> bool:
    return device.monitor_enabled and device.type == "application"


async def _collect_app_probe(device: Device, ctx: dict) -> list[MetricPoint]:
    # nginx/redis 的速率类指标需要跨周期差值计算器
    return await collect_app_metrics(device, rate_calc=ctx["rate_calc"])


# 任务注册表：配置备份 6 小时周期，数据库探针与 IPAM 5 分钟周期，其余 1 分钟
TASKS: list[CollectionTask] = [
    CollectionTask("snmp_metrics", 60, _collect_snmp_metrics, _snmp_metric_scope),
    CollectionTask("windows_snmp", 60, _collect_windows, _windows_scope),
    CollectionTask("linux_ssh", 60, _collect_linux, _linux_scope),
    CollectionTask("db_probe", 300, _collect_db, _db_scope),
    CollectionTask("config_backup", 21600, _collect_config_backup, _config_backup_scope),
    # IPAM 台账采集与 SNMP 指标同范围：network/security 且有 SNMP 凭据
    CollectionTask("ipam_collect", 300, _collect_ipam, _snmp_metric_scope),
    # 应用仿真拨测（http/dns/tcp）与中间件轻量指标（nginx/redis），无需凭据
    CollectionTask("app_probe", 60, _collect_app_probe, _app_probe_scope),
]


def write_points(points: list[MetricPoint]) -> int:
    """批量写入 metrics 表，返回写入点数（0=失败）。供 to_thread 调用。
    调度器各任务与监控循环（device_online/ping）共用的批量入库路径。"""
    if not points:
        return 0
    db = SessionLocal()
    try:
        db.add_all(
            Metric(
                time=p.time or utcnow(),
                device_id=p.device_id,
                metric=p.metric,
                labels=p.labels,
                value=p.value,
            )
            for p in points
        )
        db.commit()
        return len(points)
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("指标批量入库失败")
        return 0
    finally:
        db.close()


async def run_task_once(task: CollectionTask, ctx: dict) -> int:
    """执行一轮采集并入库，返回写入点数。任务级异常自吞。"""
    db = SessionLocal()
    try:
        devices = [d for d in db.query(Device).all() if task.applies_to(d)]
    finally:
        db.close()
    if not devices:
        return 0

    sem = asyncio.Semaphore(COLLECT_CONCURRENCY)

    async def guarded(device: Device) -> list[MetricPoint]:
        async with sem:
            try:
                return await task.collect(device, ctx)
            except Exception:  # noqa: BLE001 - 单设备失败隔离
                log.exception("采集失败 task=%s device=%s", task.name, device.ip)
                return []

    results = await asyncio.gather(*(guarded(d) for d in devices))
    points = [p for sub in results for p in sub]
    if not points:
        return 0

    # 先评估告警（事件及时性优先），再入库
    from ..alerting import engine as alert_engine

    await alert_engine.evaluate_points(points)

    # 大批量插入是较长同步块，丢线程池避免阻塞事件循环
    return await asyncio.to_thread(write_points, points)


async def _task_loop(task: CollectionTask, ctx: dict, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            n = await run_task_once(task, ctx)
            if n:
                log.debug("采集完成 task=%s 写入 %d 点", task.name, n)
        except Exception:  # noqa: BLE001
            log.exception("采集任务异常 task=%s", task.name)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=task.interval)
        except asyncio.TimeoutError:
            pass


def start_scheduler(stop_event: asyncio.Event) -> list[asyncio.Task]:
    """在应用事件循环上启动全部采集任务循环（lifespan 调用）。"""
    ctx = {"rate_calc": RateCalculator()}
    return [asyncio.create_task(_task_loop(t, ctx, stop_event)) for t in TASKS]
