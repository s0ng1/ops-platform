"""日志 API：Syslog/Trap 事件查询（过滤+分页）、日志匹配规则 CRUD。"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core import audit
from ..core.database import get_db
from ..models import LogEvent, LogRule
from .deps import get_current_user, require_operator

router = APIRouter(prefix="/api/logs", tags=["日志"])


class LogRuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    source_ip: str | None = Field(default=None, max_length=64)   # 空=任意来源
    keyword: str | None = Field(default=None, max_length=256)    # 空=不限内容
    severity_lte: int | None = Field(default=None, ge=0, le=7)   # 空=不限等级（仅 syslog）
    alert_severity: str = Field(pattern="^(critical|major|warning|info)$")


class LogRuleOut(LogRuleIn):
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}


def _apply_rule(rule: LogRule, body: LogRuleIn) -> None:
    for field_name in LogRuleIn.model_fields:
        value = getattr(body, field_name)
        # 空串按 None 存（与「可空=任意」语义一致）
        if isinstance(value, str) and value == "" and field_name in ("source_ip", "keyword"):
            value = None
        setattr(rule, field_name, value)


# ---- 日志事件查询 ----


@router.get("/events")
def list_events(
    source_ip: str = Query(default=""),
    kind: str = Query(default=""),
    severity: int | None = Query(default=None),
    keyword: str = Query(default=""),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
    _: object = Depends(get_current_user),
):
    q = db.query(LogEvent)
    if source_ip:
        q = q.filter(LogEvent.source_ip == source_ip)
    if kind:
        q = q.filter(LogEvent.kind == kind)
    if severity is not None:
        q = q.filter(LogEvent.severity == severity)
    if keyword:
        q = q.filter(LogEvent.message.contains(keyword))
    if start:
        q = q.filter(LogEvent.created_at >= start.replace(tzinfo=None))
    if end:
        q = q.filter(LogEvent.created_at <= end.replace(tzinfo=None))
    total = q.count()
    rows = (
        q.order_by(LogEvent.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    items = [
        {
            "id": r.id,
            "source_ip": r.source_ip,
            "kind": r.kind,
            "facility": r.facility,
            "severity": r.severity,
            "message": r.message,
            "created_at": r.created_at,
        }
        for r in rows
    ]
    return {"total": total, "items": items}


# ---- 日志规则 CRUD ----


@router.get("/rules", response_model=list[LogRuleOut])
def list_rules(db: Session = Depends(get_db), _: object = Depends(get_current_user)):
    return db.query(LogRule).order_by(LogRule.id).all()


@router.post("/rules", response_model=LogRuleOut, status_code=201)
def create_rule(
    body: LogRuleIn,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    rule = LogRule()
    _apply_rule(rule, body)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    audit.record(user.username, "log_rule_create", target=rule.name, ip=audit.client_ip(request))
    return rule


@router.put("/rules/{rule_id}", response_model=LogRuleOut)
def update_rule(
    rule_id: int,
    body: LogRuleIn,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    rule = db.get(LogRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    _apply_rule(rule, body)
    db.commit()
    db.refresh(rule)
    audit.record(user.username, "log_rule_update", target=rule.name, ip=audit.client_ip(request))
    return rule


@router.delete("/rules/{rule_id}")
def delete_rule(
    rule_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    rule = db.get(LogRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    name = rule.name
    db.delete(rule)
    db.commit()
    audit.record(user.username, "log_rule_delete", target=name, ip=audit.client_ip(request))
    return {"ok": True}
