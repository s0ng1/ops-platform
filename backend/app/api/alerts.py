"""告警 API：规则管理、事件查询/确认/关闭、等级计数、通知渠道配置、静默窗口。"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..core import audit
from ..core.database import get_db
from ..models import AlertEvent, AlertRule, Device, NotifyConfig, SilenceWindow
from .deps import get_current_user, require_operator

router = APIRouter(prefix="/api", tags=["告警"])


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    metric: str = Field(min_length=1, max_length=64)
    # baseline_dev：与近 7 天同时段均值比较，偏离超 N 倍标准差触发（threshold 存 N）
    op: str = Field(pattern="^(>|>=|<|<=|==|!=|baseline_dev)$")
    threshold: float
    duration_cycles: int = Field(default=1, ge=1, le=100)
    severity: str = Field(pattern="^(critical|major|warning|info)$")
    device_type: str = ""
    group_name: str = ""
    device_id: int | None = None
    labels_filter: dict = Field(default_factory=dict)
    notify: list[str] = Field(default_factory=lambda: ["*"])
    enabled: bool = True
    escalate_minutes: int = Field(default=0, ge=0, le=10080)  # 超时未确认升级；0=不升级

    @model_validator(mode="after")
    def _check_baseline_threshold(self):
        # baseline_dev 的 threshold 语义是 N 倍标准差，必须为正数
        if self.op == "baseline_dev" and self.threshold <= 0:
            raise ValueError("baseline_dev 的阈值为 N 倍标准差，必须大于 0")
        return self


class RuleOut(RuleIn):
    id: int
    builtin: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class SilenceIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    device_type: str = ""
    group_name: str = ""
    device_id: int | None = None
    start_at: datetime
    end_at: datetime
    enabled: bool = True


class SilenceOut(SilenceIn):
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}


class EventOut(BaseModel):
    id: int
    rule_id: int | None
    rule_name: str
    device_id: int
    device_name: str = ""
    device_ip: str = ""
    metric: str
    labels: dict
    severity: str
    status: str
    value: float
    fired_at: datetime
    resolved_at: datetime | None
    ack_by: str
    ack_at: datetime | None
    silenced: bool = False

    model_config = {"from_attributes": True}


class EventDetailOut(EventOut):
    """事件详情：比列表多带触发时刻指标快照（快照可能较大，列表接口不带）。"""
    snapshot: dict | None = None


class NotifyConfigIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    kind: str = Field(pattern="^(smtp|dingtalk|wecom)$")
    config: dict = Field(default_factory=dict)
    enabled: bool = True


class NotifyConfigOut(BaseModel):
    id: int
    name: str
    kind: str
    enabled: bool
    created_at: datetime

    model_config = {"from_attributes": True}


def _apply_rule(rule: AlertRule, body: RuleIn) -> None:
    for field_name in RuleIn.model_fields:
        setattr(rule, field_name, getattr(body, field_name))


# ---- 规则 ----


@router.get("/alert/rules", response_model=list[RuleOut])
def list_rules(db: Session = Depends(get_db), _: object = Depends(get_current_user)):
    return db.query(AlertRule).order_by(AlertRule.id).all()


@router.post("/alert/rules", response_model=RuleOut, status_code=201)
def create_rule(
    body: RuleIn,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    if body.device_id is not None and db.get(Device, body.device_id) is None:
        raise HTTPException(status_code=400, detail="设备不存在")
    rule = AlertRule()
    _apply_rule(rule, body)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    audit.record(user.username, "alert_rule_create", target=rule.name, ip=audit.client_ip(request))
    return rule


@router.put("/alert/rules/{rule_id}", response_model=RuleOut)
def update_rule(
    rule_id: int,
    body: RuleIn,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    rule = db.get(AlertRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    _apply_rule(rule, body)
    db.commit()
    db.refresh(rule)
    audit.record(user.username, "alert_rule_update", target=rule.name, ip=audit.client_ip(request))
    return rule


@router.delete("/alert/rules/{rule_id}")
def delete_rule(
    rule_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    rule = db.get(AlertRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    name = rule.name
    db.delete(rule)
    db.commit()
    audit.record(user.username, "alert_rule_delete", target=name, ip=audit.client_ip(request))
    return {"ok": True}


# ---- 事件 ----


@router.get("/alert/events", response_model=list[EventOut])
def list_events(
    status: str = Query(default=""),
    severity: str = Query(default=""),
    device_id: int = Query(default=0),
    limit: int = Query(default=200, le=1000),
    db: Session = Depends(get_db),
    _: object = Depends(get_current_user),
):
    q = db.query(AlertEvent)
    if status:
        q = q.filter(AlertEvent.status == status)
    if severity:
        q = q.filter(AlertEvent.severity == severity)
    if device_id:
        q = q.filter(AlertEvent.device_id == device_id)
    events = q.order_by(AlertEvent.id.desc()).limit(limit).all()
    devices = {d.id: d for d in db.query(Device).all()}
    out = []
    for e in events:
        item = EventOut.model_validate(e)
        d = devices.get(e.device_id)
        item.device_name = d.name if d else ""
        item.device_ip = d.ip if d else ""
        out.append(item)
    return out


@router.get("/alert/events/{event_id}", response_model=EventDetailOut)
def get_event(
    event_id: int,
    db: Session = Depends(get_db),
    _: object = Depends(get_current_user),
):
    """事件详情（含触发时刻指标快照）。"""
    event = db.get(AlertEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="告警不存在")
    item = EventDetailOut.model_validate(event)
    d = db.get(Device, event.device_id)
    item.device_name = d.name if d else ""
    item.device_ip = d.ip if d else ""
    return item


@router.post("/alert/events/{event_id}/ack", response_model=EventOut)
def ack_event(
    event_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    event = db.get(AlertEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="告警不存在")
    event.ack_by = user.username
    event.ack_at = datetime.now()
    db.commit()
    db.refresh(event)
    audit.record(user.username, "alert_ack", target=f"event#{event_id}", ip=audit.client_ip(request))
    return event


@router.post("/alert/events/{event_id}/resolve", response_model=EventOut)
def resolve_event(
    event_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    event = db.get(AlertEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="告警不存在")
    event.status = "resolved"
    event.resolved_at = datetime.now()
    db.commit()
    db.refresh(event)
    audit.record(user.username, "alert_resolve", target=f"event#{event_id}", ip=audit.client_ip(request))
    return event


@router.get("/alert/summary")
def alert_summary(db: Session = Depends(get_db), _: object = Depends(get_current_user)):
    """未恢复告警按等级计数（顶栏色块用）。"""
    rows = (
        db.query(AlertEvent.severity, func.count())
        .filter(AlertEvent.status == "firing")
        .group_by(AlertEvent.severity)
        .all()
    )
    counts = {s: n for s, n in rows}
    return {
        "critical": counts.get("critical", 0),
        "major": counts.get("major", 0),
        "warning": counts.get("warning", 0),
        "info": counts.get("info", 0),
        "total": sum(counts.values()),
    }


# ---- 静默窗口 ----


def _apply_silence(window: SilenceWindow, body: SilenceIn) -> None:
    for field_name in SilenceIn.model_fields:
        setattr(window, field_name, getattr(body, field_name))


@router.get("/alert/silences", response_model=list[SilenceOut])
def list_silences(db: Session = Depends(get_db), _: object = Depends(get_current_user)):
    return db.query(SilenceWindow).order_by(SilenceWindow.id).all()


@router.post("/alert/silences", response_model=SilenceOut, status_code=201)
def create_silence(
    body: SilenceIn,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    if body.end_at <= body.start_at:
        raise HTTPException(status_code=400, detail="结束时间必须晚于开始时间")
    if body.device_id is not None and db.get(Device, body.device_id) is None:
        raise HTTPException(status_code=400, detail="设备不存在")
    window = SilenceWindow()
    _apply_silence(window, body)
    db.add(window)
    db.commit()
    db.refresh(window)
    audit.record(user.username, "silence_create", target=window.name, ip=audit.client_ip(request))
    return window


@router.put("/alert/silences/{silence_id}", response_model=SilenceOut)
def update_silence(
    silence_id: int,
    body: SilenceIn,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    window = db.get(SilenceWindow, silence_id)
    if window is None:
        raise HTTPException(status_code=404, detail="静默窗口不存在")
    if body.end_at <= body.start_at:
        raise HTTPException(status_code=400, detail="结束时间必须晚于开始时间")
    if body.device_id is not None and db.get(Device, body.device_id) is None:
        raise HTTPException(status_code=400, detail="设备不存在")
    _apply_silence(window, body)
    db.commit()
    db.refresh(window)
    audit.record(user.username, "silence_update", target=window.name, ip=audit.client_ip(request))
    return window


@router.delete("/alert/silences/{silence_id}")
def delete_silence(
    silence_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    window = db.get(SilenceWindow, silence_id)
    if window is None:
        raise HTTPException(status_code=404, detail="静默窗口不存在")
    name = window.name
    db.delete(window)
    db.commit()
    audit.record(user.username, "silence_delete", target=name, ip=audit.client_ip(request))
    return {"ok": True}


# ---- 通知渠道 ----


@router.get("/notify/configs", response_model=list[NotifyConfigOut])
def list_notify_configs(db: Session = Depends(get_db), _: object = Depends(get_current_user)):
    return db.query(NotifyConfig).order_by(NotifyConfig.id).all()


@router.post("/notify/configs", response_model=NotifyConfigOut, status_code=201)
def create_notify_config(
    body: NotifyConfigIn,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    if db.query(NotifyConfig).filter(NotifyConfig.name == body.name).first():
        raise HTTPException(status_code=409, detail="同名渠道已存在")
    cfg = NotifyConfig(name=body.name, kind=body.kind, enabled=body.enabled)
    cfg.set_config(body.config)
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    audit.record(user.username, "notify_config_create", target=cfg.name, ip=audit.client_ip(request))
    return cfg


@router.put("/notify/configs/{config_id}", response_model=NotifyConfigOut)
def update_notify_config(
    config_id: int,
    body: NotifyConfigIn,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    cfg = db.get(NotifyConfig, config_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail="渠道不存在")
    cfg.name = body.name
    cfg.kind = body.kind
    cfg.enabled = body.enabled
    if body.config:  # 空 config 表示不改
        cfg.set_config(body.config)
    db.commit()
    db.refresh(cfg)
    audit.record(user.username, "notify_config_update", target=cfg.name, ip=audit.client_ip(request))
    return cfg


@router.delete("/notify/configs/{config_id}")
def delete_notify_config(
    config_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    cfg = db.get(NotifyConfig, config_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail="渠道不存在")
    name = cfg.name
    db.delete(cfg)
    db.commit()
    audit.record(user.username, "notify_config_delete", target=name, ip=audit.client_ip(request))
    return {"ok": True}
