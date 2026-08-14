"""宿主机-应用总线视图 API（第 8 期 M5）。

利用 (ip, type) 一机多对象：server_linux/server_windows 为总线基座，
同 IP 的 database/application 对象挂载其上；每个对象带在线状态与 firing 告警聚合。
纯查询接口，无 DDL。
"""
from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..core.database import get_db
from ..models import AlertEvent, Device
from ..models.alert import SEVERITIES
from .deps import get_current_user

router = APIRouter(prefix="/api/bus", tags=["总线视图"])

# 宿主机类型（总线基座）与可挂载对象类型
HOST_TYPES = ("server_linux", "server_windows")
MOUNT_TYPES = ("database", "application")


def _object_out(d: Device, alert_counts: dict) -> dict:
    """单个对象输出：基础信息 + 在线状态 + firing 计数 + 最高 firing 等级。"""
    counts = alert_counts.get(d.id, {})
    return {
        "id": d.id,
        "name": d.name,
        "type": d.type,
        "ip": d.ip,
        # 在线状态口径同设备列表：device.status 由监控协程周期更新；
        # monitor_enabled 关闭视为未启用（前端置灰，与 firing 无关）
        "status": d.status,
        "monitor_enabled": d.monitor_enabled,
        "online": bool(d.monitor_enabled and d.status == "online"),
        "alerts": {s: counts.get(s, 0) for s in SEVERITIES},
        "alert_total": sum(counts.values()),
        # 最高 firing 等级（critical > major > warning > info），无 firing 为 None
        "max_severity": next((s for s in SEVERITIES if counts.get(s)), None),
    }


@router.get("")
def bus_view(db: Session = Depends(get_db), _: object = Depends(get_current_user)):
    """宿主机总线数据：宿主机卡片 + 同 IP 挂载对象 + 各对象 firing 告警聚合。"""
    devices = (
        db.query(Device)
        .filter(Device.type.in_(HOST_TYPES + MOUNT_TYPES))
        .order_by(Device.id)
        .all()
    )
    # firing 告警按 (设备, 等级) 一次性 GROUP BY 聚合，禁止逐设备查（N+1）
    rows = (
        db.query(AlertEvent.device_id, AlertEvent.severity, func.count())
        .filter(AlertEvent.status == "firing")
        .group_by(AlertEvent.device_id, AlertEvent.severity)
        .all()
    )
    alert_counts: dict[int, dict] = {}
    for device_id, severity, n in rows:
        alert_counts.setdefault(device_id, {})[severity] = n

    # 按 IP 归组：宿主机列表 + 同 IP 的挂载对象（无宿主机的孤儿对象不展示）
    hosts = [d for d in devices if d.type in HOST_TYPES]
    mounts: dict[str, list] = {}
    for d in devices:
        if d.type in MOUNT_TYPES:
            mounts.setdefault(d.ip, []).append(d)

    out = []
    for h in hosts:
        item = _object_out(h, alert_counts)
        item["objects"] = [_object_out(m, alert_counts) for m in mounts.get(h.ip, [])]
        out.append(item)
    return {"hosts": out}
