"""设备 CRUD + 手动探测。"""
import asyncio
import ipaddress
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from ..core import audit
from ..core.database import get_db
from ..models import Credential, Device
from ..scheduler.monitor_loop import check_device
from .deps import get_current_user, require_operator
from .schemas import DeviceIn, DeviceOut

router = APIRouter(prefix="/api/devices", tags=["设备"])

# application 类型的 ip 字段语义为目标主机：允许 IP 或域名
_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Redis 拨测口令脱敏哨兵：读接口回显为星号，编辑时提交星号=「不修改」（与凭据「留空不修改」同语义）
_REDACTED = "******"


def _check_ip_or_host(body: DeviceIn) -> None:
    try:
        ipaddress.ip_address(body.ip)
        return
    except ValueError:
        pass
    if body.type == "application" and _HOST_RE.match(body.ip):
        return
    raise HTTPException(status_code=400, detail="IP 格式不正确")


def _redact_probe_config(cfg: dict) -> dict:
    """Redis 拨测口令脱敏：非空 password 回显为星号哨兵（返回新 dict，不改 ORM 对象）。"""
    if cfg and cfg.get("password"):
        out = dict(cfg)
        out["password"] = _REDACTED
        return out
    return cfg or {}


def _to_out(device: Device) -> DeviceOut:
    out = DeviceOut.model_validate(device)
    out.probe_config = _redact_probe_config(out.probe_config or {})
    out.credential_name = device.credential.name if device.credential else None
    out.ssh_credential_name = device.ssh_credential.name if device.ssh_credential else None
    return out


def _apply(device: Device, body: DeviceIn, db: Session) -> None:
    if body.credential_id is not None and db.get(Credential, body.credential_id) is None:
        raise HTTPException(status_code=400, detail="凭据不存在")
    if body.ssh_credential_id is not None:
        ssh_cred = db.get(Credential, body.ssh_credential_id)
        if ssh_cred is None:
            raise HTTPException(status_code=400, detail="备份凭据不存在")
        if ssh_cred.kind != "ssh":
            raise HTTPException(status_code=422, detail="备份凭据必须是 SSH 类型")
    device.name = body.name or body.ip
    device.ip = body.ip
    device.type = body.type
    device.subtype = body.subtype
    device.group_name = body.group_name
    device.location = body.location
    device.credential_id = body.credential_id
    device.ssh_credential_id = body.ssh_credential_id
    device.monitor_enabled = body.monitor_enabled
    cfg = dict(body.probe_config or {})
    # 编辑时 password 提交星号哨兵=「不修改」：保留设备原口令，空串=清除
    if cfg.get("password") == _REDACTED and device.probe_config and device.probe_config.get("password"):
        cfg["password"] = device.probe_config["password"]
    device.probe_config = cfg


@router.get("", response_model=list[DeviceOut])
def list_devices(
    keyword: str = Query(default=""),
    status: str = Query(default=""),
    type: str = Query(default=""),
    types: str = Query(default="", description="逗号分隔多类型，优先于 type"),
    db: Session = Depends(get_db),
    _: object = Depends(get_current_user),
):
    q = db.query(Device)
    if keyword:
        like = f"%{keyword}%"
        q = q.filter(Device.name.like(like) | Device.ip.like(like))
    if status:
        q = q.filter(Device.status == status)
    if types:
        q = q.filter(Device.type.in_([t.strip() for t in types.split(",") if t.strip()]))
    elif type:
        q = q.filter(Device.type == type)
    return [_to_out(d) for d in q.order_by(Device.id).all()]


@router.post("", response_model=DeviceOut, status_code=201)
def create_device(
    body: DeviceIn,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    _check_ip_or_host(body)
    dup = db.query(Device).filter(Device.ip == body.ip, Device.type == body.type).first()
    if dup:
        raise HTTPException(status_code=409, detail="该 IP 下同类型设备已存在")
    device = Device()
    _apply(device, body, db)
    db.add(device)
    db.commit()
    db.refresh(device)
    audit.record(user.username, "device_create", target=f"{device.name}({device.ip})",
                 ip=audit.client_ip(request))
    return _to_out(device)


@router.get("/{device_id}", response_model=DeviceOut)
def get_device(
    device_id: int,
    db: Session = Depends(get_db),
    _: object = Depends(get_current_user),
):
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    return _to_out(device)


@router.put("/{device_id}", response_model=DeviceOut)
def update_device(
    device_id: int,
    body: DeviceIn,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    _check_ip_or_host(body)
    dup = (
        db.query(Device)
        .filter(Device.ip == body.ip, Device.type == body.type, Device.id != device_id)
        .first()
    )
    if dup:
        raise HTTPException(status_code=409, detail="该 IP 下同类型设备已存在")
    _apply(device, body, db)
    db.commit()
    db.refresh(device)
    audit.record(user.username, "device_update", target=f"{device.name}({device.ip})",
                 ip=audit.client_ip(request))
    return _to_out(device)


@router.delete("/{device_id}")
def delete_device(
    device_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    target = f"{device.name}({device.ip})"
    db.delete(device)
    db.commit()
    audit.record(user.username, "device_delete", target=target, ip=audit.client_ip(request))
    return {"ok": True}


@router.post("/{device_id}/probe", response_model=DeviceOut)
async def probe_device(
    device_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    """立即探测一次（ping + SNMP），返回最新状态。"""
    if db.get(Device, device_id) is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    points = await check_device(device_id)
    if points:
        # 手动探测同样过一遍告警引擎（设备离线内置规则靠它触发）
        from ..alerting import engine as alert_engine

        await alert_engine.evaluate_points(points)
    device = db.get(Device, device_id)
    db.refresh(device)
    await asyncio.to_thread(
        audit.record, user.username, "device_probe", f"{device.name}({device.ip})",
        "", audit.client_ip(request),
    )
    return _to_out(device)
