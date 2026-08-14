"""告警规则模板：把规则定义（指标/比较符/阈值/去抖/等级 + 选择器）存成可复用模板，
经 instantiate 接口批量展开成正式告警规则（同名跳过，幂等）。
设备选择器字段与 AlertRule 一一对应（device_type / group_name / device_id 三件套）。
"""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import Base


class RuleTemplate(Base):
    __tablename__ = "rule_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(String(512), default="")
    metric: Mapped[str] = mapped_column(String(64))
    op: Mapped[str] = mapped_column(String(16), default=">")  # 含 baseline_dev，同 AlertRule
    threshold: Mapped[float] = mapped_column(Float)
    duration_cycles: Mapped[int] = mapped_column(Integer, default=1)
    severity: Mapped[str] = mapped_column(String(16), default="warning")
    # 设备选择器三件套：与 AlertRule 一一对应，全空=全部设备
    device_type: Mapped[str] = mapped_column(String(32), default="")
    group_name: Mapped[str] = mapped_column(String(128), default="")
    device_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # labels 过滤，同 AlertRule.labels_filter；可空（空=不过滤）
    labels_filter: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
