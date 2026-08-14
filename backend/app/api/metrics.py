"""指标查询：曲线数据、最新值快照、已采集指标清单。"""
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import false, func, literal_column
from sqlalchemy.orm import Session

from ..core.database import get_db
from ..models import Device, Metric
from .deps import get_current_user

router = APIRouter(prefix="/api", tags=["指标"])


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"时间格式不正确：{value}")


def _filter_labels(q, want: dict, dialect: str):
    """labels 整体相等过滤（SQL 层，须在 limit 之前应用，避免先截断再过滤误伤）。
    双方言：PG 用 jsonb 原生等值（键序无关）；SQLite 用逐键等值 AND + 键数相等。
    labels 约定为扁平标量字典；遇非标量值或非 dict 时按不匹配处理（返回空）。
    """
    if not isinstance(want, dict):
        return q.filter(false())
    if dialect == "postgresql":
        return q.filter(Metric.labels == want)
    for k, v in want.items():
        # SQLite 下按标量类型取 json_extract 对应转换（直接 == 字符串会被 JSON_QUOTE 包一层）
        if isinstance(v, str):
            q = q.filter(Metric.labels[k].as_string() == v)
        elif isinstance(v, bool):
            q = q.filter(Metric.labels[k].as_boolean() == v)
        elif isinstance(v, int):
            q = q.filter(Metric.labels[k].as_integer() == v)
        elif isinstance(v, float):
            q = q.filter(Metric.labels[k].as_float() == v)
        elif v is None:
            q = q.filter(Metric.labels[k].as_string().is_(None))
        else:  # 嵌套 dict/list：逐键比较不支持，按不匹配处理
            return q.filter(false())
    # 键数相等：json_each 展开库中 labels 计数，与 want 键数比对（SQLite JSON1）
    return q.filter(
        literal_column("(SELECT count(*) FROM json_each(metrics.labels))") == len(want)
    )


@router.get("/devices/{device_id}/metrics")
def query_metrics(
    device_id: int,
    metric: str = Query(..., description="指标名，如 if_in_bps"),
    start: str | None = Query(default=None, description="ISO 起始时间"),
    end: str | None = Query(default=None),
    labels: str | None = Query(default=None, description='精确匹配，如 {"if":"GE0/1"}'),
    limit: int = Query(default=1000, le=10000),
    db: Session = Depends(get_db),
    _: object = Depends(get_current_user),
):
    if db.get(Device, device_id) is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    q = db.query(Metric).filter(Metric.device_id == device_id, Metric.metric == metric)
    start_dt, end_dt = _parse_dt(start), _parse_dt(end)
    if start_dt:
        q = q.filter(Metric.time >= start_dt)
    if end_dt:
        q = q.filter(Metric.time <= end_dt)
    if labels:
        try:
            want = json.loads(labels)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="labels 需为 JSON")
        q = _filter_labels(q, want, db.get_bind().dialect.name)
    rows = q.order_by(Metric.time).limit(limit).all()
    return {
        "device_id": device_id,
        "metric": metric,
        "points": [
            {"time": r.time.isoformat(), "labels": r.labels, "value": r.value} for r in rows
        ],
    }


@router.get("/devices/{device_id}/metrics/latest")
def latest_metrics(
    device_id: int,
    db: Session = Depends(get_db),
    _: object = Depends(get_current_user),
):
    """每个 metric+labels 组合的最新一个点。
    加 10 分钟时间窗：hypertable 全表 GROUP BY labels 在大接口数设备上要秒级，
    最新值必在最近几个采集周期内。"""
    if db.get(Device, device_id) is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    since = datetime.utcnow() - timedelta(minutes=10)
    sub = (
        db.query(Metric.metric, Metric.labels, func.max(Metric.time).label("mt"))
        .filter(Metric.device_id == device_id, Metric.time > since)
        .group_by(Metric.metric, Metric.labels)
        .subquery()
    )
    rows = (
        db.query(Metric)
        .join(
            sub,
            (Metric.device_id == device_id)
            & (Metric.metric == sub.c.metric)
            & (Metric.labels == sub.c.labels)
            & (Metric.time == sub.c.mt),
        )
        .all()
    )
    return {
        "device_id": device_id,
        "items": [
            {"metric": r.metric, "labels": r.labels, "value": r.value, "time": r.time.isoformat()}
            for r in rows
        ],
    }


@router.get("/devices/{device_id}/metrics/catalog")
def metrics_catalog(
    device_id: int,
    db: Session = Depends(get_db),
    _: object = Depends(get_current_user),
):
    """该设备近 7 天采集过的指标名与 labels 组合清单（前端画图用）。
    加 7 天时间窗：hypertable 全表 GROUP BY 太重；7 天未见的指标多半已停采，不展示。"""
    if db.get(Device, device_id) is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    since = datetime.utcnow() - timedelta(days=7)
    rows = (
        db.query(Metric.metric, Metric.labels, func.count())
        .filter(Metric.device_id == device_id, Metric.time > since)
        .group_by(Metric.metric, Metric.labels)
        .order_by(Metric.metric)
        .all()
    )
    return {
        "device_id": device_id,
        "catalog": [{"metric": m, "labels": lb, "count": n} for m, lb, n in rows],
    }
