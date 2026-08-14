"""在线状态监控循环：周期 ping 全部启用监控的设备，有 SNMP 凭据的顺带取系统信息。
单台设备失败隔离，不阻塞整体。每轮把时延与在线状态写入 metrics 时序表。
第 8 期批量化：一轮 = ①一次批量读全部待检设备快照（含解密凭据）→ ②并发 ping/SNMP
（不持连接，保持第 5 期三段拆分语义）→ ③分批批量回写状态 + device_online/ping 指标点
走 scheduler.write_points 的批量路径；状态变化检测、WS 推送与告警引擎触发语义不变。
"""
import asyncio
import logging
from datetime import datetime

from ..collectors import scanner, snmp
from ..collectors.snmp_metrics import MetricPoint
from ..core.config import get_settings
from ..core.database import SessionLocal
from ..models import Device

log = logging.getLogger(__name__)

CHECK_CONCURRENCY = 50

# 状态批量回写的分批大小：防单批 SQL/事务过大
WRITE_BATCH_SIZE = 500

# 单台探测结果：(device_id, status, latency, info)
ProbeResult = tuple[int, str, "int | None", dict]


def _read_devices(ids: list[int] | None = None) -> dict[int, dict]:
    """一次批量读设备快照（含解密后的 SNMP 凭据 payload），读完即关。供 to_thread 调用。
    ids=None 读全部启用监控的设备（监控轮询）；否则按 id 列表读（手动探测）。"""
    db = SessionLocal()
    try:
        q = db.query(Device)
        if ids is not None:
            q = q.filter(Device.id.in_(ids))
        else:
            q = q.filter(Device.monitor_enabled.is_(True))
        snaps = {}
        for d in q.all():
            snap = {
                "ip": d.ip,
                "status": d.status,
                "cred_payload": None,
            }
            cred = d.credential
            if cred and cred.kind in ("snmp_v2c", "snmp_v3"):
                payload = cred.get_payload()
                payload["kind"] = cred.kind
                snap["cred_payload"] = payload
            snaps[d.id] = snap
        return snaps
    finally:
        db.close()


def _write_status(results: list[ProbeResult]) -> dict[int, str]:
    """分批批量回写设备状态，返回 {device_id: 写回后的设备名}（供 WS 广播与产出过滤）。
    单批失败记日志继续下一批（该批设备本轮不出点，与旧单台写失败语义一致）。供 to_thread 调用。"""
    names: dict[int, str] = {}
    for start in range(0, len(results), WRITE_BATCH_SIZE):
        chunk = results[start:start + WRITE_BATCH_SIZE]
        db = SessionLocal()
        try:
            devices = {
                d.id: d
                for d in db.query(Device)
                .filter(Device.id.in_([r[0] for r in chunk]))
                .all()
            }
            now = datetime.now()
            for device_id, status, latency, info in chunk:
                device = devices.get(device_id)
                if device is None:
                    continue
                device.last_checked = now
                device.last_latency_ms = latency
                device.status = status
                if status == "online":
                    device.last_seen = now
                if info:
                    device.sys_descr = info.get("sys_descr", "")[:512]
                    device.sys_object_id = info.get("sys_object_id", "")
                    if info.get("sys_name") and not device.name:
                        device.name = info["sys_name"]
                names[device_id] = device.name
            db.commit()
        except Exception:  # noqa: BLE001 - 单批失败不拖垮整轮
            db.rollback()
            log.exception("监控状态批量回写失败（本批 %d 台）", len(chunk))
        finally:
            db.close()
    return names


async def _probe_device(device_id: int, snap: dict) -> ProbeResult | None:
    """ping/SNMP 网络等待，全程无 DB 会话；返回探测结果，异常返回 None（单点失败隔离）。"""
    try:
        online, latency = await scanner.ping(snap["ip"])
        status = "online" if online else "offline"

        # 有 SNMP 凭据时尝试取系统信息（v2c/v3），失败不影响 ping 结论
        info = {}
        if snap["cred_payload"]:
            try:
                info = await snmp.get_system_info(snap["ip"], snap["cred_payload"])
                # SNMP 通即在线（有些设备禁 ping）
                status = "online"
            except Exception as e:  # noqa: BLE001
                log.debug("SNMP 查询失败 %s: %s", snap["ip"], e)
        return device_id, status, latency, info
    except Exception:  # noqa: BLE001 - 单点失败不阻塞整体
        log.exception("设备探测异常 device_id=%s", device_id)
        return None


