"""告警模型：规则（阈值+去抖+选择器）、事件（firing/resolved 状态机）、通知渠道。"""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, false
from sqlalchemy.orm import Mapped, mapped_column

from ..core.crypto import decrypt_text, encrypt_text
from ..core.database import Base
from .metric import JsonDict
import json as _json

SEVERITIES = ("critical", "major", "warning", "info")  # 致命/严重/警告/信息
# baseline_dev：与近 7 天同时段均值比较，偏离超 threshold 倍标准差触发（threshold 存 N）
OPS = (">", ">=", "<", "<=", "==", "!=", "baseline_dev")


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    metric: Mapped[str] = mapped_column(String(64))          # 如 if_in_util / cpu_usage / device_online
    op: Mapped[str] = mapped_column(String(16), default=">")  # 含 baseline_dev（12 字符）
    threshold: Mapped[float] = mapped_column(Float)
    duration_cycles: Mapped[int] = mapped_column(Integer, default=1)  # 连续 N 周期越限才触发（去抖）
    severity: Mapped[str] = mapped_column(String(16), default="warning")
    # 设备选择器：三者可叠加（与关系），全空=全部设备
    device_type: Mapped[str] = mapped_column(String(32), default="")
    group_name: Mapped[str] = mapped_column(String(128), default="")
    device_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # labels 过滤：规则仅匹配包含这些 labels 的指标点，如 {"if": "GE0/0/1"}
    labels_filter: Mapped[dict] = mapped_column(JSON, default=dict)
    # 通知渠道：["*"] 全部启用渠道，或指定 ["smtp","dingtalk"]
    notify: Mapped[list] = mapped_column(JSON, default=lambda: ["*"])
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    # 升级：firing 且 N 分钟未确认时 severity 升一级并重发通知；0=不升级
    escalate_minutes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="SET NULL"), nullable=True
    )
    rule_name: Mapped[str] = mapped_column(String(128), default="")  # 规则删除后仍可追溯
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), index=True
    )
    metric: Mapped[str] = mapped_column(String(64))
    labels: Mapped[dict] = mapped_column(JSON, default=dict)
    labels_key: Mapped[str] = mapped_column(String(512), default="")  # 去重键（排序后 JSON）
    severity: Mapped[str] = mapped_column(String(16), default="warning")
    status: Mapped[str] = mapped_column(String(16), default="firing", index=True)  # firing/resolved
    value: Mapped[float] = mapped_column(Float)       # 触发时的值
    fired_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ack_by: Mapped[str] = mapped_column(String(64), default="")
    ack_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    # 命中静默窗口：事件照常入库/广播，但跳过外部通知
    silenced: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # 是否已升级过（同一事件只升一级一次）
    escalated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # 触发时刻该设备全部指标最新值快照（同 metrics/latest 口径）；抓取失败静默存 NULL
    snapshot: Mapped[dict | None] = mapped_column(JsonDict, nullable=True)


class SilenceWindow(Base):
    """告警静默窗口（维护窗口）：窗口内命中的事件不入外部通知渠道。
    选择器语义同 AlertRule：三者可叠加（与），全空=全部设备。
    """

    __tablename__ = "silence_windows"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    device_type: Mapped[str] = mapped_column(String(32), default="")
    group_name: Mapped[str] = mapped_column(String(128), default="")
    device_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_at: Mapped[datetime] = mapped_column(DateTime)
    end_at: Mapped[datetime] = mapped_column(DateTime)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class NotifyConfig(Base):
    """通知渠道配置：smtp / dingtalk / wecom，敏感字段整体 Fernet 加密。"""

    __tablename__ = "notify_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(16))  # smtp / dingtalk / wecom
    config_encrypted: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def set_config(self, data: dict) -> None:
        self.config_encrypted = encrypt_text(_json.dumps(data, ensure_ascii=False))

    def get_config(self) -> dict:
        try:
            return _json.loads(decrypt_text(self.config_encrypted))
        except Exception:  # noqa: BLE001
            return {}
