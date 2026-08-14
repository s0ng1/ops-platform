"""设备模型与自动发现任务模型。"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..core.database import Base
from .metric import JsonDict

# 管理对象分类（北塔八大类子集，第 1 期聚焦前四类）
DEVICE_TYPES = (
    "network",        # 网络设备
    "security",       # 安全设备
    "server_windows", # Windows 主机
    "server_linux",   # Linux 主机
    "database",       # 数据库
    "application",    # 应用仿真拨测 + 中间件轻量指标（http/dns/tcp/nginx/redis）
    "other",
)

DEVICE_STATUS = ("unknown", "online", "offline")


class Device(Base):
    __tablename__ = "devices"
    # 同一 IP 可挂多个管理对象（如主机 + 其上的数据库），唯一性按 (ip, type)
    __table_args__ = (UniqueConstraint("ip", "type", name="uq_devices_ip_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    ip: Mapped[str] = mapped_column(String(64), index=True)
    type: Mapped[str] = mapped_column(String(32), default="other")
    # 网络设备细分类型：switch/router/firewall，空串=按 type 默认图标
    subtype: Mapped[str] = mapped_column(String(32), default="")
    # 子网/机房分组（空=未分组）；拓扑子视图与告警选择器共用，加索引按组过滤
    group_name: Mapped[str] = mapped_column(String(128), default="", index=True)
    location: Mapped[str] = mapped_column(String(128), default="")
    credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True
    )
    # 配置备份专用 SSH 凭据（辅槽）：主槽挂 SNMP 凭据的交换机也能做配置备份
    ssh_credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True
    )
    monitor_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # 监控状态（由监控协程周期更新）
    status: Mapped[str] = mapped_column(String(16), default="unknown")
    sys_descr: Mapped[str] = mapped_column(Text, default="")
    sys_object_id: Mapped[str] = mapped_column(String(128), default="")
    last_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )
    # 拓扑画布坐标（None=未摆位，前端自动布局）
    pos_x: Mapped[float | None] = mapped_column(nullable=True)
    pos_y: Mapped[float | None] = mapped_column(nullable=True)
    # 应用拨测配置（仅 type=application 使用）：{"probe_kind": "http|dns|tcp|nginx|redis", ...参数}
    probe_config: Mapped[dict] = mapped_column(JsonDict, default=dict)

    # 两个 FK 同指 credentials，必须显式 foreign_keys 区分
    credential = relationship("Credential", lazy="joined", foreign_keys=[credential_id])
    ssh_credential = relationship("Credential", lazy="joined", foreign_keys=[ssh_credential_id])


class DiscoveryJob(Base):
    """一次 IP 段扫描任务，结果存 JSON。"""

    __tablename__ = "discovery_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    ranges: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running/done/failed
    total: Mapped[int] = mapped_column(Integer, default=0)
    done: Mapped[int] = mapped_column(Integer, default=0)
    result_json: Mapped[str] = mapped_column(Text, default="[]")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
