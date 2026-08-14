"""告警规则模板 API：模板 CRUD + 批量实例化成规则（同名跳过，幂等）。
内置模板（builtin=true）禁止删改——比内置规则更严（规则允许删，重启补回；
模板是生成规则的源头，删改了补种语义会乱，故服务端直接拒绝）。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from ..core import audit
from ..core.database import get_db
from ..models import AlertRule, Device, RuleTemplate
from .deps import get_current_user, require_operator

router = APIRouter(prefix="/api", tags=["告警规则模板"])


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=512)
    metric: str = Field(min_length=1, max_length=64)
    # baseline_dev：与近 7 天同时段均值比较，偏离超 N 倍标准差触发（threshold 存 N）
    op: str = Field(pattern="^(>|>=|<|<=|==|!=|baseline_dev)$")
    threshold: float
    duration_cycles: int = Field(default=1, ge=1, le=100)
    severity: str = Field(pattern="^(critical|major|warning|info)$")
    device_type: str = ""
    group_name: str = ""
    device_id: int | None = None
    labels_filter: dict | None = None

    @model_validator(mode="after")
    def _check_baseline_threshold(self):
        # baseline_dev 的 threshold 语义是 N 倍标准差，必须为正数
        if self.op == "baseline_dev" and self.threshold <= 0:
            raise ValueError("baseline_dev 的阈值为 N 倍标准差，必须大于 0")
        return self


class TemplateOut(TemplateIn):
    id: int
    builtin: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class InstantiateIn(BaseModel):
    template_ids: list[int] = Field(min_length=1)
    device_type: str = ""  # 非空时覆盖模板自带的 device_type（批量套到另一类型上）


class InstantiateOut(BaseModel):
    created: list[str]  # 新建的规则名
    skipped: list[str]  # 同名规则已存在而跳过的规则名


def _apply_template(tpl: RuleTemplate, body: TemplateIn) -> None:
    for field_name in TemplateIn.model_fields:
        setattr(tpl, field_name, getattr(body, field_name))


def _check_device(db: Session, device_id: int | None) -> None:
    if device_id is not None and db.get(Device, device_id) is None:
        raise HTTPException(status_code=400, detail="设备不存在")


@router.get("/alert/templates", response_model=list[TemplateOut])
def list_templates(db: Session = Depends(get_db), _: object = Depends(get_current_user)):
    return db.query(RuleTemplate).order_by(RuleTemplate.id).all()


@router.post("/alert/templates", response_model=TemplateOut, status_code=201)
def create_template(
    body: TemplateIn,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    if db.query(RuleTemplate).filter(RuleTemplate.name == body.name).first():
        raise HTTPException(status_code=409, detail="同名模板已存在")
    _check_device(db, body.device_id)
    tpl = RuleTemplate()
    _apply_template(tpl, body)
    db.add(tpl)
    db.commit()
    db.refresh(tpl)
    audit.record(user.username, "alert_template_create", target=tpl.name, ip=audit.client_ip(request))
    return tpl


# instantiate 是集合级操作（无 {template_id} 路径冲突），声明在单条路由之前仅为可读性
@router.post("/alert/templates/instantiate", response_model=InstantiateOut)
def instantiate_templates(
    body: InstantiateIn,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    """把模板批量展开成告警规则：同名规则（含用户自建）已存在则跳过，幂等可重入。"""
    templates = []
    for tid in body.template_ids:
        tpl = db.get(RuleTemplate, tid)
        if tpl is None:
            raise HTTPException(status_code=404, detail=f"模板不存在：id={tid}")
        templates.append(tpl)
    existing = {name for (name,) in db.query(AlertRule.name).all()}
    created, skipped = [], []
    for tpl in templates:
        if tpl.name in existing:
            skipped.append(tpl.name)
            continue
        rule = AlertRule(
            name=tpl.name,
            metric=tpl.metric,
            op=tpl.op,
            threshold=tpl.threshold,
            duration_cycles=tpl.duration_cycles,
            severity=tpl.severity,
            device_type=body.device_type or tpl.device_type,
            group_name=tpl.group_name,
            device_id=tpl.device_id,
            labels_filter=tpl.labels_filter or {},
        )
        db.add(rule)
        existing.add(tpl.name)  # 同批内同名模板也不重复建
        created.append(tpl.name)
    if created:
        db.commit()
    audit.record(
        user.username, "alert_template_instantiate",
        target=f"{len(created)}条", detail=f"新建:{','.join(created)} 跳过:{','.join(skipped)}",
        ip=audit.client_ip(request),
    )
    return {"created": created, "skipped": skipped}


@router.put("/alert/templates/{template_id}", response_model=TemplateOut)
def update_template(
    template_id: int,
    body: TemplateIn,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    tpl = db.get(RuleTemplate, template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    if tpl.builtin:
        raise HTTPException(status_code=400, detail="内置模板不允许修改")
    if (
        body.name != tpl.name
        and db.query(RuleTemplate).filter(RuleTemplate.name == body.name).first()
    ):
        raise HTTPException(status_code=409, detail="同名模板已存在")
    _check_device(db, body.device_id)
    _apply_template(tpl, body)
    db.commit()
    db.refresh(tpl)
    audit.record(user.username, "alert_template_update", target=tpl.name, ip=audit.client_ip(request))
    return tpl


@router.delete("/alert/templates/{template_id}")
def delete_template(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    tpl = db.get(RuleTemplate, template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    if tpl.builtin:
        raise HTTPException(status_code=400, detail="内置模板不允许删除")
    name = tpl.name
    db.delete(tpl)
    db.commit()
    audit.record(user.username, "alert_template_delete", target=name, ip=audit.client_ip(request))
    return {"ok": True}
