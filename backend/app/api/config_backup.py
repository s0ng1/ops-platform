"""网络设备配置备份：版本列表 / 单版本全文 / 两版 diff / 手动立即拉取。"""
import asyncio
import difflib

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from ..collectors import config_backup as cb
from ..core import audit
from ..core.database import get_db
from ..models import ConfigBackup, Device
from .deps import get_current_user, require_operator

router = APIRouter(prefix="/api/devices", tags=["配置备份"])


def _get_device(db: Session, device_id: int) -> Device:
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    return device


def _get_backup(db: Session, device_id: int, backup_id: int) -> ConfigBackup:
    backup = db.get(ConfigBackup, backup_id)
    if backup is None or backup.device_id != device_id:
        raise HTTPException(status_code=404, detail="备份版本不存在")
    return backup


def _summary(b: ConfigBackup) -> dict:
    return {
        "id": b.id,
        "content_hash": b.content_hash,
        "size": len(b.content.encode("utf-8")),
        "created_at": b.created_at,
    }


@router.get("/{device_id}/config-backups")
def list_backups(
    device_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
    _: object = Depends(get_current_user),
):
    _get_device(db, device_id)
    q = db.query(ConfigBackup).filter(ConfigBackup.device_id == device_id)
    total = q.count()
    rows = (
        q.order_by(ConfigBackup.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {"total": total, "items": [_summary(b) for b in rows]}


# 注意：diff 路由必须声明在 {backup_id} 之前，否则 "diff" 会被当 backup_id 匹配
@router.get("/{device_id}/config-backups/diff", response_class=PlainTextResponse)
def diff_backups(
    device_id: int,
    from_: int = Query(alias="from"),
    to: int = Query(),
    db: Session = Depends(get_db),
    _: object = Depends(require_operator),
):
    """两版本 unified diff，纯文本返回。配置内容敏感，仅 operator/admin 可读。"""
    _get_device(db, device_id)
    old = _get_backup(db, device_id, from_)
    new = _get_backup(db, device_id, to)
    lines = difflib.unified_diff(
        old.content.splitlines(),
        new.content.splitlines(),
        fromfile=f"版本{old.id}（{old.created_at:%Y-%m-%d %H:%M:%S}）",
        tofile=f"版本{new.id}（{new.created_at:%Y-%m-%d %H:%M:%S}）",
        lineterm="",
    )
    return "\n".join(lines)


@router.get("/{device_id}/config-backups/{backup_id}")
def get_backup(
    device_id: int,
    backup_id: int,
    db: Session = Depends(get_db),
    _: object = Depends(require_operator),
):
    """单版本全文。设备配置常含 community/口令，仅 operator/admin 可读。"""
    _get_device(db, device_id)
    backup = _get_backup(db, device_id, backup_id)
    return {**_summary(backup), "content": backup.content}


@router.post("/{device_id}/config-backups/fetch")
async def fetch_backup(
    device_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    """手动立即拉取一次配置（照手动探测端点模式：结果同样过告警引擎）。"""
    device = _get_device(db, device_id)
    cred = device.ssh_credential
    if cred is None:
        raise HTTPException(status_code=400, detail="该设备未配置备份用 SSH 凭据")
    result = await cb.fetch_config(device, cred.get_payload())
    if result["status"] != "failed":
        # 手动拉取同样过一遍告警引擎（「配置变更」内置规则靠它触发）
        from ..alerting import engine as alert_engine

        point = cb.config_changed_point(device.id, result["status"] == "changed")
        await alert_engine.evaluate_points([point])
    await asyncio.to_thread(
        audit.record, user.username, "config_fetch", f"{device.name}({device.ip})",
        result["status"], audit.client_ip(request),
    )
    return result
