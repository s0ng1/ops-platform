"""轻量 IPAM 台账模型：IP ↔ MAC ↔ 接入设备/端口，多来源合并到同一行（ip 全表唯一）。"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import Base

# 台账来源：ping=IP 段扫描发现存活；arp=交换机 ARP 表学到；mac_table=MAC 地址表（预留，
# 本期纯 MAC 无 IP 的终端不入库，MAC 表仅用于给已知 IP 补接入端口，见 collectors/ipam.py）
IPAM_SOURCES = ("ping", "arp", "mac_table")


class IpInventory(Base):
    """一条终端台账记录。whitelisted=True 的终端为可信白名单，不再触发新终端告警。"""

    __tablename__ = "ip_inventory"

    id: Mapped[int] = mapped_column(primary_key=True)
    ip: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    mac: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 本期不做反向解析，hostname 留空；可通过 API 手工维护，作备注性质
    hostname: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 学到该终端的接入交换机（arp 来源）；ping 来源为 None
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    if_name: Mapped[str | None] = mapped_column(String(128), nullable=True)  # 接入端口名
    source: Mapped[str] = mapped_column(String(16), default="ping")
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    whitelisted: Mapped[bool] = mapped_column(Boolean, default=False)