def _make_points(results: list[ProbeResult], names: dict[int, str]) -> list[MetricPoint]:
    """产出 device_online/ping_latency_ms 采集点（只含状态回写成功的设备）。"""
    points = []
    for device_id, status, latency, _ in results:
        if device_id not in names:
            continue
        points.append(MetricPoint(device_id, "device_online", 1.0 if status == "online" else 0.0))
        points.append(MetricPoint(device_id, "ping_latency_ms", float(latency) if latency is not None else -1.0))
    return points


async def _broadcast_changes(results: list[ProbeResult], snaps: dict[int, dict], names: dict[int, str]) -> None:
    """状态变化时 WS 推送（首探 unknown→online/offline 也算变化，语义同旧逐台路径）。"""
    from ..core.broadcast import broadcaster

    for device_id, status, _, _ in results:
        if device_id in names and status != snaps[device_id]["status"]:
            await broadcaster.broadcast(
                {
                    "type": "device_status",
                    "device_id": device_id,
                    "name": names[device_id],
                    "status": status,
                }
            )


async def check_device(device_id: int) -> list[MetricPoint] | None:
    """探测单台设备并回写状态（手动探测端点用；监控轮询走 run_check_once 的批量路径）。
    返回两个采集点（device_online/ping_latency_ms）供评估；设备不存在或探测/回写异常返回 None。"""
    try:
        # ① 短会话读设备快照
        snaps = await asyncio.to_thread(_read_devices, [device_id])
        snap = snaps.get(device_id)
        if snap is None:
            return None
        # ② ping/SNMP 网络等待，全程无 DB 会话
        r = await _probe_device(device_id, snap)
        if r is None:
            return None
        _, status, _, _ = r
        # ③ 短会话写回状态 + 指标点批量入库
        names = await asyncio.to_thread(_write_status, [r])
        if device_id not in names:
            return None
        points = _make_points([r], names)
        from ..scheduler.scheduler import write_points

        await asyncio.to_thread(write_points, points)
        await _broadcast_changes([r], snaps, names)
        return points
    except Exception:  # noqa: BLE001 - 单点失败不阻塞整体
        log.exception("设备探测异常 device_id=%s", device_id)
        return None


async def run_check_once() -> None:
    """全量探测一轮：批量读快照 → 并发探测 → 分批批量回写 → 指标批量入库 → 统一过告警引擎。"""
    # ① 一次批量读出本轮全部待检设备快照
    snaps = await asyncio.to_thread(_read_devices)
    if not snaps:
        return
    # ② 并发 ping/SNMP，全程不持 DB 连接
    sem = asyncio.Semaphore(CHECK_CONCURRENCY)

    async def guarded(device_id: int, snap: dict):
        async with sem:
            return await _probe_device(device_id, snap)

    probed = await asyncio.gather(*(guarded(i, s) for i, s in snaps.items()))
    results = [r for r in probed if r is not None]
    if not results:
        return
    # ③ 分批批量回写状态（to_thread 不阻塞事件循环）
    names = await asyncio.to_thread(_write_status, results)
    points = _make_points(results, names)
    if not points:
        return
    # device_online/ping 指标点走调度器批量入库路径（先入库再评估，同调度器口径）
    from ..scheduler.scheduler import write_points

    await asyncio.to_thread(write_points, points)
    # 状态变化 WS 推送（回写成功的设备才有）
    await _broadcast_changes(results, snaps, names)
    # 在线状态/时延也过一遍告警引擎（设备离线内置规则靠它触发）；一轮只调一次
    from ..alerting import engine as alert_engine

    await alert_engine.evaluate_points(points)


async def monitor_loop(stop_event: asyncio.Event) -> None:
    """常驻循环，直到 stop_event 置位。"""
    interval = get_settings().monitor_interval
    while not stop_event.is_set():
        try:
            await run_check_once()
        except Exception:  # noqa: BLE001
            log.exception("监控轮询异常")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
