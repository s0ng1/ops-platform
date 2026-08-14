"""拓扑模型：设备间链路（端口到端口），来源分手工/LLDP/CDP。"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import Base

LINK_SOURCES = ("manual", "lldp", "cdp")


class TopoLink(Base):
    __tablename__ = "topo_links"
    # 同两端设备+端口的链路唯一（源/目颠倒视为同一条由应用层规范）
    __table_args__ = (
        UniqueConstraint(
            "src_device_id", "src_port", "dst_device_id", "dst_port",
            name="uq_topo_link_endpoints",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    src_device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), index=True
    )
    src_port: Mapped[str] = mapped_column(String(64), default="")
    dst_device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), index=True
    )
    dst_port: Mapped[str] = mapped_column(String(64), default="")
    source: Mapped[str] = mapped_column(String(16), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class TopologyLayout(Base):
    """分组子拓扑的节点坐标（按分组独立一套，与 devices.pos_x/pos_y 全图布局互不干扰）。"""

    __tablename__ = "topology_layouts"
    __table_args__ = (
        UniqueConstraint("device_id", "group_name", name="uq_topology_layout_device_group"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), index=True
    )
    group_name: Mapped[str] = mapped_column(String(128), default="")
    pos_x: Mapped[float | None] = mapped_column(nullable=True)
    pos_y: Mapped[float | None] = mapped_column(nullable=True)
