"""日志事件模型：Syslog / SNMP Trap 原始事件（log_events）与匹配规则（log_rules）。"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import Base

LOG_KINDS = ("syslog", "trap")


class LogEvent(Base):
    """收到的一条 Syslog 报文或 SNMP Trap。severity 为 syslog 等级（0~7），trap 为空。"""

    __tablename__ = "log_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_ip: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(8))            # syslog / trap
    facility: Mapped[int | None] = mapped_column(Integer, nullable=True)
    severity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


class LogRule(Base):
    """日志匹配规则：三个条件可叠加（与关系），全空=全部命中。
    source_ip 精确匹配；keyword 对 message 做子串匹配；severity_lte 仅对 syslog 生效
    （报文 severity 小于等于该值即命中，trap 事件不适用）。
    """

    __tablename__ = "log_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    source_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 空=任意来源
    keyword: Mapped[str | None] = mapped_column(String(256), nullable=True)   # 空=不限内容
    severity_lte: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 空=不限等级
    alert_severity: Mapped[str] = mapped_column(String(16), default="warning")  # 命中产生的告警等级
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
