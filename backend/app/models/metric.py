"""时序指标模型：单表存全部指标，labels 存维度（如接口名）。
PG 上由 core/timescale.py 升级为 hypertable；SQLite 下是普通表（测试用）。
"""
import itertools
import time as _time
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, PrimaryKeyConstraint, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import Base

# PG 用 JSONB（支持相等比较/GROUP BY），SQLite 退化为普通 JSON
JsonDict = JSON().with_variant(JSONB(), "postgresql")

# id 用进程内计数器生成（PK 为 (id,time)，id 无需全局唯一；
# SQLite 不支持复合主键自增，故不用数据库序列）
_id_counter = itertools.count(int(_time.time() * 1000) % 1_000_000_000)


def utcnow() -> datetime:
    """UTC 当前时间（naive，入库统一口径）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Metric(Base):
    __tablename__ = "metrics"
    # hypertable 要求唯一约束必须包含分区列 time，故主键为 (id, time) 复合键
    __table_args__ = (
        PrimaryKeyConstraint("id", "time"),
        Index("ix_metrics_device_metric_time", "device_id", "metric", "time"),
    )

    id: Mapped[int] = mapped_column(Integer, default=lambda: next(_id_counter))
    time: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), index=True
    )
    metric: Mapped[str] = mapped_column(String(64))
    labels: Mapped[dict] = mapped_column(JsonDict, default=dict)
    value: Mapped[float] = mapped_column(Float)
