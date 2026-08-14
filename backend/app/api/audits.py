"""审计日志查询（仅 admin）：用户名/动作/时间范围过滤 + 分页。"""
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..core.database import get_db
from ..models import AuditLog
from .deps import require_admin

router = APIRouter(prefix="/api/audits", tags=["审计"], dependencies=[Depends(require_admin)])


@router.get("")
def list_audits(
    username: str = Query(default=""),
    action: str = Query(default=""),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    q = db.query(AuditLog)
    if username:
        q = q.filter(AuditLog.username == username)
    if action:
        q = q.filter(AuditLog.action == action)
    if start:
        q = q.filter(AuditLog.created_at >= start.replace(tzinfo=None))
    if end:
        q = q.filter(AuditLog.created_at <= end.replace(tzinfo=None))
    total = q.count()
    rows = (
        q.order_by(AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    items = [
        {
            "id": r.id,
            "username": r.username,
            "action": r.action,
            "target": r.target,
            "detail": r.detail,
            "ip": r.ip,
            "created_at": r.created_at,
        }
        for r in rows
    ]
    return {"total": total, "items": items}
